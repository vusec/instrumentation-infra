import os
import argparse

from pathlib import Path

from ..command import Command, load_deps
from ..context import Context


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

    def run(self, ctx: Context) -> None:
        instance = self.instances[ctx.args.instance]
        ctx.args.dry_run = False

        hook_type = str(ctx.args.hooktype).replace("-", "_")
        target_file = Path(ctx.args.targetfile).resolve()
        assert hasattr(ctx.hooks, hook_type)
        assert target_file.is_file()

        # don't build packages (should have been done already since this
        # command should only be called recursively), just load them
        load_deps(ctx, instance)

        # populate ctx.hooks[hooktype]
        instance.configure(ctx)

        # run hooks in the directory of the given target file
        for hook in getattr(ctx.hooks, hook_type):
            os.chdir(target_file.parent)
            hook(ctx, str(target_file))
