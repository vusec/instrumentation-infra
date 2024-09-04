import os
import sys
import logging
import argparse
import datetime
import platform
import traceback

from multiprocessing import cpu_count

from . import commands
from .command import Command, get_deps
from .context import Context, ContextPaths
from .instance import Instance
from .package import Package
from .target import Target
from .util import FatalError, Index, LazyIndex
from .util import get_stream_formatter, get_file_formatter

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

    :func:`main` creates a :class:`context <context.Context>` that it passes
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

    ctx: Context
    instances: Index[Instance]
    targets: Index[Target]
    commands: Index[Command]

    def __init__(self, setup_path: str):
        """
        :param setup_path: Path to the script running :func:`Setup.main`.
                           Needed to allow build scripts to call back into the
                           setup script for build hooks.
        """
        self.instances = Index("instance")
        self.targets = Index("target")
        self.commands = Index("command")
        self.packages = LazyIndex("package", self._find_package)

        logger = logging.getLogger("infra")

        infra_path = os.path.dirname(os.path.dirname(__file__))
        setup_path = os.path.abspath(setup_path)
        workdir = os.getcwd()
        paths = ContextPaths(infra_path, setup_path, workdir)
        self.ctx = Context(paths, logger)

        self.ctx.arch = platform.machine()

    def _parse_argv(self) -> None:
        parser = argparse.ArgumentParser(
            description="Frontend for building/running instrumented benchmarks",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )

        # global options
        parser.add_argument(
            "-v",
            "--verbosity",
            default="info",
            choices=["critical", "error", "warning", "info", "debug"],
            help="Set logging verbosity of infrastructure utility",
        )
        parser.add_argument(
            "-j",
            "--jobs",
            default=min(cpu_count(), 64),
            help="Set the number of parallel jobs; not the same as --parallelmax",
        )

        subparsers = parser.add_subparsers(
            title="subcommands",
            metavar="COMMAND",
            dest="command",
            description='Run with "<command> --help" to see options for individual commands',
            required=True,
        )

        for name, command in self.commands.items():
            subparser = subparsers.add_parser(
                name=name,
                help=command.description,
                formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            )
            command.add_args(subparser)

        # enable bash autocompletion if supported
        try:
            import argcomplete

            # use a custom completer that moves non-positional options to the
            # end of the completion list, and excludes --help
            class MyCompleter(argcomplete.CompletionFinder):  # type: ignore
                def filter_completions(self, completions: list[str]) -> list[str]:
                    completions = super().filter_completions(completions)
                    if completions:
                        for i, value in enumerate(completions):
                            if not value.startswith("-"):
                                return completions[i:] + completions[:i]
                    return completions

            silent_commands = [c.name for c in self.commands.values() if c.description is None]
            MyCompleter()(parser, exclude=["--help"] + silent_commands)
        except ImportError:
            self.ctx.log.warning("Failed to set Python command-line autocompletion")

        self.ctx.args = parser.parse_args()

        if "jobs" in self.ctx.args:
            self.ctx.jobs = self.ctx.args.jobs

    def _create_dirs(self) -> None:
        os.makedirs(self.ctx.paths.log, exist_ok=True)
        os.makedirs(self.ctx.paths.packages, exist_ok=True)
        os.makedirs(self.ctx.paths.targets, exist_ok=True)

    def _initialize_logger(self) -> None:
        # Store the user-configured verbosity level
        self.ctx.loglevel = getattr(logging, self.ctx.args.verbosity.upper())

        # Set logger to DEBUG (set lvl per handler instead) & disable propagation to ancestors
        self.ctx.log.setLevel(logging.DEBUG)
        self.ctx.log.propagate = False

        # Set the handler for writing to stdout (not stderr!)
        strm_hndlr = logging.StreamHandler(stream=sys.stdout)
        strm_hndlr.setLevel(self.ctx.loglevel)
        strm_hndlr.setFormatter(get_stream_formatter())
        self.ctx.log.addHandler(strm_hndlr)

        # Add a file handler for outputting all messages (even when logging level is set lower
        # to debug.txt); also strips ANSI escape sequences from the messages before outputting
        file_hndlr = logging.FileHandler(self.ctx.paths.debuglog, mode="w")
        file_hndlr.setLevel(logging.DEBUG)
        file_hndlr.setFormatter(get_file_formatter())
        self.ctx.log.addHandler(file_hndlr)

    def _finalize_logger(self) -> None:
        for handler in self.ctx.log.handlers:
            handler.flush()
            handler.close()

        if self.ctx.runlog_file is not None:
            self.ctx.runlog_file.flush()
            self.ctx.runlog_file.close()

    def add_command(self, command: Command) -> None:
        """
        Register a setup command.

        :param command: The command to register.
        """
        self.commands[command.name] = command
        command.instances = self.instances
        command.targets = self.targets
        command.packages = self.packages

    def add_instance(self, instance: Instance) -> None:
        """
        Register an instance. Only registered instances can be referenced in
        commands, so also :doc:`built-in instances <instances>` must be
        registered.

        :param instance: The instance to register.
        """
        if not isinstance(instance.name, str):
            raise TypeError("Instance must have name of type str.")

        self.instances[instance.name] = instance

    def add_target(self, target: Target) -> None:
        """
        Register a target. Only registered targets can be referenced in
        commands, so also :doc:`built-in targets <targets>` must be registered.

        :param target: The target to register.
        """
        if not isinstance(target.name, str):
            raise TypeError("Target must have name of type str.")

        self.targets[target.name] = target

    def _find_package(self, name: str) -> Package:
        for package in get_deps(*self.targets.all(), *self.instances.all()):
            if package.ident() == name:
                return package
        raise ValueError(f"Unknown package {name}")

    def _run_command(self) -> None:
        try:
            self.commands[self.ctx.args.command].run(self.ctx)
        except FatalError as e:
            self.ctx.log.error(str(e))
        except KeyboardInterrupt:
            self.ctx.log.warning("exiting because of keyboard interrupt")
        except Exception:
            self.ctx.log.critical("unknown error\n" + traceback.format_exc().rstrip())

    def main(self) -> None:
        """
        Run the configured setup:

        #. Parse command-line arguments.
        #. Create build directories and log files.
        #. Run the issued command.
        """
        self.ctx.starttime = datetime.datetime.now()

        self.add_command(commands.BuildCommand())
        self.add_command(commands.PkgBuildCommand())
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
        self._finalize_logger()
