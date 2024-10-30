import datetime
import io
import os
import re
import sys
import time
import fcntl
import shlex
import locale
import random
import logging
import threading
import subprocess

from abc import ABCMeta, abstractmethod
from typing import IO, Any, Callable, Iterable, Iterator, Sequence
from pathlib import Path
from dataclasses import dataclass, field
from multiprocessing import cpu_count

from .context import Context
from .util import _Tee, FatalError, Process, require_program, run


class Job:
    def __init__(
        self,
        proc: Process,
        jobid: str,
        nnodes: int,
        out_file: str,
        start_time: float,
        out_stream: io.IOBase,
        allow_error: bool,
        pass_callback: Callable[["Job"], bool | None] | None,
        fail_callback: Callable[["Job"], bool | None] | None,
    ) -> None:
        self.__proc: Process | None = proc
        self.__jobid: str = jobid
        self.__nnodes: int = nnodes
        self.__out_file: str = out_file
        self.__start_time: float = start_time
        self.__out_stream: io.IOBase = out_stream
        self.__allow_error: bool = allow_error
        self.__pass_callback: Callable[["Job"], bool | None] | None = pass_callback
        self.__fail_callback: Callable[["Job"], bool | None] | None = fail_callback

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.close()
        self.close_buffers()

    def close(self) -> None:
        if self.__proc is not None:
            self.__proc.flush()
            self.__proc.close()
            self.__proc = None
        if not self.__out_stream.closed:
            self.__out_stream.flush()
            self.__out_stream.close()

    def close_buffers(self) -> None:
        if self.__proc is not None:
            if not self.__proc.stdout_buff_closed:
                self.__proc.close_stdout()
            if not self.__proc.stderr_buff_closed:
                self.__proc.close_stderr()

    @property
    def proc(self) -> Process:
        assert self.__proc is not None
        return self.__proc

    @property
    def jobid(self) -> str:
        return self.__jobid

    @property
    def nnodes(self) -> int:
        return self.__nnodes

    @property
    def out_file(self) -> str:
        return self.__out_file

    @property
    def start_time(self) -> float:
        return self.__start_time

    @property
    def out_stream(self) -> io.IOBase:
        return self.__out_stream

    @property
    def allow_error(self) -> bool:
        return self.__allow_error

    @property
    def pass_callback(self) -> Callable[["Job"], bool | None] | None:
        return self.__pass_callback

    @property
    def fail_callback(self) -> Callable[["Job"], bool | None] | None:
        return self.__fail_callback

    def elapsed(self) -> str:
        return str(round(time.time() - self.start_time, ndigits=3))

    @property
    def args(self):
        return self.proc.args

    @property
    def cmd_str(self) -> str:
        return self.proc.cmd_str

    @property
    def input(self) -> str | bytes:
        return self.proc.input

    @property
    def stdin(self) -> IO | None:
        return self.proc.stdin

    @property
    def stdout(self) -> str:
        return self.proc.stdout

    @property
    def stdout_io(self) -> _Tee | None:
        return self.proc.stdout_io

    @property
    def stdout_raw(self) -> bytes:
        return self.proc.stdout_raw

    @property
    def stdout_buff(self) -> memoryview | None:
        return self.proc.stdout_buff

    @property
    def stderr(self) -> str:
        return self.proc.stderr

    @property
    def stderr_io(self) -> _Tee | None:
        return self.proc.stderr_io

    @property
    def stderr_raw(self) -> bytes:
        return self.proc.stderr_raw

    @property
    def stderr_buff(self) -> memoryview | None:
        return self.proc.stderr_buff

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def flush(self) -> None:
        self.proc.flush()

    def communicate(self, input: Any | None = None, timeout: float | None = None) -> tuple[Any, Any]:
        return self.proc.communicate(input, timeout)

    def kill(self) -> None:
        self.proc.kill()

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None):
        return self.proc.wait(timeout)

    def send_signal(self, sig: int) -> None:
        self.proc.send_signal(sig)

    def terminate(self) -> None:
        self.proc.terminate()


