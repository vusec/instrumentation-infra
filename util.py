import os
import sys
import subprocess
import shlex
import io
import threading
import select
from urllib.request import urlretrieve
from urllib.parse import urlparse
from contextlib import redirect_stdout


def apply_patch(ctx, base_path, patch_name, strip_count):
    stamp = '.patched-' + patch_name

    if os.path.exists(stamp):
        # TODO: check modification time
        return False

    patch_path = '%s/%s.patch' % (base_path, patch_name)

    with open(patch_path) as patch_file:
        run(ctx, ['patch', '-p%d' % strip_count], stdin=patch_file)

    open(stamp, 'w').close()
    return True


def run(ctx, cmd, allow_error=False, silent=False, env={}, *args, **kwargs):
    cmd = shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in cmd]
    cmd_print = qjoin(cmd)
    ctx.log.debug('running: %s' % cmd_print)
    ctx.log.debug('workdir: %s' % os.getcwd())

    logenv = ctx.runenv.join_paths()
    logenv.update(env)
    renv = os.environ.copy()
    renv.update(logenv)

    log_output = not silent and 'stdout' not in kwargs and 'runlog' in ctx
    if log_output:
        # 'tee' output to logfile and string; does line buffering in a separate
        # thread to be able to flush the logfile during long-running commands
        # (use tail -f to view command output)
        if 'runtee' not in ctx:
            ctx.runtee = Tee(ctx.runlog, io.StringIO())

        strbuf = ctx.runtee.writers[1]

        with redirect_stdout(ctx.runlog):
            print('-' * 80)
            print('command: %s' % cmd_print)
            print('workdir: %s' % os.getcwd())
            for k, v in logenv.items():
                print('%s=%s' % (k, v))
            hdr = '-- output: '
            print(hdr + '-' * (80 - len(hdr)))

        kwargs['stdout'] = ctx.runtee
    elif silent:
        kwargs.setdefault('stdout', subprocess.PIPE)

    kwargs.setdefault('stderr', subprocess.STDOUT)
    kwargs.setdefault('universal_newlines', True)

    try:
        proc = subprocess.run(cmd, *args, **kwargs, env=renv)
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


def qjoin(args):
    return ' '.join(shlex.quote(arg) for arg in args)


def download(ctx, url, outfile=None):
    if outfile:
        ctx.log.debug('downloading %s to %s' % (url, outfile))
    else:
        outfile = os.path.basename(urlparse(url).path)
        ctx.log.debug('downloading %s' % url)
    urlretrieve(url, outfile)


class Tee(io.IOBase):
    def __init__(self, *writers):
        super(Tee, self).__init__()
        assert len(writers) > 0
        self.writers = list(writers)
        self.readfd, self.writefd = os.pipe()
        self.running = False
        self.thread = threading.Thread(target=self.flusher)
        self.thread.daemon = True
        self.thread.start()

    def flusher(self):
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


class Namespace(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value

    def copy(self):
        ns = self.__class__()
        for key, value in self.items():
            if isinstance(value, (self.__class__, list, dict)):
                value = value.copy()
            ns[key] = value
        return ns

    def join_paths(self):
        new = self.__class__()
        for key, value in self.items():
            if isinstance(value, (tuple, list)):
                value = ':'.join(value)
            elif isinstance(value, self.__class__):
                value = value.join_paths()
            new[key] = value
        return new


class FatalError(Exception):
    pass
