import os
import sys
import subprocess
import logging
import shlex
import io
import threading
import select
import copy
from urllib.request import urlretrieve
from urllib.parse import urlparse
from contextlib import redirect_stdout


logger = logging.getLogger('autosetup')


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


def prefix_paths(prefixes, suffix, existing):
    paths = []

    for pre in prefixes:
        if os.path.exists(pre + suffix):
            paths.append(pre + suffix)

    if existing:
        paths.append(existing)

    return ':'.join(paths)


def run(ctx, cmd, allow_error=False, silent=False, env={}, *args, **kwargs):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    cmd_print = ' '.join(map(shlex.quote, cmd))

    # TODO: stream output to logs
    try:
        logger.debug('running: %s' % cmd_print)
        logger.debug('workdir: %s' % os.getcwd())

        renv = os.environ.copy()
        renv['PATH'] = prefix_paths(ctx.prefixes, '/bin', renv.get('PATH', ''))
        renv['LD_LIBRARY_PATH'] = prefix_paths(ctx.prefixes, '/lib',
                renv.get('LD_LIBRARY_PATH', ''))
        renv.update(env)

        logenv = {'PATH': renv['PATH'],
                  'LD_LIBRARY_PATH': renv['LD_LIBRARY_PATH']}
        logenv.update(env)
        for k, v in logenv.items():
            logger.debug('%s: %s' % (k, v))

        log_output = not silent and 'stdout' not in kwargs
        if log_output:
            # 'tee' output to logfile and string; does line buffering in a
            # separate thread to be able to flush the logfile during
            # long-running commands (use tail -f to view command output)
            if 'runtee' not in ctx:
                ctx.runtee = Tee(open(ctx.paths.runlog, 'w'), io.StringIO())

            runlog, strbuf = ctx.runtee.writers

            with redirect_stdout(runlog):
                print('-' * 80)
                print('command: %s' % cmd_print)
                print('workdir: %s' % os.getcwd())
                for k, v in logenv.items():
                    print('%s: %s' % (k, v))
                hdr = '-- output: '
                print(hdr + '-' * (80 - len(hdr)))

            kwargs['stdout'] = ctx.runtee
        elif silent:
            kwargs.setdefault('stdout', subprocess.PIPE)

        kwargs.setdefault('stderr', subprocess.STDOUT)
        kwargs.setdefault('universal_newlines', True)

        proc = subprocess.run(cmd, *args, **kwargs, env=renv)

        if log_output:
            proc.stdout = strbuf.getvalue()

            # delete dangling buffer to free up memory
            del strbuf
            ctx.runtee.writers[1] = io.StringIO()

            # add trailing newline for readability
            ctx.runtee.write('\n')
            ctx.runtee.flush()

        if proc.returncode and not allow_error:
            logger.error('command returned status %d' % proc.returncode)
            logger.error('command: %s' % cmd_print)
            logger.error('workdir: %s' % os.getcwd())
            sys.stdout.write(proc.stdout)
            sys.exit(-1)

        return proc

    except FileNotFoundError:
        logfn = logger.debug if allow_error else logger.error
        logfn('command not found: %s' % cmd_print)
        logfn('workdir:           %s' % os.getcwd())
        if not allow_error:
            raise


def download(url, outfile=None):
    if outfile:
        logger.debug('downloading %s to %s' % (url, outfile))
    else:
        outfile = os.path.basename(urlparse(url).path)
        logger.debug('downloading %s' % url)
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

    def __copy__(self):
        return self.__class__(**self.items())

    def __deepcopy__(self, memo):
        ns = self.__class__()
        for key, value in self.items():
            ns[key] = copy.deepcopy(value)
        return ns


class FatalError(Exception):
    pass
