import os
from os.path import exists, join
from abc import ABCMeta
from ..package import Package
from ..util import run


class Tool(Package, metaclass=ABCMeta):
    def ident(self):
        return self.name

    def fetch(self, ctx):
        pass

    def build(self, ctx):
        self._run_make(ctx, '-j%d' % ctx.jobs)

    def install(self, ctx):
        self._run_make(ctx, 'install')

    def is_fetched(self, ctx):
        return True

    def is_built(self, ctx):
        return all(exists(join('obj', f)) for f in self.built)

    def is_installed(self, ctx):
        return all(exists(join('install', f)) for f in self.installed)

    def _run_make(self, ctx, *args):
        os.chdir(join(ctx.paths.infra, 'tools', self.name))
        run(ctx, [
            'make',
            'OBJDIR=' + self.path(ctx, 'obj'),
            'INSTALLDIR=' + self.path(ctx, 'install'),
            *args
        ])


class Nothp(Tool):
    """
    :identifier: nothp
    """
    name = 'nothp'
    built = ['nothp']
    installed = ['bin/nothp']


class BenchmarkUtils(Tool):
    """
    :identifier: benchmark-utils
    """
    name = 'benchmark-utils'
    built = ['libbenchutils.a']
    installed = ['lib/libbenchutils.a']

    def configure(self, ctx):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        :param ctx: the configuration context
        """
        ctx.ldflags += ['-L', self.path(ctx, 'install/lib'),
                        '-Wl,--whole-archive', '-l:libbenchutils.a',
                        '-Wl,--no-whole-archive']
