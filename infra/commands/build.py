import argparse
import os
from multiprocessing import cpu_count
from typing import Union

from ..command import Command, get_deps
from ..context import Context
from ..instance import Instance
from ..package import Package
from ..target import Target
from ..util import FatalError
from .clean import clean_package, clean_target

default_jobs = min(cpu_count(), 64)


class BuildCommand(Command):
    name = "build"
    description = "build target programs and their dependencies"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        target_parsers = parser.add_subparsers(
            title="target",
            metavar="TARGET",
            dest="target",
            help=" | ".join(self.targets),
        )
        target_parsers.required = True

        for name, target in self.targets.items():
            tparser = target_parsers.add_parser(
                name=target.name,
                help=f"{self.name} configuration options for {target.name}",
                formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            )

            tparser.add_argument(
                "instances",
                nargs="+",
                metavar="INSTANCE",
                choices=self.instances,
                help=" | ".join(self.instances),
            )
            tparser.add_argument(
                "-j",
                "--jobs",
                type=int,
                default=default_jobs,
                help=f"maximum number of build processes (default {default_jobs})",
            )
            tparser.add_argument(
                "--deps-only",
                action="store_true",
                help="only build dependencies, not targets themselves",
            )
            tparser.add_argument(
                "--force-rebuild-deps",
                action="store_true",
                help="always run the build commands",
            )
            tparser.add_argument(
                "--clean",
                action="store_true",
                help="clean target first",
            )
            tparser.add_argument(
                "--dry-run",
                action="store_true",
                help="don't actually build anything, just show what will be done",
            )

            self.add_pool_args(tparser)
            target.add_build_args(tparser)

            for instance in self.instances.values():
                instance.add_build_args(tparser)

    def run(self, ctx: Context) -> None:
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        pool = self.make_pool(ctx)

        deps = get_deps(target, *instances)

        self.enable_run_log(ctx)

        # clean target if requested
        if ctx.args.clean:
            clean_target(ctx, target)

        # first fetch all necessary code so that the internet connection can be
        # broken during building
        for package in deps:
            fetch_package(ctx, package, ctx.args.force_rebuild_deps)

        if not ctx.args.deps_only:
            target.goto_rootdir(ctx)
            if target.is_fetched(ctx):
                ctx.log.debug(f"{target.name} already fetched, skip")
            else:
                ctx.log.info(f"fetching {target.name}")
                target.fetch(ctx)

        cached_deps = {obj: get_deps(obj) for obj in [target] + instances}

        built_packages = set()

        def build_package_once(package: Package, force: bool) -> None:
            if package not in built_packages:
                build_package(ctx, package, force)
                install_package(ctx, package, force)
                built_packages.add(package)

        def build_deps_once(obj: Union[Instance, Target]) -> None:
            for package in cached_deps[obj]:
                force = ctx.args.force_rebuild_deps
                build_package_once(package, force)
                ctx.log.debug(f"install {package.ident()} in env")
                package.install_env(ctx)

        if ctx.args.deps_only and not instances:
            oldctx = ctx.copy()
            build_deps_once(target)
            ctx = oldctx

        for instance in instances:
            # use a copy of the context for instance configuration to avoid
            # stacking configurations between instances
            # FIXME: only copy the build env (the part that changes)
            oldctx = ctx.copy()
            instance.configure(ctx)
            build_deps_once(instance)
            build_deps_once(target)

            if not ctx.args.deps_only:
                ctx.log.info(f"building {target.name}-{instance.name}")
                if not ctx.args.dry_run:
                    target.goto_rootdir(ctx)
                    target.run_hooks_pre_build(ctx, instance)
                    target.build(ctx, instance, pool)
                    target.run_hooks_post_build(ctx, instance)

            ctx = oldctx

        if pool:
            pool.wait_all()


# This command does not appear in main --help usage because it is meant to be
# used as a callback for build scripts
class ExecHookCommand(Command):
    name = "exec-hook"
    description = ""

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "hooktype",
            choices=["pre-build", "post-build"],
            help="hook type",
        )
        parser.add_argument(
            "instance",
            metavar="INSTANCE",
            choices=self.instances,
            help=" | ".join(self.instances),
        )
        parser.add_argument(
            "targetfile",
            metavar="TARGETFILE",
            help="file to run hook on",
        )

    def run(self, ctx: Context) -> None:
        instance = self.instances[ctx.args.instance]
        ctx.args.dry_run = False

        absfile = os.path.abspath(ctx.args.targetfile)
        if not os.path.exists(absfile):
            raise FatalError(f"file {absfile} does not exist")

        hooktype = ctx.args.hooktype.replace("-", "_")
        assert hasattr(ctx.hooks, hooktype)

        # don't build packages (should have been done already since this
        # command should only be called recursively), just load them
        load_deps(ctx, instance)

        # populate ctx.hooks[hooktype]
        instance.configure(ctx)

        # run hooks
        basedir = os.path.dirname(absfile)
        for hook in getattr(ctx.hooks, hooktype):
            os.chdir(basedir)
            hook(ctx, absfile)


class PkgBuildCommand(Command):
    name = "pkg-build"
    description = "build a single package and its dependencies"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        packagearg = parser.add_argument(
            "package",
            metavar="PACKAGE",
            choices=self.packages,
            help=" | ".join(self.packages),
        )
        setattr(packagearg, "completer", self.complete_package)

        parser.add_argument(
            "-j",
            "--jobs",
            type=int,
            default=default_jobs,
            help=f"maximum number of build processes (default {default_jobs})",
        )
        parser.add_argument(
            "--force-rebuild-deps",
            action="store_true",
            help="always run the build commands",
        )
        parser.add_argument(
            "--clean",
            action="store_true",
            help="clean package first",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="don't actually build anything, just show what will be done",
        )

    def run(self, ctx: Context) -> None:
        main_package = self.packages[ctx.args.package]

        deps = get_deps(main_package)
        force_deps = ctx.args.force_rebuild_deps

        self.enable_run_log(ctx)

        if ctx.args.clean:
            clean_package(ctx, main_package)

        for package in deps:
            fetch_package(ctx, package, force_deps)

        fetch_package(ctx, main_package, True)

        for package in deps:
            fetch_package(ctx, package, force_deps)

        for package in deps:
            build_package(ctx, package, force_deps)
            install_package(ctx, package, force_deps)

        build_package(ctx, main_package, True)


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


def load_deps(ctx: Context, obj: Union[Target, Instance, Package]) -> None:
    for package in get_deps(obj):
        load_package(ctx, package)
