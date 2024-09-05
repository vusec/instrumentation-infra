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
        self.enable_run_log(ctx)
        pool = self.make_pool(ctx)
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        ctx.log.info(f"Running {self.name} command on target {target}")
        ctx.log.info(f"Building target & dependencies with instances: {instances}")

        # First clean the target if configured
        if ctx.args.clean:
            clean_target(ctx, target)

        # Next, fetch all dependencies of the target and all instances
        for package in get_deps(target, *instances):
            fetch_package(ctx, package)

        # If also building the target (not just dependencies), also fetch the target
        if ctx.args.deps_only:
            ctx.log.info(f"Not fetching target; only building dependencies of {target.name}")
        elif target.is_fetched(ctx):
            ctx.log.info(f"Target found; not re-fetching: {target.name}")
        else:
            ctx.log.info(f"Target not found; fetching: {target.name}")
            target.goto_rootdir(ctx)
            target.fetch(ctx)

        # For each instance, call its configuration method and build the its & the target's
        # dependencies; if not only building dependencies, also build the target
        for instance in instances:
            ctx.log.info(f"Building dependencies of {target.name} and {instance.name}")

            # Create a clean copy of the current context; then call the instance's configuration function
            original_ctx = ctx.copy()
            instance.configure(ctx)

            # Get unique (depth-first search) list of dependencies of instance & target;
            # build, install, and load them into the current configuration context
            for dep in get_deps(instance, target):
                ctx.log.info(f"Processing dependency: {dep}")
                build_package(ctx, dep, ctx.args.force_rebuild_deps)
                install_package(ctx, dep, ctx.args.force_rebuild_deps)

            # If the current run should only build dependencies or is only a dry-run, don't
            # actually run the build preparations or hooks nor build the target itself
            if ctx.args.deps_only:
                ctx.log.info(f"Only building & installing dependencies of {target.name} and {instance.name}; skipping")
            elif ctx.args.dry_run:
                ctx.log.warning(f"Dry-run: not running hooks or target build for {target.name} and {instance.name}")
            else:
                ctx.log.info(f"Running build sequence for {target.name} with instance {instance.name}")

                # Go to the target's root directory & run the instance's build preparation & pre-build hooks
                target.goto_rootdir(ctx)
                instance.prepare_build(ctx)
                target.run_hooks_pre_build(ctx, instance)

                # Build the target itself; if a parallel processing pool was used, wait for all of them to finish
                target.build(ctx, instance, pool)
                if pool is not None:
                    pool.wait_all()

                # Run the instance's post-build hooks and build post-processing function
                target.run_hooks_post_build(ctx, instance)
                instance.process_build(ctx)

            # Finish the build by restoring the original configuration context and processing the next instance
            ctx.log.info(f"Build of {target.name} finished ({instance.name}); restoring configuration context")
            ctx = original_ctx


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
            fetch_package(ctx, package)

        fetch_package(ctx, main_package)

        for package in deps:
            fetch_package(ctx, package)

        for package in deps:
            build_package(ctx, package, force_deps)
            install_package(ctx, package, force_deps)

        build_package(ctx, main_package, True)
