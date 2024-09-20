import io
import os
import re
import sys
import shlex
import shutil
import logging
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
    TypeVar,
    Mapping,
    Callable,
    Iterable,
    Iterator,
    KeysView,
    ItemsView,
    TypeAlias,
    ValuesView,
    MutableMapping,
)

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


def join_env_paths(env: dict[str, str | list[str]]) -> dict[str, str]:
    """
    Convert an environment dictionary to a dictionary mapping variable names to their values, all as
    strings. Lists in the given dictionary are converted to ":"-delimited lists (e.g. like $PATH).

    Note: the given dictionary should contain only str or list[str], but for both this function will
    also attempt to convert them to string if possible.

    :param env: the environment dicitonary to convert (should contain str or list[str])
    :return dict[str, str]: a str-to-str mapping that can be used to pass to e.g. subprocess.run()
    """
    return {k: ":".join(str(x) for x in v) if isinstance(v, list) else v for k, v in env.items()}


def get_stream_formatter() -> logging.Formatter:
    try:
        from textwrap import TextWrapper

        wrapper = TextWrapper(
            width=shutil.get_terminal_size(fallback=(80, 24))[0],
            initial_indent=(" " * 9),
            subsequent_indent=(" " * 9),
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
                        "%(log_color)s%(levelname)8s%(reset)s "
                        "%(bold_white)s%(module)s%(reset)s from "
                        "%(purple)s%(funcName)s%(reset)s::"
                        "%(blue)s%(filename)s%(reset)s"
                        "(%(yellow)s%(lineno)d%(reset)s) at "
                        "%(green)s%(asctime)s.%(msecs)03d%(reset)s:\n"
                        "%(message_log_color)s%(message)s%(reset)s"
                    ),
                    datefmt="%H:%M:%S",
                    log_colors={
                        "NOTSET": "bold_white",
                        "DEBUG": "bold_cyan",
                        "INFO": "bold_green",
                        "WARN": "bold_yellow",
                        "WARNING": "bold_yellow",
                        "ERROR": "bold_red",
                        "FATAL": "bold_white,bg_bold_red",
                        "CRITICAL": "bold_white,bg_bold_red",
                    },
                    secondary_log_colors={
                        "message": {
                            "NOTSET": "thin_white",
                            "DEBUG": "thin_white",
                            "INFO": "thin_white",
                            "WARN": "thin_white",
                            "WARNING": "thin_white",
                            "ERROR": "thin_white",
                            "FATAL": "thin_white",
                            "CRITICAL": "thin_white",
                        }
                    },
                )

            def format(self, record: logging.LogRecord) -> str:
                # If no wrapper was set, just format regularly
                if wrapper is None:
                    return super().format(record)
                header, *message = super().format(record).splitlines()
                return header + "\n" + ("\n".join(wrapper.fill(line) for line in message))

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

        # 7-bit C1 ANSI sequences
        ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")

        def format(self, record: logging.LogRecord) -> str:
            if isinstance(record.msg, str):
                record.msg = self.ansi_escape.sub("", record.msg)
            return super().format(record)

    return StrippingFormatter()


