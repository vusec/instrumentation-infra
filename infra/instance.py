from abc import ABCMeta, abstractmethod
from typing import Iterator
from argparse import ArgumentParser

from .context import Context
from .package import Package


class Instance(metaclass=ABCMeta):
    """
    Abstract base class for instance definitions. Built-in derived classes are
    listed :doc:`here <instances>`.

    Each instance must define a :py:attr:`name` attribute that is used to
    reference the instance on the command line. The name must be unique among
    all registered instances.

    An instance changes variables in the :py:attr:`configuration context
    <Setup.ctx>` that are used to apply instrumentation while building a target
    by :func:`Target.build` and :func:`Target.link`. This is done by
    :func:`configure`.

    Additionally, instances that need runtime support, such as a shared library,
    can implement :func:`prepare_run` which is called by the ``run`` command
    just before running the target with :func:`Target.run`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The instance's name, must be unique."""
        pass

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self) -> int:
        return hash("instance-" + self.name)

    def __repr__(self) -> str:
        return f"<'{self.name}' instance at {id(self):#x} (hash: {self.__hash__()})>"

    def __str__(self) -> str:
        return self.name

    def add_build_args(self, parser: ArgumentParser) -> None:
        """
        Extend the command-line arguments for the :ref:`build <usage-build>`
        command with custom arguments for this instance. These arguments end up
        in the global context, so it is a good idea to prefix them with the
        instance name to avoid collisions with other instances and targets.

        Use this to enable build flags for your instance on the command line,
        rather than having to create separate instances for every option when
        experimenting.

        :param parser: the argument parser to extend
        """
        pass

    def dependencies(self) -> Iterator[Package]:
        """
        Specify dependencies that should be built and installed in the run
        environment before building a target with this instance. Called before
        :func:`configure` and :func:`prepare_run`.
        """
        yield from []

    @abstractmethod
    def configure(self, ctx: Context) -> None:
        """
        Modify context variables to change how a target is built.

        Typically, this would set one or more of
        ``ctx.{cc,cxx,cflags,cxxflags,ldflags,hooks.post_build}``. It is
        recommended to use ``+=`` rather than ``=`` when assigning to lists in
        the context to avoid undoing changes by dependencies.

        Any custom command-line arguments set by :func:`add_build_args` are
        available here in ``ctx.args``.

        :param ctx: the configuration context
        """
        pass

    def prepare_run(self, ctx: Context) -> None:
        """
        Modify context variables to change how a target is run.

        Typically, this would change ``ctx.runenv``, e.g., by setting
        ``ctx.runenv.LD_LIBRARY_PATH``. :func:`Target.run` is expected to call
        :func:`util.run` which will inherit the modified environment.

        :param ctx: the configuration context
        """
        pass
