import fcntl
import io
import logging
import os
import random
import re
import select
import shlex
import sys
import threading
import time
import subprocess
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from typing import (
    IO,
    Any,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)

from .context import Context
from .util import FatalError, Process, require_program, run

# TODO: rewrite this to use
# https://docs.python.org/3/library/concurrent.futures.html?


@dataclass
class Job:
    proc: Process
    jobid: str
    nnodes: int = 1
    out_base: str = ""
    start_time: float = time.time()
    onsuccess: Callable[["Job"], bool | None] | None = None
    onerror: Callable[["Job"], bool | None] | None = None
    outs: str = ""
    errs: str = ""

    @property
    def returncode(self) -> int:
        return self.proc.returncode

    @property
    def stdout(self) -> str:
        return self.proc.stdout

    @property
    def stderr(self) -> str:
        return self.proc.stderr

    @property
    def stdout_io(self) -> IO | None:
        return self.proc.stdout_io

    @property
    def stderr_io(self) -> IO | None:
        return self.proc.stdout_io

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout)


@dataclass
class ProcessJob(Job):
    stdout_handle: IO | None = None
    stderr_handle: IO | None = None


@dataclass
class SSHJob(Job):
    outfile_handle: IO | None = None
    node: str = ""

    tunnel_src: int | None = None
    tunnel_dest: int | None = None


@dataclass
class PrunJob(Job):
    nnodes: int = 1
    outfile_handle: IO | None = None
    logged: bool = True


