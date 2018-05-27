import os
import io
import argparse
from os.path import exists, join
from abc import ABCMeta
from typing import Dict, List, Iterator, Any
from ..package import Package
from ..instance import Instance
from ..target import Target
from ..util import Namespace, FatalError, run


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
    :param target: the target that will be run
    """
    name = 'benchmark-utils'
    built = ['libbenchutils.a']
    installed = ['lib/libbenchutils.a']

    #: :class:`str` prefix for metadata lines in output files
    prefix = '[setup-report]'

    LoggedResult = Dict[str, Any]
    ParsedResult = Namespace

    def __init__(self, target: Target):
        self.target = target

    def add_report_args(self, parser: argparse.ArgumentParser):
        parser.add_argument('rundirs',
                nargs='+', metavar='RUNDIR', default=[],
                help='run directories to parse (results/run.XXX)')

    def configure(self, ctx: Namespace):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        :param ctx: the configuration context
        """
        ctx.ldflags += ['-L', self.path(ctx, 'install/lib'),
                        '-Wl,--whole-archive', '-l:libbenchutils.a',
                        '-Wl,--no-whole-archive']

    def outfile_path(self, ctx: Namespace, instance: Instance, benchmark: str):
        """
        :param ctx: the configuration context
        :param instance:
        :param benchmark:
        """
        rundir = ctx.starttime.strftime('run.%Y-%m-%d.%H-%M-%S')
        instancedir = os.path.join(ctx.paths.pool_results, rundir,
                                   self.target.name, instance.name)
        os.makedirs(instancedir, exist_ok=True)
        return os.path.join(instancedir, benchmark)

    def parse_logs(self, ctx: Namespace, instances: List[Instance],
                   args: argparse.Namespace, cache: bool = True
                   ) -> Dict[str, List[ParsedResult]]:
        """
        :param ctx: the configuration context
        :param instances:
        :param args:
        :param cache:
        """
        rundirs = []
        for d in args.rundirs:
            if not os.path.exists(d):
                raise FatalError('rundir %s does not exist' % d)
            rundirs.append(os.path.abspath(d))

        instance_names = [instance.name for instance in instances]
        instance_dirs = []
        results = dict((iname, []) for iname in instance_names)

        for rundir in rundirs:
            targetdir = os.path.join(rundir, self.target.name)
            if os.path.exists(targetdir):
                for instance in os.listdir(targetdir):
                    instancedir = os.path.join(targetdir, instance)
                    if os.path.isdir(instancedir):
                        if not instance_names or instance in instance_names:
                            instance_dirs.append((instance, instancedir))
            else:
                ctx.log.warning('rundir %s contains no results for target %s' %
                                (rundir, self.target.name))

        for iname, idir in instance_dirs:
            instance_results = results.setdefault(iname, [])

            for filename in sorted(os.listdir(idir)):
                path = os.path.join(idir, filename)
                cached = []

                if cache:
                    for result in self.parse_results(ctx, path):
                        if result.get('cached', False):
                            cached.append(result)

                if cached:
                    fresults = cached
                    ctx.log.debug('using cached results from ' + path)
                else:
                    fresults = []
                    ctx.log.debug('parsing outfile ' + path)
                    for result in self.target.parse_outfile(ctx, iname, path):
                        result['cached'] = False
                        fresults.append(result)

                    if cache:
                        ctx.log.debug('caching %d results' % len(fresults))
                        with open(path, 'a') as f:
                            for result in fresults:
                                self.log_result({**result, 'cached': True}, f)

                for result in fresults:
                    result['outfile'] = path

                instance_results += fresults

        return results

    @classmethod
    def log_result(cls, result: LoggedResult, ofile: io.TextIOWrapper):
        """
        :param result:
        :param ofile:
        """
        print(cls.prefix, 'begin', file=ofile)

        for key, value in result.items():
            print(cls.prefix, key + ':', _box_value(value), file=ofile)

        print(cls.prefix, 'end', file=ofile)

    @classmethod
    def parse_results(cls, ctx: Namespace, path: str) -> Iterator[ParsedResult]:
        """
        :param ctx: the configuration context
        :param path:
        """
        with open(path) as f:
            result = None

            for line in f:
                line = line.rstrip()
                if line.startswith(cls.prefix):
                    statement = line[len(cls.prefix) + 1:]
                    if statement == 'begin':
                        result = Namespace()
                    elif statement == 'end':
                        yield result
                        result = None
                    elif result is None:
                        ctx.log.error('ignoring %s statement outside of begin-end '
                                    'in %s' % (cls.prefix, path))
                    else:
                        name, value = statement.split(': ', 1)

                        if name in result:
                            ctx.log.warning('duplicate metadata entry for "%s" in '
                                            '%s, using the last one' % (name, path))

                        result[name] = _unbox_value(value)

        if result is not None:
            ctx.log.error('%s begin statement without end in %s' %
                          (cls.prefix, path))


def _box_value(value):
    return str(value)


def _unbox_value(value):
    # bool
    if value == 'True':
        return True
    if value == 'False':
        return False

    # int
    if value.isdigit():
        return int(value)

    # float
    try:
        return float(value)
    except ValueError:
        pass

    # string
    return value
