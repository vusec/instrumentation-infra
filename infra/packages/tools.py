import os
from os.path import exists, join
from abc import ABCMeta
from ..commands.report import parse_results
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


# TODO: rename BenchmarkUtils to RusageCounters

class BenchmarkUtils(Tool):
    """
    Utility class for the :ref:`report <usage-report>` command. Should be
    generated by :func:`Target.dependencies` by a target that uses the
    utilities. See :class:`SPEC2006` for an example.

    Defines a static library to be linked into the target binary. The library
    prints some usage data such as memory usage to ``stderr``, prefixed by
    ``[setup-report]``. These numbers can be aggregated at report time.

    The package also defines a number of utility methods for reading and
    writing metadata to/from log files in the ``results/`` directory after a
    parallel benchmark run. Metadata is organised in "results" with each result
    being a dictionary of properties corresponding to a program invocation.

    Note that :func:`parse_logs`, which is used for reporting, requires the
    ``parse_outfile()`` method to be implemented by the target.

    :identifier: benchmark-utils
    """
    name = 'benchmark-utils'
    built = ['libbenchutils.a']
    installed = ['lib/libbenchutils.a']

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
        ctx.ldflags += ['-L', self.path(ctx, 'install/lib'),
                        '-Wl,--whole-archive', '-l:libbenchutils.a',
                        '-Wl,--no-whole-archive']

    def pkg_config_options(self, ctx):
        yield ('--includes', 'include path for reporting helpers',
               ['-I', self._srcpath(ctx)])
        yield from super().pkg_config_options(ctx)

    @staticmethod
    def parse_rusage_counters(ctx, path):
        return parse_results(ctx, path, 'rusage-counters')