class Pool(metaclass=ABCMeta):
    """
    A pool is used to run processes in parallel as jobs when ``--parallel`` is
    specified on the command line. The pool is created automatically by
    :class:`Setup` and passed to :func:`Target.build` and :func:`Target.run`.
    However, the pool is only passed if the method implementation defines a
    parameter for the pool, i.e.::

        class MyTarget(Target):
            def build(self, ctx, instance, pool): # receives Pool instance
               ...
            def run(self, ctx, instance):         # does not receive it
               ...

    The maximum number of parallel jobs is controlled by ``--parallelmax``. For
    ``--parallel=proc`` this is simply the number of parallel processes on the
    current machine. For ``--parallel=prun`` it is the maximum number of
    simultaneous jobs in the job queue (pending or running).
    """

    # Wait time between checking for output and such
    poll_interval: float = 0.050

    running: bool
    jobs: dict[int, Job]
    pollthread: threading.Thread | None

    @abstractmethod
    def make_jobs(
        self,
        ctx: Context,
        cmd: str | Iterable[str],
        jobid_base: str,
        outfile_base: str,
        nnodes: int,
        **kwargs: Any,
    ) -> Iterator[Job]:
        pass

    @abstractmethod
    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        pass

    def __init__(self, logger: logging.Logger, parallelmax: int):
        """
        :param logger: logging object for status updates (set to ``ctx.log``)
        :param parallelmax: value of ``--parallelmax``
        """
        self.log = logger
        self.parallelmax = parallelmax
        self.jobs = {}
        self.running = True
        self.pollthread = None
        self._start_poller()

    def __del__(self) -> None:
        self.wait_all()
        if self.pollthread is not None:
            self.running = False
            self.pollthread.join()  # Don't timeout on this join; has to finish

    def _start_poller(self) -> None:
        if self.pollthread is None:
            self.poller = select.epoll()
            self.pollthread = threading.Thread(target=self._poller_thread, name="pool-poller")
            self.pollthread.daemon = True
            self.running = True
            self.pollthread.start()

    def _poller_thread(self) -> None:
        # Monitor all jobs for finished jobs/output; call output/success/error handler callbacks
        while self.running:
            for fd, flags in self.poller.poll(timeout=self.poll_interval):

                job = self.jobs[fd]

                # Regular data is available for read; handle it
                if flags & (select.EPOLLIN | select.EPOLLPRI):
                    self.process_job_output(job, fd)

                # There was an error in the pipe
                if flags & select.EPOLLERR:
                    self.log.error(f"Error in file descriptor: {fd}")
                    self.poller.unregister(fd)
                    del self.jobs[fd]

                    # Wait for the process itself to complete to ensure all output is captured
                    job.proc.wait()

                    # Check if another file descriptor in the list still refers to this job; remove it
                    for alt_fd in {_fd for _fd, _job in self.jobs.items() if _job is job}:
                        self.log.error(f"Found alternative file descriptor (with same job): {alt_fd}")
                        self.process_job_output(job, alt_fd)
                        self.poller.unregister(alt_fd)
                        del self.jobs[alt_fd]

                    # Call the error callback
                    self.onerror(job)

                # The process finished & closed the connection
                if flags & select.EPOLLHUP:
                    if job.proc.poll() is None:
                        self.log.debug(f"Job {job.jobid} hung up but no return code yet; check later")
                        continue

                    self.poller.unregister(fd)
                    del self.jobs[fd]

                    # Check if another file descriptor of this same process is still in the jobs dict
                    if job not in self.jobs.values():
                        if job.proc.poll() == 0:
                            self.onsuccess(job)
                        else:
                            self.onerror(job)

    def _wait_for_queue_space(self, nodes_needed: int) -> None:
        if self.parallelmax is not None:

            def nodes_in_use() -> int:
                return sum(job.nnodes for job in self.jobs.values())

            while nodes_in_use() + nodes_needed > self.parallelmax:
                time.sleep(self.poll_interval)

    def wait_all(self) -> None:
        """
        Block (busy-wait) until all jobs in the queue have been completed.
        Called automatically by :class:`Setup` after the ``build`` and ``run``
        commands.
        """
        while len(self.jobs):
            time.sleep(self.poll_interval)

    def run(
        self,
        ctx: Context,
        cmd: str | Iterable[str],
        jobid: str,
        outfile: str | None = None,
        nnodes: int = 1,
        onsuccess: Callable[[Job], bool | None] | None = None,
        onerror: Callable[[Job], bool | None] | None = None,
        **kwargs: Any,
    ) -> list[Job]:
        """
        A non-blocking wrapper for :func:`util.run`, to be used when
        ``--parallel`` is specified.

        :param ctx: the configuration context
        :param cmd: the command to run
        :param jobid: a human-readable ID for status reporting
        :param outfile: full path to target file for command output
        :param nnodes: number of cores or machines to run the command on
        :param onsuccess: callback when the job finishes successfully
        :param onerror: callback when the job exits with (typically I/O) error
        :param kwargs: passed directly to :func:`util.run`
        :returns: handles to created job processes
        """
        self._start_poller()

        if outfile is None:
            outfile = f"{jobid}"

        jobs = []
        for job in self.make_jobs(ctx, cmd, jobid, outfile, nnodes, **kwargs):
            assert job.proc.proc is not None
            job.onsuccess = onsuccess
            job.onerror = onerror
            job.outs = ""
            job.errs = ""

            if (outs_io := job.stdout_io) is not None:
                self.jobs[outs_io.fileno()] = job
                self.poller.register(outs_io, select.EPOLLIN | select.EPOLLPRI | select.EPOLLERR | select.EPOLLHUP)

            if (errs_io := job.stderr_io) is not None:
                self.jobs[errs_io.fileno()] = job
                self.poller.register(errs_io, select.EPOLLIN | select.EPOLLPRI | select.EPOLLERR | select.EPOLLHUP)

            jobs.append(job)

        return jobs

    def onsuccess(self, job: Job) -> None:
        # don't log if onsuccess() returns False
        if not job.onsuccess or job.onsuccess(job) is not False:
            self.log.info(f"job {job.jobid} finished {self._get_elapsed(job)}")

    def onerror(self, job: Job) -> None:
        # don't log if onerror() returns False
        if not job.onerror or job.onerror(job) is not False:
            self.log.error(
                "Command returned non-zero error status code:\n"
                + f"\tJob ID:       {job.jobid}\n"
                + f"\tRuntime:      {self._get_elapsed(job)}\n"
                + f"\tCommand:      {job.proc.cmd_str}\n"
                + f"\tReturn code:  {job.returncode}\n\n"
                + f"{''.join(f'STDOUT > {line}\n' for line in job.outs)}\n\n"
                + f"{''.join(f'STDERR > {line}\n' for line in job.errs)}\n\n"
            )

    def _get_elapsed(self, job: Job) -> str:
        return f"after {round(time.time() - job.start_time)} seconds"


