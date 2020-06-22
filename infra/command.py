import os
import shlex
from abc import ABCMeta, abstractmethod
from argparse import ArgumentParser
from collections import OrderedDict
from inspect import signature
from multiprocessing import cpu_count
from .parallel import ProcessPool, SSHPool, PrunPool
from .util import FatalError, Namespace, Index, param_attrs


class Command(metaclass=ABCMeta):
    name = ''
    description = ''

    _max_default_jobs = 16

    @param_attrs
    def set_maps(self, instances: Index, targets: Index, packages: Index):
        pass

    @abstractmethod
    def add_args(self, parser: ArgumentParser):
        pass

    @abstractmethod
    def run(self, ctx: Namespace):
        pass

    def enable_run_log(self, ctx):
        os.chdir(ctx.paths.root)
        ctx.runlog = open(ctx.paths.runlog, 'w')

    def add_pool_args(self, parser):
        parser.add_argument('--parallel', choices=('proc', 'ssh', 'prun'),
                default=None,
                help='build benchmarks in parallel ("proc" for local '
                     'processes, "prun" for DAS cluster)')
        parser.add_argument('--parallelmax', metavar='PROCESSES_OR_NODES',
                type=int, default=None,
                help='limit simultaneous node reservations (default: %d for '
                     'proc, 64 for prun)' % cpu_count())
        parser.add_argument('--ssh-nodes', nargs='+', default='',
                help='ssh remotes to run jobs on (for --parallel=ssh)')
        parser.add_argument('--prun-opts', default='',
                help='additional options for prun (for --parallel=prun)')

    def make_pool(self, ctx):
        prun_opts = shlex.split(ctx.args.prun_opts)

        if ctx.args.parallel == 'proc':
            if len(prun_opts):
                raise FatalError('--prun-opts not supported for --parallel=proc')
            if ctx.args.ssh_nodes:
                raise FatalError('--ssh-nodes not supported for --parallel=proc')
            pmax = cpu_count() if ctx.args.parallelmax is None \
                   else ctx.args.parallelmax
            return ProcessPool(ctx.log, pmax)

        if ctx.args.parallel == 'ssh':
            if len(prun_opts):
                raise FatalError('--prun-opts not supported for --parallel=ssh')
            if not ctx.args.ssh_nodes:
                raise FatalError('--ssh-nodes required for --parallel=ssh')
            pmax = len(ctx.args.ssh_nodes) if ctx.args.parallelmax is None \
                   else ctx.args.parallelmax
            return SSHPool(ctx, ctx.log, pmax, ctx.args.ssh_nodes)

        if ctx.args.parallel == 'prun':
            if ctx.args.ssh_nodes:
                raise FatalError('--ssh-nodes not supported for --parallel=prun')
            pmax = 64 if ctx.args.parallelmax is None else ctx.args.parallelmax
            return PrunPool(ctx.log, pmax, prun_opts)

        if ctx.args.parallelmax:
            raise FatalError('--parallelmax not supported for --parallel=none')
        if len(prun_opts):
            raise FatalError('--prun-opts not supported for --parallel=none')

    def call_with_pool(self, fn, args, pool):
        # FIXME: this is dirty and could be improved by having a default
        # DummyPool that runs stuff locally
        if len(signature(fn).parameters) == len(args) + 1:
            fn(*args, pool)
            return True
        if pool:
            return False
        fn(*args)
        return True

    def complete_package(self, prefix, parsed_args, **kwargs):
        for package in get_deps(*self.targets.all(), *self.instances.all()):
            name = package.ident()
            if name.startswith(prefix):
                yield name


def get_deps(*objs):
    deps = OrderedDict()

    def add_dep(dep, visited):
        if dep in visited:
            raise FatalError('recursive dependency %s' % dep)
        visited.add(dep)

        for nested_dep in dep.dependencies():
            add_dep(nested_dep, set(visited))

        deps.setdefault(dep, True)

    for obj in objs:
        for dep in obj.dependencies():
            add_dep(dep, set())

    return list(deps)
