import os
import shutil
from ..package import Package
from ..util import run, download, require_program, param_attrs


class Wrk2(Package):
    """
    The wrk2 benchmark.

    :identifier: wrk2-<version>
    :param version: version to download
    """
    def __init__(self, version='master'):
        self.version = version

    def ident(self):
        return 'wrk2-' + self.version

    def fetch(self, ctx):
        run(ctx, 'git clone https://github.com/giltene/wrk2.git src')
        os.chdir('src')
        run(ctx, ['git', 'checkout', self.version])

    def build(self, ctx):
        os.chdir('src')
        if not os.path.exists('Makefile'):
            run(ctx, ['../src/configure',
                      '--prefix=' + self.path(ctx, 'install')])
        run(ctx, 'make -j%d' % ctx.jobs)

    def install(self, ctx):
        os.makedirs('install/bin', exist_ok=True)
        shutil.copy('src/wrk', 'install/bin')

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('src/wrk')

    def is_installed(self, ctx):
        return os.path.exists('install/bin/wrk')
