import os
import shutil
from abc import ABCMeta, abstractmethod


class Package(metaclass=ABCMeta):
    def __eq__(self, other):
        return other.ident() == self.ident()

    def __hash__(self):
        return hash(self.ident())

    @abstractmethod
    def ident(self):
        pass

    def dependencies(self):
        yield from []

    @abstractmethod
    def is_fetched(self, ctx):
        pass

    @abstractmethod
    def is_built(self, ctx):
        pass

    @abstractmethod
    def is_installed(self, ctx):
        pass

    @abstractmethod
    def fetch(self, ctx):
        return NotImplemented

    @abstractmethod
    def build(self, ctx):
        pass

    @abstractmethod
    def install(self, ctx):
        pass

    def clean(self, ctx):
        shutil.rmtree(self.path(ctx))

    def configure(self, ctx):
        return NotImplemented

    def path(self, ctx, *args):
        return os.path.join(ctx.paths.packages, self.ident(), *args)

    def install_env(self, ctx):
        prefix = self.path(ctx, 'install')
        if os.path.exists(prefix):
            ctx.prefixes.insert(0, prefix)

    def goto_rootdir(self, ctx):
        path = self.path(ctx)
        os.makedirs(path, exist_ok=True)
        os.chdir(path)
