import os
from os.path import exists, join
from abc import ABCMeta
from ..commands.report import parse_results
from ..package import Package
from ..util import Namespace, run


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

    def _srcpath(self, ctx, *args):
        return join(ctx.paths.infra, 'tools', self.name, *args)

    def _run_make(self, ctx, *args):
        os.chdir(self._srcpath(ctx))
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


class RusageCounters(Tool):
    """
    Utility library for targets that want to measure resource counters:

    - memory (max resident set size)
    - page faults
    - I/O operations
    - context switches
    - runtime (esimated by gettimeofday in constructor+destructor)

    The target only needs to depend on this package and :func:`configure` it to
    link the static library which will then log a reportable result in a
    destructor. See :class:`SPEC2006` for a usage example.

    :identifier: rusage-counters
    """
    name = 'rusage-counters'
    built = ['librusagecounters.a']
    installed = ['lib/librusagecounters.a']

    #: :class:`dict` reportable fields (add to reportable fields of target)
    reportable_fields = {
        'maxrss':            'peak resident set size in KB',
        'page_faults':       'number of page faults',
        'io_operations':     'number of I/O operations',
        'context_switches':  'number of context switches',
        'estimated_runtime': 'benchmark runtime in seconds estimated by '
                             'rusage-counters constructor/destructor',
    }

    def configure(self, ctx):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``build`` method of a target to link in the static library.

        :param ctx: the configuration context
        """
        ctx.ldflags += ['-L' + self.path(ctx, 'install', 'lib'),
                        '-Wl,--whole-archive', '-l:librusagecounters.a',
                        '-Wl,--no-whole-archive']

    def pkg_config_options(self, ctx: Namespace):
        yield ('--includes', 'include path for reporting helpers',
               ['-I', self._srcpath(ctx)])
        yield from super().pkg_config_options(ctx)

    @staticmethod
    def parse_results(ctx: Namespace, path: str):
        """
        Parse any results containing counters by this package.

        :param ctx: the configuration context
        :param path: path to file to parse
        :returns: counter results
        """
        return parse_results(ctx, path, 'rusage-counters')
