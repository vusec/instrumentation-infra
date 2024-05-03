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

                for pre_build_hook in ctx.hooks.pre_build:
                    pre_build_hook(ctx, str(target_dir))

            case "post-build" | "pre-run" | "post-run":
                target_file = Path(ctx.args.targetfile).resolve()
                target_dir = target_file.parent
                assert target_file.is_file() and target_dir.is_dir()
                os.chdir(target_dir)

                # Since things should already be built, don't re-build but just load
                # Also ensure ctx.hooks is populated (done by instance.configure())
                instance = self.instances[ctx.args.instance]
                ctx.args.dry_run = False
                load_deps(ctx, instance)
                instance.configure(ctx)

                match ctx.args.hooktype:
                    case "post-build":
                        for post_build_hook in ctx.hooks.post_build:
                            post_build_hook(ctx, str(target_file))
                    case "pre-run":
                        for pre_run_hook in ctx.hooks.pre_run:
                            pre_run_hook(ctx, str(target_file))
                    case "post-run":
                        for post_run_hook in ctx.hooks.post_run:
                            post_run_hook(ctx, str(target_file))
                    case _:
                        raise RuntimeError(f"Unknown error; bad hook type: {ctx.args.hooktype}!")

            case _:
                raise RuntimeError(f"Bad hook type: {ctx.args.hooktype}; expected one of: {self.hook_types}")
