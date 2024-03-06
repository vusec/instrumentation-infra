import io
import logging
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
from time import sleep
import typing
from collections import OrderedDict
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import (
    IO,
    Any,
    AnyStr,
    Callable,
    Dict,
    ItemsView,
    Iterable,
    Iterator,
    KeysView,
    List,
    Literal,
    Mapping,
    MutableMapping,
    Optional,
    TypeVar,
    Union,
    ValuesView,
)
from urllib.parse import urlparse
from urllib.request import urlretrieve

from .context import Context

EnvDict = Mapping[str, Union[str, List[str]]]

ResultVal = Union[bool, int, float, str]
ResultDict = MutableMapping[str, ResultVal]
ResultsByInstance = MutableMapping[str, List[ResultDict]]

T = TypeVar("T")


class Index(MutableMapping[str, T]):
    mem: MutableMapping[str, T]

    def __init__(self, thing_name: str):
        self.mem = OrderedDict()
        self.thing_name = thing_name

    def __getitem__(self, key: str) -> T:
        if key not in self.mem:
            raise FatalError(f"no {self.thing_name} called '{key}'")
        return self.mem[key]

    def __setitem__(self, key: str, value: T) -> None:
        if key in self.mem:
            raise FatalError(f"{self.thing_name} '{key}' already exists")
        self.mem[key] = value

    def __delitem__(self, key: str) -> None:
        if key not in self.mem:
            raise FatalError(f"no {self.thing_name} called '{key}'")
        del self.mem[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.mem)

    def __len__(self) -> int:
        return len(self.mem)

    def keys(self) -> KeysView[str]:
        return self.mem.keys()

    def values(self) -> ValuesView[T]:
        return self.mem.values()

    def items(self) -> ItemsView[str, T]:
        return self.mem.items()

    def all(self) -> List[T]:
        return list(self.mem.values())

    def select(self, keys: Iterable[str]) -> List[T]:
        return [self[key] for key in keys]


class LazyIndex(Index):
    def __init__(self, thing_name: str, find_value: Callable[[str], Any]):
        super().__init__(thing_name)
        self.find_value = find_value

    def __getitem__(self, key: str) -> Any:
        value = self.mem.get(key, None)
        if value is None:
            self.mem[key] = value = self.find_value(key)
        if value is None:
            raise FatalError(f"no {self.thing_name} called '{key}'")
        return value


class FatalError(Exception):
    """
    Raised for errors that should stop the execution immediately, but do not
    need a backtrace. Results in only the exception message being logged. This
    typically means there is an error in the user input, rather than in the code
    that raises the error.
    """

    pass


def apply_patch(ctx: Context, path: str, strip_count: int) -> bool:
    """
    Applies a patch in the current directory by calling ``patch -p<strip_count>
    < <path>``.

    Afterwards, a stamp file called ``.patched-<basename>`` is created to
    indicate that the patch has been applied. If the stamp file is already
    present, the patch is not applied at all. ``<basename>`` is generated from
    the patch file name: ``path/to/my-patch.patch`` becomes ``my-patch``.

    :param ctx: the configuration context
    :param path: path to the patch file
    :param strip_count: number of leading elements to strip from patch paths
    :returns: ``True`` if the patch was applied, ``False`` if it was already
              applied before
    """
    path = os.path.abspath(path)
    name = os.path.basename(path).replace(".patch", "")
    stamp = ".patched-" + name

    if os.path.exists(stamp):
        # TODO: check modification time
        return False

    ctx.log.debug(f"applying patch {name}")
    require_program(ctx, "patch", "required to apply source patches")

    with open(path) as f:
        run(ctx, f"patch -p{strip_count}", stdin=f)

    open(stamp, "w").close()
    return True


