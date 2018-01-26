from ..instance import Instance


class Default(Instance):
    name = 'default'

    def __init__(self, llvm):
        self.llvm = llvm

    def dependencies(self):
        yield self.llvm

    def configure(self, ctx):
        self.llvm.configure(ctx)


class DefaultLTO(Default):
    name = 'default-lto'

    def configure(self, ctx):
        self.llvm.configure(ctx, lto=True)
