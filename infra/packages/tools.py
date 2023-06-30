import os
from os.path import exists, join
from abc import ABCMeta, abstractmethod
from typing import List, Iterator, Mapping
from ..context import Context
from ..commands.report import parse_results
from ..package import Package, PkgConfigOption
from ..util import FatalError, run, ResultDict


class Tool(Package, metaclass=ABCMeta):
    name: str
    built: List[str]
    installed: List[str]

    def ident(self) -> str:
        return self.name

    def fetch(self, ctx: Context) -> None:
        pass

    def build(self, ctx: Context) -> None:
        self._run_make(ctx, f'-j{ctx.jobs}')

    def install(self, ctx: Context) -> None:
        self._run_make(ctx, 'install')

    def is_fetched(self, ctx: Context) -> bool:
        return True

    def is_built(self, ctx: Context) -> bool:
        return all(exists(join('obj', f)) for f in self.built)

    def is_installed(self, ctx: Context) -> bool:
        return all(exists(join('install', f)) for f in self.installed)

    def _srcpath(self, ctx: Context, *args: str) -> str:
        return join(ctx.paths.infra, 'tools', self.name, *args)

    def _run_make(self, ctx: Context, *args: str) -> None:
        os.chdir(self._srcpath(ctx))
        run(ctx, [
            'make',
            'OBJDIR=' + self.path(ctx, 'obj'),
            'INSTALLDIR=' + self.path(ctx, 'install'),
            *args
        ])


class ReportableTool(Tool):
    """
    Tool that adds reportable run-time statistics.

    For example, a wrapper program, or a static library linked into the target.

    Tools that derive from this class must specify all fields that may be
    reported at runtime. By default logfiles are parsed using
    :func:`parse_results`, searching for the section name corresponding to
    `cls.name`.

    """
    @staticmethod
    @abstractmethod
    def reportable_fields() -> Mapping[str, str]:
        pass

    @classmethod
    def parse_results(cls, ctx: Context, path: str, allow_missing: bool = True) \
            -> ResultDict:
        """
        Parse any results containing counters by this package.

        :param ctx: the configuration context
        :param path: path to file to parse
        :returns: counter results
        """
        all_results = list(parse_results(ctx, path, cls.name))
        if not all_results:
            if allow_missing:
                return {}
            else:
                raise FatalError(f'Failure while parsing results: required '
                                 f'reporter {cls.name} is missing from logs '
                                 f'at {path}.')

        aggregated_results: ResultDict = {}
        for results in all_results:
            for counter, value in results.items():
                assert isinstance(value, (int, float))
                aggrval = aggregated_results.get(counter, 0)
                assert isinstance(aggrval, (int, float))
                aggrval += value
                aggregated_results[counter] = aggrval

        return aggregated_results


class Nothp(Tool):
    """
    :identifier: nothp
    """
    name = 'nothp'
    built = ['nothp']
    installed = ['bin/nothp']


class RusageCounters(ReportableTool):
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

    @staticmethod
    def reportable_fields() -> Mapping[str, str]:
        return {
            'maxrss':            'peak resident set size in KB',
            'page_faults':       'number of page faults',
            'io_operations':     'number of I/O operations',
            'context_switches':  'number of context switches',
            'estimated_runtime': 'benchmark runtime in seconds estimated by '
                                 'rusage-counters constructor/destructor',
        }

    @classmethod
    def parse_results(cls, ctx: Context, path: str, allow_missing: bool = False) \
            -> ResultDict:
        return super().parse_results(ctx, path, allow_missing)

    def configure(self, ctx: Context) -> None:
        """
        Set build/link flags in **ctx**. Should be called from the
        ``build`` method of a target to link in the static library.

        :param ctx: the configuration context
        """
        ctx.ldflags += ['-L' + self.path(ctx, 'install', 'lib'),
                        '-Wl,--whole-archive', '-l:librusagecounters.a',
                        '-Wl,--no-whole-archive']

    def pkg_config_options(self, ctx: Context) -> Iterator[PkgConfigOption]:
        yield ('--includes', 'include path for reporting helpers',
               ['-I', self._srcpath(ctx)])
        yield from super().pkg_config_options(ctx)

