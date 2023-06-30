import argparse
from ..context import Context
from ..command import Command
from ..package import Package
from ..target import Target
from ..util import FatalError


class CleanCommand(Command):
    name = 'clean'
    description = '''remove all source/build/install files of the given
                     packages and targets'''

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                default=[], choices=self.targets,
                help=' | '.join(self.targets))
        packagearg = parser.add_argument('-p', '--packages', nargs='+', metavar='PACKAGE',
                default=[],
                help='which packages to clean')
        setattr(packagearg, 'completer', self.complete_package)

    def run(self, ctx: Context) -> None:
        targets = self.targets.select(ctx.args.targets)
        packages = self.packages.select(ctx.args.packages)

        if not packages and not targets:
            raise FatalError('need at least one target or package to clean')

        for package in packages:
            clean_package(ctx, package)
        for target in targets:
            clean_target(ctx, target)


def clean_package(ctx: Context, package: Package) -> None:
    if package.is_clean(ctx):
        ctx.log.debug(f'package {package.ident()} is already cleaned')
    else:
        ctx.log.info('cleaning package ' + package.ident())
        package.clean(ctx)


def clean_target(ctx: Context, target: Target) -> None:
    if target.is_clean(ctx):
        ctx.log.debug(f'target {target.name} is already cleaned')
    else:
        ctx.log.info('cleaning target ' + target.name)
        target.clean(ctx)
