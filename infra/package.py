import os
import shutil
from abc import ABCMeta, abstractmethod
from typing import Iterable, Iterator, Tuple
from .util import Namespace


class Package(metaclass=ABCMeta):
    """
    """

    def __eq__(self, other):
        return isinstance(other, self.__class__) and \
               other.ident() == self.ident()

    def __hash__(self):
        return hash('package-' + self.ident())

    @abstractmethod
    def ident(self) -> str:
        """
        """
        pass

    def dependencies(self) -> Iterator['Package']:
        """
        """
        yield from []

    @abstractmethod
    def is_fetched(self, ctx: Namespace) -> bool:
        """
        """
        pass

    @abstractmethod
    def is_built(self, ctx: Namespace) -> bool:
        """
        """
        pass

    @abstractmethod
    def is_installed(self, ctx: Namespace) -> bool:
        """
        """
        pass

    @abstractmethod
    def fetch(self, ctx: Namespace):
        """
        """
        pass

    @abstractmethod
    def build(self, ctx: Namespace):
        """
        """
        pass

    @abstractmethod
    def install(self, ctx: Namespace):
        """
        """
        pass

    def is_clean(self, ctx: Namespace) -> bool:
        """
        """
        return not os.path.exists(self.path(ctx))

    def clean(self, ctx: Namespace):
        """
        """
        shutil.rmtree(self.path(ctx))

    def configure(self, ctx: Namespace):
        """
        """
        pass

    def path(self, ctx: Namespace, *args: Iterable[str]) -> str:
        """
        """
        return os.path.join(ctx.paths.packages, self.ident(), *args)

    def install_env(self, ctx: Namespace):
        """
        """
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

    def pkg_config_options(self, ctx: Namespace) -> Iterator[Tuple[str, str, str]]:
        """
        """
        yield ('--root',
               'absolute root path',
               self.path(ctx))
        yield ('--prefix',
               'absolute install path',
               self.path(ctx, 'install'))
