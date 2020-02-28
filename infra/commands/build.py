import os
from multiprocessing import cpu_count
from ..command import Command, get_deps
from ..package import Package
from ..util import FatalError, Namespace
from .clean import clean_package, clean_target


default_jobs = min(cpu_count(), 16)


class BuildCommand(Command):
    name = 'build'
    description = 'build target programs and their dependencies'

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
            tparser.add_argument('-j', '--jobs', type=int, default=default_jobs,
                    help='maximum number of build processes (default %d)' %
                         default_jobs)
            tparser.add_argument('--deps-only', action='store_true',
                    help='only build dependencies, not targets themselves')
            tparser.add_argument('--force-rebuild-deps', action='store_true',
                    help='always run the build commands')
            tparser.add_argument('--clean', action='store_true',
                    help='clean target first')
            tparser.add_argument('--dry-run', action='store_true',
                    help='don\'t actually build anything, just show what will be done')

            self.add_pool_args(tparser)
            target.add_build_args(tparser)

            for instance in self.instances.values():
                instance.add_build_args(tparser)

    def run(self, ctx):
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        pool = self.make_pool(ctx)

        deps = get_deps(target, *instances)

        self.enable_run_log(ctx)

        # clean target if requested
        if ctx.args.clean:
            clean_target(ctx, target)

        # first fetch all necessary code so that the internet connection can be
        # broken during building
        for package in deps:
            fetch_package(ctx, package, ctx.args.force_rebuild_deps)

        if not ctx.args.deps_only:
            target.goto_rootdir(ctx)
            if target.is_fetched(ctx):
                ctx.log.debug('%s already fetched, skip' % target.name)
            else:
                ctx.log.info('fetching %s' % target.name)
                target.fetch(ctx)

        cached_deps = {obj: get_deps(obj) for obj in [target] + instances}

        built_packages = set()

        def build_package_once(package, force):
            if package not in built_packages:
                build_package(ctx, package, force)
                install_package(ctx, package, force)
                built_packages.add(package)

        def build_deps_once(obj):
            for package in cached_deps[obj]:
                force = ctx.args.force_rebuild_deps
                build_package_once(package, force)
                ctx.log.debug('install %s in env' % package.ident())
                package.install_env(ctx)

        if ctx.args.deps_only and not instances:
            oldctx = ctx.copy()
            build_deps_once(target)
            ctx = oldctx

        for instance in instances:
            # use a copy of the context for instance configuration to avoid
            # stacking configurations between instances
            # FIXME: only copy the build env (the part that changes)
            oldctx = ctx.copy()
            instance.configure(ctx)
            build_deps_once(instance)
            build_deps_once(target)

            if not ctx.args.deps_only:
                ctx.log.info('building %s-%s' %
                                    (target.name, instance.name))
                if not ctx.args.dry_run:
                    target.goto_rootdir(ctx)
                    self.call_with_pool(target.build, (ctx, instance), pool)
                    target.run_hooks_post_build(ctx, instance)

            ctx = oldctx

        if pool:
            pool.wait_all()


# This command does not appear in main --help usage because it is meant to be
# used as a callback for build scripts
class ExecHookCommand(Command):
    name = 'exec-hook'
    description = None

    def add_args(self, parser):
        parser.add_argument('hooktype', choices=['post-build'],
                help='hook type')
        parser.add_argument('instance',
                metavar='INSTANCE', choices=self.instances,
                help=' | '.join(self.instances))
        parser.add_argument('targetfile', metavar='TARGETFILE',
                help='file to run hook on')

    def run(self, ctx):
        instance = self.instances[ctx.args.instance]

        absfile = os.path.abspath(ctx.args.targetfile)
        if not os.path.exists(absfile):
            raise FatalError('file %s does not exist' % absfile)

        hooktype = ctx.args.hooktype.replace('-', '_')
        assert hooktype in ctx.hooks

        # don't build packages (should have been done already since this
        # command should only be called recursively), just load them
        load_deps(ctx, instance)

        # populate ctx.hooks[hooktype]
        instance.configure(ctx)

        # run hooks
        basedir = os.path.dirname(absfile)
        for hook in ctx.hooks[hooktype]:
            os.chdir(basedir)
            hook(ctx, absfile)


class PkgBuildCommand(Command):
    name = 'pkg-build'
    description = 'build a single package and its dependencies'

    def add_args(self, parser):
        packagearg = parser.add_argument('package', metavar='PACKAGE',
                help='package to build')
        packagearg.completer = self.complete_package
        parser.add_argument('-j', '--jobs', type=int, default=default_jobs,
                help='maximum number of build processes (default %d)' %
                     default_jobs)
        parser.add_argument('--force-rebuild-deps', action='store_true',
                help='always run the build commands')
        parser.add_argument('--clean', action='store_true',
                help='clean package first')
        parser.add_argument('--dry-run', action='store_true',
                help='don\'t actually build anything, just show what will be done')

    def run(self, ctx):
        main_package = self.packages[ctx.args.package]

        deps = get_deps(main_package)
        force_deps = ctx.args.force_rebuild_deps

        self.enable_run_log(ctx)

        if ctx.args.clean:
            clean_package(ctx, main_package)

        for package in deps:
            fetch_package(ctx, package, force_deps)

        fetch_package(ctx, main_package, True)

        for package in deps:
            fetch_package(ctx, package, force_deps)

        for package in deps:
            build_package(ctx, package, force_deps)
            install_package(ctx, package, force_deps)

        build_package(ctx, main_package, True)


def fetch_package(ctx: Namespace, package: Package, force_rebuild: bool):
    package.goto_rootdir(ctx)

    if package.is_fetched(ctx):
        ctx.log.debug('%s already fetched, skip' % package.ident())
    elif not force_rebuild and package.is_installed(ctx):
        ctx.log.debug('%s already installed, skip fetching' % package.ident())
    else:
        ctx.log.info('fetching %s' % package.ident())
        if not ctx.args.dry_run:
            package.goto_rootdir(ctx)
            package.fetch(ctx)


def build_package(ctx: Namespace, package: Package, force_rebuild: bool):
    package.goto_rootdir(ctx)
    built = package.is_built(ctx)

    if not force_rebuild:
        if built:
            ctx.log.debug('%s already built, skip' % package.ident())
            return
        if package.is_installed(ctx):
            ctx.log.debug('%s already installed, skip building' % package.ident())
            return

    force = ' (forced rebuild)' if force_rebuild and built else ''
    ctx.log.info('building %s' % package.ident() + force)
    if not ctx.args.dry_run:
        package.goto_rootdir(ctx)
        package.build(ctx)


def install_package(ctx: Namespace, package: Package, force_rebuild: bool):
    package.goto_rootdir(ctx)
    installed = package.is_installed(ctx)

    if not force_rebuild and installed:
        ctx.log.debug('%s already installed, skip' % package.ident())
    else:
        force = ' (forced reinstall)' if force_rebuild and installed else ''
        ctx.log.info('installing %s' % package.ident() + force)
        if not ctx.args.dry_run:
            package.goto_rootdir(ctx)
            package.install(ctx)

    package.goto_rootdir(ctx)


def load_deps(ctx: Namespace, obj):
    for package in get_deps(obj):
        ctx.log.debug('install %s into env' % package.ident())
        package.install_env(ctx)
