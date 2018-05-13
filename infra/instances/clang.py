from ..instance import Instance
from ..packages import LLVM


class Clang(Instance):
    """
    Sets ``clang`` as the compiler, and adds ``-O2`` to CFLAGS and CXXFLAGS.

    The version of clang used is determined by the LLVM package passed to the
    constructor.
    """

    name = 'clang'

    def __init__(self, llvm: LLVM):
        """
        :param llvm: an LLVM package containing the relevant clang version
        """
        self.llvm = llvm

    def dependencies(self):
        yield self.llvm

    def configure(self, ctx):
        self.llvm.configure(ctx)
        ctx.cflags += ['-O2']
        ctx.cxxflags += ['-O2']


class ClangLTO(Clang):
    """
    Clang with link-time optimizations (LTO). Same as :class:`Clang` but adds
    ``-flto`` to CFLAGS/CXXFLAGS/LDFLAGS.
    """

    name = 'clang-lto'

    def configure(self, ctx):
        super().configure(ctx)
        ctx.cflags += ['-flto']
        ctx.cxxflags += ['-flto']
        ctx.ldflags += ['-flto']
