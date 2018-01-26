import os
from ..package import Package
from ..util import run
from .python import Python


class PyElfTools(Package):
    def __init__(self, version, python_version):
        self.version = version
        self.python = Python(python_version)

    def ident(self):
        return 'pyelftools-' + self.version

    def dependencies(self):
        yield self.python

    def fetch(self, ctx):
        os.chdir(ctx.paths.packsrc)
        run(ctx, ['git', 'clone', '--branch', 'v' + self.version,
                'https://github.com/eliben/pyelftools.git', self.ident()])

    def build(self, ctx):
        os.chdir(self.path(ctx, 'src'))
        run(ctx, [self.python.binary(), 'setup.py', 'build'])

    def install(self, ctx):
        os.chdir(self.path(ctx, 'src', 'build'))
        run(ctx, [self.python.binary(),
                'setup.py', 'install', '--skip-build',
                '--prefix=' + self.path(ctx, 'install')])

    def is_fetched(self, ctx):
        return os.path.exists(self.path(ctx, 'src'))

    def is_built(self, ctx):
        return os.path.exists(self.path(ctx, 'src', 'build'))

    def is_installed(self, ctx):
        return os.path.exists(self.path(ctx, 'install',
            'lib', self.python.binary(), 'site-packages', 'elftools'))
