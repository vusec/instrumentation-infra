import io
import os
import time
import shlex
import random
import logging
import datetime
import threading

from abc import ABCMeta, abstractmethod
from typing import IO, Any, Callable, Iterable, Iterator
from pathlib import Path
from multiprocessing import cpu_count

from .context import Context
from .util import _Tee, Process, run


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
            self.__proc.close()
            self.__proc = None
        if not self.__out_stream.closed:
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
    def __init__(
        self,
        proc: Process,
        jobid: str,
        nnodes: int,
        out_file: str,
        start_time: float,
        out_stream: io.IOBase,
        allow_error: bool,
        pass_callback: Callable[[Job], bool | None] | None,
        fail_callback: Callable[[Job], bool | None] | None,
        node: str = "",
        tunnel_src: int | None = None,
        tunnel_dst: int | None = None,
    ) -> None:
        self.__node = node
        self.__tunnel_src = tunnel_src
        self.__tunnel_dst = tunnel_dst
        super().__init__(proc, jobid, nnodes, out_file, start_time, out_stream, allow_error, pass_callback, fail_callback)

    @property
    def node(self) -> str:
        return self.__node

    @property
    def tunnel_src(self) -> int | None:
        return self.__tunnel_src

    @property
    def tunnel_dst(self) -> int | None:
        return self.__tunnel_dst