@dataclass
class Process:
    """Wrapper class around the result of a call to :func:`subprocess.run()` or :func:`subprocess.Popen()`.

    The return value of the call to :func:`subprocess.run()`/:func:`subprocess.Popen()` is stored in
    :var:`self.proc` and the stringified command that was run is stored in :var:`self.cmd_str`.

    This class also provides convenience accessors for a process' return code, stdout, and stderr through
    :prop:`self.returncode`, :prop:`self.stdout`, and :prop:`self.stderr`. Note that these properties
    are "guaranteed"; i.e. if the stored process was deffered through :func:`subprocess.Popen()`,
    fetching :prop:`self.returncode` will wait for completion to return the return code. Similarly,
    fetching :prop:`self.stdout` or :prop:`self.stderr` will return the underlying process' stdout or
    stderr (and decode them if necessary); if the underlying process' stdout/stderr properties are
    an IO type, they are read to a string and stored (meaning :prop:`self.stdout`/:prop:`self.stderr`
    will only read from the IO streams once and return previously read strings on subsequent
    calls to :prop:`self.stdout` or :prop:`self.stderr`).
    """

    proc: subprocess.CompletedProcess | subprocess.Popen | None
    teeout: bool
    cmd_str: str

    stdout_override: str | None = None
    stderr_override: str | None = None

    @property
    def returncode(self) -> int:
        """Returns the return code of the executed process.

        If the underlying process is of type :type:`subprocecss.CompletedProcess` (i.e. from
        :func:`subprocess.run()`) the return code is directly returned.

        If the underlying type is of type :type:`subprocess.Popen()` (i.e. a deferred process),
        this is equivalent to calling :func:`proc.wait(timeout=None)` (i.e. this call will block
        until the process finished running and the return code is available).

        :raises ProcessLookupError: raised if the stored process :var:`self.proc` is None
        :return int: the return code of the process
        """
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no return code!")

        return self.proc.returncode if isinstance(self.proc, subprocess.CompletedProcess) else self.proc.wait()

    @property
    def stdout(self) -> str:
        """Returns whatever the executed process wrote the stdout as a string object.

        If the type of stdout is :type:`bytes`, the output is decoded to a string (encoding is
        assumed to be "ascii") and returned. Note the read/decoded output is also stored in
        :param:`self.stdout_override` so it doesn't need to be decoded again in the future.

        If the type of stdout is an IO stream (:type:`typing.IO`), the output is read (and decoded if
        applicable) from the stream (with :func:`self.proc.stdout.read()`). The result is stored in
        :param:`self.stdout_override` so that subsequent accesses to this property won't try to
        read from the stream again and instead return what was read/decoded previously

        Note that this property will return the empty string if :param:`self.proc.stdout` was not
        captured at all (i.e. is :type:`None`)

        :raises ProcessLookupError: raised if the stored process :param:`self.proc` is invalid (i.e. is `None`)
        :raises ValueError: raised if the type of :param:`self.proc.stdout` is not :type:`None|str|bytes|IO`
        :return str: returns whatever the executed command wrote to :param:`stdout`
        """
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stdout!")

        if self.stdout_override is not None:
            return self.stdout_override

        if self.proc.stdout is None:
            return ""

        if isinstance(self.proc.stdout, str):
            self.stdout_override = self.proc.stdout
            return self.stdout_override

        if isinstance(self.proc.stdout, bytes):
            self.stdout_override = self.proc.stdout.decode(encoding="ascii", errors="replace")
            return self.stdout_override

        if isinstance(self.proc.stdout, IO):
            outs = self.proc.stdout.read()
            if isinstance(outs, str):
                self.stdout_override = outs
                return outs
            if isinstance(outs, bytes):
                outs = outs.decode(encoding="ascii", errors="replace")
                self.stdout_override = outs
                return outs

        raise ValueError(f"Unsupported type for stdout; expected str/bytes/IO, got: {type(self.proc.stdout)}")

    @property
    def stderr(self) -> str:
        """Returns whatever the executed process wrote the stderr as a string object.

        If the type of stderr is :type:`bytes`, the output is decoded to a string (encoding is
        assumed to be "ascii") and returned. Note the read/decoded output is also stored in
        :param:`self.stderr_override` so it doesn't need to be decoded again in the future.

        If the type of stderr is an IO stream (:type:`typing.IO`), the output is read (and decoded if
        applicable) from the stream (with :func:`self.proc.stderr.read()`). The result is stored in
        :param:`self.stderr_override` so that subsequent accesses to this property won't try to
        read from the stream again and instead return what was read/decoded previously

        Note that this property will return the empty string if :param:`self.proc.stderr` was not
        captured at all (i.e. is :type:`None`)

        :raises ProcessLookupError: raised if the stored process :param:`self.proc` is invalid (i.e. is `None`)
        :raises ValueError: raised if the type of :param:`self.proc.stderr` is not :type:`None|str|bytes|IO`
        :return str: returns whatever the executed command wrote to :param:`stderr`
        """
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stderr!")

        if self.stderr_override is not None:
            return self.stderr_override

        if self.proc.stderr is None:
            return ""

        if isinstance(self.proc.stderr, str):
            self.stderr_override = self.proc.stderr
            return self.stderr_override

        if isinstance(self.proc.stderr, bytes):
            self.stderr_override = self.proc.stderr.decode(encoding="ascii", errors="replace")
            return self.stderr_override

        if isinstance(self.proc.stderr, IO):
            errs = self.proc.stderr.read()
            if isinstance(errs, str):
                self.stderr_override = errs
                return errs
            if isinstance(errs, bytes):
                errs = errs.decode(encoding="ascii", errors="replace")
                return errs

        raise ValueError(f"Unsupported type for stderr; expected str/bytes/IO, got: {type(self.proc.stderr)}")

    @property
    def stdout_io(self) -> IO[AnyStr] | None:
        """Alternative version of :prop:`self.stdout` that instead returns the IO stream of the underlying
        process' stdout instead of reading from it and returning the contained string value; equivalent
        to accessing :param:`self.proc.stdout` directly to use in :func:`self.proc.stdout.read()`

        :raises ProcessLookupError: raised if the stored process :param:`self.proc` is invalid (i.e. is `None`)
        :raises ValueError: raised if the type of :param:`self.proc.stdout` is not an IO type
        :return IO[AnyStr] | None: the IO stream of :param:`self.proc.stdout`
        """
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stdout!")

        if isinstance(self.proc.stdout, IO):
            return self.proc.stdout

        raise ValueError(f"Cannot get stdout IO stream; stdout is {type(self.proc.stdout)}")

    @property
    def stderr_io(self) -> IO[AnyStr] | None:
        """Alternative version of :prop:`self.stderr` that instead returns the IO stream of the underlying
        process' stderr instead of reading from it and returning the contained string value; equivalent
        to accessing :param:`self.proc.stderr` directly to use in :func:`self.proc.stderr.read()`

        :raises ProcessLookupError: raised if the stored process :param:`self.proc` is invalid (i.e. is `None`)
        :raises ValueError: raised if the type of :param:`self.proc.stderr` is not an IO type
        :return IO[AnyStr] | None: the IO stream of :param:`self.proc.stderr`
        """
        if self.proc is None:
            raise ProcessLookupError("Invalid (None) process has no stderr!")

        if isinstance(self.proc.stderr, IO):
            return self.proc.stderr

        raise ValueError(f"Cannot get stderr IO stream; stderr is {type(self.proc.stderr)}")

    def poll(self) -> int | None:
        """Calls :func:`self.proc.poll()` iff the underlying process is of type :type:`subprocess.Popen`,
        otherwise if the underlying process is of type :type:`subprocess.CompletedProcess`, this simply
        returns :param:`self.proc.returncode`

        :raises ProcessLookupError: raised if the stored process :param:`self.proc` is invalid (i.e. is `None`)
        :return int | None: the return code or the result of :func:`self.proc.poll()`
        """
        if self.proc is None:
            raise ProcessLookupError("Cannot poll invalid (None) process!")

        return self.returncode if isinstance(self.proc, subprocess.CompletedProcess) else self.proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        """Calls :func:`self.proc.wait()` iff the underlying process is of type :type:`subprocess.Popen`,
        otherwise if the underlying process is of type :type:`subprocess.CompletedProcess`, this simply
        returns :param:`self.proc.returncode`

        :raises ProcessLookupError: raised if the stored process :param:`self.proc` is invalid (i.e. is `None`)
        :return int: the return code or the result of :func:`self.proc.wait()`
        """
        if self.proc is None:
            raise ProcessLookupError("Cannot wait on invalid (None) process!")

        return self.returncode if isinstance(self.proc, subprocess.CompletedProcess) else self.proc.wait(timeout)


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


