import sys
import os
import shutil
from abc import ABCMeta, abstractmethod


class Package(metaclass=ABCMeta):
    def __eq__(self, other):
        return isinstance(other, self.__class__) and \
               other.ident() == self.ident()

    def __hash__(self):
        return hash('package-' + self.ident())

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

    def is_clean(self, ctx):
        return not os.path.exists(self.path(ctx))

    def clean(self, ctx):
        shutil.rmtree(self.path(ctx))

    def configure(self, ctx):
        return NotImplemented

    def path(self, ctx, *args):
        return os.path.join(ctx.paths.packages, self.ident(), *args)

    def install_env(self, ctx):
        bins = self.path(ctx, 'install/bin')
        if os.path.exists(bins):
            ctx.runenv.PATH.insert(0, bins)

        libs = self.path(ctx, 'install/lib')
        if os.path.exists(libs):
            ctx.runenv.LD_LIBRARY_PATH.insert(0, libs)

    def goto_rootdir(self, ctx):
        path = self.path(ctx)
        os.makedirs(path, exist_ok=True)
        os.chdir(path)

    def pkg_config_options(self, ctx):
        yield ('--root',
               'absolute root path',
               self.path(ctx))
        yield ('--prefix',
               'absolute install path',
               self.path(ctx, 'install'))
