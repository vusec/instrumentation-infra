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
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from subprocess import STDOUT
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .context import Context
from .util import FatalError, Process, require_program, run

# TODO: rewrite this to use
# https://docs.python.org/3/library/concurrent.futures.html?


@dataclass
class Job:
    proc: Process
    jobid: str
    outfiles: List[str]

    nnodes: int = field(default=1, init=False)
    start_time: float = field(default_factory=time.time, init=False)
    onsuccess: Optional[Callable[["Job"], Optional[bool]]] = field(default=None, init=False)
    onerror: Optional[Callable[["Job"], Optional[bool]]] = field(default=None, init=False)
    output: str = field(default="", init=False)
    err_output: str = field(default="", init=False)

    @property
    def stdout(self) -> IO:
        return self.proc.stdout_io

    @property
    def stderr(self) -> IO:
        return self.proc.stderr_io


@dataclass
class ProcessJob(Job):
    stdout_outfile_handle: IO
    stderr_outfile_handle: IO


@dataclass
class SSHJob(Job):
    outfile_handle: IO
    node: str

    tunnel_src: Optional[int] = None
    tunnel_dest: Optional[int] = None


@dataclass
class PrunJob(Job):
    nnodes: int

    logged: bool = False


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

    poll_interval: float = 0.050  # seconds to wait for blocking actions

    jobs: Dict[int, Job]
    pollthread: Optional[threading.Thread]

    @abstractmethod
    def make_jobs(
        self,
        ctx: Context,
        cmd: Union[str, Iterable[str]],
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
        self.pollthread = None

    def __del__(self) -> None:
        if self.pollthread is not None:
            self.done = True
            self.pollthread.join(self.poll_interval)

    def _start_poller(self) -> None:
        if self.pollthread is None:
            self.poller = select.epoll()
            self.pollthread = threading.Thread(target=self._poller_thread, name="pool-poller")
            self.pollthread.daemon = True
            self.done = False
            self.pollthread.start()

    def _poller_thread(self) -> None:
        # monitor the job queue for finished jobs, remove them from the queue
        # and call success/error callbacks
        while not self.done:
            for fd, flags in self.poller.poll(timeout=self.poll_interval):
                job = self.jobs[fd]
                if flags & (select.EPOLLIN | select.EPOLLPRI):
                    self.process_job_output(job, fd)

                if flags & select.EPOLLERR:
                    self.log.error(f"Error in file descriptor: {fd}")
                    self.poller.unregister(fd)
                    del self.jobs[fd]
                    for alt_fd in (_fd for _fd, _job in self.jobs.items() if _job is job):
                        self.log.error(f"Found alternative file descriptor (with same job): {alt_fd}")
                        del self.jobs[alt_fd]
                    self.onerror(job)

                if flags & select.EPOLLHUP:
                    if job.proc.poll() is None:
                        self.log.debug(f"job {job.jobid} hung up but does not yet have a " "return code, check later")
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
        cmd: Union[str, Iterable[str]],
        jobid: str,
        outfile: str,
        nnodes: int,
        onsuccess: Optional[Callable[[Job], Optional[bool]]] = None,
        onerror: Optional[Callable[[Job], Optional[bool]]] = None,
        **kwargs: Any,
    ) -> Iterable[Job]:
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
        # TODO: generate outfile from jobid
        self._start_poller()

        if isinstance(cmd, str):
            cmd = shlex.split(cmd)

        jobs = []

        for job in self.make_jobs(ctx, cmd, jobid, outfile, nnodes, **kwargs):
            job.onsuccess = onsuccess
            job.onerror = onerror
            job.output = ""
            job.err_output = ""
            self.jobs[job.proc.stdout_io.fileno()] = job
            self.jobs[job.proc.stderr_io.fileno()] = job
            self.poller.register(
                job.proc.stdout_io,
                select.EPOLLIN | select.EPOLLPRI | select.EPOLLERR | select.EPOLLHUP,
            )
            self.poller.register(
                job.proc.stderr_io,
                select.EPOLLIN | select.EPOLLPRI | select.EPOLLERR | select.EPOLLHUP,
            )
            jobs.append(job)

        return jobs

    def onsuccess(self, job: Job) -> None:
        # don't log if onsuccess() returns False
        if not job.onsuccess or job.onsuccess(job) is not False:
            self.log.info(f"job {job.jobid} finished {self._get_elapsed(job)}")
            self.log.debug(f"command: {job.proc.cmd_str}")

    def onerror(self, job: Job) -> None:
        # don't log if onerror() returns False
        if not job.onerror or job.onerror(job) is not False:
            self.log.error(f"job {job.jobid} returned status {job.proc.returncode} " f"{self._get_elapsed(job)}")
            self.log.error(f"command: {job.proc.cmd_str}")
            sys.stdout.write(f"stdout:\n{job.output}\n")
            sys.stdout.write(f"stderr:\n{job.err_output}\n")
            sys.stdout.flush()

    def _get_elapsed(self, job: Job) -> str:
        elapsed = round(time.time() - job.start_time)
        return f"after {elapsed} seconds"


