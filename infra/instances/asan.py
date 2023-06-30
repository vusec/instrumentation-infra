from dataclasses import dataclass
from typing import Optional, Union

from ..context import Context
from ..packages import LLVM
from .clang import Clang


@dataclass
class ASan(Clang):
    """
    AddressSanitizer instance. Added ``-fsanitize=address`` plus any
    configuration options at compile time and link time, and sets
    ``ASAN_OPTIONS`` at runtime.

    Runtime options are currently hard-coded to the following:

    - ``alloc_dealloc_mismatch=0``
    - ``detect_odr_violation=0``
    - ``detect_leaks=0``

    :name: asan[-heap|-nostack|-noglob][-wo][-lto]
    :param llvm: an LLVM package with compiler-rt included
    :param stack: toggle stack instrumentation
    :param temporal: toggle temporal safety (`False` sets quarantine size to 0)
    :param glob: toggle globals instrumentation
    :param check_writes: toggle checks on stores
    :param check_reads: toggle checks on loads
    :param lto: perform link-time optimizations
    :param redzone: minimum heap redzone size (default 16, always 32 for stack)
    """

    llvm: LLVM
    temporal: bool = True
    stack: bool = True
    glob: bool = True
    check_writes: bool = True
    check_reads: bool = True
    redzone: Optional[int] = None
    optlevel: Union[int, str] = 2
    lto: bool = False

    def __post_init__(self) -> None:
        assert self.llvm.compiler_rt, "ASan needs LLVM with runtime support"
        super().__init__(self.llvm, lto=self.lto, optlevel=self.optlevel)
        assert (
            not self.check_reads or self.check_writes
        ), "will not check reads without writes"
        if self.redzone is not None:
            assert isinstance(self.redzone, int), "redzone size must be a number"

    @property
    def name(self) -> str:
        name = "asan"

        if self.redzone is not None:
            name += str(self.redzone)

        if not self.temporal:
            name += "-spatial"

        if not self.stack and not self.glob:
            name += "-heap"
        elif not self.stack:
            name += "-nostack"
        elif not self.glob:
            name += "-noglob"

        if not self.check_reads:
            name += "-wo" if self.check_writes else "-nochecks"

        if self.lto:
            name += "-lto"

        return name

    def configure(self, ctx: Context) -> None:
        super().configure(ctx)
        cflags = ["-g"]
        cflags += ["-fsanitize=address"]
        if not self.stack:
            cflags = ["-mllvm", "-asan-stack=0"]
        if not self.glob:
            cflags += ["-mllvm", "-asan-globals=0"]
        if not self.check_reads:
            cflags += ["-mllvm", "-asan-instrument-reads=false"]
        if not self.check_writes:
            cflags += ["-mllvm", "-asan-instrument-writes=false"]
            cflags += ["-mllvm", "-asan-instrument-atomics=false"]
        ctx.cflags += cflags
        ctx.cxxflags += cflags
        ctx.ldflags += ["-g"]
        ctx.ldflags += ["-fsanitize=address"]

    def prepare_run(self, ctx: Context) -> None:
        opts = {
            "alloc_dealloc_mismatch": 0,
            "detect_odr_violation": 0,
            "detect_leaks": 0,
        }

        if self.redzone is not None:
            opts["redzone"] = self.redzone

        if not self.temporal:
            opts["detect_stack_use_after_return"] = 0
            opts["thread_local_quarantine_size_kb"] = 0
            opts["quarantine_size_mb"] = 0

        if not self.check_writes:
            opts["replace_intrin"] = 0

        ctx.runenv["ASAN_OPTIONS"] = ":".join(
            f"{opt[0]}={opt[1]}" for opt in opts.items()
        )
