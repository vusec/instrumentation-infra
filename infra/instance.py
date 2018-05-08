from abc import ABCMeta, abstractmethod
from argparse import ArgumentParser
from typing import Iterator
from .util import Namespace
from .package import Package


class Instance(metaclass=ABCMeta):
    """
    Abstract base class for instance definitions. Built-in derived classes are
    listed :doc:`here <instances>`.

    Each instance must define a :py:attr:`name` attribute that is used to
    reference the instance on the command line. The name must be unique among
    all registered instances.
    """

    #: The instance's name, must be unique.
    name: str = None

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self):
        return hash('instance-' + self.name)

    def add_build_args(self, parser: ArgumentParser):
        """
        """
        pass

    def dependencies(self) -> Iterator[Package]:
        """
        """
        yield from []

    @abstractmethod
    def configure(self, ctx: Namespace):
        """
        """
        pass

    def prepare_run(self, ctx: Namespace):
        """
        """
        pass
