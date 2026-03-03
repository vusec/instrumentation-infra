import copy
import datetime
import os
import random
import argparse

from ..command import Command, load_deps
from ..context import Context
from ..util import FatalError
from .build import BuildCommand


class RunRandomCommand(Command):
    """Build and run a target multiple times, each with a fresh random seed.

    Each iteration generates a unique 24-bit random seed, sets it on
    ``ctx.rngseed`` and ``ctx.uniqueid``, performs a full rebuild, and then
    runs the target.  This is designed for use with :class:`RandomizingClang`
    which substitutes the ``RNGSEED`` placeholder in compiler flags.
    """

    @property
    def name(self) -> str:
        return "run-random"

    @property
    def description(self) -> str:
        return "run a single target program multiple times with fresh builds in between"

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
                help=f"run-random configuration options for {target.name}",
                formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            )

            tparser.add_argument(
                "instances",
                nargs="+",
                metavar="INSTANCE",
                choices=[instance.name for instance in self.instances.all()],
                help=" | ".join(
                    [instance.name for instance in self.instances.all()]
                ),
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
                instance.add_build_args(tparser)
                instance.add_run_args(tparser)

    def run(self, ctx: Context) -> None:
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        pool = self.make_pool(ctx)
        if pool is None:
            raise FatalError("run-random requires --parallel (e.g. --parallel proc)")
        self.enable_run_log(ctx)

        iterations = ctx.args.iterations
        orig_cwd = os.getcwd()

        for instance in instances:
            ctx.log.info(f"building and running instance {instance.name}")

            for iteration in range(iterations):
                seed = str(random.randint(0, 0xFFFFFF))
                ctx.log.info(
                    f"starting iteration {iteration + 1} with seed {seed}"
                )

                # Build phase: use a dedicated context copy so that
                # BuildCommand's internal configure() call does not
                # pollute the run-phase context.
                build_ctx = ctx.copy()
                build_ctx.args = copy.deepcopy(ctx.args)
                build_ctx.rngseed = seed
                build_ctx.uniqueid = seed
                build_ctx.args.dry_run = False
                build_ctx.args.targets = [build_ctx.args.target]
                build_ctx.args.packages = []
                build_ctx.args.deps_only = False
                build_ctx.args.clean = False
                build_ctx.args.relink = False
                build_ctx.args.iterations = 1
                build_ctx.args.instances = [instance.name]

                build_command = BuildCommand()
                build_command.instances = self.instances
                build_command.targets = self.targets
                build_command.packages = self.packages
                build_command.run(build_ctx)

                # Run phase: start from a fresh copy of the original
                # context so flags are not duplicated by a second
                # configure() call.
                run_ctx = ctx.copy()
                run_ctx.args = copy.deepcopy(ctx.args)
                run_ctx.rngseed = seed
                run_ctx.uniqueid = seed
                run_ctx.args.iterations = 1
                # Give each iteration a unique starttime so that
                # outfile_path() places results into separate run
                # directories instead of overwriting the previous
                # iteration's logs.
                run_ctx.starttime = datetime.datetime.now()

                load_deps(run_ctx, target)
                load_deps(run_ctx, instance)
                instance.configure(run_ctx)
                instance.prepare_run(run_ctx)

                ctx.log.info(f"running {target.name}-{instance.name}")
                target.goto_rootdir(run_ctx)
                target.run_hooks_pre_run(run_ctx, instance)
                target.run(run_ctx, instance, pool)
                target.run_hooks_post_run(run_ctx, instance)
                os.chdir(orig_cwd)

                instance.process_run(run_ctx)
                os.chdir(orig_cwd)

                # Wait for all pool jobs to finish before the next
                # iteration, otherwise the next build would overwrite
                # binaries still in use by the current run.
                pool.wait_all()

        pool.wait_all()
