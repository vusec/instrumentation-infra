from ..instance import Instance


class Default(Instance):
    name = 'default'

    def __init__(self, llvm):
        self.llvm = llvm

    def dependencies(self):
        yield self.llvm

    def configure(self, ctx):
        self.llvm.configure(ctx)
        ctx.cflags += ['-O2']
        ctx.cxxflags += ['-O2']


class DefaultLTO(Default):
    name = 'default-lto'

    def configure(self, ctx):
        super().configure(ctx)
        ctx.cflags += ['-flto']
        ctx.cxxflags += ['-flto']
        ctx.ldflags += ['-flto']
