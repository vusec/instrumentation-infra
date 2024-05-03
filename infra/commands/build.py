import argparse

from ..command import Command, build_package, fetch_package, get_deps, install_package
from ..context import Context
from ..instance import Instance
from ..package import Package
from ..target import Target
from .clean import clean_package, clean_target


class BuildCommand(Command):
    @property
    def name(self) -> str:
        return "build"

    @property
    def description(self) -> str:
        return "build target programs and their dependencies"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        target_parsers = parser.add_subparsers(
            title="target",
            metavar="TARGET",
            dest="target",
            help=" | ".join([target.name for target in self.targets.all()]),
        )
        target_parsers.required = True

        for target in self.targets.all():
            tparser = target_parsers.add_parser(
                name=target.name,
                help=f"{self.name} configuration options for {target.name}",
                formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            )

            tparser.add_argument(
                "instances",
                nargs="+",
                metavar="INSTANCE",
                choices=[instance.name for instance in self.instances.all()],
                help=" | ".join([instance.name for instance in self.instances.all()]),
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

        def build_deps_once(obj: Instance | Target) -> None:
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
            instance.prepare_build(ctx)

            if not ctx.args.deps_only:
                ctx.log.info(f"building {target.name}-{instance.name}")
                if not ctx.args.dry_run:
                    target.goto_rootdir(ctx)
                    target.run_hooks_pre_build(ctx, instance)
                    target.build(ctx, instance, pool)
                    target.run_hooks_post_build(ctx, instance)

            instance.process_build(ctx)
            ctx = oldctx

        if pool:
            pool.wait_all()


class PkgBuildCommand(Command):
    @property
    def name(self) -> str:
        return "pkg-build"

    @property
    def description(self) -> str:
        return "build a single package and its dependencies"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        packagearg = parser.add_argument(
            "package",
            metavar="PACKAGE",
            choices=[package.ident() for package in self.packages.all()],
            help=" | ".join([package.ident() for package in self.packages.all()]),
        )
        setattr(packagearg, "completer", self.complete_package)

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
