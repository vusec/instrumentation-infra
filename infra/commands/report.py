import argparse
import sys
from ..command import Command


class ReportCommand(Command):
    name = 'report'
    description = 'report results after a (parallel) run'

    def add_args(self, parser):
        parser.add_argument('-i', '--instances', nargs='+', metavar='INSTANCE',
                default=[], choices=self.instances,
                help=' | '.join(self.instances))
        parser.add_argument('-o', '--outfile',
                type=argparse.FileType('w'), default=sys.stdout,
                help='outfile (default: stdout)')
        subparser = parser.add_subparsers(
                title='target', metavar='TARGET', dest='target',
                help=' | '.join(self.targets))
        subparser.required = True

        for name, target in self.targets.items():
            target.add_report_args(subparser.add_parser(name))

    def run(self, ctx):
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        target.report(ctx, instances, ctx.args.outfile, ctx.args)
