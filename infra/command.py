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
    """Iterates over the dependencies of all given objects (instances, packages, or targets) in a
    depth-first manner (i.e. the deepest dependency will be at the head of the returned list) such
    that building can happen in order of dependency.

    Note that if dependencies are shared among the initial objects, they are returned in the order
    they are encountered (i.e. if the first object has a shared dependency higher in the dependency-
    -chain than a subsequent object, the dependency will be returned at the level it is first seen)

    :return list[Package]: a list of all dependencies in depth-first order
    """
    seen: set[Package] = set()
    deps: list[Package] = list()

    def _add_deps(pkg: Package) -> None:
        if pkg in seen:
            return
        seen.add(pkg)

        for dep in pkg.dependencies():
            _add_deps(dep)
        deps.append(pkg)

    for obj in objs:
        for dep in obj.dependencies():
            _add_deps(dep)

    return deps


def load_deps(ctx: Context, *objs: Instance | Package | Target) -> None:
    """For all dependencies of the given object(s), load/install them into the
    running environment so they can be used while building the given objects

    :param Context ctx: the configuration context
    :param Instance | Package | Target object: the object whose dependencies to load
    """
    for obj in objs:
        ctx.log.info(f"Loading dependencies of {obj.ident() if isinstance(obj, Package) else obj.name}")
        for dep in get_deps(obj):
            dep.goto_rootdir(ctx)
            dep.install_env(ctx)


def fetch_target(ctx: Context, target: Target) -> None:
    """If the target hasn't been fetched yet (i.e. :fun:`target.is_fetched(ctx)` returns `False`),
    this function will call the target's :fun:`target.fetch(ctx)` function to fetch the target

    :param Context ctx: the configuration context
    :param Target target: the target to possibly fetch
    """
    target.goto_rootdir(ctx)

    if target.is_fetched(ctx):
        ctx.log.debug(f"Target {target.name} is already fetched; skipping")
    else:
        if ctx.args.dry_run:
            ctx.log.warning(f"Only running as a dry-run; not fetching target: {target.name}")
        else:
            ctx.log.info(f"Target {target.name} not found; fetching")


def fetch_package(ctx: Context, package: Package) -> None:
    """If the package hasn't been fetched yet (i.e. :fun:`package.is_fetched(ctx)` returns `False`),
    this function will call the package's :fun:`package.fetch(ctx)` function to fetch the package

    :param Context ctx: the configuration context
    :param Package package: the package to possibly fetch
    """
    package.goto_rootdir(ctx)

    if not package.is_fetched(ctx):
        ctx.log.info(f"Package {package.ident()} is not found; fetching")
        package.fetch(ctx)
    else:
        ctx.log.debug(f"Package {package.ident()} is already fetched; skipping")


def build_package(ctx: Context, package: Package, force_rebuild: bool = False) -> None:
    """Checks if the given package should be rebuilt, and if so, rebuilds the package using the
    current configuration context. Forcing a rebuild can be done with :param:`force_rebuild`

    :param Context ctx: the configuration context
    :param Package package: the package to possibly build
    :param bool force_rebuild: always build the package, even if already built, defaults to False
    """
    package.goto_rootdir(ctx)

    if not package.is_built(ctx):
        ctx.log.info(f"Package {package.ident()} is not built; building")
        package.build(ctx)
    elif force_rebuild:
        ctx.log.warning(f"Forcing rebuilds enabled; building {package.ident()}")
        package.build(ctx)
    else:
        ctx.log.debug(f"Package {package.ident()} is already built; skipping")


def install_package(ctx: Context, package: Package, force_rebuild: bool = False) -> None:
    """Checks if the given package should be installed, and if so, installs the package using the
    current configuration context. If the rebuilding was forced (with :param:`force_rebuild`),
    the package will also be re-installed.

    Note that this function also installs the package into the configuration context's environment
    by calling :fun:`package.install_env(ctx)` on the package (even if not (re-)installed)

    :param Context ctx: the configuration context
    :param Package package: the package to possibly install
    :param bool force_rebuild: if the package was rebuilt, also re-install it, defaults to False
    """
    package.goto_rootdir(ctx)

    if not package.is_installed(ctx):
        ctx.log.info(f"Package {package.ident()} is not installed; installing")
        package.install(ctx)
    elif force_rebuild:
        ctx.log.warning(f"Forcing rebuilds enabled; installing {package.ident()}")
        package.install(ctx)
    else:
        ctx.log.debug(f"Package {package.ident()} is already installed; skipping")

    ctx.log.info(f"Installing package {package.ident()} into configuration environment")
    package.install_env(ctx)
