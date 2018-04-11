import os
import shutil
from abc import ABCMeta, abstractmethod


class Target(metaclass=ABCMeta):
    name = None

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.name == self.name

    def __hash__(self):
        return hash('target-' + self.name)

    def add_build_args(self, parser):
        pass

    def add_run_args(self, parser):
        pass

    def dependencies(self):
        yield from []

    def path(self, ctx, *args):
        return os.path.join(ctx.paths.targets, self.name, *args)

    def goto_rootdir(self, ctx):
        path = self.path(ctx)
        os.makedirs(path, exist_ok=True)
        os.chdir(path)

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

    def run(self, ctx, instance, args):
        raise NotImplementedError(self.__class__.__name__)

    def is_clean(self, ctx):
        return not os.path.exists(self.path(ctx))

    def clean(self, ctx):
        shutil.rmtree(self.path(ctx))

    def binary_paths(self, ctx, instance):
        raise NotImplementedError(self.__class__.__name__)

    def run_hooks_post_build(self, ctx, instance):
        for binary in self.binary_paths(ctx, instance):
            absbin = os.path.abspath(binary)
            basedir = os.path.dirname(absbin)
            for hook in ctx.hooks.post_build:
                os.chdir(basedir)
                hook(ctx, absbin)
