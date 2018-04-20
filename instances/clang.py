from ..instance import Instance


class Clang(Instance):
    name = 'clang'

    def __init__(self, llvm):
        self.llvm = llvm

    def dependencies(self):
        yield self.llvm

    def configure(self, ctx):
        self.llvm.configure(ctx)
        ctx.cflags += ['-O2']
        ctx.cxxflags += ['-O2']


class ClangLTO(Clang):
    name = 'clang-lto'

    def configure(self, ctx):
        super().configure(ctx)
        ctx.cflags += ['-flto']
        ctx.cxxflags += ['-flto']
        ctx.ldflags += ['-flto']
