from ..command import Command
from ..package import Package
from ..target import Target
from ..util import FatalError, Namespace


class CleanCommand(Command):
    name = 'clean'
    description = '''remove all source/build/install files of the given
                      packages and targets'''

    def add_args(self, parser):
        parser.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                default=[], choices=self.targets,
                help=' | '.join(self.targets))
        parser.add_argument('-p', '--packages', nargs='+', metavar='PACKAGE',
                default=[],
                help='which packages to clean').completer = self.complete_package

    def run(self, ctx):
        targets = self.targets.select(ctx.args.targets)
        packages = self.packages.select(ctx.args.packages)

        if not packages and not targets:
            raise FatalError('no packages or targets specified')

        ctx.args.dry_run = False
        for package in packages:
            clean_package(ctx, package)
        for target in targets:
            clean_target(ctx, target)


def clean_package(ctx: Namespace, package: Package):
    if package.is_clean(ctx):
        ctx.log.debug('package %s is already cleaned' % package.ident())
    else:
        ctx.log.info('cleaning package ' + package.ident())
        if not ctx.args.dry_run:
            package.clean(ctx)


def clean_target(ctx: Namespace, target: Target):
    if target.is_clean(ctx):
        ctx.log.debug('target %s is already cleaned' % target.name)
    else:
        ctx.log.info('cleaning target ' + target.name)
        if not ctx.args.dry_run:
            target.clean(ctx)
