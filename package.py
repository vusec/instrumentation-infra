import os.path
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

    def install(self, ctx):
        return NotImplemented

    @abstractmethod
    def clean(self, ctx):
        pass

    def configure(self, ctx):
        return NotImplemented

    def path(self, ctx, *args):
        return os.path.join(ctx.paths.packages, self.ident(), *args)

    #def src_path(self, ctx, *args):
    #    return os.path.join(ctx.paths.packsrc, self.ident(), *args)

    #def build_path(self, ctx, *args):
    #    return os.path.join(ctx.paths.packobj, self.ident(), *args)

    #def install_path(self, ctx, *args):
    #    return os.path.join(ctx.paths.prefix, *args)

    def prefix(self, ctx):
        return self.path(ctx, 'install')

    def install_env(self, ctx):
        ctx.env.PATH += ':' + self.prefix(ctx)
        pass
        #ctx.prefixes
        #paths.packobj = os.path.join(ctx.paths.installroot, 'common')
        #ctx.paths.prefix = 