class JobFailedException(Exception):
    def __init__(self, job: Job):
        super().__init__(
            f"Job '{job.jobid}' failed with return code {job.returncode}"
            + (f"\nJob stdout:\n\n{outs}\n" if (outs := job.stdout) else "")
            + (f"\nJob stderr:\n\n{errs}\n" if (errs := job.stderr) else "")
        )
        self.job = job


class ProcessJob(Job):
    pass


class SSHJob(Job):
    outfile_handle: io.IOBase | IO | None = field(default=None)
    node: str = field(default="")

    tunnel_src: int | None = field(default=None)
    tunnel_dest: int | None = field(default=None)


class PrunJob(Job):
    outfile_handle: io.IOBase | IO | None = field(default=None)
    logged: bool = field(default=True)


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

    poll_interval: float = 0.01

    def __init__(self, logger: logging.Logger, parallelmax: int | None = None):
        self.log = logger

        # Store the maximum parallelism; default to the number of CPUs available
        self.__limit = parallelmax if parallelmax is not None else cpu_count()

        # Store a list to hold currently running jobs in
        self.__curr_jobs: list[Job] = []

        # Create & start a thread to continuously poll for finished jobs & handle them
        self.__running = True
        self.__thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.__thread.start()

        # Track exceptions that occurred in child processes
        self.__exceptions: list[Exception] = []

    @property
    def stopped(self) -> bool:
        return not self.__running

    def _poll_loop(self) -> None:
        while self.__running:
            while len(self.__curr_jobs) > 0:
                for job in self.__curr_jobs[:]:
                    try:
                        if (ret_code := job.poll()) is not None:
                            self.log.info(f"Job '{job.jobid}' returned {ret_code} after {job.elapsed()} second(s)")

                            # Check return code; call appropriate callback (if any) and log if they return True
                            if ret_code == 0:
                                if job.pass_callback is not None and job.pass_callback(job) is True:
                                    self.log.info(
                                        "[PASS] Job finished successfully\n"
                                        + f"  Status:       {ret_code}\n"
                                        + f"  Job ID:      '{job.jobid}'\n"
                                        + f"  Command:     '{job.cmd_str}'\n"
                                        + f"  Runtime:      {job.elapsed()} second(s)\n"
                                        + f"  Job stdout:   {f'\n{outs}' if (outs := job.stdout) else "<EMPTY>"}\n"
                                        + f"  Job stderr:   {f'\n{errs}' if (errs := job.stderr) else "<EMPTY>"}\n"
                                    )
                            else:
                                if job.fail_callback is not None and job.fail_callback(job) is True:
                                    self.log.info(
                                        "[FAIL] Job execution failed\n"
                                        + f"  Status:       {ret_code}\n"
                                        + f"  Job ID:      '{job.jobid}'\n"
                                        + f"  Command:     '{job.cmd_str}'\n"
                                        + f"  Runtime:      {job.elapsed()} second(s)\n"
                                        + f"  Job stdout:   {f'\n{outs}' if (outs := job.stdout) else "<EMPTY>"}\n"
                                        + f"  Job stderr:   {f'\n{errs}' if (errs := job.stderr) else "<EMPTY>"}\n"
                                    )

                                # Check if errors are allowed, otherwise store the exception to rethrow later
                                if not job.allow_error:
                                    self.__exceptions.append(JobFailedException(job))

                            # Free the resources/file descriptors and such
                            job.close()

                            # After the job was handled, remove it from the list of running jobs
                            self.__curr_jobs.remove(job)

                    except Exception as err:
                        # Log but don't re-raise any jobs that threw an error; just remove them
                        self.log.error(f"Handling of job failed ({err}) for: {job}")

                        # If errors aren't allowed, store the exception to rethrow later
                        if not job.allow_error:
                            self.__exceptions.append(JobFailedException(job))

                        # Free the resources/file descriptors and such
                        if job.stdout_io is not None:
                            job.stdout_io.close_write_fd()
                            job.stdout_io.flush()
                        if job.stderr_io is not None:
                            job.stderr_io.close_write_fd()
                            job.stderr_io.flush()
                        if not job.out_stream.closed:
                            job.out_stream.flush()
                            job.out_stream.close()

                        # Delete the node from the currently running jobs
                        self.__curr_jobs.remove(job)

            # If there were no more remaining jobs, sleep until checking again
            time.sleep(self.poll_interval)

    def shutdown(self) -> None:
        self.__running = False
        self.__thread.join()

    def wait(self, check_errors: bool = True) -> None:
        while len(self.__curr_jobs) > 0:
            time.sleep(self.poll_interval)
        if check_errors:
            self.check_job_errors()

    def check_job_errors(self) -> None:
        if self.__exceptions:
            raise self.__exceptions[0]

    def wait_for_space(self, nnodes: int = 1) -> None:
        if nnodes > self.__limit:
            raise RuntimeError(f"Requested nodes exceed limit; requested {nnodes}; limit: {self.__limit}")

        while sum(job.nnodes for job in self.__curr_jobs) + nnodes > self.__limit:
            time.sleep(self.poll_interval)

    def run(
        self,
        ctx: Context,
        cmd: str | Iterable[Any],
        jobid: str,
        nnodes: int | None = None,
        outfile: str | None = None,
        allow_error: bool | None = None,
        pass_callback: Callable[["Job"], bool | None] | None = None,
        fail_callback: Callable[["Job"], bool | None] | None = None,
        **kwargs,
    ) -> list[Job]:
        nnodes = nnodes if nnodes is not None else 1
        out_file = outfile if outfile is not None else f"{jobid}"
        allow_error = allow_error if allow_error is not None else False
        ctx.log.info(f"Running {nnodes} nodes for job base ID '{jobid}'; logfile base: {out_file}")

        jobs: list[Job] = []
        for job in self.make_jobs(
            ctx=ctx,
            cmd=cmd,
            job_id=jobid,
            nnodes=nnodes if nnodes is not None else 1,
            out_file=out_file if out_file is not None else f"{jobid}",
            allow_error=allow_error if allow_error is not None else False,
            pass_callback=pass_callback,
            fail_callback=fail_callback,
            **kwargs,
        ):
            self.__curr_jobs.append(job)
            jobs.append(job)
        return jobs

    @abstractmethod
    def make_jobs(
        self,
        ctx: Context,
        cmd: str | Iterable[Any],
        job_id: str,
        nnodes: int,
        out_file: str,
        allow_error: bool,
        pass_callback: Callable[["Job"], bool | None] | None,
        fail_callback: Callable[["Job"], bool | None] | None,
        **kwargs,
    ) -> Iterator[Job]:
        pass


