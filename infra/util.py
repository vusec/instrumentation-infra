import os
import sys
import subprocess
import shlex
import io
import threading
import select
import inspect
import functools
from typing import Union, List, Dict, Iterable, Optional, Callable, Any
from urllib.request import urlretrieve
from urllib.parse import urlparse
from contextlib import redirect_stdout
from functools import reduce


class Namespace(dict):
    """
    A dictionary in which keys can be accessed as attributes, i.e., ``ns.key``
    is the same as ``ns['key']``. Used for the context (see
    :class:`Setup`).
    """
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value

    def copy(self) -> 'Namespace':
        """
        Make a deepcopy of this namespace, but only copy values with type
        ``Namespace|list|dict``.
        """
        ns = self.__class__()
        for key, value in self.items():
            if isinstance(value, (self.__class__, list, dict)):
                value = value.copy()
            ns[key] = value
        return ns

    def join_paths(self) -> 'Namespace':
        """
        Create a new namespace in which all lists/tuples of strings are
        replaced with ``':'.join(list_or_tuple)``. Used by :func:`run` to squash
        lists of paths in environment variables.
        """
        new = self.__class__()
        for key, value in self.items():
            if isinstance(value, (tuple, list)):
                value = ':'.join(value)
            elif isinstance(value, self.__class__):
                value = value.join_paths()
            new[key] = str(value)
        return new


class FatalError(Exception):
    """
    Raised for errors that should stop the execution immediately, but do not
    need a backtrace. Results in only the exception message being logged. This
    typically means there is an error in the user input, rather than in the code
    that raises the error.
    """
    pass


def apply_patch(ctx: Namespace, path: str, strip_count: int) -> bool:
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
    name = os.path.basename(path).replace('.patch', '')
    stamp = '.patched-' + name

    if os.path.exists(stamp):
        # TODO: check modification time
        return False

    ctx.log.debug('applying patch %s' % name)

    with open(path) as f:
        run(ctx, ['patch', '-p%d' % strip_count], stdin=f)

    open(stamp, 'w').close()
    return True


def run(ctx: Namespace, cmd: Union[str, List[str]], allow_error=False,
        silent=False, teeout=False, defer=False,
        env: Dict[str, Union[str, List[str]]] = {}, **kwargs) -> \
        Union[subprocess.CompletedProcess, subprocess.Popen]:
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
    values are joined with a ':' separator using :func:`Namespace.join_paths`.

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
    stdin = kwargs.get('stdin', None)
    if isinstance(stdin, io.IOBase):
        cmd_print += ' < ' + shlex.quote(stdin.name)
    ctx.log.debug('running: %s' % cmd_print)
    ctx.log.debug('workdir: %s' % os.getcwd())

    logenv = ctx.runenv.join_paths()
    logenv.update(Namespace.join_paths(env))
    renv = os.environ.copy()
    renv.update(logenv)

    log_output = False
    if defer or silent:
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
    elif 'stdout' not in kwargs and 'runlog' in ctx:
        log_output = True

        # 'tee' output to logfile and string; does line buffering in a separate
        # thread to be able to flush the logfile during long-running commands
        # (use tail -f to view command output)
        if 'runtee' not in ctx:
            ctx.runtee = _Tee(ctx.runlog, io.StringIO())

        strbuf = ctx.runtee.writers[1]

        with redirect_stdout(ctx.runlog):
            print('-' * 80)
            print('command: %s' % cmd_print)
            print('workdir: %s' % os.getcwd())
            for k, v in logenv.items():
                print('%s=%s' % (k, v))
            hdr = '-- output: '
            print(hdr + '-' * (80 - len(hdr)))

        if teeout:
            kwargs['stdout'] = _Tee(ctx.runtee, sys.stdout)
        else:
            kwargs['stdout'] = ctx.runtee

        kwargs.setdefault('stderr', subprocess.STDOUT)

    kwargs.setdefault('universal_newlines', True)

    try:
        if defer:
            proc = subprocess.Popen(cmd, env=renv, **kwargs)
            proc.cmd_print = cmd_print
            proc.teeout = False
            return proc

        proc = subprocess.run(cmd, env=renv, **kwargs)
        proc.teeout = teeout

    except FileNotFoundError:
        logfn = ctx.log.debug if allow_error else ctx.log.error
        logfn('command not found: %s' % cmd_print)
        logfn('workdir:           %s' % os.getcwd())
        if allow_error:
            return
        raise

    if log_output:
        proc.stdout = strbuf.getvalue()

        # delete dangling buffer to free up memory
        ctx.runtee.writers[1] = io.StringIO()

        # add trailing newline to logfile for readability
        ctx.runlog.write('\n')
        ctx.runlog.flush()

    if proc.returncode and not allow_error:
        ctx.log.error('command returned status %d' % proc.returncode)
        ctx.log.error('command: %s' % cmd_print)
        ctx.log.error('workdir: %s' % os.getcwd())
        for k, v in logenv.items():
            ctx.log.error('%s=%s' % (k, v))
        if proc.stdout is not None:
            sys.stdout.write(proc.stdout)
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
    return ' '.join(shlex.quote(str(arg)) for arg in args)


