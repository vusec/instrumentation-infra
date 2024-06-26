import io
import os
import re
import sys
import shlex
import shutil
import select
import logging
import textwrap
import threading
import subprocess

from datetime import datetime
from pathlib import Path

from .context import Context

from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import urlretrieve
from collections import OrderedDict
from typing import (
    IO,
    Any,
    AnyStr,
    Literal,
    Mapping,
    TypeVar,
    Callable,
    Iterable,
    Iterator,
    KeysView,
    ItemsView,
    TypeAlias,
    ValuesView,
    MutableMapping,
)

EnvDict: TypeAlias = Mapping[str, str | list[str]]
ResultVal: TypeAlias = bool | int | float | str
ResultDict: TypeAlias = MutableMapping[str, ResultVal]
ResultsByInstance: TypeAlias = MutableMapping[str, list[ResultDict]]
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

    :param EnvDict env: the environment dicitonary to convert (should contain str or list[str])
    :return dict[str, str]: a str-to-str mapping that can be used to pass to e.g. subprocess.run()
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
    proc: subprocess.CompletedProcess | subprocess.Popen | None
    cmd_str: str
    teeout: bool

    stdout_override: str | None = None
    stderr_override: str | None = None

    @property
    def returncode(self) -> int:
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no return code!")
        return self.proc.returncode if self.proc is not None else -1

    @property
    def stdout(self) -> str:
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stdout!")
        if self.stdout_override is not None:
            return self.stdout_override
        if isinstance(self.proc.stdout, str):
            return self.proc.stdout
        if isinstance(self.proc.stdout, bytes):
            return self.proc.stdout.decode(encoding="ascii", errors="replace")
        raise ValueError(f"Only bytes/str values for proc.stdout are supported, got {type(self.proc.stdout)}")

    @property
    def stderr(self) -> str:
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stderr!")
        if self.stderr_override is not None:
            return self.stderr_override
        if isinstance(self.proc.stderr, str):
            return self.proc.stderr
        if isinstance(self.proc.stderr, bytes):
            return self.proc.stderr.decode(encoding="ascii", errors="replace")
        raise ValueError(f"Only bytes/str values for proc.stderr are supported, got {type(self.proc.stderr)}")

    @property
    def stdout_io(self) -> IO[AnyStr]:
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stdout!")
        assert self.stdout_override is None
        assert self.proc.stdout is not None
        assert not isinstance(self.proc.stdout, (str, bytes))
        return self.proc.stdout

    @property
    def stderr_io(self) -> IO[AnyStr]:
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stderr!")
        assert self.stderr_override is None
        assert self.proc.stderr is not None
        assert not isinstance(self.proc.stderr, (str, bytes))
        return self.proc.stderr

    def poll(self) -> int | None:
        if self.proc is None:
            raise ProcessLookupError("Cannot poll invalid (None) process!")
        if isinstance(self.proc, subprocess.CompletedProcess):
            return self.returncode
        return self.proc.poll()

    def wait(self) -> int:
        if self.proc is None:
            raise ProcessLookupError("Cannot wait on invalid (None) process!")
        if isinstance(self.proc, subprocess.CompletedProcess):
            return self.proc.returncode
        return self.proc.wait()


def get_cmd_list(raw_cmd: Iterable[Any] | str) -> list[str] | None:
    """Converts the given raw command string/iterable to a list of strings to pass to something
    like :func:`subprocess.run`. The given object :param:`raw_cmd` can be a string, in which case
    :func:`shlex.split()` is used to split it into components. If the given object :param:`raw_cmd`
    is an iterable, each element is stringified (by calling :func:`str()` on the element) and
    stripped. If any object from the iterable does not support conversion to string, this function
    returns None. Empty elements (including after stripping) are discarded.

    :param str | Iterable[Any] raw_cmd: the raw command (usually passed to :func:`run()`)
    :return list[str] | None: the command split into parts as it would on the command line
    """
    if isinstance(raw_cmd, str):
        return shlex.split(raw_cmd.strip())
    try:
        return [str(arg).strip() for arg in raw_cmd if str(arg).strip()]
    except ValueError:
        return None


def get_safe_cmd_str(cmd_list: Iterable[str], stdin: Any | None = None) -> str:
    """Converts the given command list (e.g. output of :func:`get_cmd_list()`) to a safe-to-print
    string. Uses :func:`qjoin()` (which uses :func:`shlex.quote()`) to convert each element from
    the iterable to a safely quoted element. The concatenated string is returned.

    If :param:`stdin` is given (and is :type:`io.FileIO`), "< [IN_FILE]" is appended to the string

    :param Iterable[str] cmd_list: command to convert to string; output of :func:`get_cmd_list()`
    :param io.FileIO | None stdin: _description_, defaults to None optional input file
    :return str: a safe to print string
    """
    if not isinstance(stdin, io.FileIO):
        return qjoin(cmd_list)
    return f"{qjoin(cmd_list)} < {shlex.quote(str(stdin))}"


