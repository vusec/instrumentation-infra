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


def prefix_paths(prefixes, suffix, existing):
    paths = []

    for pre in prefixes:
        if os.path.exists(pre + suffix):
            paths.append(pre + suffix)

    if existing:
        paths.append(existing)

    return ':'.join(paths)


def run(ctx, cmd, allow_error=False, *args, **kwargs):
    cmd_print = ' '.join(map(shlex.quote, cmd))

    # TODO: stream output to logs
    try:
        logger.debug('running: %s' % cmd_print)
        logger.debug('workdir: %s' % os.getcwd())

        env = os.environ.copy()
        env['PATH'] = prefix_paths(ctx.prefixes, '/bin', env.get('PATH', ''))
        env['LD_LIBRARY_PATH'] = prefix_paths(ctx.prefixes, '/lib',
                                            env.get('LD_LIBRARY_PATH', ''))
        env.update(kwargs.get('env', {}))

        logger.debug('PATH:            ' + env['PATH'])
        logger.debug('LD_LIBRARY_PATH: ' + env['LD_LIBRARY_PATH'])

        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.STDOUT)
        kwargs.setdefault('universal_newlines', True)

        proc = subprocess.run(cmd, *args, **kwargs, env=env)

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


class Namespace(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


class FatalError(Exception):
    pass