class ProcessPool(Pool):
    def make_jobs(
        self,
        ctx: Context,
        cmd: str | Iterable[Any],
        job_id: str,
        nnodes: int,
        out_file: str,
        allow_error: bool,
        pass_callback: Callable[[Job], bool | None] | None,
        fail_callback: Callable[[Job], bool | None] | None,
        **kwargs,
    ) -> Iterator[Job]:
        # Set required flags
        kwargs["defer"] = True
        kwargs["silent"] = True
        kwargs["teeout"] = False
        kwargs["merge_outputs"] = True

        cmd = cmd if isinstance(cmd, str) else [str(part) for part in cmd if part]
        cmd_arr = shlex.split(cmd) if isinstance(cmd, str) else cmd
        cmd_str = shlex.join(cmd_arr)
        assert cmd_arr and cmd_str

        # Start one process (with subprocess.Popen) per requested node
        for i in range(nnodes):
            self.wait_for_space(1)
            _job_id = job_id if nnodes == 1 else f"{job_id}-{i}"
            _out_file = f"{out_file if nnodes == 1 else f'{out_file}-{i}'}.log"
            ctx.log.debug(f"Starting job '{_job_id}' (log: {_out_file})")

            os.makedirs(name=os.path.dirname(_out_file), exist_ok=True)
            log_file = open(_out_file, mode="w", errors="replace")
            log_file.write(f"Job ID:        '{_job_id}'\n")
            log_file.write(f"Start time:    '{datetime.datetime.now()}'\n")
            log_file.write(f"Raw command:   '{cmd_str}'\n")
            log_file.write(f"Base command:  '{cmd_arr[0]}'\n")
            for idx, arg in enumerate(cmd_arr[1:]):
                log_file.write(f"    Arg {idx: 3d}:   '{arg}'\n")
            log_file.write(f"\n{'=' * 80}\n\n")
            log_file.flush()
            yield ProcessJob(
                proc=run(ctx=ctx, cmd=cmd, allow_error=allow_error, writers=[log_file], **kwargs),
                jobid=_job_id,
                nnodes=1,
                out_file=_out_file,
                start_time=time.time(),
                out_stream=log_file,
                allow_error=allow_error,
                pass_callback=pass_callback,
                fail_callback=fail_callback,
            )


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

    # def __init__(self, ctx: Context, logger: logging.Logger, parallelmax: int, nodes: list[str]):
    #     if parallelmax > len(nodes):
    #         raise FatalError("parallelmax cannot be greater than number of available nodes")
    #     super().__init__(logger, parallelmax)
    #     self._ctx = ctx
    #     self.nodes = nodes[:]
    #     self.available_nodes = nodes[:]
    #     self.has_tested_nodes = False
    #     self.has_created_tempdirs = False

    # @property
    # def tempdir(self) -> str:
    #     if not self.has_created_tempdirs:
    #         self.create_tempdirs()
    #     assert self._tempdir is not None
    #     return self._tempdir

    # def _ssh_cmd(
    #     self,
    #     node: str,
    #     cmd: str | Iterable[str],
    #     extra_opts: Sequence[Any] | None = None,
    # ) -> list[str]:
    #     if not isinstance(cmd, str):
    #         cmd = " ".join(shlex.quote(str(c)) for c in cmd)
    #     extra_opts = extra_opts or []
    #     return ["ssh", *self.ssh_opts, *extra_opts, node, cmd]

    # def test_nodes(self) -> None:
    #     if self.has_tested_nodes:
    #         return
    #     for node in self.nodes:
    #         cmd = ["ssh", *self.ssh_opts, node, "echo -n hi"]
    #         p = run(self._ctx, cmd, stderr=subprocess.STDOUT, silent=True)
    #         if p.returncode or not str(p.stdout).endswith("hi"):
    #             self._ctx.log.error("Testing SSH node " + node + " failed:\n" + p.stdout)
    #             sys.exit(-1)
    #     self.has_tested_nodes = True

    # def create_tempdirs(self) -> None:
    #     if self.has_created_tempdirs:
    #         return

    #     self.test_nodes()

    #     starttime = self._ctx.starttime.strftime("%Y-%m-%d.%H-%M-%S")
    #     self._tempdir = os.path.join("/tmp", "infra-" + starttime)

    #     self._ctx.log.debug(f"creating SSHPool temp dir {self._tempdir} on nodes {self.nodes}")

    #     for node in self.nodes:
    #         run(self._ctx, self._ssh_cmd(node, ["mkdir", "-p", self._tempdir]))

    #     self.has_created_tempdirs = True

    # def cleanup_tempdirs(self) -> None:
    #     if not self.has_created_tempdirs:
    #         return
    #     assert self._tempdir is not None
    #     self._ctx.log.debug(f"cleaning up SSHPool temp directory {self._tempdir} on nodes {self.nodes}")
    #     for node in self.nodes:
    #         run(self._ctx, self._ssh_cmd(node, ["rm", "-rf", self._tempdir]))
    #     self.has_created_tempdirs = False
    #     self._tempdir = None

    # def sync_to_nodes(
    #     self,
    #     sources: str | Iterable[str],
    #     destination: str = "",
    #     target_nodes: str | Iterable[str] | None = None,
    # ) -> None:
    #     if isinstance(sources, str):
    #         sources = [sources]
    #     if isinstance(target_nodes, str):
    #         target_nodes = [target_nodes]
    #     nodes = target_nodes or self.nodes
    #     self._ctx.log.debug(f"syncing file to SSHPool nodes, sources={sources}," f"destination={destination}, nodes={nodes}")
    #     for node in nodes:
    #         dest = f"{node}:{os.path.join(self.tempdir, destination)}"
    #         cmd = ["scp", *self.scp_opts, *sources, dest]
    #         run(self._ctx, cmd)

    # def sync_from_nodes(
    #     self,
    #     source: str,
    #     destination: str = "",
    #     source_nodes: Sequence[str] | None = None,
    # ) -> None:
    #     if isinstance(source_nodes, str):
    #         source_nodes = [source_nodes]
    #     nodes = source_nodes or self.nodes

    #     self._ctx.log.debug(f"syncing file from SSHPool nodes, source={source}," f"destination={destination}, nodes={nodes}")

    #     for i, node in enumerate(nodes):
    #         dest = destination or os.path.basename(source)
    #         if len(nodes) > 1:
    #             dest += "." + node
    #             if len(nodes) != len(set(nodes)):
    #                 dest = f"{dest}{i}"
    #         src = f"{node}:{os.path.join(self.tempdir, source)}"
    #         cmd = ["scp", *self.scp_opts, src, dest]
    #         run(self._ctx, cmd)

    # def get_free_node(self, override_node: str | None = None) -> str:
    #     if override_node:
    #         assert override_node in self.nodes
    #         assert override_node in self.available_nodes
    #         self.available_nodes.remove(override_node)
    #         return override_node
    #     else:
    #         return self.available_nodes.pop()

    # def make_jobs(
    #     self,
    #     ctx: Context,
    #     cmd: str | Iterable[str],
    #     jobid_base: str,
    #     outfile_base: str,
    #     nnodes: int,
    #     nodes: str | list[str] | None = None,
    #     tunnel_to_nodes_dest: int | None = None,
    #     **kwargs: Any,
    # ) -> Iterator[Job]:
    #     if isinstance(nodes, str):
    #         nodes = [nodes]

    #     self.test_nodes()

    #     for i in range(nnodes):
    #         jobid = jobid_base
    #         outfile = outfile_base
    #         if nnodes > 1:
    #             jobid += f"-{i}"
    #             outfile += f"-{i}"

    #         self._wait_for_queue_space(1)
    #         override_node = nodes[i] if nodes else None
    #         node = self.get_free_node(override_node)
    #         ctx.log.info("running " + jobid + " on " + node)

    #         ssh_node_opts = []
    #         tunnel_src = None
    #         if tunnel_to_nodes_dest:
    #             tunnel_src = random.randint(10000, 30000)
    #             ssh_node_opts += [f"-Llocalhost:{tunnel_src}:0.0.0.0:{tunnel_to_nodes_dest}"]

    #         ssh_cmd = self._ssh_cmd(node, cmd, ssh_node_opts)
    #         proc = run(
    #             ctx,
    #             ssh_cmd,
    #             defer=True,
    #             stderr=subprocess.STDOUT,
    #             bufsize=io.DEFAULT_BUFFER_SIZE,
    #             universal_newlines=False,
    #             **kwargs,
    #         )

    #         if (outs_io := proc.stdout_io) is not None:
    #             _set_non_blocking(outs_io)

    #         os.makedirs(os.path.dirname(outfile), exist_ok=True)
    #         yield SSHJob(
    #             proc=proc,
    #             jobid=jobid,
    #             nnodes=1,
    #             out_base=outfile,
    #             start_time=time.time(),
    #             onsuccess=None,
    #             onerror=None,
    #             outs="",
    #             errs="",
    #             outfile_handle=open(outfile, mode="w"),
    #             tunnel_src=tunnel_src if tunnel_to_nodes_dest else None,
    #             tunnel_dest=tunnel_to_nodes_dest if tunnel_to_nodes_dest else None,
    #         )

    # def process_job_output(self, job: Job, fd: int | None = None) -> None:
    #     assert isinstance(job, SSHJob)

    #     if (outs_io := job.stdout_io) is not None:
    #         while _outs_line := outs_io.readline():
    #             if isinstance(_outs_line, str):
    #                 outs_line = _outs_line
    #             elif isinstance(_outs_line, bytes):
    #                 outs_line = _outs_line.decode(encoding=locale.getpreferredencoding(False), errors="replace")
    #             else:
    #                 raise TypeError(f"Type of line read from stdout is invalid; got: {type(_outs_line)}")

    #             assert job.outfile_handle is not None
    #             job.outfile_handle.write(outs_line)
    #             job.outfile_handle.flush()
    #             job.outs += outs_line

    # def onsuccess(self, job: Job) -> None:
    #     assert isinstance(job, SSHJob)

    #     if (outs_io := job.stdout_io) is not None:
    #         self.process_job_output(job, outs_io.fileno())
    #         assert job.outfile_handle is not None
    #         if not job.outfile_handle.closed:
    #             job.outfile_handle.close()

    #     self.available_nodes.append(job.node)
    #     super().onsuccess(job)

    # def onfailure(self, job: Job) -> None:
    #     assert isinstance(job, SSHJob)

    #     if (outs_io := job.stdout_io) is not None:
    #         self.process_job_output(job, outs_io.fileno())
    #         assert job.outfile_handle is not None
    #         if not job.outfile_handle.closed:
    #             job.outfile_handle.close()

    #     super().onfailure(job)


