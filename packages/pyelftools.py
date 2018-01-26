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
        if not os.path.exists(self.src_path(ctx)) and not self.installed(ctx):
            os.chdir(ctx.paths.packsrc)
            run(['git', 'clone', '--branch', 'v' + self.version,
                 'https://github.com/eliben/pyelftools.git', self.ident()])

    def build(self, ctx):
        if not os.path.exists(self.build_path(ctx)) and not self.installed(ctx):
            os.chdir(self.src_path(ctx))
            run([self.python.binary(), 'setup.py', 'build'])

    def install(self, ctx):
        if not self.installed(ctx):
            os.chdir(self.build_path(ctx))
            run([self.python.binary(),
                 'setup.py', 'install', '--skip-build',
                 '--prefix=' + self.install_path(ctx)])

    def build_path(self, ctx):
        return self.src_path(ctx, 'build')

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx,
            'lib', self.python.binary(), 'site-packages', 'elftools'))