class ProcessPool(Pool):
    # 7-bit C1 ANSI sequences
    ansi_escape = re.compile(r"(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]")

    def make_jobs(
        self,
        ctx: Context,
        cmd: str | Iterable[str],
        jobid_base: str,
        outfile_base: str,
        nnodes: int,
        **kwargs: Any,
    ) -> Iterator[Job]:
        for i in range(nnodes):
            outfile: str = outfile_base if nnodes == 1 else f"{outfile_base}-{i}"
            jobid: str = jobid_base if nnodes == 1 else f"{jobid_base}-{i}"

            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            self._wait_for_queue_space(1)

            ctx.log.info(f"Running {jobid}; output base-file: {outfile}")
            if (proc := run(ctx, cmd, defer=True, bufsize=io.DEFAULT_BUFFER_SIZE, **kwargs)).proc is not None:
                outfiles: list[str] = []
                if (outs_io := proc.stdout_io) is not None:
                    outfiles.append(f"{outfile}.stdout.log")
                    _set_non_blocking(outs_io)
                if (errs_io := proc.stderr_io) is not None:
                    outfiles.append(f"{outfile}.stderr.log")
                    _set_non_blocking(errs_io)

                yield ProcessJob(
                    proc=proc,
                    jobid=jobid,
                    nnodes=1,
                    out_base=outfile,
                    start_time=time.time(),
                    onsuccess=None,
                    onerror=None,
                    outs="",
                    errs="",
                    stdout_handle=open(f"{outfile}.stdout.log", mode="w") if proc.stdout_io is not None else None,
                    stderr_handle=open(f"{outfile}.stderr.log", mode="w") if proc.stderr_io is not None else None,
                )
            else:
                RuntimeError(f"Failed to create process {jobid} for command: {cmd}")

    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        assert isinstance(job, ProcessJob)

        # If no descriptor is given and both stdout & stderr are None, there's nothing to process
        if fd is None:
            if (outs_io := job.stdout_io) is not None:
                fd = outs_io.fileno()
            elif (errs_io := job.stderr_io) is not None:
                fd = errs_io.fileno()
            else:
                return

        # Read from the stdout file descriptor if it's not None & write to the stdout file
        if (outs_io := job.stdout_io) is not None and outs_io.fileno() == fd:
            assert job.stdout_handle is not None

            # Read until EOF is reached; write to outfile handle (decode if necessary)
            while _outs_line := outs_io.readline():
                if isinstance(_outs_line, str):
                    outs_line = self.ansi_escape.sub("", _outs_line)
                elif isinstance(_outs_line, bytes):
                    outs_line = self.ansi_escape.sub("", _outs_line.decode(encoding="ascii", errors="replace"))
                else:
                    raise TypeError(f"Invalid type from stdout stream: {type(_outs_line)}")

                job.outs += outs_line
                job.stdout_handle.write(outs_line)
                job.stdout_handle.flush()

        # Read from the stderr file descriptor if it's not None & write to the stderr file
        if (errs_io := job.stderr_io) is not None and errs_io.fileno() == fd:
            assert job.stderr_handle is not None

            # Read until EOF is reached; write to outfile handle (decode if necessary)
            while _errs_line := errs_io.readline():
                if isinstance(_errs_line, str):
                    errs_line = self.ansi_escape.sub("", _errs_line)
                elif isinstance(_errs_line, bytes):
                    errs_line = self.ansi_escape.sub("", _errs_line.decode(encoding="ascii", errors="replace"))
                else:
                    raise TypeError(f"Invalid type from stderr stream: {type(_errs_line)}")

                job.stderr_handle.write(errs_line)
                job.stderr_handle.flush()
                job.errs += errs_line

    def onsuccess(self, job: Job) -> None:
        assert isinstance(job, ProcessJob)
        job.proc.wait()

        if (outs_io := job.stdout_io) is not None:
            assert job.stdout_handle is not None
            self.process_job_output(job, outs_io.fileno())
            job.stdout_handle.flush()
            job.stdout_handle.close()

        if (errs_io := job.stderr_io) is not None:
            assert job.stderr_handle is not None
            self.process_job_output(job, errs_io.fileno())
            job.stderr_handle.flush()
            job.stderr_handle.close()

        super().onsuccess(job)

    def onerror(self, job: Job) -> None:
        assert isinstance(job, ProcessJob)
        job.proc.wait()

        if (outs_io := job.stdout_io) is not None:
            assert job.stdout_handle is not None
            self.process_job_output(job, outs_io.fileno())
            job.stdout_handle.flush()
            job.stdout_handle.close()

        if (errs_io := job.stderr_io) is not None:
            assert job.stderr_handle is not None
            self.process_job_output(job, errs_io.fileno())
            job.stderr_handle.flush()
            job.stderr_handle.close()

        super().onerror(job)


