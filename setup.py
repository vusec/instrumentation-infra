import os
import argparse
import logging
import sys
import traceback
import copy
from collections import OrderedDict
from multiprocessing import cpu_count
from .package import Package
from .util import FatalError, Namespace


# disable .pyc file generation
sys.dont_write_bytecode = True


class Setup:
    def __init__(self, root_path):
        self.root_path = os.path.abspath(root_path)
        self.instances = OrderedDict()
        self.targets = OrderedDict()

    def main(self):
        self.ctx = Namespace()
        self.parse_argv()
        self.initialize_logger()
        self.init_context()
        self.create_dirs()
        self.run_command()

    def parse_argv(self):
        parser = argparse.ArgumentParser(
                description='Frontend for building/running instrumented benchmarks.')

        nproc = cpu_count()
        progname = os.path.basename(sys.argv[0])

        # global options
        parser.add_argument('-v', '--verbosity', default='info',
                choices=['critical', 'error', 'warning', 'info', 'debug'],
                help='set logging verbosity (default info)')

        self.subparsers = parser.add_subparsers(
                title='subcommands', metavar='command', dest='command',
                description='run with "<command> --help" to see options for '
                            'individual commands')
        self.subparsers.required = True

        # command: build-deps
        pdeps = self.subparsers.add_parser('build-deps',
                help='build dependencies for target programs and/or instances')
        pdeps.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                help='build dependencies for these target programs')
        pdeps.add_argument('-i', '--instances', nargs='+', metavar='INSTANCE',
                help='build dependencies for these instances')
        pdeps.add_argument('-j', '--jobs', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)
        pdeps.add_argument('--force-rebuild', action='store_true',
                help='always run the build commands')
        pdeps.add_argument('-n', '--dry-run', action='store_true',
                help='don\'t actually build anything, just show what will be done')

        # command: build
        pbuild = self.subparsers.add_parser('build',
                help='build target programs (also builds dependencies)')
        pbuild.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                required=True,
                help='which target programs to build')
        pbuild.add_argument('-i', '--instances', nargs='+', metavar='INSTANCE',
                required=True,
                help='which instances to build')
        pbuild.add_argument('-j', '--jobs', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)
        #pbuild.add_argument('-b', '--benchmarks', nargs='+', metavar='BENCHMARK',
        #        help='which benchmarks to build for the given target (only works '
        #            'for a single target)')
        pbuild.add_argument('--force-rebuild-deps', action='store_true',
                help='always run the build commands')
        pbuild.add_argument('--relink', action='store_true',
                help='only link targets, don\'t rebuild object files')
        pbuild.add_argument('-n', '--dry-run', action='store_true',
                help='don\'t actually build anything, just show what will be done')

        # command: build-pkg
        ppackage = self.subparsers.add_parser('build-pkg',
                help='build a single package (implies --force-rebuild)')
        ppackage.add_argument('package',
                help='which package to build (see %s config --packages '
                     'for choices)' % progname)
        ppackage.add_argument('-j', '--jobs', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)

        for target in self.targets.values():
            target.add_build_args(pbuild)

        for instance in self.instances.values():
            instance.add_build_args(pbuild)

        # command: run
        prun = self.subparsers.add_parser('run',
                help='run target program (does not build anything)')
        prun.add_argument('target',
                help='which target to run')
        prun.add_argument('instance',
                help='which instance to run')

        # command: config
        pconfig = self.subparsers.add_parser('config',
                help='print information about command line arguments and build flags')
        pconfig_group = pconfig.add_mutually_exclusive_group(required=True)
        pconfig_group.add_argument('--instances', action='store_true',
                dest='list_instances',
                help='list all registered instances')
        pconfig_group.add_argument('--targets', action='store_true',
                dest='list_targets',
                help='list all registered targets')
        pconfig_group.add_argument('--packages', action='store_true',
                dest='list_packages',
                help='list dependencies of all registered targets/instances')

        # command: pkg-config
        ppkgconfig = self.subparsers.add_parser('pkg-config',
                help='print package-specific information')
        ppkgconfig.add_argument('package',
                help='package to configure (see %s config --packages '
                     'for choices)' % progname)
        #ppkgconfig.add_argument('-h', '--help',
        #        help='show available config arguments for this package')
        ppkgconfig.add_argument('args', nargs=argparse.REMAINDER,
                help='configuration args')
        self.pkgconfig_parser = ppkgconfig

        self.args = parser.parse_args()

    def init_context(self):
        if 'jobs' in self.args:
            self.ctx.jobs = self.args.jobs

        self.ctx.hooks = Namespace(post_build=[])

        self.ctx.paths = paths = Namespace()
        paths.root = self.root_path
        paths.infra = os.path.dirname(__file__)
        paths.buildroot = os.path.join(paths.root, 'build')
        paths.log = os.path.join(paths.buildroot, 'log')
        paths.debuglog = os.path.join(paths.log, 'debug.txt')
        paths.runlog = os.path.join(paths.log, 'commands.txt')
        paths.packages = os.path.join(paths.buildroot, 'packages')
        paths.targets = os.path.join(paths.buildroot, 'targets')

        # FIXME move to package?
        self.ctx.prefixes = []
        self.ctx.cc = 'cc'
        self.ctx.cxx = 'c++'
        self.ctx.ar = 'ar'
        self.ctx.nm = 'nm'
        self.ctx.ranlib = 'ranlib'
        self.ctx.cflags = []
        self.ctx.ldflags = []

    def create_dirs(self):
        os.makedirs(self.ctx.paths.log, exist_ok=True)
        os.makedirs(self.ctx.paths.packages, exist_ok=True)
        os.makedirs(self.ctx.paths.targets, exist_ok=True)

    def initialize_logger(self):
        level = getattr(logging, self.args.verbosity.upper())

        # create logger
        fmt = '%(asctime)s [%(levelname)s] %(message)s'
        datefmt = '%H:%M:%S'
        logging.basicConfig(format=fmt, datefmt=datefmt, level=level)
        self.ctx.log = logging.getLogger('autosetup')

        # colorize log if supported
        try:
            import coloredlogs
            coloredlogs.install(fmt=fmt, datefmt=datefmt, level=level)
        except ImportError:
            pass

    def add_instance(self, instance):
        if instance.name in self.instances:
            self.ctx.log.warning('overwriting existing instance "%s"' % instance)
        self.instances[instance.name] = instance

    def get_instance(self, name):
        if name not in self.instances:
            raise FatalError('no instance called "%s"' % name)
        return self.instances[name]

    def add_target(self, target):
        if target.name in self.targets:
            self.ctx.log.warning('overwriting existing target "%s"' % target)
        self.targets[target.name] = target

    def get_target(self, name):
        if name not in self.targets:
            raise FatalError('no target called "%s"' % name)
        return self.targets[name]

    def run_command(self):
        try:
            os.chdir(self.ctx.paths.root)

            if self.args.command == 'build-deps':
                self.run_build_deps()
            elif self.args.command == 'build':
                self.run_build()
            elif self.args.command == 'build-pkg':
                self.run_build_package()
            elif self.args.command == 'run':
                self.run_run()
            elif self.args.command == 'config':
                self.run_config()
            elif self.args.command == 'pkg-config':
                self.run_pkg_config()
            else:
                raise FatalError('unknown command %s' % self.args.command)
        except FatalError as e:
            self.ctx.log.error(str(e))
        except KeyboardInterrupt:
            self.ctx.log.info('exiting because of keyboard interrupt')
        except Exception as e:
            self.ctx.log.critical('unkown error\n' + traceback.format_exc().rstrip())

    def get_deps(self, objs):
        deps = []

        def add_dep(dep, visited):
            if dep in visited:
                raise FatalError('recursive dependency %s' % dep)
            visited.add(dep)

            for nested_dep in dep.dependencies():
                add_dep(nested_dep, set(visited))

            #if dep in deps:
            #    self.ctx.log.debug('skipping duplicate dependency %s' % dep.ident())
            #else:
            if dep not in deps:
                deps.append(dep)

        for obj in objs:
            for dep in obj.dependencies():
                add_dep(dep, set())

        return deps

    def run_build_deps(self):
        objs = []

        if self.args.targets:
            objs += [self.get_target(name) for name in self.args.targets]
        if self.args.instances:
            objs += [self.get_instance(name) for name in self.args.instances]

        if not objs:
            raise FatalError('no targets or instances specified')

        deps = self.get_deps(objs)

        if not deps:
            self.ctx.log.debug('no dependencies to build')

        for package in deps:
            self.fetch_package(package, self.args.force_rebuild)

        for package in deps:
            self.build_package(package, self.args.force_rebuild)
            self.install_package(package, self.args.force_rebuild)

    def fetch_package(self, package, force_rebuild, *args):
        package.goto_rootdir(self.ctx)

        if package.is_fetched(self.ctx):
            self.ctx.log.debug('%s already fetched, skip' % package.ident())
        elif not force_rebuild and package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip fetching' % package.ident())
        else:
            self.ctx.log.info('fetching %s' % package.ident())
            if not self.args.dry_run:
                package.goto_rootdir(self.ctx)
                package.fetch(self.ctx, *args)

    def build_package(self, package, force_rebuild, *args):
        package.goto_rootdir(self.ctx)

        if not force_rebuild and package.is_built(self.ctx):
            self.ctx.log.debug('%s already built, skip' % package.ident())
            return
        elif not force_rebuild and package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip building' % package.ident())
        else:
            self.ctx.log.info('building %s' % package.ident())
            if not self.args.dry_run:
                package.goto_rootdir(self.ctx)
                package.build(self.ctx, *args)

    def install_package(self, package, force_rebuild, *args):
        package.goto_rootdir(self.ctx)

        if not force_rebuild and package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip' % package.ident())
        else:
            self.ctx.log.info('installing %s' % package.ident())
            if not self.args.dry_run:
                package.goto_rootdir(self.ctx)
                package.install(self.ctx, *args)

        package.goto_rootdir(self.ctx)
        package.install_env(self.ctx)

    def run_build(self):
        targets = [self.get_target(name) for name in self.args.targets]
        instances = [self.get_instance(name) for name in self.args.instances]
        deps = self.get_deps(targets + instances)

        if not deps:
            self.ctx.log.debug('no dependencies to build')

        # first fetch all necessary code so that the internet connection can be
        # broken during building
        for package in deps:
            self.fetch_package(package, self.args.force_rebuild_deps)

        for target in targets:
            if target.is_fetched(self.ctx):
                self.ctx.log.debug('%s already fetched, skip' % target.name)
            else:
                self.ctx.log.info('fetching %s' % target.name)
                target.goto_rootdir(self.ctx)
                target.fetch(self.ctx)

        for package in deps:
            self.build_package(package, self.args.force_rebuild_deps)
            self.install_package(package, self.args.force_rebuild_deps)

        for instance in instances:
            # use a copy of the context for instance configuration to avoid
            # stacking configurations between instances
            # FIXME use copy.copy here? in case ppl put big objects in ctx
            ctx = copy.deepcopy(self.ctx)
            instance.configure(ctx)

            for target in targets:
                ctx.log.info('building %s-%s' % (target.name, instance.name))
                if not self.args.dry_run:
                    if not self.args.relink:
                        target.goto_rootdir(ctx)
                        target.build(ctx, instance)
                    target.goto_rootdir(ctx)
                    target.link(ctx, instance)
                    target.run_hooks_post_build(ctx, instance)

    def run_build_package(self):
        package = self.find_package(self.args.package)
        self.args.dry_run = False
        self.fetch_package(package, True)
        self.build_package(package, True)
        self.install_package(package, True)

    def find_package(self, name):
        objs = list(self.targets.values())
        objs += list(self.instances.values())
        for package in self.get_deps(objs):
            if package.ident() == name:
                return package
        raise FatalError('no package called %s' % name)

    def run_run(self):
        raise NotImplementedError

    def run_config(self):
        if self.args.list_instances:
            for name in self.instances.keys():
                print(name)
        elif self.args.list_targets:
            for name in self.targets.keys():
                print(name)
        elif self.args.list_packages:
            objs = list(self.targets.values())
            objs += list(self.instances.values())
            for package in self.get_deps(objs):
                print(package.ident())
        else:
            raise NotImplementedError

    def run_pkg_config(self):
        package = self.find_package(self.args.package)
        parser = self.subparsers.add_parser(
                '%s %s' % (self.args.command, package.ident()))
        package.run_pkg_config(self.ctx, parser, self.args.args)
