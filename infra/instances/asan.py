from .clang import Clang
from ..packages import LLVM
from ..util import param_attrs


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
    :param glob: toggle globals instrumentation
    :param only_writes: only instrument writes
    :param lto: perform link-time optimizations
    """
    @param_attrs
    def __init__(self, llvm: LLVM, *, stack=True, glob=True,
                 only_writes=False, lto=False):
        assert llvm.compiler_rt, 'ASan needs LLVM with runtime support'
        super().__init__(llvm, lto=lto)

    @property
    def name(self):
        name = 'asan'

        if not self.stack and not self.glob:
            name += '-heap'
        elif not self.stack:
            name += '-nostack'
        elif not self.glob:
            name += '-noglob'

        if self.only_writes:
            name += '-wo'

        if self.lto:
            name += '-lto'

        return name

    def configure(self, ctx):
        super().configure(ctx)
        cflags = ['-fsanitize=address']
        if not self.stack:
            cflags = ['-mllvm', '-asan-stack=0']
        if not self.glob:
            cflags += ['-mllvm', '-asan-globals=0']
        if self.only_writes:
            cflags += ['-mllvm', '-asan-instrument-reads=false']
        ctx.cflags += cflags
        ctx.cxxflags += cflags
        ctx.ldflags += ['-fsanitize=address']

    def prepare_run(self, ctx):
        opts = {
            'alloc_dealloc_mismatch': 0,
            'detect_odr_violation': 0,
            'detect_leaks': 0,
            # uncomment the following to disable quarantining, thus disabling
            # temporal safety:
            # TODO: make this a configuration option
            #'detect_stack_use_after_return': 0,
            #'thread_local_quarantine_size_kb': 0,
            #'quarantine_size_mb': 0
        }
        ctx.runenv.ASAN_OPTIONS = ':'.join('%s=%s' % i for i in opts.items())