class PrunPool(Pool):
    default_job_time = 900  # if prun reserves this amount, it is not logged

    # def __init__(self, logger: logging.Logger, parallelmax: int, prun_opts: Iterable[str]):
    #     super().__init__(logger, parallelmax)
    #     self.prun_opts = prun_opts

    # def make_jobs(
    #     self,
    #     ctx: Context,
    #     cmd: Iterable[str] | str,
    #     jobid_base: str,
    #     outfile_base: str,
    #     nnodes: int,
    #     **kwargs: Any,
    # ) -> Iterator[Job]:
    #     require_program(ctx, "prun")
    #     self._wait_for_queue_space(nnodes)
    #     ctx.log.info("scheduling " + jobid_base)
    #     cmd = [
    #         "prun",
    #         "-v",
    #         "-np",
    #         str(nnodes),
    #         "-1",
    #         "-o",
    #         outfile_base,
    #         *self.prun_opts,
    #         *cmd,
    #     ]
    #     proc = run(
    #         ctx,
    #         cmd,
    #         defer=True,
    #         stderr=subprocess.STDOUT,
    #         bufsize=0,
    #         universal_newlines=False,
    #         **kwargs,
    #     )

    #     if (outs_io := proc.stdout_io) is not None:
    #         _set_non_blocking(outs_io)

    #     yield PrunJob(
    #         proc=proc,
    #         jobid=jobid_base,
    #         nnodes=nnodes,
    #         out_base=outfile_base,
    #         start_time=time.time(),
    #         onsuccess=None,
    #         onerror=None,
    #         outs="",
    #         errs="",
    #         outfile_handle=open(outfile_base, mode="w") if proc.stdout_io is not None else None,
    #     )

    # def process_job_output(self, job: Job, fd: int | None = None) -> None:
    #     assert isinstance(job, PrunJob)

    #     def group_nodes(nodes: Sequence[tuple[int, int]]) -> list[tuple[list[int], list[int]]]:
    #         groups = [([m], [c]) for m, c in sorted(nodes)]
    #         for i in range(len(groups) - 1, 0, -1):
    #             lmachines, lcores = groups[i - 1]
    #             rmachines, rcores = groups[i]
    #             if lmachines == rmachines and lcores[-1] + 1 == rcores[0]:
    #                 groups[i - 1] = lmachines, lcores + rcores
    #                 del groups[i]
    #             elif len(lcores) == 1 and lmachines[-1] + 1 == rmachines[0] and lcores == rcores:
    #                 groups[i - 1] = lmachines + rmachines, lcores
    #                 del groups[i]
    #         return groups

    #     def stringify_groups(groups: list[tuple[list[int], list[int]]]) -> str:
    #         samecore = set(c for m, cores in groups for c in cores) == set([0])

    #         def join(n: Sequence[Any], fmt: str) -> str:
    #             if len(n) == 1:
    #                 return fmt % n[0]
    #             else:
    #                 return fmt % n[0] + "-" + fmt % n[-1]

    #         if samecore:
    #             # all on core 0, omit it
    #             groupstrings = (join(m, "%03d") for m, c in groups)
    #         else:
    #             # different cores, add /N suffix
    #             groupstrings = (f"{join(m, '%03d')}/{join(c, '%d')}" for m, c in groups)

    #         if len(groups) == 1:
    #             m, c = groups[0]
    #             if len(m) == 1 and len(c) == 1:
    #                 return "node" + next(groupstrings)

    #         return f"node[{','.join(groupstrings)}]"

    #     numseconds: int | None = None
    #     nodes: list[tuple[int, int]] = []

    #     if (outs_io := job.stdout_io) is not None:
    #         while _outs_line := outs_io.readline():
    #             if isinstance(_outs_line, str):
    #                 outs_line = _outs_line
    #             elif isinstance(_outs_line, bytes):
    #                 outs_line = _outs_line.decode(encoding=locale.getpreferredencoding(False), errors="replace")
    #             else:
    #                 raise TypeError(f"Type of line read from stdout is invalid; got: {type(_outs_line)}")

    #             if job.logged and job.outfile_handle is not None:
    #                 job.outfile_handle.write(outs_line)
    #                 job.outfile_handle.flush()
    #                 job.outs += outs_line

    #             if outs_line.startswith(":"):
    #                 for m in re.finditer(r"node(\d+)/(\d+)", outs_line):
    #                     nodes.append((int(m.group(1)), int(m.group(2))))
    #             elif numseconds is None:
    #                 if (match := re.search(r"for (\d+) seconds", outs_line)) is not None:
    #                     numseconds = int(match.group(1))

    #     for line in job.outs.splitlines():
    #         if line.startswith(":"):
    #             for m in re.finditer(r"node(\d+)/(\d+)", line):
    #                 nodes.append((int(m.group(1)), int(m.group(2))))
    #         elif numseconds is None:
    #             match = re.search(r"for (\d+) seconds", line)
    #             if match:
    #                 numseconds = int(match.group(1))

    #     if len(nodes) == job.nnodes:
    #         assert numseconds is not None
    #         nodestr = stringify_groups(group_nodes(nodes))
    #         self.log.info(f"running {job.jobid} on {nodestr}")
    #         job.start_time = time.time()
    #         job.logged = True


def _set_non_blocking(f: io.IOBase | IO) -> None:
    flags = fcntl.fcntl(f, fcntl.F_GETFL)
    fcntl.fcntl(f, fcntl.F_SETFL, flags | os.O_NONBLOCK)
