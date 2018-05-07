import os
import shutil
from abc import ABCMeta, abstractmethod


class Target(metaclass=ABCMeta):
    """
    Abstract base class for target definitions. Built-in derived classes are
    listed :doc:`here <targets>`.

    Each target must define a :any:`name` attribute that is used to reference
    the target on the command line. The name must be unique among all
    registered targets. Each target must also implement a number of methods
    that are called by :class:`Setup` when running commands.

    The ``build`` command follows the following steps for each target:

    #. It calls :func:`add_build_args` to include any custom command-line
       arguments for this target, and then parses the command-line arguments.
    #. It calls :func:`is_fetched` to see if the source code for this target
       has been downloaded yet.
    #. If ``is_fetched() == False``, it calls :func:`fetch`.
    #. All packages listed by :func:`dependencies` are built and installed into
       the environment (i.e., ``PATH`` and such are set).
    #. Unless ``--relink`` was passed on the command line, it calls
       :func:`build` to build the target object files.
    #. It calls :func:`link` to link the target binaries.
    #. If any post-build hooks are installed by the current instance, it calls
       :func:`binary_paths` to get paths to all built binaries. These are then
       passed directly to the build hooks.

    For the ``run`` command:

    #. It calls :func:`add_run_args` to include any custom command-line
       arguments for this target.
    #. If ``--build`` was specified, it performs all build steps above.
    #. It calls :func:`run` to run the target binaries.

    For the ``clean`` command:

    #. It calls :func:`is_clean` to see if any build files exist for this target.
    #. If ``is_clean() == False``, it calls :func:`clean`.

    Naturally, when defining your own target, all the methods listed above must
    have working implementations. Some implementations are optional and some
    have a default implementation that works for almost all cases (see docs
    below), but the following are mandatory to implement for each new target:
    :func:`is_fetched`, :func:`fetch`, :func:`build`, :func:`link` and
    :func:`run`.

    :var str name: The target's name, must be unique.
    """

    #: The target's name, must be unique.
    name = None

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self):
        return hash('target-' + self.name)

    def add_build_args(self, parser):
        """
        Extend the command-line arguments for the ``build`` command with
        custom arguments for this target. These arguments end up in the global
        namespace, so it is a good idea to prefix them with the target name to
        avoid collisions with other targets.

        For example, :class:`SPEC2006 <targets.SPEC2006>` defines
        ``--spec2006-benchmarks`` (rather than ``--benchmarks``).

        :param argparse.ArgumentParser parser: the argument parser to extend
        """
        pass

    def add_run_args(self, parser):
        """
        Extend the command-line arguments for the ``run`` command with custom
        arguments for this target. Since only a single target can be run at a
        time, prefixing to avoid naming conflicts with other targets is not
        necessary here.

        For example, :class:`SPEC2006 <targets.SPEC2006>` defines
        ``--benchmarks`` and ``--test``.

        :param argparse.ArgumentParser parser: the argument parser to extend
        """
        pass

    def dependencies(self):
        """
        Specify dependencies that should be built and installed in the run
        environment before building this target.

        :returns: the packages this target depends on
        :rtype: Iterator[Package]
        """
        yield from []

    def path(self, ctx, *args):
        """
        Get the absolute path to the build directory of this target, optionally
        suffixed with a subpath.

        :param util.Namespace ctx: the configuration context
        :param Iterable[str] args: additional subpath to pass to :func:`os.path.join`
        :returns: the requested path
        :rtype: str
        """
        return os.path.join(ctx.paths.targets, self.name, *args)

    def goto_rootdir(self, ctx):
        path = self.path(ctx)
        os.makedirs(path, exist_ok=True)
        os.chdir(path)

    @abstractmethod
    def is_fetched(self, ctx):
        """
        :param util.Namespace ctx: the configuration context
        :rtype: bool
        """
        pass

    @abstractmethod
    def fetch(self, ctx):
        """
        :param util.Namespace ctx: the configuration context
        """
        pass

    @abstractmethod
    def build(self, ctx, instance, pool):
        """
        :param util.Namespace ctx: the configuration context
        :param Instance instance: instance to build
        :param pool: parallel process pool if ``--parallel`` is specified
        :type pool: parallel.Pool or None
        """
        pass

    @abstractmethod
    def link(self, ctx, instance):
        """
        :param util.Namespace ctx: the configuration context
        :param Instance instance: instance to link
        """
        pass

    @abstractmethod
    def run(self, ctx, instance, pool):
        """
        :param util.Namespace ctx: the configuration context
        :param Instance instance: instance to run
        :param pool: parallel process pool if ``--parallel`` is specified
        :type pool: parallel.Pool or None
        :raises NotImplementedError: unless implemented
        """
        pass

    def is_clean(self, ctx):
        """
        :param util.Namespace ctx: the configuration context
        :rtype: bool
        """
        return not os.path.exists(self.path(ctx))

    def clean(self, ctx):
        """
        :param util.Namespace ctx: the configuration context
        """
        shutil.rmtree(self.path(ctx))

    def binary_paths(self, ctx, instance):
        """
        :param util.Namespace ctx: the configuration context
        :param Instance instance: instance to get paths for
        :returns: paths to binaries
        :rtype: List[str]
        :raises NotImplementedError: unless implemented
        """
        raise NotImplementedError(self.__class__.__name__)

    def run_hooks_post_build(self, ctx, instance):
        for binary in self.binary_paths(ctx, instance):
            absbin = os.path.abspath(binary)
            basedir = os.path.dirname(absbin)
            for hook in ctx.hooks.post_build:
                os.chdir(basedir)
                hook(ctx, absbin)
