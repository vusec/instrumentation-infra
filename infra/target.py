import argparse
import os
import shutil
from abc import ABCMeta, abstractmethod
from typing import Iterable, Iterator, Mapping

from .context import Context
from .instance import Instance
from .package import Package
from .parallel import Pool
from .util import ResultDict


class Target(metaclass=ABCMeta):
    """
    Abstract base class for target definitions. Built-in derived classes are
    listed :doc:`here <targets>`.

    Each target must define a :py:attr:`name` attribute that is used to
    reference the target on the command line. The name must be unique among all
    registered targets. Each target must also implement a number of methods
    that are called by :class:`Setup` when running commands.

    The :ref:`build <usage-build>` command follows the following steps for each
    target:

    #. It calls :func:`add_build_args` to include any custom command-line
       arguments for this target, and then parses the command-line arguments.
    #. It calls :func:`is_fetched` to see if the source code for this target
       has been downloaded yet.
    #. If ``is_fetched() == False``, it calls :func:`fetch`.
    #. It calls :func:`Instance.configure` on the instance that will be passed
       to :func:`build`.
    #. All packages listed by :func:`dependencies` are built and installed into
       the environment (i.e., ``PATH`` and such are set).
    #. It calls :func:`build` to build the target binaries.
    #. If any post-build hooks are installed by the current instance, it calls
       :func:`binary_paths` to get paths to all built binaries. These are then
       passed directly to the build hooks.

    For the :ref:`run <usage-run>` command:

    #. It calls :func:`add_run_args` to include any custom command-line
       arguments for this target.
    #. If ``--build`` was specified, it performs all build steps above.
    #. It calls :func:`Instance.prepare_run` on the instance that will be passed
       to :func:`run`.
    #. It calls :func:`run` to run the target binaries.

    For the :ref:`clean <usage-clean>` command:

    #. It calls :func:`is_clean` to see if any build files exist for this target.
    #. If ``is_clean() == False``, it calls :func:`clean`.

    For the :ref:`report <usage-report>` command:

    #. It calls :func:`parse_outfile` for every log file before creating the
       report.

    Naturally, when defining your own target, all the methods listed above must
    have working implementations. Some implementations are optional and some
    have a default implementation that works for almost all cases (see docs
    below), but the following are mandatory to implement for each new target:
    :func:`is_fetched`, :func:`fetch`, :func:`build` and :func:`run`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The target's name, must be unique."""
        pass

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self) -> int:
        return hash("target-" + self.name)

    def __repr__(self) -> str:
        return f"<'{self.name}' target at {id(self):#x} (hash: {self.__hash__()})>"

    def __str__(self) -> str:
        return self.name

    @property
    def aggregation_field(self) -> str:
        """The default reportable field to group by when aggregating results."""
        return ""

    def reportable_fields(self) -> Mapping[str, str]:
        """
        Run-time statistics reported by this target. Examples include the
        runtime of the benchmark, its memory or CPU utilization, or
        benchmarks-specific measurements such as throughput and latency of
        requests.

        The format is a dictionary mapping the name of the statistic to a
        (human-readable) description. For each entry, the name is looked up
        in the logs and saved per run.
        """
        return {}

    def add_build_args(self, parser: argparse.ArgumentParser) -> None:
        """
        Extend the command-line arguments for the :ref:`build <usage-build>`
        command with custom arguments for this target. These arguments end up
        in the global context, so it is a good idea to prefix them with the
        target name to avoid collisions with other targets and instances.

        For example, :class:`SPEC2006 <targets.SPEC2006>` defines
        ``--spec2006-benchmarks`` (rather than ``--benchmarks``).

        :param parser: the argument parser to extend
        """
        pass

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        """
        Extend the command-line arguments for the :ref:`run <usage-run>`
        command with custom arguments for this target. Since only a single
        target can be run at a time, prefixing to avoid naming conflicts with
        other targets is not necessary here.

        For example, :class:`SPEC2006 <targets.SPEC2006>` defines
        ``--benchmarks`` and ``--test``.

        :param parser: the argument parser to extend
        """
        pass

    def dependencies(self) -> Iterator[Package]:
        """
        Specify dependencies that should be built and installed in the run
        environment before building this target.
        """
        yield from []

    def path(self, ctx: Context, *args: str) -> str:
        """
        Get the absolute path to the build directory of this target, optionally
        suffixed with a subpath.

        :param ctx: the configuration context
        :param args: additional subpath to pass to :func:`os.path.join`
        :returns: ``build/targets/<name>[/<subpath>]``
        """
        return os.path.join(ctx.paths.targets, self.name, *args)

    def goto_rootdir(self, ctx: Context, *args: str) -> None:
        """
        Change directories into the local directory for this package; optionally
        suffixed with a subpath. Creates new directories if they did not exist.

        :param ctx: the configuration context
        :param args: additional subpath relative to this package's base directory
        """
        path = self.path(ctx, *args)

        if os.path.isfile(path):
            ctx.log.warning(f"{path} points to a file; switching to parent: {os.path.dirname(path)}")
            path = os.path.dirname(path)

        os.makedirs(path, exist_ok=True)
        os.chdir(path)

    @abstractmethod
    def is_fetched(self, ctx: Context) -> bool:
        """
        Returns ``True`` if :func:`fetch` should be called before building.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def fetch(self, ctx: Context) -> None:
        """
        Fetches the source code for this target. This step is separated from
        :func:`build` because the ``build`` command first fetches all packages
        and targets before starting the build process.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def build(self, ctx: Context, instance: Instance, pool: Pool | None = None) -> None:
        """
        Build the target object files. Called some time after :func:`fetch` (see
        :class:`above <Target>`).

        ``ctx.runenv`` will have been populated with the exported environments
        of all packages returned by :func:`dependencies` (i.e.,
        :func:`Package.install_env` has been called for each dependency). This
        means that when you call :func:`util.run` here, the programs and
        libraries from the dependencies are available in ``PATH`` and
        ``LD_LIBRARY_PATH``, so you don't need to reference them with absolute
        paths.

        The build function should respect variables set in the configuration
        context such as ``ctx.cc`` and ``ctx.cflags``, passing them to the
        underlying build system as required. :py:attr:`Setup.ctx` shows default
        variables in the context that should at least be respected, but complex
        instances may optionally overwrite them to be used by custom targets.

        Any custom command-line arguments set by :func:`add_build_args` are
        available here in ``ctx.args``.

        If ``pool`` is defined (i.e., when ``--parallel`` is passed), the
        target is expected to use :func:`pool.run() <parallel.Pool.run>`
        instead of :func:`util.run` to invoke build commands.

        :param ctx: the configuration context
        :param instance: instance to build
        :param pool: parallel process pool if ``--parallel`` is specified
        """
        pass

    @abstractmethod
    def run(self, ctx: Context, instance: Instance, pool: Pool | None = None) -> None:
        """
        Run the target binaries. This should be done using :func:`util.run` so
        that ``ctx.runenv`` is used (which can be set by an instance or
        dependencies). It is recommended to pass ``teeout=True`` to make the
        output of the process stream to ``stdout``.

        Any custom command-line arguments set by :func:`add_run_args` are
        available here in ``ctx.args``.

        If ``pool`` is defined (i.e., when ``--parallel`` is passed), the
        target is expected to use :func:`pool.run() <parallel.Pool.run>`
        instead of :func:`util.run` to launch runs.

        Implementations of this method should respect the ``--iterations``
        option of the run command.

        :param ctx: the configuration context
        :param instance: instance to run
        :param pool: parallel process pool if ``--parallel`` is specified
        """
        pass

    def parse_outfile(self, ctx: Context, outfile: str) -> Iterator[ResultDict]:
        """
        Callback method for :func:`commands.report.parse_logs`. Used by report
        command to get reportable results.

        :param ctx: the configuration context
        :param outfile: path to outfile to parse
        :raises NotImplementedError: unless implemented
        """
        raise NotImplementedError(self.__class__.__name__)

    def is_clean(self, ctx: Context) -> bool:
        """
        Returns ``True`` if :func:`clean` should be called before cleaning.

        :param ctx: the configuration context
        """
        return not os.path.exists(self.path(ctx))

    def clean(self, ctx: Context) -> None:
        """
        Clean generated files for this target, called by the :ref:`clean
        <usage-clean>` command. By default, this removes
        ``build/targets/<name>``.

        :param ctx: the configuration context
        """
        shutil.rmtree(self.path(ctx))

    def binary_paths(self, ctx: Context, instance: Instance) -> Iterable[str]:
        """
        If implemented, this should return a list of absolute paths to binaries
        created by :func:`build` for the given instance. This is only used if
        the instance specifies post-build hooks. Each hook is called for each of
        the returned paths.

        :param ctx: the configuration context
        :param instance: instance to get paths for
        :returns: paths to binaries
        :raises NotImplementedError: unless implemented
        """
        raise NotImplementedError(self.__class__.__name__)

    def run_hooks_pre_build(self, ctx: Context, instance: Instance) -> None:
        """
        If ctx.hooks.pre_build contains any callable instances, they are called
        in the target's root directory. Each callable received the root path
        of the target as an argument.

        :param Context ctx: the configuration context
        :param Instance instance: instance used to build the target
        """
        if ctx.hooks.pre_build:
            self.goto_rootdir(ctx)  # Run hooks in target root
            for hook in ctx.hooks.pre_build:
                ctx.log.info(f"Running pre-build hook {hook} in {self.path(ctx)}")
                hook(ctx, self.path(ctx))

    def run_hooks_post_build(self, ctx: Context, instance: Instance) -> None:
        """
        If ctx.hooks.post_build contains any callable instances, they are called
        after this target's :func:`build()` function has completed. The callable
        is called once for each compiled binary, as reported by this target's
        :func:`binary_paths()` function.

        :param Context ctx: the configuration context
        :param Instance instance: instance used to get the compiled binaries with
        """
        if ctx.hooks.post_build:
            for binary in self.binary_paths(ctx, instance):
                absbin = os.path.abspath(binary)
                basedir = os.path.dirname(absbin)
                for hook in ctx.hooks.post_build:
                    ctx.log.info(f"Running post-build hook {hook} on {absbin} in {basedir}")
                    os.chdir(basedir)
                    hook(ctx, absbin)
