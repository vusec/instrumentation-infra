import os
import sys
import subprocess
import logging
import shlex
from urllib.request import urlretrieve


logger = logging.getLogger('autosetup')


def apply_patch(base_path, patch_name, strip_count):
    stamp = '.patched-' + patch_name

    if os.path.exists(stamp):
        # TODO: check modification time
        return False

    patch_path = '%s/%s.patch' % (base_path, patch_name)

    with open(patch_path) as patch_file:
        run(['patch', '-p%d' % strip_count], stdin=patch_file)

    open(stamp, 'w').close()
    return True


def run(cmd, *args, **kwargs):
    cmd_print = ' '.join(map(shlex.quote, cmd))
    logger.debug('running: %s' % cmd_print)
    logger.debug('workdir: %s' % os.getcwd())

    # TODO: stream output to logs
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, *args, **kwargs)
    if proc.returncode:
        logger.error('command returned status %d' % proc.returncode)
        logger.error('command: %s' % cmd_print)
        logger.error('workdir: %s' % os.getcwd())
        sys.stdout.write(proc.stdout)
        sys.exit(-1)
    return proc


def download(url, outfile=None):
    if not outfile:
        outfile = os.path.basename(url)
    logger.debug('downloading %s to %s' % (url, outfile))
    urlretrieve(url, outfile)


class FatalError(Exception):
    pass
