import io
import os
import re
import sys
import errno
import shlex
import locale
import shutil
import logging
import threading
import subprocess

from pathlib import Path
from datetime import datetime
import time

from .context import LOG_LEVEL_ABBREVIATIONS, Context

from urllib.parse import urlparse
from urllib.request import urlretrieve
from collections import OrderedDict
from typing import (
    IO,
    TextIO,
    BinaryIO,
    Any,
    Callable,
    Iterable,
    Iterator,
    KeysView,
    ItemsView,
    TypeAlias,
    ValuesView,
    MutableMapping,
)

ANSI_ESCAPE_RAW = re.compile(rb"(\x9B|\x1B\[|\033\[)[0-?]*[ -\/]*[@-~]")
ANSI_ESCAPE_STR = re.compile(r"(\x9B|\x1B\[|\033\[)[0-?]*[ -\/]*[@-~]")
PREF_ENCODING = locale.getpreferredencoding(False)


ResultVal: TypeAlias = bool | int | float | str
ResultDict: TypeAlias = MutableMapping[str, ResultVal]
ResultsByInstance: TypeAlias = MutableMapping[str, list[ResultDict]]
EnvDict: TypeAlias = dict[str, str | list[str]] | dict[str, str] | dict[str, list[str]]


class Index[T](MutableMapping[str, T]):
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

    def all(self) -> list[T]:
        return list(self.mem.values())

    def select(self, keys: Iterable[str]) -> list[T]:
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


class WriterClosedError(Exception):
    """
    Raised when a writer stored in a :type:`_Tee` class has closed and ought to be removed
    """

    pass


