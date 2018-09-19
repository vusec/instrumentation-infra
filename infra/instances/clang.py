from typing import Optional, Union
from ..instance import Instance
from ..packages import LLVM, Gperftools


class Clang(Instance):
    """
    Sets ``clang`` as the compiler. The version of clang used is determined by
    the LLVM package passed to the constructor.

    By default, `-O2` optimization is set in CFLAGS and CXXFLAGS. This can be
    customized by setting **optlevel** to 0/1/2/3/s.

    **alloc** can be **system** (the default) or **tcmalloc**. For custom
    tcmalloc hackery, overwrite the ``gperftools`` property of this package
    with a custom :class:`Gperftools` object.

    :name: clang[-O<optlevel>][-lto][-tcmalloc]
    :param llvm: an LLVM package containing the relevant clang version
    :param optlevel: optimization level for ``-O`` (default: 2)
    :param lto: whether to apply link-time optimizations
    :param alloc: which allocator to use (default: system)
    """

    def __init__(self,
                 llvm: LLVM,
                 *,
                 optlevel: Union[int, str] = 2,
                 lto = False,
                 alloc = 'system'):
        assert optlevel in (0, 1, 2, 3, 's'), 'invalid optimization level'
        assert not (lto and optlevel == 0), 'LTO needs compile-time opts'
        assert alloc in ('system', 'tcmalloc'), 'unsupported allocator'

        self.llvm = llvm
        self.optflag = '-O' + str(optlevel)
        self.lto = lto
        self.alloc = alloc

        if self.alloc == 'tcmalloc':
            self.gperftools = Gperftools('master')

    @property
    def name(self):
        name = 'clang'
        if self.optflag != '-O2':
            name += self.optflag
        if self.lto:
            name += '-lto'
        if self.alloc != 'system':
            name += '-' + self.alloc
        return name

    def dependencies(self):
        yield self.llvm
        if self.alloc == 'tcmalloc':
            yield self.gperftools

    def configure(self, ctx):
        self.llvm.configure(ctx)

        if self.alloc == 'tcmalloc':
            self.gperftools.configure(ctx)
        else:
            assert self.alloc == 'system'

        ctx.cflags += [self.optflag]
        ctx.cxxflags += [self.optflag]

        if self.lto:
            ctx.cflags += ['-flto']
            ctx.cxxflags += ['-flto']
            ctx.ldflags += ['-flto']