def get_safe_cmd_str(cmd_list: Iterable[str] | None, stdin: Any | None = None) -> str:
    """Converts the given command list (e.g. output of :func:`get_cmd_list()`) to a safe-to-print
    string. Uses :func:`qjoin()` (which uses :func:`shlex.quote()`) to convert each element from
    the iterable to a safely quoted element. The concatenated string is returned.

    If :param:`stdin` is given (and is :type:`io.FileIO`), "< [IN_FILE]" is appended to the string

    :param Iterable[str] cmd_list: command to convert to string; output of :func:`get_cmd_list()`
    :param io.FileIO | None stdin: _description_, defaults to None optional input file
    :return str: a safe to print string
    """
    if not cmd_list:
        return ""
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
    env: dict[str, str | list[str]] | None = None,
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
    cmd_str = get_safe_cmd_str(cmd_list, kwargs.get("stdin", None))
    ctx.log.info(f"Running command: {cmd_str} (working dir: {os.getcwd()})")
    assert cmd_list is not None

    # Start the local environment with the current running environment and the stored C/C++/etc compilers
    loc_env: dict[str, str | list[str]] = {
        "CC": ctx.cc,
        "CXX": ctx.cxx,
        "FC": ctx.fc,
        "AR": ctx.ar,
        "NM": ctx.nm,
        "RANLIB": ctx.ranlib,
        **ctx.runenv,
    }

    # Overwrite any values from the env argument (if given)
    if env is not None:
        loc_env |= env

    # Take the OS' environment and merge the local running environment into it; overwrite simple string
    # variables; merge path-like variables (prepending components from ctx.runenv variables)
    run_env: dict[str, str] = {
        key: ":".join(loc_val + os.environ.get(key, "").split(":")) if isinstance(loc_val, list) else loc_val
        for key, loc_val in loc_env.items()
    } | {key: os_val for key, os_val in os.environ.items() if key not in loc_env}

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

        (ctx.log.error if allow_error else ctx.log.critical)(
            f"Running command:   '{cmd_str}'\n"
            + f"Unquoted command:  '{' '.join(cmd_list)}'\n"
            + f"Working directory: '{os.getcwd()}'\n"
            + "Local environment: {"
            + "\n".join([f"\t{key}={val}" for key, val in run_env.items()])
            + "}"
        )

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
        ctx.log.critical(
            f"Return code:       {proc.returncode}\n"
            + f"Executed command:  {cmd_str}\n"
            + f"Working directory: {os.getcwd()}\n"
            + "Local environment: {\n\t"
            + "\n\t".join([f"\t{key}={val}" for key, val in run_env.items()])
            + "\n}"
        )
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
    runenv_path = _path if isinstance(_path := ctx.runenv.get("PATH", []), list) else _path.split(":")
    global_path = os.getenv("PATH", "").split(":")
    path = ":".join(runenv_path + global_path)

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
