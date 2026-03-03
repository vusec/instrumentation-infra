from typing import Iterator

from ..context import Context
from ..instance import Instance
from ..package import Package
from ..packages import LLVM, Gperftools
from ..util import FatalError


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

    def __init__(self, llvm: LLVM, *, optlevel: int | str = 2, lto: bool = False, alloc: str = "system"):
        assert optlevel in (0, 1, 2, 3, "s"), "invalid optimization level"
        assert not (lto and optlevel == 0), "LTO needs compile-time opts"
        assert alloc in ("system", "tcmalloc"), "unsupported allocator"

        self.llvm = llvm
        self.optflag = "-O" + str(optlevel)
        self.lto = lto
        self.alloc = alloc

        if self.alloc == "tcmalloc":
            self.gperftools = Gperftools("master")

    @property
    def name(self) -> str:
        name = "clang"
        if self.optflag != "-O2":
            name += self.optflag
        if self.lto:
            name += "-lto"
        if self.alloc != "system":
            name += "-" + self.alloc
        return name

    def dependencies(self) -> Iterator[Package]:
        yield self.llvm
        if self.alloc == "tcmalloc":
            yield self.gperftools

    def configure(self, ctx: Context) -> None:

        if self.alloc == "tcmalloc":
            self.gperftools.configure(ctx)
        else:
            assert self.alloc == "system"

        ctx.cflags += [self.optflag]
        ctx.cxxflags += [self.optflag]

        if self.lto:
            ctx.cflags += ["-flto"]
            ctx.cxxflags += ["-flto"]
            ctx.ldflags += ["-flto"]
            ctx.lib_ldflags += ["-flto"]


class ParameterizedClang(Clang):
    """
    Extends :class:`Clang` with custom naming and extra compile/link flags.

    Extra flags are appended after :class:`Clang`'s own flags during
    :func:`configure`.

    :param llvm: an LLVM package containing the relevant clang version
    :param instance_name: custom name for this instance
    :param extra_cflags: additional C compiler flags
    :param extra_cxxflags: additional C++ compiler flags
    :param extra_ldflags: additional linker flags
    :param extra_lib_ldflags: additional library linker flags
    :param optlevel: optimization level for ``-O`` (default: 2)
    :param lto: whether to apply link-time optimizations
    :param alloc: which allocator to use (default: system)
    """

    def __init__(
        self,
        llvm: LLVM,
        instance_name: str,
        extra_cflags: list[str] | None = None,
        extra_cxxflags: list[str] | None = None,
        extra_ldflags: list[str] | None = None,
        extra_lib_ldflags: list[str] | None = None,
        *,
        optlevel: int | str = 2,
        lto: bool = False,
        alloc: str = "system",
    ) -> None:
        super().__init__(llvm, optlevel=optlevel, lto=lto, alloc=alloc)
        self.instance_name = instance_name
        self.extra_cflags = extra_cflags or []
        self.extra_cxxflags = extra_cxxflags or []
        self.extra_ldflags = extra_ldflags or []
        self.extra_lib_ldflags = extra_lib_ldflags or []

    @property
    def name(self) -> str:
        return self.instance_name

    def add_all_flags(self, flags: list[str]) -> None:
        """Append ``flags`` to all flag lists (cflags, cxxflags, ldflags, lib_ldflags)."""
        self.extra_cflags.extend(flags)
        self.extra_cxxflags.extend(flags)
        self.extra_ldflags.extend(flags)
        self.extra_lib_ldflags.extend(flags)

    def add_linker_flags(self, flags: list[str]) -> None:
        """Append ``flags`` to the extra linker flags."""
        self.extra_ldflags.extend(flags)

    def add_lib_linker_flags(self, flags: list[str]) -> None:
        """Append ``flags`` to the extra library linker flags."""
        self.extra_lib_ldflags.extend(flags)

    def configure(self, ctx: Context) -> None:
        super().configure(ctx)
        ctx.cflags += self.extra_cflags
        ctx.cxxflags += self.extra_cxxflags
        ctx.ldflags += self.extra_ldflags
        ctx.lib_ldflags += self.extra_lib_ldflags


class RandomizingClang(ParameterizedClang):
    """
    Extends :class:`ParameterizedClang` to substitute the ``RNGSEED``
    placeholder in all compiler flags with the actual random seed from
    ``ctx.rngseed``.

    This is intended to be used with the ``run-random`` command, which
    sets ``ctx.rngseed`` to a fresh value for each iteration.

    Raises :class:`FatalError` if ``ctx.rngseed`` is empty when
    :func:`configure` is called.
    """

    def configure(self, ctx: Context) -> None:
        super().configure(ctx)
        if not ctx.rngseed:
            raise FatalError(
                f"Instance '{self.name}' requires an RNG seed. "
                "Currently only the run-random command creates such a seed."
            )

        def replace_placeholder(flag: str) -> str:
            return flag.replace("RNGSEED", ctx.rngseed)

        ctx.cflags = list(map(replace_placeholder, ctx.cflags))
        ctx.cxxflags = list(map(replace_placeholder, ctx.cxxflags))
        ctx.ldflags = list(map(replace_placeholder, ctx.ldflags))
        ctx.lib_ldflags = list(map(replace_placeholder, ctx.lib_ldflags))
