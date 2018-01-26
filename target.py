import os
from abc import ABCMeta, abstractmethod


class Target(metaclass=ABCMeta):
    @property
    @abstractmethod
    def name(self):
        pass

    def add_build_args(self, parser):
        pass

    def dependencies(self):
        yield from []

    @abstractmethod
    def is_fetched(self, ctx):
        pass

    @abstractmethod
    def fetch(self, ctx):
        pass

    @abstractmethod
    def build(self, ctx, instance):
        pass

    @abstractmethod
    def link(self, ctx, instance):
        pass

    def binary_paths(self, ctx, instance):
        raise NotImplementedError(self.__class__.__name__)

    def run_hooks_post_build(self, ctx, instance):
        for binary in self.binary_paths(ctx, instance):
            for hook in ctx.hooks.post_build:
                os.chdir(os.path.dirname(binary))
                hook(ctx, binary)