class SSHPool(Pool):
    """
    An SSHPool runs jobs on remote nodes via ssh.

    The --ssh-nodes argument specified a list of ssh hosts to distribute the
    work over. These hosts are passed as-is to the ssh command; the best way for
    specifying alternative ssh ports, user, and other options is to add your
    hosts to the ~/.ssh/config file. Additionally, make sure the hosts can be
    reached without password prompts (e.g., by using passphrase-less keys or
    using an ssh agent).

    For targets that are being run via an SSHPool additional functionality is
    available, such as distributing files to/from nodes.
    """

    ssh_opts = [
        # Block stdin and background ssh before executing command.
        "-f",
        # Eliminate some of the yes/no questions ssh may ask.
        "-oStrictHostKeyChecking=accept-new",
    ]
    scp_opts = [
        # Quiet mode to disable progress meter
        "-q",
        # Batch mode to prevent asking for password
        "-B",
        # Copy directories
        "-r",
    ]

    _tempdir: str | None

    def __init__(self, ctx: Context, logger: logging.Logger, parallelmax: int, nodes: list[str]):
        if parallelmax > len(nodes):
            raise FatalError("parallelmax cannot be greater than number of available nodes")
        super().__init__(logger, parallelmax)
        self._ctx = ctx
        self.nodes = nodes[:]
        self.available_nodes = nodes[:]
        self.has_tested_nodes = False
        self.has_created_tempdirs = False

    @property
    def tempdir(self) -> str:
        if not self.has_created_tempdirs:
            self.create_tempdirs()
        assert self._tempdir is not None
        return self._tempdir

    def _ssh_cmd(
        self,
        node: str,
        cmd: str | Iterable[str],
        extra_opts: Sequence[Any] | None = None,
    ) -> list[str]:
        if not isinstance(cmd, str):
            cmd = " ".join(shlex.quote(str(c)) for c in cmd)
        extra_opts = extra_opts or []
        return ["ssh", *self.ssh_opts, *extra_opts, node, cmd]

    def test_nodes(self) -> None:
        if self.has_tested_nodes:
            return
        for node in self.nodes:
            cmd = ["ssh", *self.ssh_opts, node, "echo -n hi"]
            p = run(self._ctx, cmd, stderr=subprocess.STDOUT, silent=True)
            if p.returncode or not str(p.stdout).endswith("hi"):
                self._ctx.log.error("Testing SSH node " + node + " failed:\n" + p.stdout)
                sys.exit(-1)
        self.has_tested_nodes = True

    def create_tempdirs(self) -> None:
        if self.has_created_tempdirs:
            return

        self.test_nodes()

        starttime = self._ctx.starttime.strftime("%Y-%m-%d.%H-%M-%S")
        self._tempdir = os.path.join("/tmp", "infra-" + starttime)

        self._ctx.log.debug(f"creating SSHPool temp dir {self._tempdir} on nodes {self.nodes}")

        for node in self.nodes:
            run(self._ctx, self._ssh_cmd(node, ["mkdir", "-p", self._tempdir]))

        self.has_created_tempdirs = True

    def cleanup_tempdirs(self) -> None:
        if not self.has_created_tempdirs:
            return
        assert self._tempdir is not None
        self._ctx.log.debug(f"cleaning up SSHPool temp directory {self._tempdir} on nodes {self.nodes}")
        for node in self.nodes:
            run(self._ctx, self._ssh_cmd(node, ["rm", "-rf", self._tempdir]))
        self.has_created_tempdirs = False
        self._tempdir = None

    def sync_to_nodes(
        self,
        sources: str | Iterable[str],
        destination: str = "",
        target_nodes: str | Iterable[str] | None = None,
    ) -> None:
        if isinstance(sources, str):
            sources = [sources]
        if isinstance(target_nodes, str):
            target_nodes = [target_nodes]
        nodes = target_nodes or self.nodes
        self._ctx.log.debug(f"syncing file to SSHPool nodes, sources={sources}," f"destination={destination}, nodes={nodes}")
        for node in nodes:
            dest = f"{node}:{os.path.join(self.tempdir, destination)}"
            cmd = ["scp", *self.scp_opts, *sources, dest]
            run(self._ctx, cmd)

    def sync_from_nodes(
        self,
        source: str,
        destination: str = "",
        source_nodes: Sequence[str] | None = None,
    ) -> None:
        if isinstance(source_nodes, str):
            source_nodes = [source_nodes]
        nodes = source_nodes or self.nodes

        self._ctx.log.debug(f"syncing file from SSHPool nodes, source={source}," f"destination={destination}, nodes={nodes}")

        for i, node in enumerate(nodes):
            dest = destination or os.path.basename(source)
            if len(nodes) > 1:
                dest += "." + node
                if len(nodes) != len(set(nodes)):
                    dest = f"{dest}{i}"
            src = f"{node}:{os.path.join(self.tempdir, source)}"
            cmd = ["scp", *self.scp_opts, src, dest]
            run(self._ctx, cmd)

    def get_free_node(self, override_node: str | None = None) -> str:
        if override_node:
            assert override_node in self.nodes
            assert override_node in self.available_nodes
            self.available_nodes.remove(override_node)
            return override_node
        else:
            return self.available_nodes.pop()

    def make_jobs(
        self,
        ctx: Context,
        cmd: str | Iterable[str],
        jobid_base: str,
        outfile_base: str,
        nnodes: int,
        nodes: str | list[str] | None = None,
        tunnel_to_nodes_dest: int | None = None,
        **kwargs: Any,
    ) -> Iterator[Job]:
        if isinstance(nodes, str):
            nodes = [nodes]

        self.test_nodes()

        for i in range(nnodes):
            jobid = jobid_base
            outfile = outfile_base
            if nnodes > 1:
                jobid += f"-{i}"
                outfile += f"-{i}"

            self._wait_for_queue_space(1)
            override_node = nodes[i] if nodes else None
            node = self.get_free_node(override_node)
            ctx.log.info("running " + jobid + " on " + node)

            ssh_node_opts = []
            tunnel_src = None
            if tunnel_to_nodes_dest:
                tunnel_src = random.randint(10000, 30000)
                ssh_node_opts += [f"-Llocalhost:{tunnel_src}:0.0.0.0:{tunnel_to_nodes_dest}"]

            ssh_cmd = self._ssh_cmd(node, cmd, ssh_node_opts)
            proc = run(
                ctx,
                ssh_cmd,
                defer=True,
                stderr=subprocess.STDOUT,
                bufsize=io.DEFAULT_BUFFER_SIZE,
                universal_newlines=False,
                **kwargs,
            )

            if (outs_io := proc.stdout_io) is not None:
                _set_non_blocking(outs_io)

            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            yield SSHJob(
                proc=proc,
                jobid=jobid,
                nnodes=1,
                out_base=outfile,
                start_time=time.time(),
                onsuccess=None,
                onerror=None,
                outs="",
                errs="",
                outfile_handle=open(outfile, mode="w"),
                tunnel_src=tunnel_src if tunnel_to_nodes_dest else None,
                tunnel_dest=tunnel_to_nodes_dest if tunnel_to_nodes_dest else None,
            )

    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        assert isinstance(job, SSHJob)

        if (outs_io := job.stdout_io) is not None:
            while _outs_line := outs_io.readline():
                if isinstance(_outs_line, str):
                    outs_line = _outs_line
                elif isinstance(_outs_line, bytes):
                    outs_line = _outs_line.decode(encoding="ascii", errors="replace")
                else:
                    raise TypeError(f"Type of line read from stdout is invalid; got: {type(_outs_line)}")

                assert job.outfile_handle is not None
                job.outfile_handle.write(outs_line)
                job.outfile_handle.flush()
                job.outs += outs_line

    def onsuccess(self, job: Job) -> None:
        assert isinstance(job, SSHJob)

        if (outs_io := job.stdout_io) is not None:
            self.process_job_output(job, outs_io.fileno())
            assert job.outfile_handle is not None
            if not job.outfile_handle.closed:
                job.outfile_handle.close()

        self.available_nodes.append(job.node)
        super().onsuccess(job)

    def onerror(self, job: Job) -> None:
        assert isinstance(job, SSHJob)

        if (outs_io := job.stdout_io) is not None:
            self.process_job_output(job, outs_io.fileno())
            assert job.outfile_handle is not None
            if not job.outfile_handle.closed:
                job.outfile_handle.close()

        super().onerror(job)


