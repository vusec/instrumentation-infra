from ..command import Command
from ..util import FatalError
from .build import BuildCommand, default_jobs, load_deps


class RunCommand(Command):
    name = 'run'
    description = 'run a single target program'

    def add_args(self, parser):
        target_parsers = parser.add_subparsers(
                title='target', metavar='TARGET', dest='target',
                help=' | '.join(self.targets))
        target_parsers.required = True

        for target in self.targets.values():
            tparser = target_parsers.add_parser(target.name)

            tparser.add_argument('instances', nargs='+',
                    metavar='INSTANCE', choices=self.instances,
                    help=' | '.join(self.instances))
            tparser.add_argument('--build', action='store_true',
                    help='build target first (no custom target/instance arguments)')
            tparser.add_argument('-j', '--jobs', type=int, default=default_jobs,
                    help='maximum number of build processes (default %d)' %
                        default_jobs)
            tparser.add_argument('-i', '--iterations', metavar='ITERATIONS',
                    type=int, default=1,
                    help='number of runs per benchmark')

            self.add_pool_args(tparser)
            target.add_run_args(tparser)


    def run(self, ctx):
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        pool = self.make_pool(ctx)

        ctx.args.dry_run = False

        if ctx.args.build:
            ctx.args.targets = [ctx.args.target]
            ctx.args.packages = []
            ctx.args.deps_only = False
            ctx.args.clean = False
            ctx.args.force_rebuild_deps = False
            ctx.args.relink = False
            build_command = BuildCommand()
            build_command.set_maps(self.instances, self.targets, self.packages)
            build_command.run(ctx)

        load_deps(ctx, target)

        for instance in instances:
            oldctx = ctx.copy()
            ctx.log.info('running %s-%s' % (target.name, instance.name))

            load_deps(ctx, instance)
            instance.prepare_run(ctx)
            target.goto_rootdir(ctx)

            if not self.call_with_pool(target.run, (ctx, instance), pool):
                raise FatalError('target %s does not support parallel runs' %
                                 target.name)

            ctx = oldctx

        if pool:
            pool.wait_all()
