import argparse

from ..command import Command
from ..context import Context
from .build import BuildCommand, default_jobs, load_deps


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
                "-j",
                "--jobs",
                type=int,
                default=default_jobs,
                help=f"maximum number of build processes (default {default_jobs})",
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

    def run(self, ctx: Context) -> None:
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        pool = self.make_pool(ctx)

        self.enable_run_log(ctx)

        ctx.args.dry_run = False
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
        load_deps(ctx, target)

        for instance in instances:
            oldctx = ctx.copy()
            ctx.log.info(f"running {target.name}-{instance.name}")

            load_deps(ctx, instance)
            instance.prepare_run(ctx)
            target.goto_rootdir(ctx)

            target.run(ctx, instance, pool)

            ctx = oldctx

        if pool:
            pool.wait_all()