class PrunPool(Pool):
    default_job_time = 900  # if prun reserves this amount, it is not logged

    def __init__(self, logger: logging.Logger, parallelmax: int, prun_opts: Iterable[str]):
        super().__init__(logger, parallelmax)
        self.prun_opts = prun_opts

    def make_jobs(
        self,
        ctx: Context,
        cmd: Iterable[str] | str,
        jobid_base: str,
        outfile_base: str,
        nnodes: int,
        **kwargs: Any,
    ) -> Iterator[Job]:
        require_program(ctx, "prun")
        self._wait_for_queue_space(nnodes)
        ctx.log.info("scheduling " + jobid_base)
        cmd = [
            "prun",
            "-v",
            "-np",
            str(nnodes),
            "-1",
            "-o",
            outfile_base,
            *self.prun_opts,
            *cmd,
        ]
        proc = run(
            ctx,
            cmd,
            defer=True,
            stderr=subprocess.STDOUT,
            bufsize=0,
            universal_newlines=False,
            **kwargs,
        )

        if (outs_io := proc.stdout_io) is not None:
            _set_non_blocking(outs_io)

        yield PrunJob(
            proc=proc,
            jobid=jobid_base,
            nnodes=nnodes,
            out_base=outfile_base,
            start_time=time.time(),
            onsuccess=None,
            onerror=None,
            outs="",
            errs="",
            outfile_handle=open(outfile_base, mode="w") if proc.stdout_io is not None else None,
        )

    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        assert isinstance(job, PrunJob)

        def group_nodes(nodes: Sequence[tuple[int, int]]) -> list[tuple[list[int], list[int]]]:
            groups = [([m], [c]) for m, c in sorted(nodes)]
            for i in range(len(groups) - 1, 0, -1):
                lmachines, lcores = groups[i - 1]
                rmachines, rcores = groups[i]
                if lmachines == rmachines and lcores[-1] + 1 == rcores[0]:
                    groups[i - 1] = lmachines, lcores + rcores
                    del groups[i]
                elif len(lcores) == 1 and lmachines[-1] + 1 == rmachines[0] and lcores == rcores:
                    groups[i - 1] = lmachines + rmachines, lcores
                    del groups[i]
            return groups

        def stringify_groups(groups: list[tuple[list[int], list[int]]]) -> str:
            samecore = set(c for m, cores in groups for c in cores) == set([0])

            def join(n: Sequence[Any], fmt: str) -> str:
                if len(n) == 1:
                    return fmt % n[0]
                else:
                    return fmt % n[0] + "-" + fmt % n[-1]

            if samecore:
                # all on core 0, omit it
                groupstrings = (join(m, "%03d") for m, c in groups)
            else:
                # different cores, add /N suffix
                groupstrings = (f"{join(m, '%03d')}/{join(c, '%d')}" for m, c in groups)

            if len(groups) == 1:
                m, c = groups[0]
                if len(m) == 1 and len(c) == 1:
                    return "node" + next(groupstrings)

            return f"node[{','.join(groupstrings)}]"

        numseconds: int | None = None
        nodes: list[tuple[int, int]] = []

        if (outs_io := job.stdout_io) is not None:
            while _outs_line := outs_io.readline():
                if isinstance(_outs_line, str):
                    outs_line = _outs_line
                elif isinstance(_outs_line, bytes):
                    outs_line = _outs_line.decode(encoding="ascii", errors="replace")
                else:
                    raise TypeError(f"Type of line read from stdout is invalid; got: {type(_outs_line)}")

                if job.logged and job.outfile_handle is not None:
                    job.outfile_handle.write(outs_line)
                    job.outfile_handle.flush()
                    job.outs += outs_line

                if outs_line.startswith(":"):
                    for m in re.finditer(r"node(\d+)/(\d+)", outs_line):
                        nodes.append((int(m.group(1)), int(m.group(2))))
                elif numseconds is None:
                    if (match := re.search(r"for (\d+) seconds", outs_line)) is not None:
                        numseconds = int(match.group(1))

        for line in job.outs.splitlines():
            if line.startswith(":"):
                for m in re.finditer(r"node(\d+)/(\d+)", line):
                    nodes.append((int(m.group(1)), int(m.group(2))))
            elif numseconds is None:
                match = re.search(r"for (\d+) seconds", line)
                if match:
                    numseconds = int(match.group(1))

        if len(nodes) == job.nnodes:
            assert numseconds is not None
            nodestr = stringify_groups(group_nodes(nodes))
            self.log.info(f"running {job.jobid} on {nodestr}")
            job.start_time = time.time()
            job.logged = True


def _set_non_blocking(f: IO) -> None:
    flags = fcntl.fcntl(f, fcntl.F_GETFL)
    fcntl.fcntl(f, fcntl.F_SETFL, flags | os.O_NONBLOCK)
