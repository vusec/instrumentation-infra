import io
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
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
    ret = {}
    for k, v in env.items():
        if isinstance(v, str):
            ret[k] = v
        else:
            ret[k] = ":".join(v)
    return ret


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

    It is useful to permanently have a terminal window open running ``tail -f
    build/log/commands.txt``, This way, command output is available in case of
    errors but does not clobber the setup's progress log.

    The run environment is based on :any:`os.environ`, first adding
    ``ctx.runenv`` (populated by packages/instances, see also :class:`Setup`)
    and then the ``env`` parameter. The combination of ``ctx.runenv`` and
    ``env`` is logged to the log file. Any lists of strings in environment
    values are joined with a ':' separator.

    If the command exits with a non-zero status code, the corresponding output
    is logged to the command line and the process is killed with
    ``sys.exit(-1)``.

    :param ctx: the configuration context
    :param cmd: command to run, can be a string or a list of strings like in
                :func:`subprocess.run`
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
    cmd = shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in cmd]
    cmd_print = qjoin(cmd)
    stdin = kwargs.get("stdin", None)
    if isinstance(stdin, io.FileIO):
        cmd_print += " < " + shlex.quote(str(stdin.name))
    ctx.log.debug(f"running: {cmd_print}")
    ctx.log.debug(f"workdir: {os.getcwd()}")

    logenv = join_env_paths(ctx.runenv)
    logenv.update(join_env_paths(env))
    renv = os.environ.copy()
    renv.update(logenv)

    strbuf = None
    log_output = False
    if defer or silent:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    elif "stdout" not in kwargs and ctx.runlog_file is not None:
        log_output = True

        # 'tee' output to logfile and string; does line buffering in a separate
        # thread to be able to flush the logfile during long-running commands
        # (use tail -f to view command output)
        if ctx.runtee is None:
            ctx.runtee = _Tee(ctx.runlog_file, io.StringIO())
        assert isinstance(ctx.runtee, _Tee)

        strbuf = ctx.runtee.writers[1]
        assert isinstance(strbuf, io.StringIO)

        with redirect_stdout(ctx.runlog_file):
            print("-" * 80)
            print(f"command: {cmd_print}")
            print(f"workdir: {os.getcwd()}")
            for k, v in logenv.items():
                print(f"{k}={v}")
            hdr = "-- output: "
            print(hdr + "-" * (80 - len(hdr)))

        if teeout:
            kwargs["stdout"] = _Tee(ctx.runtee, sys.stdout)
        else:
            kwargs["stdout"] = ctx.runtee

        kwargs.setdefault("stderr", subprocess.STDOUT)

    kwargs.setdefault("universal_newlines", True)

    try:
        if defer:
            proc = Process(subprocess.Popen(cmd, env=renv, **kwargs), cmd_print, False)
            return proc

        proc = Process(subprocess.run(cmd, env=renv, **kwargs), cmd_print, teeout)

    except FileNotFoundError:
        logfn = ctx.log.debug if allow_error else ctx.log.error
        logfn(f"command not found: {cmd_print}")
        logfn(f"workdir:           {os.getcwd()}")
        if allow_error:
            return Process(None, cmd_print, teeout)
        raise

    if log_output:
        assert ctx.runlog_file is not None
        assert isinstance(ctx.runtee, _Tee)
        assert isinstance(strbuf, io.StringIO)

        proc.stdout_override = strbuf.getvalue()

        # delete dangling buffer to free up memory
        ctx.runtee.writers[1] = io.StringIO()

        # add trailing newline to logfile for readability
        ctx.runlog_file.write("\n")
        ctx.runlog_file.flush()

    if proc.returncode and not allow_error:
        ctx.log.error(f"command returned status {proc.returncode}")
        ctx.log.error(f"command: {cmd_print}")
        ctx.log.error(f"workdir: {os.getcwd()}")
        for k, v in logenv.items():
            ctx.log.error(f"{k}={v}")
        assert proc.proc is not None
        if proc.proc.stdout is not None:
            output = proc.stdout
            if isinstance(output, bytes):
                output = output.decode()
            assert isinstance(output, str)
            sys.stdout.write(output)
        sys.exit(-1)

    return proc


def qjoin(args: Iterable[Any]) -> str:
    """
    Join the command-line arguments to a single string to make it safe to pass
    to paste in a shell. Basically this adds quotes to each element containing
    spaces (uses :func:`shlex.quote`). Arguments are stringified by
    :class:`str` before joining.

    :param args: arguments to join
    """
    return " ".join(shlex.quote(str(arg)) for arg in args)


def download(ctx: Context, url: str, outfile: Optional[str] = None) -> None:
    """
    Download a file (logs to the debug log).

    :param ctx: the configuration context
    :param url: URL to the file to download
    :param outfile: optional path/filename to download to
    """
    if outfile:
        ctx.log.debug(f"downloading {url} to {outfile}")
    else:
        outfile = os.path.basename(urlparse(url).path)
        ctx.log.debug(f"downloading {url}")
    urlretrieve(url, outfile)


class _Tee(io.IOBase):
    def __init__(self, *writers: Union[io.IOBase, typing.IO]):
        super().__init__()
        assert len(writers) > 0
        self.writers = list(writers)
        self.readfd, self.writefd = os.pipe()
        self.running = False
        self.thread = threading.Thread(target=self._flusher)
        self.thread.daemon = True
        self.thread.start()

    def _flusher(self) -> None:
        self.running = True
        poller = select.poll()
        poller.register(self.readfd, select.POLLIN | select.POLLPRI)
        buf = b""
        while self.running:
            for fd, flag in poller.poll():
                assert fd == self.readfd
                if flag & (select.POLLIN | select.POLLPRI):
                    buf += os.read(fd, io.DEFAULT_BUFFER_SIZE)
                    nl = buf.find(b"\n") + 1
                    while nl > 0:
                        self.write(buf[:nl].decode(errors="replace"))
                        self.flush()
                        buf = buf[nl:]
                        nl = buf.find(b"\n") + 1

    def flush(self) -> None:
        for w in self.writers:
            w.flush()

    def write(self, data: str) -> int:
        len1 = self.writers[0].write(data)
        for w in self.writers[1:]:
            len2 = w.write(data)
            assert len2 == len1
        return len1

    emit = write

    def fileno(self) -> int:
        return self.writefd

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        if self.running:
            self.running = False
            self.thread.join(0)
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
    dest: Optional[str] = None,
    *,
    remove: bool = True,
    basename: Optional[str] = None,
) -> None:
    """
    TODO: docs
    """
    if basename is None:
        basename = re.sub(r"\.tar(\.\w+)?", "", tarname)
    require_program(ctx, "tar", "required to unpack source tarfile")
    run(ctx, ["tar", "-xf", tarname])
    if dest:
        shutil.move(basename, dest)
    if remove:
        os.remove(tarname)