class PrunJob(Job):
    def __init__(
        self,
        proc: Process,
        jobid: str,
        nnodes: int,
        out_file: str,
        start_time: float,
        out_stream: io.IOBase,
        allow_error: bool,
        pass_callback: Callable[[Job], bool | None] | None,
        fail_callback: Callable[[Job], bool | None] | None,
        logged: bool = True,
    ) -> None:
        self.__logged = True
        super().__init__(proc, jobid, nnodes, out_file, start_time, out_stream, allow_error, pass_callback, fail_callback)

    @property
    def logged(self) -> bool:
        return self.__logged


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

        # Create a lock to ensure modifying the list of currently running jobs doesn't cause races
        self.__lock: threading.Lock = threading.Lock()
        self.__condition: threading.Condition = threading.Condition(self.__lock)

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
            with self.__lock:
                curr_jobs = self.__curr_jobs.copy()
            if curr_jobs:
                for job in curr_jobs:
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
                            with self.__condition:
                                self.__curr_jobs.remove(job)
                                self.__condition.notify_all()

                    except Exception as err:
                        # Log but don't re-raise any jobs that threw an error; just remove them
                        self.log.error(f"Handling of job failed ({err}) for: {job}")

                        # If errors aren't allowed, store the exception to rethrow later
                        if not job.allow_error:
                            self.__exceptions.append(JobFailedException(job))

                        # Free the resources/file descriptors and such
                        job.close()

                        # Delete the node from the currently running jobs
                        with self.__condition:
                            self.__curr_jobs.remove(job)
                            self.__condition.notify_all()
            else:
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

        with self.__condition:
            while sum(job.nnodes for job in self.__curr_jobs) + nnodes > self.__limit:
                self.__condition.wait()

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
            with self.__lock:
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

    # Block stdin & background SSH sessions before executing command; also pre-answer some yes/no questions
    ssh_opts = ["-f", "-oStrictHostKeyChecking=accept-new"]

    # Quiet mode to disable progress meter, batch mode to disable asking for password, and also copy directories
    scp_opts = ["-q", "-B", "-r"]

    _tempdir: str | None

    def __init__(self, ctx: Context, parallelmax: int | None = None, nodes: list[str] | None = None):
        if nodes is None:
            nodes = []
        if parallelmax is None:
            parallelmax = cpu_count()
        assert parallelmax <= len(nodes)

        self.__ctx: Context = ctx

        self.__nodes: list[str] = sorted(set(nodes[:]))
        self.__avail: list[str] = sorted(set(nodes[:]))

        self.__tmp_dir: str | None = None

        self.__tested_nodes: bool = False

        super().__init__(ctx.log, parallelmax)

    @property
    def ctx(self) -> Context:
        return self.__ctx

    @property
    def tmpdir(self) -> str:
        if self.__tmp_dir is None:
            self.create_tmp_dirs()
        assert self.__tmp_dir is not None
        return self.__tmp_dir

    def test_nodes(self) -> None:
        if self.__tested_nodes:
            return

        # Check each node by running a simple echo command
        for node in self.__nodes:
            proc = run(self.ctx, ["ssh", *self.ssh_opts, node, "--", "echo", "-n", f"Testing: {node}"], merge_outputs=True)
            if proc.wait() != 0 or f"Testing: {node}" not in proc.stdout:
                raise RuntimeError(f"{node} failed: {proc.stdout}")

        self.__tested_nodes = True

    def create_tmp_dirs(self) -> None:
        if self.__tmp_dir is not None:
            return
        self.test_nodes()

        # Create a temporary directory in /tmp based on the start time of the infrastructure's context object
        self.__tmp_dir = os.path.join("/tmp", f"infra-{self.ctx.starttime.strftime('%Y-%m-%d_%H-%M-%S')}")
        self.ctx.log.info(f"Using temporary directory: {self.__tmp_dir}")

        # Create the temporary directories on each of the stored nodes
        for node in self.__nodes:
            run(self.ctx, ["ssh", *self.ssh_opts, node, "--", "mkdir", "-p", self.__tmp_dir])

    def cleanup_tmp_dirs(self) -> None:
        if self.__tmp_dir is None:
            return

        # Delete the temporary directory on each node
        for node in self.__nodes:
            run(self.ctx, ["ssh", *self.ssh_opts, node, "--", "rm", "-rf", self.__tmp_dir])

        # Clear the stored temporary directory
        self.__tmp_dir = None

    def sync_to_nodes(
        self,
        sources: str | os.PathLike | Iterable[str | os.PathLike],
        destination: str | os.PathLike,
        target_nodes: str | Iterable[str] | None = None,
    ) -> None:
        self.test_nodes()

        # Ensure the sources are a list of paths
        if isinstance(sources, (str, os.PathLike)):
            _sources = [Path(sources)]
        else:
            _sources = sorted(set(Path(source) for source in sources))

        # Get the destination as a path
        _destination = Path(destination)

        # If not given, all nodes are targeted
        if target_nodes is None:
            _target_nodes = self.__nodes
        elif isinstance(target_nodes, str):
            _target_nodes = [target_nodes]
        else:
            _target_nodes = sorted(set(target_nodes))
        self.ctx.log.debug(f"Syncing files to nodes; sources={_sources}; destination={_destination}; nodes={_target_nodes}")

        # For each of the target nodes, copy the sources to it
        for node in _target_nodes:
            run(self.ctx, ["scp", *self.scp_opts, *_sources, f"{node}:{Path(self.tmpdir) / _destination}"])

    def sync_from_nodes(
        self,
        source: str | os.PathLike,
        destination: str | os.PathLike | None = None,
        source_nodes: str | Iterable[str] | None = None,
    ) -> None:
        self.test_nodes()

        # Get the source as a path object
        _source = Path(source)

        # Get the destination as a path; use basename of source as default
        _destination = _source.name if destination is None else Path(destination)

        # If not given, all nodes are targeted
        if source_nodes is None:
            _source_nodes = self.__nodes
        elif isinstance(source_nodes, str):
            _source_nodes = [source_nodes]
        else:
            _source_nodes = sorted(set(source_nodes))
        self.ctx.log.debug(f"Syncing files from nodes; source={_source}; destination={_destination}; nodes={_source_nodes}")

        # For each of the target nodes, copy the sources to it
        for node in _source_nodes:
            run(self.ctx, ["scp", *self.scp_opts, f"{node}:{Path(self.tmpdir) / _source}", _destination])

    def get_free_node(self, override: str | None = None) -> str:
        if override is None:
            return self.__avail.pop()
        assert override in self.__nodes
        assert override in self.__avail
        self.__avail.remove(override)
        return override

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
        force_nodes: str | list[str] | None = None,
        tunnel_to_dest: int | None = None,
        **kwargs,
    ) -> Iterator[Job]:
        self.test_nodes()

        # Set required flags
        kwargs["defer"] = True
        kwargs["silent"] = True
        kwargs["teeout"] = False
        kwargs["merge_outputs"] = True

        cmd = cmd if isinstance(cmd, str) else [str(part) for part in cmd if part]
        cmd_arr = shlex.split(cmd) if isinstance(cmd, str) else cmd
        cmd_str = shlex.join(cmd_arr)
        assert cmd_arr and cmd_str

        if isinstance(force_nodes, str):
            force_nodes = [force_nodes]

        for i in range(nnodes):
            self.wait_for_space(1)
            _job_id = job_id if nnodes == 1 else f"{job_id}-{i}"
            _out_file = out_file if nnodes == 1 else f"{out_file}-{i}"
            _run_node = self.get_free_node(None if force_nodes is None else force_nodes[i])
            ctx.log.debug(f"Starting SSH job '{_job_id}' (node: {_run_node}; log: {_out_file})")

            _cmd: list[str] = ["ssh", *self.ssh_opts]

            if tunnel_to_dest is None:
                rand_src = None
                _cmd: list[str] = ["ssh", *self.ssh_opts, _run_node]
            else:
                rand_src = random.randint(10000, 30000)
                _cmd: list[str] = ["ssh", *self.ssh_opts, f"-Llocalhost:{rand_src}:0.0.0.0:{tunnel_to_dest}", _run_node]

            if isinstance(cmd, str):
                _cmd.append(cmd)
            else:
                _cmd.extend(str(part) for part in cmd)

            os.makedirs(name=os.path.dirname(_out_file), exist_ok=True)
            log_file = open(_out_file, mode="w", errors="replace")
            log_file.write(f"Job ID:        '{_job_id}'\n")
            log_file.write(f"Run node:      '{_run_node}'\n")
            log_file.write(f"Start time:    '{datetime.datetime.now()}'\n")
            log_file.write(f"Raw command:   '{cmd_str}'\n")
            log_file.write(f"Base command:  '{cmd_arr[0]}'\n")
            for idx, arg in enumerate(cmd_arr[1:]):
                log_file.write(f"    Arg {idx: 3d}:   '{arg}'\n")
            log_file.write(f"\n{'=' * 80}\n\n")
            log_file.flush()

            yield SSHJob(
                proc=run(ctx=ctx, cmd=_cmd, allow_error=allow_error, writers=[log_file], **kwargs),
                jobid=_job_id,
                nnodes=1,
                out_file=_out_file,
                start_time=time.time(),
                out_stream=log_file,
                allow_error=allow_error,
                pass_callback=pass_callback,
                fail_callback=fail_callback,
                node=_run_node,
                tunnel_src=rand_src,
                tunnel_dst=tunnel_to_dest,
            )