def join_env_paths(env: EnvDict) -> Dict[str, str]:
    """
    Convert an environment dictionary to a dictionary mapping variable names to their values, all as
    strings. Lists in the given dictionary are converted to ":"-delimited lists (e.g. like $PATH).

    Note: the given dictionary should contain only str or list[str], but for both this function will
    also attempt to convert them to string if possible.

    :param EnvDict env: the environment dicitonary to convert (should contain str or list[str])
    :return Dict[str, str]: a str-to-str mapping that can be used to pass to e.g. subprocess.run()
    """
    ret = {}
    for k, v in env.items():
        if isinstance(v, str):
            ret[k] = v
        elif isinstance(v, Iterable):
            ret[k] = ":".join([str(x) for x in v])
        else:
            ret[k] = str(v)
    return ret


class StrippingFormatter(logging.Formatter):
    """Formatter that strips ANSI escape sequences from the message"""

    # 7-bit C1 ANSI sequences
    ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, str):
            record.msg = self.ansi_escape.sub("", record.msg)
        return super().format(record)


class MultiFormatter(logging.Formatter):
    """Wraps long lines & indents subsequent lines to configured width"""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: Literal["%", "{", "$"] = "%",
        validate: bool = True,
        hdr_wrapper: textwrap.TextWrapper | None = None,
        msg_wrapper: textwrap.TextWrapper | None = None,
        *,
        defaults: Mapping[str, Any] | None = None,
    ) -> None:
        self.hdr_wrapper = hdr_wrapper
        self.msg_wrapper = msg_wrapper
        super().__init__(fmt, datefmt, style, validate, defaults=defaults)

    def format(self, record: logging.LogRecord) -> str:
        """Aligns (multiline) message indented to width of formatted header"""
        # If no wrapper was set, just format regularly
        if self.hdr_wrapper is None or self.msg_wrapper is None:
            return super().format(record)

        first, *trailing = super().format(record).splitlines()
        head = self.hdr_wrapper.fill(first)
        rest = "\n".join(self.msg_wrapper.fill(line) for line in trailing)
        return head if len(rest) == 0 else head + "\n" + rest


@dataclass
class Process:
    proc: Union[None, subprocess.CompletedProcess, subprocess.Popen]
    cmd_str: str
    teeout: bool

    stdout_override: Optional[str] = None

    @property
    def returncode(self) -> Optional[int]:
        if self.proc is None:
            return -1
        return self.proc.returncode

    @property
    def stdout(self) -> str:
        if self.proc is None:
            raise Exception("invalid process has no stdout")
        if self.stdout_override is not None:
            return self.stdout_override
        assert isinstance(self.proc.stdout, str)
        return self.proc.stdout

    @property
    def stdout_io(self) -> IO[AnyStr]:
        if self.proc is None:
            raise Exception("invalid process has no stdout")
        assert self.stdout_override is None
        assert self.proc.stdout is not None
        assert not isinstance(self.proc.stdout, (str, bytes))
        return self.proc.stdout

    def poll(self) -> Optional[int]:
        assert isinstance(self.proc, subprocess.Popen)
        return self.proc.poll()


