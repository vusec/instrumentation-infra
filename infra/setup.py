import os
import argparse
import logging
import sys
import traceback
import datetime
from . import commands
from .command import Command, get_deps
from .util import FatalError, Namespace, Index, LazyIndex
from .instance import Instance
from .target import Target


# disable .pyc file generation
sys.dont_write_bytecode = True


class Setup:
    """
    Defines framework commands.

    The setup takes care of complicated things like command-line parsing,
    logging, parallelism, environment setup and generating build paths. You
    should only need to use the methods documented here. To use the setup, you
    must first populate it with targets and instances using :func:`add_target`
    and :func:`add_instance`, and then call :func:`main` to run the command
    issued in the command-line arguments:

    .. _setup-example:

    ::

        setup = infra.Setup(__file__)
        setup.add_instance(MyAwesomeInstance())
        setup.add_target(MyBeautifulTarget())
        setup.main()

    :func:`main` creates a :py:attr:`configuration context<ctx>` that it passes
    to methods of targets/instances/packages. You can see it being used as
    ``ctx`` by many API methods below. The context contains setup configuration
    data, such as absolute build paths, and environment variables for build/run
    commands, such as which compiler and CFLAGS to use to build the current
    target. Your own targets and instances should read/write to the context.

    **The job of an instance is to manipulate the the context such that a
    target is built in the desired way.** This manipulation happens in
    predefined API methods which you must overwrite (see below). Hence, these
    methods receive the context as a parameter.
    """

    ctx = None
    """
    :class:`util.Namespace` The configuration context.

    Consider an example project hosted in directory `/project`, with the
    infrastructure cloned as a submodule in `/project/infra` and a setup script
    like the one :ref:`above <setup-example>` in `/project/setup.py`. The context
    will look like this after initialization::

        Namespace({
            'log':         logging.Logger(...),
            'args':        argparse.Namespace(...),
            'jobs':        8,
            'paths':       Namespace({
                               'root':         '/project',
                               'setup':        '/project/setup.py',
                               'infra':        '/project/infra'
                               'buildroot':    '/project/build',
                               'log':          '/project/build/log',
                               'debuglog':     '/project/build/log/debug.txt',
                               'runlog':       '/project/build/log/commands.txt',
                               'packages':     '/project/build/packages',
                               'targets':      '/project/build/targets',
                               'pool_results': '/project/results'
                           }),
            'runenv':      Namespace({}),
            'cc':          'cc',
            'cxx':         'c++',
            'ar':          'ar',
            'nm':          'nm',
            'ranlib':      'ranlib',
            'cflags':      [],
            'cxxflags':    [],
            'ldflags':     [],
            'lib_ldflags': [],
            'hooks':       Namespace({
                               'post_build': []
                           }),
            'starttime':   datetime.datetime
        })

    The :class:`util.Namespace` class is simply a :class:`dict` whose members
    can be accessed like attributes.

    ``ctx.log`` is a logging object used for status updates. Use this to
    provide useful information about what your implementation is doing.

    ``ctx.args`` is populated with processed command-line arguments, It is
    available to read custom build/run arguments from that are added by
    targets/instances.

    ``ctx.jobs`` contains the value of the ``-j`` command-line option,
    defaulting to the number of CPU cores returned by
    :func:`multiprocessing.cpu_count`.

    ``ctx.paths`` are absolute paths to be used (readonly) throughout the
    framework.

    ``ctx.runenv`` defines environment variables for :func:`util.run`, which is
    a wrapper for :func:`subprocess.run` that does logging and other useful
    things. 

    ``ctx.{cc,cxx,ar,nm,ranlib}`` define default tools of the compiler
    toolchain, and should be used by target definitions to configure build
    scripts. ``ctx.{c,cxx,ld}flags`` similarly define build flags for targets
    in a list and should be joined into a string using :func:`util.qjoin` when
    being passed as a string to a build script by a target definition.

    ``ctx.{c,cxx,ld}flags`` should be set by instances to define flags for
    compiling targets.

    ``ctx.lib_ldflags`` is a special set of linker flags set by some packages,
    and is passed when linking target libraries that will later be (statically)
    linked into the binary. In practice it is either empty or ``['-flto']`` when
    compiling with LLVM.

    ``ctx.hooks.post_build`` defines a list of post-build hooks, which are
    python functions called with the path to the binary as the only parameter.

    ``ctx.starttime`` is set to ``datetime.datetime.now()``.

    ``ctx.workdir`` is set to the work directory from which the setup script is
    invoked.

    ``ctx.log`` is set to a new ``logging.Logger`` object.
    """

    _max_default_jobs = 16

    def __init__(self, setup_path: str):
        """
        :param setup_path: Path to the script running :func:`Setup.main`.
                           Needed to allow build scripts to call back into the
                           setup script for build hooks.
        """
        self.setup_path = os.path.abspath(setup_path)
        self.instances = Index('instance')
        self.targets = Index('target')
        self.commands = Index('command')
        self.packages = LazyIndex('package', self._find_package)
        self.ctx = Namespace()
        self._init_context()

    def _parse_argv(self):
        parser = argparse.ArgumentParser(
                description='Frontend for building/running instrumented benchmarks.')

        # global options
        parser.add_argument('-v', '--verbosity', default='info',
                choices=['critical', 'error', 'warning', 'info', 'debug'],
                help='set logging verbosity (default info)')


        subparsers = parser.add_subparsers(
                title='subcommands', metavar='COMMAND', dest='command',
                description='run with "<command> --help" to see options for '
                            'individual commands')
        subparsers.required = True

        for name, command in self.commands.items():
            subparser = subparsers.add_parser(name, help=command.description)
            command.add_args(subparser)

        # enable bash autocompletion if supported
        try:
            import argcomplete

            # use a custom completer that moves non-positional options to the
            # end of the completion list, and excludes --help
            class MyCompleter(argcomplete.CompletionFinder):
                def filter_completions(self, completions):
                    completions = super().filter_completions(completions)
                    if completions:
                        for i, value in enumerate(completions):
                            if not value.startswith('-'):
                                return completions[i:] + completions[:i]
                    return completions

            silent_commands = [c.name for c in self.commands.values()
                               if c.description is None]
            MyCompleter().__call__(parser, exclude=['--help'] + silent_commands)
        except ImportError:
            pass

        self.ctx.args = parser.parse_args()

        if 'jobs' in self.ctx.args:
            self.ctx.jobs = self.ctx.args.jobs

    def _complete_pkg(self, prefix, parsed_args, **kwargs):
        objs = list(self.targets.values())
        objs += list(self.instances.values())
        for package in self._get_deps(objs):
            name = package.ident()
            if name.startswith(prefix):
                yield name

    def _init_context(self):
        self.ctx.hooks = Namespace(post_build=[])

        self.ctx.paths = paths = Namespace()
        paths.setup = self.setup_path
        paths.root = os.path.dirname(self.setup_path)
        paths.infra = os.path.dirname(os.path.dirname(__file__))
        paths.buildroot = os.path.join(paths.root, 'build')
        paths.log = os.path.join(paths.buildroot, 'log')
        paths.debuglog = os.path.join(paths.log, 'debug.txt')
        paths.runlog = os.path.join(paths.log, 'commands.txt')
        paths.packages = os.path.join(paths.buildroot, 'packages')
        paths.targets = os.path.join(paths.buildroot, 'targets')
        paths.pool_results = os.path.join(paths.root, 'results')

        self.ctx.runenv = Namespace()
        self.ctx.cc = 'cc'
        self.ctx.cxx = 'c++'
        self.ctx.ar = 'ar'
        self.ctx.nm = 'nm'
        self.ctx.ranlib = 'ranlib'
        self.ctx.cflags = []
        self.ctx.cxxflags = []
        self.ctx.ldflags = []
        self.ctx.lib_ldflags = []

        self.ctx.starttime = None
        self.ctx.workdir = None
        self.ctx.log = logging.getLogger('autosetup')

    def _create_dirs(self):
        os.makedirs(self.ctx.paths.log, exist_ok=True)
        os.makedirs(self.ctx.paths.packages, exist_ok=True)
        os.makedirs(self.ctx.paths.targets, exist_ok=True)

    def _initialize_logger(self):
        fmt = '%(asctime)s [%(levelname)s] %(message)s'
        datefmt = '%H:%M:%S'

        log = self.ctx.log
        log.setLevel(logging.DEBUG)
        log.propagate = False
        self.ctx.loglevel = getattr(logging, self.ctx.args.verbosity.upper())

        termlog = logging.StreamHandler(sys.stdout)
        termlog.setLevel(self.ctx.loglevel)
        termlog.setFormatter(logging.Formatter(fmt, datefmt))
        log.addHandler(termlog)

        # always write debug log to file
        debuglog = logging.FileHandler(self.ctx.paths.debuglog, mode='w')
        debuglog.setLevel(logging.DEBUG)
        debuglog.setFormatter(logging.Formatter(fmt, '%Y-%m-%d ' + datefmt))
        log.addHandler(debuglog)

        # colorize log if supported
        try:
            import coloredlogs
            coloredlogs.install(logger=log, fmt=fmt, datefmt=datefmt,
                                level=termlog.level)
        except ImportError:
            pass

    def add_command(self, command: Command):
        """
        Register a setup command.

        :param command: The command to register.
        """
        self.commands[command.name] = command
        command.set_maps(self.instances, self.targets, self.packages)

    def add_instance(self, instance: Instance):
        """
        Register an instance. Only registered instances can be referenced in
        commands, so also :doc:`built-in instances <instances>` must be
        registered.

        :param instance: The instance to register.
        """
        self.instances[instance.name] = instance

    def add_target(self, target: Target):
        """
        Register a target. Only registered targets can be referenced in
        commands, so also :doc:`built-in targets <targets>` must be registered.

        :param target: The target to register.
        """
        self.targets[target.name] = target

    def _find_package(self, name):
        for package in get_deps(*self.targets.all(), *self.instances.all()):
            if package.ident() == name:
                return package

    def _run_command(self):
        try:
            self.commands[self.ctx.args.command].run(self.ctx)
        except FatalError as e:
            self.ctx.log.error(str(e))
        except KeyboardInterrupt:
            self.ctx.log.warning('exiting because of keyboard interrupt')
        except Exception as e:
            self.ctx.log.critical('unkown error\n' + traceback.format_exc().rstrip())

    def main(self):
        """
        Run the configured setup:

        #. Parse command-line arguments.
        #. Create build directories and log files.
        #. Run the issued command.
        """
        self.ctx.starttime = datetime.datetime.now()
        self.ctx.workdir = os.getcwd()

        self.add_command(commands.BuildCommand())
        self.add_command(commands.RunCommand())
        self.add_command(commands.ReportCommand())
        self.add_command(commands.CleanCommand())
        self.add_command(commands.ConfigCommand())
        self.add_command(commands.PkgConfigCommand())
        self.add_command(commands.ExecHookCommand())

        self._parse_argv()
        self._create_dirs()
        self._initialize_logger()
        self._run_command()
