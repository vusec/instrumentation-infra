import os
import shlex
import argparse

from abc import ABCMeta, abstractmethod
from typing import Any, Iterator, MutableMapping
from argparse import ArgumentParser
from collections import OrderedDict
from multiprocessing import cpu_count

from .context import Context
from .instance import Instance
from .package import Package
from .parallel import Pool, ProcessPool, PrunPool, SSHPool
from .target import Target
from .util import FatalError, Index


class Command(metaclass=ABCMeta):
    @property
    @abstractmethod
    def name(self) -> str:
        """Returns this command's name. Should be unique."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Returns a description of this command's behaviour."""
        pass

    targets: Index[Target]
    instances: Index[Instance]
    packages: Index[Package]

    @abstractmethod
    def add_args(self, parser: ArgumentParser) -> None:
        pass

    @abstractmethod
    def run(self, ctx: Context) -> None:
        pass

    def enable_run_log(self, ctx: Context) -> None:
        os.chdir(ctx.paths.root)
        ctx.runlog_file = open(ctx.paths.runlog, "w")

    def add_pool_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--parallel",
            choices=("proc", "ssh", "prun"),
            default=None,
            help=('build benchmarks in parallel ("proc" for local ' 'processes, "prun" for DAS cluster)'),
        )
        parser.add_argument(
            "--parallelmax",
            metavar="PROCESSES_OR_NODES",
            type=int,
            default=None,
            help=(f"limit simultaneous node reservations (default: {cpu_count()} " "for proc, 64 for prun)"),
        )
        parser.add_argument(
            "--ssh-nodes",
            nargs="+",
            default="",
            help="ssh remotes to run jobs on (for --parallel=ssh)",
        )
        parser.add_argument(
            "--prun-opts",
            default="",
            help="additional options for prun (for --parallel=prun)",
        )

    def make_pool(self, ctx: Context) -> Pool | None:
        prun_opts = shlex.split(ctx.args.prun_opts)

        if ctx.args.parallel == "proc":
            if len(prun_opts):
                raise FatalError("--prun-opts not supported for --parallel=proc")
            if ctx.args.ssh_nodes:
                raise FatalError("--ssh-nodes not supported for --parallel=proc")
            pmax = cpu_count() if ctx.args.parallelmax is None else ctx.args.parallelmax
            return ProcessPool(ctx.log, pmax)

        if ctx.args.parallel == "ssh":
            if len(prun_opts):
                raise FatalError("--prun-opts not supported for --parallel=ssh")
            if not ctx.args.ssh_nodes:
                raise FatalError("--ssh-nodes required for --parallel=ssh")
            pmax = len(ctx.args.ssh_nodes) if ctx.args.parallelmax is None else ctx.args.parallelmax
            return SSHPool(ctx, ctx.log, pmax, ctx.args.ssh_nodes)

        if ctx.args.parallel == "prun":
            if ctx.args.ssh_nodes:
                raise FatalError("--ssh-nodes not supported for --parallel=prun")
            pmax = 64 if ctx.args.parallelmax is None else ctx.args.parallelmax
            return PrunPool(ctx.log, pmax, prun_opts)

        if ctx.args.parallelmax:
            raise FatalError("--parallelmax not supported for --parallel=none")
        if len(prun_opts):
            raise FatalError("--prun-opts not supported for --parallel=none")
        return None

    def complete_package(self, prefix: str, parsed_args: argparse.Namespace, **kwargs: Any) -> Iterator[str]:
        for package in get_deps(*self.targets.all(), *self.instances.all()):
            name = package.ident()
            if name.startswith(prefix):
                yield name


def get_deps(*objs: Instance | Package | Target) -> list[Package]:
    deps: MutableMapping[Package, bool] = OrderedDict()

    def add_dep(dep: Package, visited: set[Package]) -> None:
        if dep in visited:
            raise FatalError(f"recursive dependency {dep}")
        visited.add(dep)

        for nested_dep in dep.dependencies():
            add_dep(nested_dep, set(visited))

        deps.setdefault(dep, True)

    for obj in objs:
        for dep in obj.dependencies():
            add_dep(dep, set())

    return list(deps)


def fetch_package(ctx: Context, package: Package, force_rebuild: bool) -> None:
    package.goto_rootdir(ctx)

    if package.is_fetched(ctx):
        ctx.log.debug(f"{package.ident()} already fetched, skip")
    elif not force_rebuild and package.is_installed(ctx):
        ctx.log.debug(f"{package.ident()} already installed, skip fetching")
    else:
        ctx.log.info(f"fetching {package.ident()}")
        if not ctx.args.dry_run:
            package.goto_rootdir(ctx)
            package.fetch(ctx)


def build_package(ctx: Context, package: Package, force_rebuild: bool) -> None:
    package.goto_rootdir(ctx)
    built = package.is_built(ctx)

    if not force_rebuild:
        if built:
            ctx.log.debug(f"{package.ident()} already built, skip")
            return
        if package.is_installed(ctx):
            ctx.log.debug(f"{package.ident()} already installed, skip building")
            return

    load_deps(ctx, package)

    force = " (forced rebuild)" if force_rebuild and built else ""
    ctx.log.info(f"building {package.ident()}" + force)
    if not ctx.args.dry_run:
        package.goto_rootdir(ctx)
        package.build(ctx)


def install_package(ctx: Context, package: Package, force_rebuild: bool) -> None:
    package.goto_rootdir(ctx)
    installed = package.is_installed(ctx)

    if not force_rebuild and installed:
        ctx.log.debug(f"{package.ident()} already installed, skip")
    else:
        force = " (forced reinstall)" if force_rebuild and installed else ""
        ctx.log.info(f"installing {package.ident()}" + force)
        if not ctx.args.dry_run:
            package.goto_rootdir(ctx)
            package.install(ctx)

    package.goto_rootdir(ctx)


def load_package(ctx: Context, package: Package) -> None:
    ctx.log.debug(f"install {package.ident()} into env")
    if not ctx.args.dry_run:
        package.install_env(ctx)


def load_deps(ctx: Context, *objs: Target | Instance | Package) -> None:
    ctx.log.info(f"Loading deps of {len(objs)} objects")
    for obj in objs:
        ctx.log.info(f"Loading dependencies of {obj.ident if isinstance(obj, Package) else obj.name}")
        for package in get_deps(obj):
            load_package(ctx, package)