def run(
    ctx: Context,
    cmd: Union[str, Iterable[Any]],
    allow_error: bool = False,
    silent: bool = False,
    teeout: bool = False,
    defer: bool = False,
    env: EnvDict = {},
    **kwargs: Any,
) -> Process:
    """
    Wrapper for :func:`subprocess.run` that does environment/output logging and
    provides a few useful options. The log file is ``build/log/commands.txt``.
    Where possible, use this wrapper in favor of :func:`subprocess.run` to
    facilitate easier debugging.

    Note that this requires the runlog file (:var:`ctx.runlog_file`) to be enabled
    for the running command (by calling :func:`command.enable_run_log(ctx)`); no
    output is logged otherwise.

    Note also that this function by default captures the output of the command;
    this can be disabled by passing `stdout=...` to the call. The `stderr` stream
    is redirected to `stdout`.

    It is useful to permanently have a terminal window open running ``tail -f
    build/log/commands.txt``, This way, command output is available in case of
    errors but does not clobber the setup's progress log.

    The run environment is based on :any:`os.environ`, first adding
    ``ctx.runenv`` (populated by packages/instances, see also :class:`Setup`)
    and then the ``env`` parameter. The combination of ``ctx.runenv`` and
    ``env`` is logged to the log file. Any lists of strings in environment
    values are joined with a ':' separator.

    If the command cannot be found, an error is reported to the command line
    and -- unless :param:`allow_error` is `True` -- the `FileNotFound` exception
    is propagated.

    If the command exits with a non-zero status code, the corresponding output
    is logged to the command line and the process is killed with
    ``sys.exit(-1)``, unless :param:`allow_error` is `True`.

    :param ctx: the configuration context
    :param cmd: command to run; can be a string or as a list of objects that
                support stringification, as in :func:`subprocess.run()`
    :param allow_error: avoids calling ``sys.exit(-1)`` if the command returns
                        an error
    :param silent: disables output logging (only logs the invocation and
                   environment)
    :param teeout: streams command output to ``sys.stdout`` as well as to the
                   log file
    :param defer: Do not wait for the command to finish. Similar to
                  ``./program &`` in Bash. Returns a :class:`subprocess.Popen`
                  instance.
    :param env: variables to add to the environment
    :param kwargs: passed directly to :func:`subprocess.run` (or
                   :class:`subprocess.Popen` if ``defer==True``)
    :returns: a handle to the completed or running process
    """
    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd.strip())
    else:
        cmd_list = [str(arg).strip() for arg in cmd if str(arg).strip()]

    cmd_str = qjoin(cmd_list)  # Safe to print to terminal
    log_output = False  # Set to true iff not captured & runlog file is enabled
    local_tee: _Tee | None = None  # Set to used tee
    str_buf: io.StringIO | None = None  # Buffer to redirect stdout into

    # If a file is provided for stdin, add '< <filename>' to the command string
    if "stdin" in kwargs and isinstance(kwargs["stdin"], io.FileIO):
        cmd_str = f"{cmd_str} < {shlex.quote(str(kwargs['stdin']).strip())}"
    ctx.log.info(f"Running command: '{cmd_str}'")
    ctx.log.debug(f"Working directory: '{os.getcwd()}'")

    # Running env is OS' overriden by runenv & passed env, log only runenv & passed env
    local_env = join_env_paths(ctx.runenv) | join_env_paths(env)
    run_env = os.environ | join_env_paths(local_env)
    ctx.log.debug(f"Local command environment: '{local_env}'")

    # Set "text=True" to read output as text, not binary
    kwargs.setdefault("universal_newlines", True)

    # If defer/silent are true, redirect & capture output (if not done by caller) & don't log it
    if defer or silent:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    elif "stdout" not in kwargs and ctx.runlog_file is not None:
        log_output = True

        # If logging output, ensure the running tee is configured (create it if not)
        if ctx.runtee is None:
            ctx.runtee = _Tee(ctx.runlog_file, io.StringIO())
        assert isinstance(ctx.runtee, _Tee)
        assert len(ctx.runtee.writers) == 2
        assert isinstance(ctx.runtee.writers[1], io.StringIO)

        # Store the string I/O buffer to store the tee'd stdout as a string in Process
        str_buf = ctx.runtee.writers[1]
        assert isinstance(str_buf, io.StringIO)

        # If teeing, split output to runtee & stdout, otherwise just use the runtee
        local_tee = _Tee(ctx.runtee, sys.stdout) if teeout else ctx.runtee
        kwargs["stdout"] = local_tee

        # If not overwritten by the caller, redirect stderr to the process' stdout
        kwargs.setdefault("stderr", subprocess.STDOUT)

        # Write the command, the working directory, and the local environment to runlog file
        ctx.runlog_file.write(f"{'-' * 80}\n")
        ctx.runlog_file.write(f"Command: {cmd_str}\n")
        ctx.runlog_file.write(f"Working directory: {os.getcwd()}\n")
        ctx.runlog_file.write("Local environment:\n")
        ctx.runlog_file.write("\n".join([f"\t{key}={val}" for key, val in local_env.items()]))

    if local_tee is not None:
        local_tee.flush_all()
    for handler in ctx.log.handlers:
        handler.flush()

    # If deferring, return immediately; check if command exists by catching FileNotFoundError
    try:
        if defer:
            return Process(subprocess.Popen(cmd_list, env=run_env, **kwargs), cmd_str=cmd_str, teeout=False)
        proc = Process(subprocess.run(cmd_list, env=run_env, **kwargs), cmd_str=cmd_str, teeout=teeout)
    except FileNotFoundError:
        logger = ctx.log.error if allow_error else ctx.log.critical
        logger(f"Command not found: {cmd_str}")
        logger(f"Working directory: {os.getcwd()}")
        logger(f"Failure environment:")
        logger("\n".join([f"\t{key}={val}" for key, val in local_env.items()]))

        if local_tee is not None:
            local_tee.flush_all()
        for handler in ctx.log.handlers:
            handler.flush()

        if allow_error:
            return Process(None, cmd_str=cmd_str, teeout=teeout)
        raise

    if local_tee is not None:
        local_tee.flush_all()
    for handler in ctx.log.handlers:
        handler.flush()

    if log_output:
        assert ctx.runlog_file is not None
        assert ctx.runtee is not None
        assert local_tee is not None
        assert isinstance(str_buf, io.StringIO)
        assert isinstance(ctx.runtee, _Tee)
        assert isinstance(local_tee, _Tee)

        # Store string buffer's contents & clear dangling buffer from ctx.runtee
        proc.stdout_override = str_buf.getvalue()
        ctx.runtee.writers[1] = io.StringIO()

        # Write newline to logfile for readability & flush all buffers
        ctx.runlog_file.write("\n")
        local_tee.flush_all()

    if proc.returncode != 0 and not allow_error:
        ctx.log.critical(f"Command return code: {proc.returncode}")
        ctx.log.critical(f"Executed command: {cmd_str}")
        ctx.log.critical(f"Working directory: {os.getcwd()}")
        ctx.log.critical(f"Failure environment:")
        ctx.log.critical("\n".join([f"\t{key}={val}" for key, val in local_env.items()]))

        assert proc.proc is not None
        if proc.proc.stdout is not None:
            if isinstance(proc.proc.stdout, bytes):
                output = proc.proc.stdout.decode()
            else:
                output = proc.proc.stdout
            assert isinstance(output, str)
            sys.stdout.write(output)
            sys.stdout.write("\n")
            sys.stdout.flush()
            sys.exit(proc.returncode)

    return proc


