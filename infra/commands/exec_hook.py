import os
import argparse

from pathlib import Path

from ..command import Command, load_deps
from ..context import Context, HookFunc


# This command does not appear in main --help usage because it is meant to be
# used as a callback for build scripts
class ExecHookCommand(Command):
    hook_types = ["pre-build", "post-build", "pre-run", "post-run"]

    @property
    def name(self) -> str:
        return "exec-hook"

    @property
    def description(self) -> str:
        return "intended to be used as a callback for build scripts"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "hooktype",
            choices=self.hook_types,
            metavar="<TYPE>",
            help=" | ".join(self.hook_types),
        )
        parser.add_argument(
            "instance",
            metavar="INSTANCE",
            choices=[instance.name for instance in self.instances.all()],
            help=" | ".join([instance.name for instance in self.instances.all()]),
        )
        parser.add_argument(
            "targetfile",
            metavar="TARGETFILE",
            help="File to run hook on -- usually the specific binary",
        )

        for instance in self.instances.values():
            # Hook should be called in context with build/run args available
            instance.add_build_args(parser)
            instance.add_run_args(parser)

    def run(self, ctx: Context) -> None:
        # While the target hasn't always been built yet, the instance &
        # its dependencies should have, so just load them here
        instance = self.instances[ctx.args.instance]
        ctx.args.dry_run = False
        load_deps(ctx, instance)
        instance.configure(ctx)

        target_file = Path(ctx.args.targetfile).resolve()
        hook_type = str(ctx.args.hooktype).replace("-", "_")
        ctx.log.info(f"Running {hook_type} hooks on {target_file}")
        assert hasattr(ctx.hooks, hook_type)

        # Get the hooks from the hook type; execute each
        hook: HookFunc
        for hook in getattr(ctx.hooks, hook_type):
            ctx.log.info(f"Running {hook_type} hook '{ctx.hooks.hook_name(hook)}' on {target_file}")
            hook(ctx, str(target_file))