class ProcessPool(Pool):
    def make_jobs(
        self,
        ctx: Context,
        cmd: Union[str, Iterable[str]],
        jobid_base: str,
        outfile_base: str,
        nnodes: int,
        **kwargs: Any,
    ) -> Iterator[Job]:
        for i in range(nnodes):
            jobid = jobid_base
            outfile = outfile_base
            if nnodes > 1:
                jobid += f"-{i}"
                outfile += f"-{i}"

            stdout_outfile = f"{outfile}.stdout"
            stderr_outfile = f"{outfile}.stderr"

            self._wait_for_queue_space(1)
            ctx.log.info("running " + jobid)

            # kwargs.setdefault("stderr", STDOUT)

            proc = run(
                ctx,
                cmd,
                defer=True,
                bufsize=io.DEFAULT_BUFFER_SIZE,
                universal_newlines=False,
                **kwargs,
            )
            _set_non_blocking(proc.stdout_io)
            _set_non_blocking(proc.stderr_io)
            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            job = ProcessJob(
                proc,
                jobid,
                [stdout_outfile, stderr_outfile],
                open(stdout_outfile, "wb"),
                open(stderr_outfile, "wb"),
            )

            yield job

    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        assert isinstance(job, ProcessJob)
        if fd is None or (fd is not None and fd == job.stdout.fileno()):
            buf = job.stdout.read(io.DEFAULT_BUFFER_SIZE)
            if buf is not None:
                job.output += buf.decode("ascii", errors="replace")
                job.stdout_outfile_handle.write(buf)
                job.stdout_outfile_handle.flush()
        elif fd is not None and fd == job.stderr.fileno():
            buf = job.stderr.read(io.DEFAULT_BUFFER_SIZE)
            if buf is not None:
                job.err_output += buf.decode("ascii", errors="replace")
                job.stderr_outfile_handle.write(buf)
                job.stderr_outfile_handle.flush()
        else:
            self.log.error(f"Invalid file descriptor received, not stdout/stderr: {fd}")

    def onsuccess(self, job: Job) -> None:
        assert isinstance(job, ProcessJob)
        job.stdout_outfile_handle.close()
        job.stderr_outfile_handle.close()
        super().onsuccess(job)

    def onerror(self, job: Job) -> None:
        assert isinstance(job, ProcessJob)
        job.stdout_outfile_handle.close()
        job.stderr_outfile_handle.close()
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

    _tempdir: Optional[str]

    def __init__(self, ctx: Context, logger: logging.Logger, parallelmax: int, nodes: List[str]):
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
        cmd: Union[str, Iterable[str]],
        extra_opts: Optional[Sequence[Any]] = None,
    ) -> List[str]:
        if not isinstance(cmd, str):
            cmd = " ".join(shlex.quote(str(c)) for c in cmd)
        extra_opts = extra_opts or []
        return ["ssh", *self.ssh_opts, *extra_opts, node, cmd]

    def test_nodes(self) -> None:
        if self.has_tested_nodes:
            return
        for node in self.nodes:
            cmd = ["ssh", *self.ssh_opts, node, "echo -n hi"]
            p = run(self._ctx, cmd, stderr=STDOUT, silent=True)
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
        sources: Union[str, Iterable[str]],
        destination: str = "",
        target_nodes: Optional[Union[str, Iterable[str]]] = None,
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
        source_nodes: Optional[Sequence[str]] = None,
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

    def get_free_node(self, override_node: Optional[str] = None) -> str:
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
        cmd: Union[str, Iterable[str]],
        jobid_base: str,
        outfile_base: str,
        nnodes: int,
        nodes: Optional[Union[str, List[str]]] = None,
        tunnel_to_nodes_dest: Optional[int] = None,
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
                stderr=STDOUT,
                bufsize=io.DEFAULT_BUFFER_SIZE,
                universal_newlines=False,
                **kwargs,
            )
            _set_non_blocking(proc.stdout_io)

            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            job = SSHJob(proc, jobid, [outfile], open(outfile, "wb"), node)

            if tunnel_to_nodes_dest:
                job.tunnel_src = tunnel_src
                job.tunnel_dest = tunnel_to_nodes_dest

            yield job

    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        assert isinstance(job, SSHJob)
        buf = job.stdout.read(io.DEFAULT_BUFFER_SIZE)
        if buf is not None:
            job.output += buf.decode("ascii", errors="replace")
            job.outfile_handle.write(buf)

    def onsuccess(self, job: Job) -> None:
        assert isinstance(job, SSHJob)
        job.outfile_handle.close()
        self.available_nodes.append(job.node)
        super().onsuccess(job)

    def onerror(self, job: Job) -> None:
        assert isinstance(job, SSHJob)
        self.available_nodes.append(job.node)
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
        cmd: Union[str, Iterable[str]],
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
            stderr=STDOUT,
            bufsize=0,
            universal_newlines=False,
            **kwargs,
        )
        _set_non_blocking(proc.stdout_io)
        outfiles = [f"{outfile_base}.{i}" for i in range(nnodes)]
        job = PrunJob(proc, jobid_base, outfiles, nnodes)
        yield job

    def process_job_output(self, job: Job, fd: int | None = None) -> None:
        assert isinstance(job, PrunJob)

        def group_nodes(nodes: Sequence[Tuple[int, int]]) -> List[Tuple[List[int], List[int]]]:
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

        def stringify_groups(groups: List[Tuple[List[int], List[int]]]) -> str:
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

        buf = job.stdout.read(1024)
        if buf is None:
            return

        job.output += buf.decode("ascii")

        if job.logged:
            return

        numseconds = None
        nodes: List[Tuple[int, int]] = []

        for line in job.output.splitlines():
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