def qjoin(args: Iterable[Any]) -> str:
    """
    Join the command-line arguments to a single string to make it safe to pass to paste in a shell.
    Basically, this adds quotes to each element containing spaces (using :func:`shlex.quote`).
    Arguments are additionally stringified (using :class:`str`) before joining them together.

    :param args: arguments to join
    """
    return " ".join(shlex.quote(str(arg).strip()) for arg in args if str(arg).strip())


def download(ctx: Context, url: str, outfile: Optional[str] = None) -> str:
    """
    Download a file (logs to the debug log).

    :param ctx: the configuration context
    :param url: URL to the file to download
    :param outfile: optional path/filename to download to
    :returns: the name of the downloaded file
    """
    if outfile:
        ctx.log.debug(f"downloading {url} to {outfile}")
    else:
        outfile = os.path.basename(urlparse(url).path)
        ctx.log.debug(f"downloading {url}")

    if os.path.exists(outfile):
        ctx.log.warning(f"overwriting existing outfile: {outfile}")

    urlretrieve(url, outfile)
    return outfile


class _Tee(io.IOBase):
    """
    Extension of io.IOBase to split output over multiple given writers (other IOBases,
    IO objects, or other _Tee objects). An asynchronous thread is started to read
    input from the given I/O objects without blocking the main thread. If all output
    should be flushed, :func:`_Tee.flush_all()` can be used.
    """

    ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")  # 7-bit C1 ANSI sequences

    def __init__(self, *writers: Union[io.IOBase, typing.IO]):
        super().__init__()
        assert len(writers) > 0
        self.writers = list(writers)
        self.readfd, self.writefd = os.pipe()
        self.running = False
        self.do_sync = False
        self.synced = threading.Event()
        self.contin = threading.Event()
        self.thread = threading.Thread(target=self._flusher)
        self.thread.daemon = True
        self.thread.start()

    def _flusher(self) -> None:
        self.running = True
        poller = select.poll()
        poller.register(self.readfd, select.POLLIN | select.POLLPRI)
        buf = b""

        while self.running:
            # Check for output; periodically check for do_sync to allow for deep flushing
            for fd, flag in poller.poll(100):
                assert fd == self.readfd
                if flag & (select.POLLIN | select.POLLPRI):
                    buf += os.read(self.readfd, io.DEFAULT_BUFFER_SIZE)
                    nl = buf.find(b"\n") + 1
                    while nl > 0:
                        self.write(buf[:nl].decode(errors="replace"))
                        self.flush()
                        buf = buf[nl:]
                        nl = buf.find(b"\n") + 1
            if self.do_sync:
                if buf:
                    self.write(buf.decode(errors="replace"))
                    self.flush()
                    buf = b""  # Clear old data
                self.synced.set()
                self.contin.wait()

        # Write any remaining data if there is any
        if buf:
            self.write(buf.decode(errors="replace"))
            self.flush()
            buf = b""  # Clear old data

    def flush(self) -> None:
        for writer in self.writers:
            writer.flush()

    def flush_all(self) -> None:
        self.do_sync = True
        self.synced.wait()
        for writer in self.writers:
            if isinstance(writer, _Tee):
                writer.flush_all()
        self.do_sync = False
        self.contin.set()

    def write(self, data: str) -> int:
        total = 0
        for writer in self.writers:
            total += writer.write(data if writer.isatty() else self.ansi_escape.sub("", data))
            writer.flush()
        return total

    emit = write

    def fileno(self) -> int:
        return self.writefd

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        if self.running:
            self.do_sync = True
            self.synced.wait()
            for writer in self.writers:
                if isinstance(writer, _Tee):
                    writer.close()
            self.running = False
            self.contin.set()
            self.thread.join()
            os.close(self.readfd)
            os.close(self.writefd)


