import argparse
import os

from ..command import Command, load_deps
from ..context import Context
from .build import BuildCommand


class RunCommand(Command):
    @property
    def name(self) -> str:
        return "run"

    @property
    def description(self) -> str:
        return "run a single target program"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        target_parsers = parser.add_subparsers(
            title="target",
            metavar="TARGET",
            dest="target",
            help=" | ".join([target.name for target in self.targets.all()]),
        )
        target_parsers.required = True

        for name, target in self.targets.items():
            tparser = target_parsers.add_parser(
                name=name,
                help=f"configuration options for running {target.name}",
                formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            )
            setattr(tparser, "completer", self.complete_package)

            tparser.add_argument(
                "instances",
                nargs="+",
                metavar="INSTANCE",
                choices=[instance.name for instance in self.instances.all()],
                help=" | ".join([instance.name for instance in self.instances.all()]),
            )
            tparser.add_argument(
                "--build",
                action="store_true",
                help="build target first (no custom target/instance arguments)",
            )
            tparser.add_argument(
                "--force-rebuild-deps",
                action="store_true",
                help="force rebuilding of dependencies (implies --build)",
            )
            tparser.add_argument(
                "-i",
                "--iterations",
                metavar="ITERATIONS",
                type=int,
                default=1,
                help="number of runs per benchmark",
            )

            self.add_pool_args(tparser)
            target.add_run_args(tparser)

            for instance in self.instances.values():
                # Run can be called with --build/from a hook so also add build args
                instance.add_build_args(tparser)
                instance.add_run_args(tparser)

    def run(self, ctx: Context) -> None:
        ctx.args.dry_run = False
        instances = self.instances.select(ctx.args.instances)
        target = self.targets[ctx.args.target]
        pool = self.make_pool(ctx)
        self.enable_run_log(ctx)

        # If build flag is set, make backup of context & call plain build command
        oldctx = ctx.copy()
        if ctx.args.build or ctx.args.force_rebuild_deps:
            ctx.args.targets = [ctx.args.target]
            ctx.args.packages = []
            ctx.args.deps_only = False
            ctx.args.clean = False
            ctx.args.relink = False
            build_command = BuildCommand()
            build_command.instances = self.instances
            build_command.targets = self.targets
            build_command.packages = self.packages
            build_command.run(ctx)
        ctx = oldctx

        # Load the dependencies of the target & then backup the context again
        load_deps(ctx, target)
        orig_cwd = os.getcwd()
        orig_ctx = ctx.copy()

        for instance in instances:
            ctx.log.info(f"running {target.name}-{instance.name}")

            # Ensure all dependencies of the instance are loaded before running
            load_deps(ctx, instance)
            instance.configure(ctx)
            instance.prepare_run(ctx)

            # Run the hooks & target run function
            target.goto_rootdir(ctx)
            target.run_hooks_pre_run(ctx, instance)
            target.run(ctx, instance, pool)
            target.run_hooks_post_run(ctx, instance)
            os.chdir(orig_cwd)

            # Process the run & restore the backed up configuration
            instance.process_run(ctx)
            os.chdir(orig_cwd)
            ctx = orig_ctx

        if pool:
            pool.wait_all()