def download(ctx: Namespace, url: str, outfile: Optional[str] = None):
    """
    Download a file (logs to the debug log).

    :param ctx: the configuration context
    :param url: URL to the file to download
    :param outfile: optional path/filename to download to
    """
    if outfile:
        ctx.log.debug('downloading %s to %s' % (url, outfile))
    else:
        outfile = os.path.basename(urlparse(url).path)
        ctx.log.debug('downloading %s' % url)
    urlretrieve(url, outfile)


class _Tee(io.IOBase):
    def __init__(self, *writers):
        super().__init__()
        assert len(writers) > 0
        self.writers = list(writers)
        self.readfd, self.writefd = os.pipe()
        self.running = False
        self.thread = threading.Thread(target=self._flusher)
        self.thread.daemon = True
        self.thread.start()

    def _flusher(self):
        self.running = True
        poller = select.poll()
        poller.register(self.readfd, select.POLLIN | select.POLLPRI)
        buf = b''
        while self.running:
            for fd, flag in poller.poll():
                assert fd == self.readfd
                if flag & (select.POLLIN | select.POLLPRI):
                    buf += os.read(fd, io.DEFAULT_BUFFER_SIZE)
                    nl = buf.find(b'\n') + 1
                    while nl > 0:
                        self.write(buf[:nl].decode())
                        self.flush()
                        buf = buf[nl:]
                        nl = buf.find(b'\n') + 1

    def flush(self):
        for w in self.writers:
            w.flush()

    def write(self, data):
        len1 = self.writers[0].write(data)
        for w in self.writers[1:]:
            len2 = w.write(data)
            assert len2 == len1
        return len1
    emit = write

    def fileno(self):
        return self.writefd

    def __del__(self):
        self.close()

    def close(self):
        if self.running:
            self.running = False
            self.thread.join(0)
            os.close(self.readfd)
            os.close(self.writefd)


def geomean(values: Iterable[Union[float, int]]) -> float:
    """
    Compute the geometric mean of a list of numbers.

    :param values: non-empty list of numbers
    """
    assert len(values) > 0
    return reduce(lambda x, y: x * y, values) ** (1.0 / len(values))


def param_attrs(constructor: Callable) -> Callable:
    """
    Decorator for class constructors that sets parameter values as object
    attributes::

        >>> class Foo:
        ...     @param_attrs
        ...     def __init__(self, a, b=1, *, c=True):
        ...         pass

        >>> foo = Foo('a')
        >>> foo.a
        'a'
        >>> foo.b
        1
        >>> foo.c
        True

    :param constructor: the ``__init__`` method being decorated
    """
    params = inspect.signature(constructor).parameters
    positional = [p.name for p in params.values()
                  if p.kind == p.POSITIONAL_OR_KEYWORD]
    assert positional.pop(0) == 'self'

    @functools.wraps(constructor)
    def wrapper(self, *args, **kwargs):
        for name, param in params.items():
            if name in kwargs:
                setattr(self, name, kwargs[name])
            elif param.default != param.empty:
                setattr(self, name, param.default)

        for name, value in zip(positional, args):
            setattr(self, name, value)

        constructor(self, *args, **kwargs)

    return wrapper