def run(
    ctx: Context,
    cmd: Iterable[Any] | str,
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
    cmd_list = get_cmd_list(raw_cmd=cmd)
    assert cmd_list is not None

    cmd_str = get_safe_cmd_str(cmd_list, kwargs.get("stdin", None))
    ctx.log.info(f"Running command: {cmd_str}")
    ctx.log.debug(f"Running in directory: {os.getcwd()}")

    # Local env is given env merged with CTX's running env, final env is merged with OS' env
    loc_env = join_env_paths(ctx.runenv) | join_env_paths(env)
    run_env = os.environ | loc_env

    # Set "universal_newlines=True" to read output as text, not binary
    kwargs.setdefault("universal_newlines", True)

    # If the runlog file is not None, log the command & environment to be executed
    if ctx.runlog_file is not None:
        assert isinstance(ctx.runlog_file, io.TextIOWrapper)
        ctx.runlog_file.write(f"{'-' * 100}\n")
        ctx.runlog_file.write(f"Running command:   '{cmd_str}'\n")
        ctx.runlog_file.write(f"Unquoted command:  '{' '.join(cmd_list)}'\n")
        ctx.runlog_file.write(f"Working directory: '{os.getcwd()}'\n")
        ctx.runlog_file.write("Local environment: ")
        ctx.runlog_file.write("{\n" if len(run_env) > 0 else "{")
        ctx.runlog_file.write("\n".join([f"\t{key}={val}" for key, val in sorted(run_env.items(), key=lambda item: item[0])]))
        ctx.runlog_file.write("\n}" if len(run_env) > 0 else "}")
        if defer or silent or "stdout" in kwargs or "stderr" in kwargs:
            ctx.runlog_file.write("\n\nOutput redirected; not captured in runlog file\n\n")
        ctx.runlog_file.flush()

    # Create tee to split output to runlog file & string buffer; also to stdout if teeout is true
    _stdout_tee = None
    _stderr_tee = None

    if defer or silent:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    else:
        # If stdout wasn't redirected by the user, create a _Tee for it to capture & redirect stdout
        if "stdout" not in kwargs:
            stdout_writers: list[io.IOBase | IO] = [io.StringIO()]

            # If the runlog file is an open file, add it as an output so stdout is written to it
            if ctx.runlog_file is not None and isinstance(ctx.runlog_file, io.TextIOWrapper):
                stdout_writers.append(ctx.runlog_file)

            # If teeout is set, redirect stdout to STDERR (this avoids conflicts with logging prints)
            if teeout:
                stdout_writers.append(sys.stderr)

            # Create a new asynchronous _Tee object to write to the runlog file/stdout while the command runs
            _stdout_tee = _Tee(*stdout_writers)
            kwargs["stdout"] = _stdout_tee

        # If stderr wasn't redirected by the user, create a _Tee for it to capture & redirect stderr
        if "stderr" not in kwargs:
            stderr_writers: list[io.IOBase | IO] = [io.StringIO()]

            # If the runlog file is an open file, add it as an output so stderr is written to it
            if ctx.runlog_file is not None and isinstance(ctx.runlog_file, io.TextIOWrapper):
                stderr_writers.append(ctx.runlog_file)

            # If teeout is set, also redirect stderr to the system stderrr
            if teeout:
                stderr_writers.append(sys.stderr)

            # Create a new asynchronous _Tee object to write stderr to the runlog file/stderr while the command runs
            _stderr_tee = _Tee(*stderr_writers)
            kwargs["stderr"] = _stderr_tee

    # If deferring, return immediately; check if command exists by catching FileNotFoundError
    try:
        if defer:
            return Process(subprocess.Popen(cmd_list, env=run_env, **kwargs), cmd_str=cmd_str, teeout=False)
        proc = Process(subprocess.run(cmd_list, env=run_env, **kwargs), cmd_str=cmd_str, teeout=teeout)
    except FileNotFoundError:
        if ctx.runlog_file is not None:
            assert isinstance(ctx.runlog_file, io.TextIOWrapper)
            ctx.runlog_file.write(f"> ERROR: Command not found: {cmd_str}")
            ctx.runlog_file.flush()

        log_stream = ctx.log.error if allow_error else ctx.log.critical
        log_stream(f"Running command:   '{cmd_str}'\n")
        log_stream(f"Unquoted command:  '{' '.join(cmd_list)}'\n")
        log_stream(f"Working directory: '{os.getcwd()}'\n")
        log_stream("Local environment: {")
        log_stream("\n".join([f"\t{key}={val}" for key, val in loc_env.items()]))
        log_stream("}")

        if allow_error:
            return Process(None, cmd_str=cmd_str, teeout=teeout)
        raise

    # Close the stdout/stderr tee's if they were open; this also flushes them
    if _stdout_tee is not None:
        _stdout_tee.close()
    if _stderr_tee is not None:
        _stderr_tee.close()

    # Store the stdout/stderr values in the overwrite buffers for quicker access (also as a string)
    if _stdout_tee is not None:
        assert len(_stdout_tee.writers) > 0
        assert isinstance(_stdout_tee.writers[0], io.StringIO)
        proc.stdout_override = _stdout_tee.writers[0].getvalue()
    if _stderr_tee is not None:
        assert len(_stderr_tee.writers) > 0
        assert isinstance(_stderr_tee.writers[0], io.StringIO)
        proc.stderr_override = _stderr_tee.writers[0].getvalue()

    # Finally, check the process' return code & if errors weren't allowed, raise an error now
    if proc.returncode != 0 and not allow_error:
        ctx.log.critical(f"Return code:       {proc.returncode}")
        ctx.log.critical(f"Executed command:  {cmd_str}")
        ctx.log.critical(f"Working directory: {os.getcwd()}")
        ctx.log.critical("Local environment: {")
        ctx.log.critical("\n".join([f"\t{key}={val}" for key, val in loc_env.items()]))
        ctx.log.critical("}")
        raise RuntimeError(f"Command failed but allow_errors was False; invalid return code: {proc.returncode}")

    return proc


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


class _Tee(io.IOBase):
    """
    Extension of io.IOBase to split output over multiple given writers (other IOBases,
    IO objects, or other _Tee objects). An asynchronous thread is started to read
    input from the given I/O objects without blocking the main thread. If all output
    should be flushed, :func:`_Tee.flush_all()` can be used.
    """

    ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")  # 7-bit C1 ANSI sequences

    def __init__(self, *writers: io.IOBase | io.TextIOBase | IO):
        super().__init__()
        self.writers: list[io.IOBase | io.TextIOBase | IO] = list(writers)
        assert self.writers, "At least one writer must be provided to _Tee!"

        # Create new pipes to read/write from (used as input for select)
        self.readfd, self.writefd = os.pipe()
        os.set_blocking(self.readfd, False)

        # Configure events to synchronise the flusher thread and signal when to flush/close
        self.running = threading.Event()
        self.thread = threading.Thread(target=self._flusher, daemon=True)

        # Set the running condition to true and start the thread
        self.running.set()
        self.thread.start()

    def fileno(self) -> int:
        """Anything writing to this object will be read by the flusher thread"""
        return self.writefd

    def read_fileno(self) -> int:
        """Returns the file number of the pipe the flusher thread is reading from"""
        return self.readfd

    def write(self, data: str | bytes) -> int:
        """Writes input to all writers in self.writers; converts text data to binary to support both"""
        if isinstance(data, str):
            data = data.encode()
        os.write(self.writefd, data)
        return len(data)

    def close(self) -> None:
        """Signals flusher to stop & waits until all data is read; then joins the thread & flushes all writers"""
        self.running.clear()
        os.close(self.writefd)
        self.thread.join()
        os.close(self.readfd)
        for writer in self.writers:
            writer.flush()

    def flush(self) -> None:
        """Flushes all stored writers"""
        for writer in self.writers:
            writer.flush()

    def _flusher(self) -> None:
        # Wrap the main read-write loop in a try-finally block to ensure lingering data is read/written
        try:
            # While the _Tee hasn't been closed yet
            while self.running.is_set():
                # Try to read the data; if the IO blocks just try again
                try:
                    data = os.read(self.readfd, io.DEFAULT_BUFFER_SIZE)

                    # Skip empty data
                    if not data:
                        continue

                    # Try to decode the data as text; if that fails, only write to supporting writers
                    try:
                        for writer in self.writers:
                            # Only write ANSII-escape sequences to TTY writers; otherwise strip them
                            if writer.isatty() and isinstance(writer, io.TextIOBase):
                                writer.write(data.decode(encoding="utf-8"))
                            elif isinstance(writer, io.TextIOBase):
                                writer.write(self.ansi_escape.sub("", data.decode(encoding="utf-8")))
                            else:
                                # This writer expects binary data; don't decode the textual data
                                writer.write(data)
                            writer.flush()
                    except UnicodeDecodeError:
                        # The data is binary; only write it to writers that support it (not strings/stdout)
                        for writer in self.writers:
                            if isinstance(writer, (io.BufferedWriter, io.RawIOBase)):
                                writer.write(data)
                                writer.flush()
                except BlockingIOError:
                    continue
        finally:
            # Flush any remaining data (if any)
            while True:
                try:
                    data = os.read(self.readfd, io.DEFAULT_BUFFER_SIZE)
                    if not data:
                        break

                    try:
                        for writer in self.writers:
                            if writer.isatty():
                                assert isinstance(writer, io.TextIOBase)
                                writer.write(data.decode(encoding="utf-8"))
                            elif isinstance(writer, io.TextIOBase):
                                writer.write(self.ansi_escape.sub("", data.decode(encoding="utf-8")))
                            else:
                                writer.write(data)
                            writer.flush()
                    except UnicodeDecodeError:
                        # The data is binary; only write it to writers that support it (not strings/stdout)
                        for writer in self.writers:
                            if isinstance(writer, (io.BufferedWriter, io.RawIOBase)):
                                writer.write(data)
                                writer.flush()
                except BlockingIOError:
                    break


def require_program(ctx: Context, name: str, error: str | None = None) -> None:
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