def require_program(ctx: Context, name: str, error: Optional[str] = None) -> None:
    """
    Require a program to be available in ``PATH`` or ``ctx.runenv.PATH``.

    :param ctx: the configuration context
    :param name: name of required program
    :param error: optional error message
    :raises FatalError: if program is not found
    """
    if "PATH" in ctx.runenv:
        path = ":".join(ctx.runenv["PATH"])
    else:
        path = os.getenv("PATH", "")

    if shutil.which(name, path=path) is None:
        msg = f"'{name}' not found in PATH"
        if error:
            msg += ": " + error
        raise FatalError(msg)


def untar(
    ctx: Context,
    tarname: str,
    dest: str | None = None,
    *,
    remove: bool = True,
    basename: str | None = None,
) -> None:
    """
    Extract a given archive using `tar -xf`. Optionally deletes the archive
    after extracting and renames the extracted directory.

    :param ctx: the configuration context
    :param tarname: name/path of the archive to extract
    :param dest: directory holding extracted archive contents, defaults to None
    :param remove: remove the archive after extracting, defaults to True
    :param basename: name of output directory, defaults to archive name without .tar.*
    """
    require_program(ctx, "tar", "required to unpack source tarfile")

    if basename is None:
        basename = re.sub(r"\.tar(\.\w+)?", "", tarname)

    ctx.log.debug(f"Extracting {tarname} (output directory basename: {basename})")
    run(ctx, ["tar", "-xf", tarname])

    if dest:
        ctx.log.debug(f"Moving output directory {basename} to {dest}")
        shutil.move(basename, dest)
    if remove:
        ctx.log.debug(f"Deleting original archive {tarname}")
        os.remove(tarname)
