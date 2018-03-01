from abc import ABCMeta, abstractmethod


class Instance(metaclass=ABCMeta):
    name = None

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self):
        return hash('instance-' + self.name)

    def add_build_args(self, parser):
        pass

    def dependencies(self):
        yield from []

    @abstractmethod
    def configure(self, ctx):
        pass
