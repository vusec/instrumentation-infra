import argparse

from ..command import Command, get_deps
from ..context import Context
from ..util import qjoin


class ConfigCommand(Command):
    name = "config"
    description = "get configuration information"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--instances", action="store_true", help="list all registered instances"
        )
        group.add_argument(
            "--targets", action="store_true", help="list all registered targets"
        )
        group.add_argument(
            "--packages",
            action="store_true",
            help="list dependencies of all registered targets/instances",
        )

    def run(self, ctx: Context) -> None:
        if ctx.args.instances:
            for name in self.instances:
                print(name)
        elif ctx.args.targets:
            for name in self.targets:
                print(name)
        else:
            assert ctx.args.packages
            for package in get_deps(*self.targets.all(), *self.instances.all()):
                print(package.ident())


class PkgConfigCommand(Command):
    name = "pkg-config"
    description = "get package-specific information"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        packagearg = parser.add_argument(
            "package", metavar="PACKAGE", help="package to configure"
        )
        setattr(packagearg, "completer", self.complete_package)
        parser.add_argument(
            "args",
            nargs=argparse.REMAINDER,
            choices=[],
            metavar="...",
            help="configuration args (package dependent)",
        )

        self.subparsers = parser.add_subparsers(
            title="pkg-config options", metavar="", description=""
        )

    def run(self, ctx: Context) -> None:
        package = self.packages[ctx.args.package]
        subparser = self.subparsers.add_parser(f"{ctx.args.command} {package.ident()}")
        pgroup = subparser.add_mutually_exclusive_group(required=True)
        for opt, desc, value in package.pkg_config_options(ctx):
            pgroup.add_argument(
                opt, action="store_const", dest="value", const=value, help=desc
            )
        value = subparser.parse_args(ctx.args.args).value

        # for lists (handy for flags), join by spaces while adding quotes where
        # necessary
        if isinstance(value, (list, tuple)):
            value = qjoin(value)

        print(value)