class PrunPool(Pool):
    default_job_time = 900  # if prun reserves this amount, it is not logged

    def __init__(self, logger: logging.Logger, parallelmax: int | None = None, prun_opts: Iterable[str] | None = None):
        self.__prun_opts = prun_opts
        super().__init__(logger, parallelmax)

    @property
    def prun_opts(self) -> Iterable[str]:
        return self.__prun_opts if self.__prun_opts is not None else []

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
        ctx.log.debug(f"Waiting for {nnodes} node(s) to become available...")
        self.wait_for_space(nnodes)
        ctx.log.debug(f"Scheduling '{job_id}' with {nnodes} node(s) (log: {out_file})")

        # Set required flags
        kwargs["defer"] = True
        kwargs["silent"] = True
        kwargs["teeout"] = False
        kwargs["merge_outputs"] = True

        cmd = cmd if isinstance(cmd, str) else [str(part) for part in cmd if part]
        cmd_arr = shlex.split(cmd) if isinstance(cmd, str) else cmd
        cmd_str = shlex.join(cmd_arr)
        assert cmd_arr and cmd_str

        os.makedirs(name=os.path.dirname(out_file), exist_ok=True)
        log_file = open(out_file, mode="w", errors="replace")
        log_file.write(f"Job ID:        '{job_id}'\n")
        log_file.write(f"Num nodes:     '{nnodes}'\n")
        log_file.write(f"Start time:    '{datetime.datetime.now()}'\n")
        log_file.write(f"Raw command:   '{cmd_str}'\n")
        log_file.write(f"Base command:  '{cmd_arr[0]}'\n")
        for idx, arg in enumerate(cmd_arr[1:]):
            log_file.write(f"    Arg {idx: 3d}:   '{arg}'\n")
        log_file.write(f"\n{'=' * 80}\n\n")
        log_file.flush()

        yield PrunJob(
            proc=run(
                ctx=ctx,
                cmd=["prun", "-v", "-np", str(nnodes), "1", "-o", out_file, *self.prun_opts, *cmd_arr],
                allow_error=allow_error,
                bufsize=0,
                **kwargs,
            ),
            jobid=job_id,
            nnodes=nnodes,
            out_file=out_file,
            start_time=time.time(),
            out_stream=log_file,
            allow_error=allow_error,
            pass_callback=pass_callback,
            fail_callback=fail_callback,
            logged=True,
        )
