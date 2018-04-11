import os
import shutil
from abc import ABCMeta, abstractmethod
from ..package import Package
from ..util import run


class Nothp(Package):
    def ident(self):
        return 'nothp'

    def fetch(self, ctx):
        pass

    def build(self, ctx):
        self.run_make(ctx, '-j%d' % ctx.jobs)

    def install(self, ctx):
        self.run_make(ctx, 'install')

    def is_fetched(self, ctx):
        return True

    def is_built(self, ctx):
        return os.path.exists('obj/nothp')

    def is_installed(self, ctx):
        return os.path.exists('install/nothp')

    def run_make(self, ctx, *args):
        os.chdir(ctx.paths.tools + '/nothp')
        run(ctx, [
            'make',
            'OBJDIR=' + self.path(ctx, 'obj'),
            'INSTALLDIR=' + self.path(ctx, 'install'),
            *args
        ])
