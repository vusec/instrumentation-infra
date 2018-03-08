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
        prevbinpath = os.getenv('PATH', '').split(':')
        binpath = self.path(ctx, 'install/bin')
        if os.path.exists(binpath):
            ctx.runenv.setdefault('PATH', prevbinpath).insert(0, binpath)

        prevlibpath = os.getenv('LD_LIBRARY_PATH', '').split(':')
        libpath = self.path(ctx, 'install/lib')
        if os.path.exists(libpath):
            ctx.runenv.setdefault('LD_LIBRARY_PATH', prevlibpath).insert(0, libpath)

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
