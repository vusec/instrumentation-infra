from abc import ABCMeta, abstractmethod


class Instance(metaclass=ABCMeta):
    """
    Abstract base class for instance definitions. Built-in derived classes are
    listed :doc:`here <instances>`.

    Each instance must define a ``name`` attribute that is used to reference
    the instance on the command line. The name must be unique among all
    registered instances.

    :var str name: The instance's name, must be unique.
    """

    name = None

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self):
        return hash('instance-' + self.name)

    def add_build_args(self, parser):
        """
        """
        pass

    def dependencies(self):
        """
        """
        yield from []

    @abstractmethod
    def configure(self, ctx):
        """
        """
        pass

    def prepare_run(self, ctx):
        """
        """
        pass