class _Tee(io.IOBase):
    """
    Mimics the behaviour of the standard :cmd:`tee` command; takes any number of I/O streams (e.g.
    from :func:`open()`) and writes any data written to this :type:`_Tee` to those streams.
    """

    poll_interval: float = 0.01

    def __init__(self, *writers: io.IOBase | IO) -> None:
        super().__init__()

        # Store the writers locally; create a buffer to store written data; and track opened-status
        self.__writers = list(writers)
        self.__buffer = io.BytesIO()
        self.__closed = False

        # Create a pair of pipes to read/write data from/into this tee; set them to use blocking I/O
        while True:
            try:
                self.__r_fd, self.__w_fd = os.pipe()
                os.set_blocking(self.__r_fd, True)
                os.set_blocking(self.__w_fd, True)
                break
            except OSError:
                time.sleep(self.poll_interval)
                continue

        # Track the opened status of the read & write pipe ends
        self.__r_fd_closed = False
        self.__w_fd_closed = False

        # Create a daemon thread to continuously flush incoming data to all of the stored writer objects
        self.__thread = threading.Thread(target=self._flusher_loop, daemon=True)
        self.__thread.start()

    def __enter__(self) -> "_Tee":
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.close()
        self.close_buffer()

    def __del__(self) -> None:
        self.close()
        self.close_buffer()

    @property
    def closed(self) -> bool:
        return self.__closed

    @property
    def read_closed(self) -> bool:
        return self.__r_fd_closed

    @property
    def write_closed(self) -> bool:
        return self.__w_fd_closed

    @property
    def buffer_closed(self) -> bool:
        return self.__buffer.closed

    def close_buffer(self) -> None:
        if not self.__buffer.closed:
            self.__buffer.flush()
            self.__buffer.close()

    def close_read_fd(self) -> None:
        if not self.__r_fd_closed:
            os.close(self.__r_fd)
            self.__r_fd_closed = True

    def close_write_fd(self) -> None:
        if not self.__w_fd_closed:
            os.close(self.__w_fd)
            self.__w_fd_closed = True

    def join(self) -> None:
        self.close_write_fd()
        self.__thread.join()
        self.close_read_fd()

    def close(self) -> None:
        if self.closed:
            return
        self.join()
        self.__closed = True

    def _flusher_loop(self) -> None:
        try:
            # Read data that was written into this tee's pipe
            while data := os.read(self.__r_fd, io.DEFAULT_BUFFER_SIZE):
                # First store the incoming data in this tee's byte data buffer
                self.__buffer.seek(0, io.SEEK_END)
                self.__buffer.write(data)

                # Write data to stored writers (stripping/decoding data if applicable); remove closed writers
                for writer in self.__writers[:]:
                    if writer.closed:
                        self.__writers.remove(writer)
                        continue

                    # Write to the writer; strip/decode data first if necesasry for the writer type
                    try:
                        match writer:
                            case io.RawIOBase() | BinaryIO():
                                if writer.isatty():
                                    writer.write(data)
                                else:
                                    writer.write(ANSI_ESCAPE_RAW.sub(b"", data))

                            case io.TextIOBase() | TextIO():
                                if writer.isatty():
                                    writer.write(data.decode(PREF_ENCODING, "replace"))
                                else:
                                    writer.write(ANSI_ESCAPE_RAW.sub(b"", data).decode(PREF_ENCODING, "replace"))

                            case _:
                                raise TypeError(f"Unsupported type of writer; got: {type(writer)} ({writer})")
                    except:
                        self.__writers.remove(writer)

                # Flush all remaining writers (doesn't flush removed writers)
                self.flush()

                # Wait a small amount of time until re-trying
                time.sleep(self.poll_interval)
        finally:
            # Flush remaining data & close read end of the pipe
            self.flush()
            self.close_read_fd()

    def flush(self) -> None:
        for writer in self.__writers[:]:
            if writer.closed:
                self.__writers.remove(writer)
                continue

            try:
                writer.flush()
            except:
                self.__writers.remove(writer)

    def write(self, _raw: str | bytes) -> int:
        # Encode the data to bytes if it was given as string data
        data = _raw if not isinstance(_raw, str) else _raw.encode(PREF_ENCODING, "replace")

        written = 0
        while written < len(data):
            try:
                written += os.write(self.__w_fd, data[written:])
            except OSError as err:
                if err.errno in (errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise err
        return written

    def fileno(self) -> int:
        return self.__w_fd

    def read_fileno(self) -> int:
        return self.__r_fd

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return True

    def getbuffer(self) -> memoryview:
        return self.__buffer.getbuffer()

    def getvalue(self) -> bytes:
        return self.__buffer.getvalue()

    def getstr(self) -> str:
        return self.__buffer.getvalue().decode(PREF_ENCODING, "replace")

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self.__buffer.seek(offset, whence)

    def tell(self) -> int:
        return self.__buffer.tell()

    def read(self, size: int = -1) -> bytes:
        return self.__buffer.read(size)

    def readline(self, size: int | None = -1) -> bytes:
        return self.__buffer.readline(size)

    def readlines(self, hint: int = -1) -> list[bytes]:
        return self.__buffer.readlines(hint)

    def truncate(self, size: int | None = None) -> int:
        return self.__buffer.truncate(size)


class Process:
    def __init__(
        self,
        *,
        proc: subprocess.Popen | None,
        input: str | bytes | None,
        cmd_str: str | None,
        stdout_tee: _Tee | None,
        stderr_tee: _Tee | None,
    ) -> None:
        self.__proc = proc
        self.__input = input
        self.__cmd_str = cmd_str
        self.__outs_tee = stdout_tee
        self.__errs_tee = stderr_tee

    def __del__(self) -> None:
        self.close()
        self.close_buffers()

    def __enter__(self) -> "Process":
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.close()
        self.close_buffers()

    def close(self) -> None:
        if self.__proc is not None:
            try:
                try:
                    if self.__proc.stdin is not None and not self.__proc.stdin.closed:
                        self.__proc.stdin.close()
                except:
                    pass
                try:
                    if self.__proc.stdout is not None and not self.__proc.stdout.closed:
                        self.__proc.stdout.close()
                except:
                    pass
                try:
                    if self.__proc.stderr is not None and not self.__proc.stderr.closed:
                        self.__proc.stderr.close()
                except:
                    pass
                try:
                    if self.__proc.poll() is None:
                        self.__proc.wait()
                except:
                    pass
            except:
                pass
            finally:
                self.__proc = None

        if self.__outs_tee is not None and not self.__outs_tee.closed:
            self.__outs_tee.close()
        if self.__errs_tee is not None and not self.__errs_tee.closed:
            self.__errs_tee.close()

    def close_stdout(self) -> None:
        if self.__outs_tee is not None and not self.__outs_tee.buffer_closed:
            self.__outs_tee.close_buffer()

    def close_stderr(self) -> None:
        if self.__errs_tee is not None and not self.__errs_tee.buffer_closed:
            self.__errs_tee.close_buffer()

    def close_buffers(self) -> None:
        self.close_stdout()
        self.close_stderr()

    @property
    def stdout_buff_closed(self) -> bool:
        return self.__outs_tee is None or self.__outs_tee.buffer_closed

    @property
    def stderr_buff_closed(self) -> bool:
        return self.__errs_tee is None or self.__errs_tee.buffer_closed

    @property
    def proc(self) -> subprocess.Popen | None:
        return self.__proc

    @property
    def args(self):
        assert self.proc is not None
        return self.proc.args

    @property
    def cmd_str(self) -> str:
        return self.__cmd_str if self.__cmd_str is not None else ""

    @property
    def input(self) -> str | bytes:
        return self.__input if self.__input is not None else ""

    @property
    def stdin(self) -> IO | None:
        assert self.proc is not None
        return self.proc.stdin

    @property
    def stdout(self) -> str:
        return self.__outs_tee.getstr() if self.__outs_tee is not None else ""

    @property
    def stdout_io(self) -> _Tee | None:
        return self.__outs_tee

    @property
    def stdout_raw(self) -> bytes:
        return self.__outs_tee.getvalue() if self.__outs_tee is not None else b""

    @property
    def stdout_buff(self) -> memoryview | None:
        return self.__outs_tee.getbuffer() if self.__outs_tee is not None else None

    @property
    def stderr(self) -> str:
        return self.__errs_tee.getstr() if self.__errs_tee is not None else ""

    @property
    def stderr_io(self) -> _Tee | None:
        return self.__errs_tee

    @property
    def stderr_raw(self) -> bytes:
        return self.__errs_tee.getvalue() if self.__errs_tee is not None else b""

    @property
    def stderr_buff(self) -> memoryview | None:
        return self.__errs_tee.getbuffer() if self.__errs_tee is not None else None

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    @property
    def returncode(self) -> int | None:
        assert self.proc is not None
        return self.proc.returncode

    def flush(self) -> None:
        if self.__outs_tee is not None:
            self.__outs_tee.flush()
        if self.__errs_tee is not None:
            self.__errs_tee.flush()

    def communicate(self, input: Any | None = None, timeout: float | None = None) -> tuple[Any, Any]:
        assert self.proc is not None
        return self.proc.communicate()

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.kill()

    def poll(self) -> int | None:
        assert self.proc is not None
        return self.proc.poll()

    def wait(self, timeout: float | None = None):
        assert self.proc is not None
        return self.proc.wait(timeout)

    def send_signal(self, sig: int) -> None:
        assert self.proc is not None
        self.proc.send_signal(sig)

    def terminate(self) -> None:
        assert self.proc is not None
        self.proc.terminate()


def run(
    ctx: Context,
    cmd: Any,
    allow_error: bool = False,
    silent: bool = False,
    teeout: bool = False,
    defer: bool = False,
    input: str | bytes | None = None,
    env: EnvDict | None = None,
    merge_outputs: bool = False,
    with_env_flags: bool = False,
    writers: Iterable[io.IOBase | IO] | None = None,
    **kwargs: Any,
) -> Process:
    """
    Runs the given command with :func:`subprocess.Popen()` and performs additional logging. Returns a
    :type:`Process` object, which always captures `stdout` and `stderr`. Note that `stdin` is always
    piped; the :param:`input` allows for passing input directly after creation, otherwise it's
    possible to communicate with the returned :type:`subprocess.Popen` object.

    :param Context ctx: the configuration context
    :param Iterable[Any] | str cmd: the command to pass to :func:`subprocess.Popen()`
    :param bool allow_error: whether to throw a fatal error on errors, defaults to False
    :param bool silent: supresses the output of the command (also from the runlog file), defaults to False
    :param bool teeout: tee's the output to both the command line and the runlog file, defaults to False
    :param bool defer: iff true, won't wait for command completion before returning, defaults to False
    :param str | None input: any optional input; passed with :func:`Popen.communicate(stdin=...)`
    :param bool merge_outputs: whether to merge stderr into stdout, defaults to False
    :param bool with_env_flags: whether to include flags from `ctx.[c|cxx|ld]FLAGS`, defaults to False
    :param dict[str, str  |  list[str]] | None env: an override environment over the context env, defaults to None
    :param Iterable[io.IOBase] | IO | None writers: allows for specifying additional writers to tee the output to
    :return Process: the resulting :type:`Process` object; captures `stdout` and `stderr` and other information
    """
    # Get a safe-to-print version of the input command
    cmd = cmd if isinstance(cmd, str) else [str(part) for part in cmd if part]
    cmd_arr = shlex.split(cmd) if isinstance(cmd, str) else cmd
    cmd_str = shlex.join(cmd_arr)
    ctx.log.debug(f'Running command: "{cmd_str}"')

    # Determine if the input has text mode enabled or if it's in the default binary mode
    text_mode = kwargs.get("text", False) or kwargs.get("universal_newlines", False) or kwargs.get("encoding") is not None

    # Merge the context's environment into the OS (prioritising context)
    run_env: dict[str, str] = {key: val for key, val in os.environ.items()}
    ctx_env: EnvDict = ctx.getEnvironment(include_flags=with_env_flags)
    loc_env: EnvDict = (ctx_env | env) if env is not None else (ctx_env)
    for key, val in loc_env.items():
        if env is not None and (override := env.get(key, None)) is not None:
            run_env[key] = override if isinstance(override, str) else os.pathsep.join(override)
        else:
            run_env[key] = val if isinstance(val, str) else os.pathsep.join(val + os.environ.get(key, "").split(os.pathsep))

    # If the runlog file is enabled, log the command and its environment
    if ctx.runlog_file is not None:
        ctx.runlog_file.write(f"{'=' * 100}\n")
        ctx.runlog_file.write(f"Running command:            '{cmd_str}'\n")
        ctx.runlog_file.write(f"Start time of command:      {datetime.now().strftime('%Y/%m/%d %H:%M:%S.%f')}\n")
        ctx.runlog_file.write(f"Current working directory:  '{os.getcwd()}'\n")
        ctx.runlog_file.write(f"Using command environment:\n")
        for idx, (key, val) in enumerate(run_env.items()):
            ctx.runlog_file.write(f"  env[{idx:03d}]: {key}={val}\n")
        ctx.runlog_file.write("Output (if any):\n\n")
        ctx.runlog_file.flush()

    # Get all writers for the output tee's (e.g. runlog file, stderr, etc)
    tee_writers: list[io.IOBase | IO] = list(writers) if writers is not None else []
    tee_writers += [sys.stderr] if teeout else []
    tee_writers += [ctx.runlog_file] if ctx.runlog_file is not None and not silent else []

    # Merging outputs is enable if set or stderr is redirected to stdout
    merge_outputs = merge_outputs or (kwargs.get("stderr", None) == subprocess.STDOUT)

    # Get a Tee for stdout & optionally one for stderr (None if merged)
    stdout_tee = _Tee(*tee_writers)
    stderr_tee = _Tee(*tee_writers) if not merge_outputs else None
    kwargs["stdout"] = stdout_tee
    kwargs["stderr"] = stderr_tee if stderr_tee is not None else subprocess.STDOUT

    # Always pipe stdin to be safe
    kwargs["stdin"] = subprocess.PIPE

    try:
        _proc = subprocess.Popen(cmd, env=run_env, **kwargs)
    except Exception as err:
        # Clean up the tees
        stdout_tee.close()
        if stderr_tee is not None:
            stderr_tee.close()

        # If not allowing errors, re-raise the exception
        if not allow_error:
            ctx.log.fatal(f"Execution of command failed ({err}): '{cmd_str}'")
            raise err

        # Otherwise log a warning and return a None-process object
        ctx.log.warning(f"Execution failed but allowing errors of: '{cmd_str}'")
        return Process(proc=None, input=input, cmd_str=cmd_str, stdout_tee=None, stderr_tee=None)

    # If any input was given, communicate it to the process (warning: could block)
    if input is not None:
        if isinstance(input, str):
            _proc.communicate(input if text_mode else input.encode(PREF_ENCODING, "replace"))  # type: ignore
        else:
            _proc.communicate(input.decode(PREF_ENCODING, "replace") if text_mode else input)  # type: ignore

    # If not deferring the command, wait for completion & report result and/or errors
    if not defer:
        ret_code = _proc.wait(timeout=None)
        if stdout_tee is not None:
            stdout_tee.close_write_fd()
            stdout_tee.flush()
        if stderr_tee is not None:
            stderr_tee.close_write_fd()
            stderr_tee.flush()
        if ret_code != 0 and not allow_error:
            ctx.log.fatal(
                "[FAIL] Command execution failed\n"
                + f"  Status:       {ret_code}\n"
                + f"  Command:     '{cmd_str}'\n"
                + f"  Cmd stdout:   {f'\n{outs}' if stdout_tee is not None and (outs := stdout_tee.getstr()) else "<EMPTY>"}\n"
                + f"  Cmd stderr:   {f'\n{errs}' if stderr_tee is not None and (errs := stderr_tee.getstr()) else "<EMPTY>"}\n"
            )
            raise FatalError("Command execution failed")

    # Return the process (not necessarily completed) wrapped in a Process object
    return Process(proc=_proc, input=input, cmd_str=cmd_str, stdout_tee=stdout_tee, stderr_tee=stderr_tee)


def apply_patch(ctx: Context, patch_path: Path | str, strip_count: int) -> bool:
    """
    Applies a patch in the current directory by calling ``patch -p<strip_count> < <path>``.

    Afterwards, a stamp file called ``.patched-<basename>`` is created to indicate that the patch has
    been applied. If the stamp file is already present, the patch is not applied at all, unless the
    patch file from :param:`path` was modified after the creation date of the stamp file.
    ``<basename`` is the final path component of the patch file with the .patch suffix removed:
    ``path/to/my-patch.patch`` becomes `my-patch`.

    :param ctx: the configuration context
    :param path: path to the patch file
    :param strip_count: number of leading elements to strip from patch paths
    :returns: ``True`` if the patch was applied, ``False`` if it was already applied before
    """
    if isinstance(patch_path, str):
        patch_path = Path(patch_path)
    if not patch_path.exists():
        raise FileNotFoundError(f"Cannot apply patch; patch file not found: {patch_path}")

    # Stamp file is the final name component of the patch without the suffix
    stamp_path = Path(f".patched-{patch_path.stem}")

    # Check if the stamp exists
    if stamp_path.exists():
        # Only exit now if the patch was applied after the patch file was modified last
        patch_date = datetime.fromtimestamp(patch_path.stat().st_mtime)
        stamp_date = datetime.fromtimestamp(stamp_path.stat().st_mtime)
        if stamp_date > patch_date:
            ctx.log.info(f"Not applying patch; already applied {patch_path.stem}")
            ctx.log.debug(f"Applied patch on {stamp_date}; patch last modified on {patch_date}")
            return False

    ctx.log.debug(f"Applying patch {patch_path.stem}")
    require_program(ctx, "patch", "Required to apply source patches")

    with open(patch_path) as f:
        run(ctx, f"patch -N -p{strip_count}", stdin=f, allow_error=True)
    open(stamp_path, "w").close()

    return True


def join_env_paths(env: EnvDict) -> dict[str, str]:
    """
    Convert an environment dictionary to a dictionary mapping variable names to their values, all as
    strings. Lists in the given dictionary are converted to ":"-delimited lists (e.g. like $PATH).

    Note: the given dictionary should contain only str or list[str], but for both this function will
    also attempt to convert them to string if possible.

    :param env: the environment dicitonary to convert (should contain str or list[str])
    :return dict[str, str]: a str-to-str mapping that can be used to pass to e.g. subprocess.run()
    """
    return {k: os.pathsep.join(str(x) for x in v) if isinstance(v, list) else v for k, v in env.items()}


def get_stream_formatter() -> logging.Formatter:
    try:
        from textwrap import TextWrapper

        wrapper = TextWrapper(
            width=shutil.get_terminal_size(fallback=(80, 24))[0],
            initial_indent=f"  ",
            subsequent_indent=f"    → ",
            tabsize=4,
        )

    except ImportError:
        wrapper = None

    try:
        import colorlog

        class ColourWrapper(colorlog.ColoredFormatter):
            def __init__(self) -> None:
                super().__init__(
                    fmt=(
                        "[%(log_color)s%(levelname)s%(reset)s] "
                        "|%(bold_white)s%(module)s%(reset)s| "
                        "%(purple)s%(funcName)s%(reset)s::"
                        "%(blue)s%(filename)s%(reset)s"
                        "(%(yellow)s%(lineno)d%(reset)s) "
                        "[%(green)s%(asctime)s.%(msecs)03d%(reset)s]\n"
                        "%(message_log_color)s%(message)s%(reset)s"
                    ),
                    datefmt="%H:%M:%S",
                    log_colors={
                        "NST": "bold_white",
                        "DBG": "bold_cyan",
                        "INF": "bold_green",
                        "WRN": "bold_yellow",
                        "ERR": "bold_red",
                        "FTL": "bold_white,bg_bold_red",
                        "CRT": "bold_white,bg_bold_red",
                    },
                    secondary_log_colors={
                        "message": {
                            "NST": "thin_white",
                            "DBG": "thin_white",
                            "INF": "thin_white",
                            "WRN": "thin_white",
                            "ERR": "thin_white",
                            "FTL": "thin_white",
                            "CRT": "thin_white",
                        }
                    },
                )

            def format(self, record: logging.LogRecord) -> str:
                record.levelname = LOG_LEVEL_ABBREVIATIONS.get(record.levelname, "???")
                if wrapper is None:
                    return super().format(record)
                header, *message = super().format(record).splitlines()
                formatted_message = "\n".join(wrapper.fill(line.rstrip()) for line in message)
                return f"{header}\n{formatted_message}"

        return ColourWrapper()
    except ImportError:

        class Wrapper(logging.Formatter):
            def __init__(self) -> None:
                super().__init__(
                    fmt="",
                    datefmt="",
                )

            def format(self, record: logging.LogRecord) -> str:
                return super().format(record)

        return Wrapper()


def get_file_formatter() -> logging.Formatter:
    """Creates and returns formatter that strips ANSI escape sequences from messages"""

    class StrippingFormatter(logging.Formatter):
        """Formatter that strips ANSI escape sequences from the message"""

        def __init__(self) -> None:
            super().__init__(
                fmt="%(asctime)s.%(msecs)03d [%(funcName)s(%(module)s::%(lineno)d)] |%(levelname)s| %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        def format(self, record: logging.LogRecord) -> str:
            if isinstance(record.msg, str):
                record.msg = strip_ansi(record.msg)
            return super().format(record)

    return StrippingFormatter()


def qjoin(args: Iterable[Any]) -> str:
    """
    Join the command-line arguments to a single string to make it safe to pass to paste in a shell.
    Basically, this adds quotes to each element containing spaces (using :func:`shlex.quote`).
    Arguments are additionally stringified (using :class:`str`) before joining them together.

    :param args: arguments to join
    """
    return " ".join(shlex.quote(str(arg).strip()) for arg in args if str(arg).strip())


def download(ctx: Context, url: str, outfile: str | None = None) -> str:
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


def require_program(ctx: Context, name: str, error: str | None = None) -> None:
    """
    Require a program to be available in ``PATH`` or ``ctx.runenv.PATH``.

    :param ctx: the configuration context
    :param name: name of required program
    :param error: optional error message
    :raises FatalError: if program is not found
    """
    runenv_path = _path if isinstance(_path := ctx.runenv.get("PATH", []), list) else _path.split(os.pathsep)
    global_path = os.getenv("PATH", "").split(os.pathsep)
    path = os.pathsep.join(runenv_path + global_path)

    if shutil.which(name, path=path) is None:
        raise FatalError(f"'{name}' not found in PATH ({error if error else ''}): {path}")


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


def strip_ansi(data: str | bytes) -> str | bytes:
    """Strips ANSI escape sequences from the given byte-string or string object"""
    match data:
        case str():
            return ANSI_ESCAPE_STR.sub("", data)
        case bytes():
            return ANSI_ESCAPE_RAW.sub(b"", data)
        case _:
            raise TypeError(f"Cannot strip ANSI sequences from type: {type(data)}")
