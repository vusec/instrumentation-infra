import os
import argparse
import logging
import sys
import traceback
import copy
from collections import OrderedDict
from multiprocessing import cpu_count
from .package import Package
from .util import FatalError


# disable .pyc file generation
sys.dont_write_bytecode = True


class Namespace(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


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

        # global options
        parser.add_argument('-v', '--verbosity', default='info',
                choices=['critical', 'error', 'warning', 'info', 'debug'],
                help='set logging verbosity (default info)')

        subparsers = parser.add_subparsers(
                title='subcommands', metavar='command', dest='command',
                description='run with "<command> --help" to see options for '
                            'individual commands')
        subparsers.required = True

        # command: build-deps
        pdeps = subparsers.add_parser('build-deps',
                help='build dependencies for target programs and/or instances')
        pdeps.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                help='build dependencies for these target programs')
        pdeps.add_argument('-i', '--instances', nargs='+', metavar='INSTANCE',
                help='build dependencies for these instances')
        pdeps.add_argument('-j', '--nproc', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)
        pdeps.add_argument('-n', '--dry-run', action='store_true',
                help='don\'t actually build anything, just show what will be done')

        # command: build
        pbuild = subparsers.add_parser('build',
                help='build target programs (also builds dependencies)')
        pbuild.add_argument('-t', '--targets', nargs='+', metavar='TARGET',
                required=True,
                help='which target programs to build')
        pbuild.add_argument('-i', '--instances', nargs='+', metavar='INSTANCE',
                required=True,
                help='which instances to build')
        pbuild.add_argument('-j', '--nproc', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)
        #pbuild.add_argument('-b', '--benchmarks', nargs='+', metavar='BENCHMARK',
        #        help='which benchmarks to build for the given target (only works '
        #            'for a single target)')
        pbuild.add_argument('-n', '--dry-run', action='store_true',
                help='don\'t actually build anything, just show what will be done')

        # command: build-pkg
        ppackage = subparsers.add_parser('build-pkg',
                help='build a single package')
        ppackage.add_argument('package',
                help='which package to build (see %s config --packages '
                     'for choices)' % sys.argv[0])
        ppackage.add_argument('-j', '--nproc', type=int, default=nproc,
                help='maximum number of build processes (default %d)' % nproc)

        for target in self.targets.values():
            target.add_build_args(pbuild)

        for instance in self.instances.values():
            instance.add_build_args(pbuild)

        # command: run
        prun = subparsers.add_parser('run',
                help='run target program (does not build anything)')
        prun.add_argument('target',
                help='which target to run')
        prun.add_argument('instance',
                help='which instance to run')

        # command: config
        pconfig = subparsers.add_parser('config',
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

        self.args = parser.parse_args()

    def init_context(self):
        if 'nproc' in self.args:
            self.ctx.nproc = self.args.nproc
        self.ctx.hooks = Namespace(post_build=[])

    def create_dirs(self):
        self.ctx.paths = paths = Namespace()
        paths.root = self.root_path
        paths.buildroot = os.path.join(paths.root, 'build')
        paths.installroot = os.path.join(paths.buildroot, 'install')
        paths.log = os.path.join(paths.buildroot, 'log')
        paths.packages = os.path.join(paths.buildroot, 'packages')
        paths.packsrc = os.path.join(paths.packages, 'src')
        paths.packobj = os.path.join(paths.packages, 'obj')
        paths.targets = os.path.join(paths.buildroot, 'targets')
        paths.targetsrc = os.path.join(paths.targets, 'src')
        paths.targetobj = os.path.join(paths.targets, 'obj')
        os.makedirs(paths.log, exist_ok=True)
        os.makedirs(paths.packsrc, exist_ok=True)
        os.makedirs(paths.packobj, exist_ok=True)
        os.makedirs(paths.targetsrc, exist_ok=True)
        os.makedirs(paths.targetobj, exist_ok=True)
        self.ctx.prefixes = []
        paths.prefix = None

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
            if self.args.command == 'build-deps':
                self.run_build_deps(self.args)
            elif self.args.command == 'build':
                self.run_build(self.args)
            elif self.args.command == 'build-pkg':
                self.run_build_package(self.args)
            elif self.args.command == 'run':
                self.run_run(self.args)
            elif self.args.command == 'config':
                self.run_config(self.args)
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
            self.do_fetch(package)

        for package in deps:
            self.do_build(package)
            self.do_install(package)

    def do_fetch(self, package, *args):
        if package.is_built(self.ctx):
            self.ctx.log.debug('%s already fetched, skip' % package.ident())
        else:
            self.ctx.log.info('fetching %s' % package.ident())
            if not args.dry_run:
                package.fetch(self.ctx, *args)

    def do_build(self, package, *args):
        if package.is_built(self.ctx):
            self.ctx.log.debug('%s already built, skip' % package.ident())
        else:
            self.ctx.log.info('building %s' % package.ident())
            if not args.dry_run:
                package.build(self.ctx, *args)

    def do_install(self, package, *args):
        if package.is_installed(self.ctx):
            self.ctx.log.debug('%s already installed, skip' % package.ident())
        else:
            self.ctx.log.info('installing %s' % package.ident())
            if not args.dry_run:
                package.install(self.ctx, *args)

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
            self.do_fetch(package)

        for target in targets:
            self.do_fetch(package, instances)

        for package in deps:
            self.do_build(package)
            self.do_install(package)

        for instance in instances:
            for target in targets:
                self.ctx.log.info('building %s-%s' %
                        (target.name, instance.name))
                if not self.args.dry_run:
                    target.build(self.ctx, instance)
                    target.link(self.ctx, instance)
                    target.run_hooks_post_build(self.ctx, instance)

    def run_build_package(self):
        objs = list(self.targets.values())
        objs += list(self.instances.values())
        for package in self.get_deps(objs):
            if package.ident() == self.args.package:
                break
        else:
            raise FatalError('no package called %s' % self.args.package)

        self.do_fetch(self.ctx, package)
        self.do_build(self.ctx, package)

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
