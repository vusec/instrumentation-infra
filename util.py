import os
import sys
import subprocess
import logging
import shlex
from urllib.request import urlretrieve
from urllib.parse import urlparse


logger = logging.getLogger('autosetup')


def apply_patch(base_path, patch_name, strip_count):
    stamp = '.patched-' + patch_name

    if os.path.exists(stamp):
        # TODO: check modification time
        return False

    patch_path = '%s/%s.patch' % (base_path, patch_name)
    ctx = Namespace(prefixes=[])

    with open(patch_path) as patch_file:
        run(ctx, ['patch', '-p%d' % strip_count], stdin=patch_file)

    open(stamp, 'w').close()
    return True


def run_raw(ctx, cmd, *args, **kwargs):
    cmd_print = ' '.join(map(shlex.quote, cmd))
    logger.debug('running: %s' % cmd_print)
    logger.debug('workdir: %s' % os.getcwd())

    env = os.environ.copy()

    path = ':'.join(pre + '/bin' for pre in ctx.prefixes)
    if 'PATH' in env:
        path += ':' + env['PATH']
    env['PATH'] = path

    libpath = ':'.join(pre + '/lib' for pre in ctx.prefixes)
    if 'LD_LIBRARY_PATH' in env:
        libpath += ':' + env['LD_LIBRARY_PATH']
    env['LD_LIBRARY_PATH'] = libpath

    env.update(kwargs.get('env', {}))

    logger.debug('PATH:            ' + env['PATH'])
    logger.debug('LD_LIBRARY_PATH: ' + env['LD_LIBRARY_PATH'])

    return subprocess.run(cmd, env=env, *args, **kwargs)


def run(ctx, cmd, *args, **kwargs):
    # TODO: stream output to logs
    proc = run_raw(ctx, cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, *args, **kwargs)
    if proc.returncode:
        cmd_print = ' '.join(map(shlex.quote, cmd))
        logger.error('command returned status %d' % proc.returncode)
        logger.error('command: %s' % cmd_print)
        logger.error('workdir: %s' % os.getcwd())
        sys.stdout.write(proc.stdout)
        sys.exit(-1)
    return proc


def download(url, outfile=None):
    if outfile:
        logger.debug('downloading %s to %s' % (url, outfile))
    else:
        outfile = os.path.basename(urlparse(url).path)
        logger.debug('downloading %s' % url)
    urlretrieve(url, outfile)


class Namespace(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


class FatalError(Exception):
    pass
