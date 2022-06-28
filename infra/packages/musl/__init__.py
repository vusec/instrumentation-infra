import os
import shutil
from typing import List, Optional
from ...package import Package
from ...util import Namespace, run, apply_patch, download, qjoin
from ..gnu import AutoMake
from ..llvm import LLVM


class Musl(Package):
    """
    Musl that should be build with instrumentation (to provide full
    library instrumentation. Musl is significantly easier to build
    with LLVM making it a better candidate than glibc.
    This is intended to be used together with libcxx to build a
    static binary that is completely instrumented (instrumentation of
    all static libraries).

    Finally, you may specify a list of patches to apply before building. These
    may be paths to .patch files that will be applied with ``patch -p1``, or
    choices from the following built-in patches:

    - **musl-symbol-hack**  removes some weak symbols from musl. This has been take
      from https://github.com/vusec/typeisolation.

    Currently MUSL is built in a way that the target binary *should* be PIE,
    although this is not a strict requirement

    :identifier: musl-<version>
    :param version: the full musl version to download, like X.Y.Z
    :param name: optionally specifcy name to be used for ident
    :param patches: optional patches to apply before building
    """

    def __init__(self, version: str, llvm: LLVM, name: Optional[str] = None,
            patches: List[str] = []):
        self.version = version
        self.patches = patches
        self.name = name
        self.llvm = llvm

    def ident(self):
        if self.name:
            return 'musl-'+self.name
        return 'musl-' + self.version

    def dependencies(self):
        yield AutoMake.default()
        yield self.llvm

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def fetch(self, ctx):
        dirname = 'musl-%s' % (self.version)
        tarname = dirname + '.tar.gz'

        download(ctx, 'https://musl.libc.org/releases/%s' % (tarname))
        run(ctx, ['tar', '-xf', tarname])
        shutil.move(dirname, 'src')
        os.remove(tarname)

    def is_built(self, ctx):
        return os.path.exists('obj/lib/libc.a')

    def _apply_patches(self, ctx):
        os.chdir(self.path(ctx, 'src'))
        config_root = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_root, path)
            if apply_patch(ctx, path, 1):
                ctx.log.warning('applied patch %s to musl '
                                'directory' % path)
        os.chdir(self.path(ctx))

    def build(self, ctx):
        ctx = ctx.copy()
        self._apply_patches(ctx)

        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')

        self.llvm.configure(ctx)
        ctx.cflags += ['-fPIE']
        ctx.cxxflags += ['-fPIE']
        ctx.ldflags += ['-fPIE']
        ctx.runenv.update({
            'CC': ctx.cc,
            'CXX': ctx.cxx,
            'CFLAGS': qjoin(ctx.cflags),
            'CXXFLAGS': qjoin(ctx.cxxflags),
            'LDFLAGS': qjoin(ctx.ldflags),
        })

        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install')
            run(ctx, [
                '../src/configure',
                '--disable-shared',
                '--prefix=' + prefix
            ])

        # GPERFTOOLS SPECIFIC START
        # we don't want musl's malloc
        shutil.rmtree('../src/src/malloc')
        # GPERFTOOLS SPECIFIC END

        os.makedirs(self.path(ctx, 'musl-hacks/include'), exist_ok=True)
        os.symlink('/usr/include/linux', self.path(ctx, 'musl-hacks/include/linux'))
        os.symlink('/usr/include/asm', self.path(ctx, 'musl-hacks/include/asm'))
        os.symlink('/usr/include/asm-generic', self.path(ctx, 'musl-hacks/include/asm-generic'))

        run(ctx, 'make clean')
        run(ctx, 'make -j%d' % ctx.jobs)

    def is_installed(self, ctx):
        return os.path.exists('install/lib/libc.a')

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, 'make install')

    def configure(self, ctx: Namespace):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        :param ctx: the configuration context
        """
        ctx.cflags += [
            '-nostdinc',
            '-isystem', self.path(ctx, 'src')+'/include',
            '-isystem', self.path(ctx, 'obj')+'/obj/include',
            '-isystem', self.path(ctx, 'src')+'/arch/x86_64',
            '-isystem', self.path(ctx, 'src')+'/arch/generic',
            '-isystem', self.path(ctx, 'musl-hacks')+'/include',
        ]
        ctx.cxxflags += [
            '-fPIE', '-pie', '-stdlib=libc++',
            '-nostdinc',
            '-nostdinc++',
            '-isystem', self.path(ctx, 'src')+'/include',
            '-isystem', self.path(ctx, 'obj')+'/obj/include',
            '-isystem', self.path(ctx, 'src')+'/arch/x86_64',
            '-isystem', self.path(ctx, 'src')+'/arch/generic',
            '-isystem', self.path(ctx, 'musl-hacks')+'/include',
        ]

        GCCPATHHACK = '/usr/lib/gcc/x86_64-linux-gnu/7.5.0'
        ldflags = [
            '-Wl,--verbose',
            '-nostdlib', '-nostdinc', '-static-libgcc',
            '--sysroot='+self.path(ctx, 'install'),
            '-isystem', self.path(ctx, 'install/include'),
            '-fPIE', '-pie',
            '-Bstatic', '--no-undefined',
            self.path(ctx, 'install')+'/lib/Scrt1.o',
            self.path(ctx, 'install')+'/lib/crti.o',
            GCCPATHHACK+'/crtbeginS.o',
            '-L'+self.path(ctx, 'install')+'/lib',
            '-Wl,--start-group',
        ]
        ldflags += ctx.ldflags
        ctx.ldflags = ldflags

        ctx.extra_libs = [
            '-Wl,-whole-archive', '-lunwind', '-Wl,-no-whole-archive',
            '-lc', '-lm', '-lc++abi', '-lc++',
            '-Wl,--end-group',
            GCCPATHHACK+'/crtendS.o',
            self.path(ctx, 'install')+'/lib/crtn.o',
        ]

        benchmark_flags = {
            '400.perlbench=default=default=default': {
                'CPORTABILITY': [
                    '-DSPEC_CPU_NO_USE_STDIO_PTR', '-DSPEC_CPU_NO_USE_STDIO_BASE',
                    '-DI_FCNTL', '-DSPEC_CPU_NEED_TIME_H', '-fno-builtin-ceil'
                ],
            },
            '444.namd=default=default=default': {
                'CXXPORTABILITY': [
                    '-fno-builtin-ceil'
                ],
            },
            '445.gobmk=default=default=default': {
                'CPORTABILITY': [
                    '-fno-builtin-ceil', '-fno-builtin-floor'
                ],
            },
            '453.povray=default=default=default': {
                'CXXPORTABILITY': [
                    '-fno-builtin-floorf'
                ],
            },
            '456.hmmer=default=default=default': {
                'CPORTABILITY': [
                    '-fno-builtin-ceil', '-fno-builtin-floor'
                ],
            },
            '462.libquantum=default=default=default': {
                'EXTRA_SOURCES': [
                    (self.path(ctx, f'../llvm-{self.llvm.version}/src')+
                        '/projects/compiler-rt/lib/builtins/mulsc3.c')
                ],
            },
            '464.h264ref=default=default=default': {
                'CPORTABILITY': [
                    '-fno-builtin-ceil', '-fno-builtin-floor'
                ],
            },
        }

        if 'benchmark_flags' not in ctx:
            ctx.benchmark_flags = {}
        for benchmark, flags in benchmark_flags.items():
            if benchmark not in ctx.benchmark_flags:
                ctx.benchmark_flags[benchmark] = {}
            for flag, value in flags.items():
                if flag not in ctx.benchmark_flags[benchmark]:
                    ctx.benchmark_flags[benchmark][flag] = []
                ctx.benchmark_flags[benchmark][flag].extend(value)
