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
        run(ctx, ['git', 'clone', '--branch', 'v' + self.version,
                'https://github.com/eliben/pyelftools.git', 'src'])

    def build(self, ctx):
        os.chdir('src')
        run(ctx, [self.python.binary(), 'setup.py', 'build'])

    def install(self, ctx):
        os.chdir('src')
        run(ctx, [self.python.binary(),
                  'setup.py', 'install', '--skip-build',
                  '--prefix=' + self.path(ctx, 'install')])

    def install_env(self, ctx):
        relpath = 'install/lib/python%s/site-packages' % self.python.version
        abspath = self.path(ctx, relpath)
        pypath = os.getenv('PYTHONPATH', '').split(':')
        ctx.runenv.setdefault('PYTHONPATH', pypath).insert(0, abspath)

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('src/build')

    def is_installed(self, ctx):
        return os.path.exists('install/lib/%s/site-packages/elftools' %
                              self.python.binary())
