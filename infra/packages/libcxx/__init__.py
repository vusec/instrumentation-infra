import os
import shutil
from typing import List, Optional
from ...package import Package
from ...util import Namespace, run, apply_patch, download, qjoin
from ..cmake import CMake
from ..musl import Musl

class Libcxx(Package):
    """

    Libcxx that should be build with instrumentation (to provide full
    library instrumentation. This is intended to be used together with musl
    to build a static binary that is completely instrumented (instrumentation
    of all static libraries).

    Finally, you may specify a list of patches to apply before building. These
    may be paths to .patch files that will be applied with ``patch -p1``, or
    choices from the following built-in patches:

    - **libcxx-fix-init-order-D31413** fixes an issue with basic streams not initialized early enough. This has been taken from https://github.com/vusec/typeisolation.

    :identifier: libcxx-<version>
    :param version: the full libcxx version to download, like X.Y.Z
    :param musl: specify musl instance to be used
    :param name: optionally specifcy name to be used for ident
    :param patches: optional patches to apply before building
    """

    def __init__(self, version: str, musl: Musl, name: Optional[str] = None,
            patches: List[str] = []):
        self.version = version
        self.patches = patches
        self.musl = musl
        self.name = name

    def ident(self):
        if self.name:
            return 'libcxx-' + self.name
        return 'libcxx-' + self.version

    def dependencies(self):
        yield CMake('3.14.0')
        yield self.musl
        yield self.musl.llvm

    def is_fetched(self, ctx):
        return os.path.exists('src/libcxxabi')

    def fetch(self, ctx):
        def get(repo, clonedir):
            basedir = os.path.dirname(clonedir)
            if basedir:
                os.makedirs(basedir, exist_ok=True)

            dirname = '%s-%s.src' % (repo, self.version)
            tarname = dirname + '.tar.xz'
            major_version = int(self.version.split('.')[0])

            if major_version >= 8:
                # use github now
                url_prefix = 'https://github.com/llvm/llvm-project/releases/download'
                download(ctx, '%s/llvmorg-%s/%s' % (url_prefix, self.version, tarname))
            else:
                download(ctx, 'https://releases.llvm.org/%s/%s' % (self.version, tarname))

            run(ctx, ['tar', '-xf', tarname])
            shutil.move(dirname, clonedir)
            os.remove(tarname)
        get('libunwind', 'src/libunwind')
        get('libcxx', 'src/libcxx')
        get('libcxxabi', 'src/libcxxabi')

    def is_built(self, ctx):
        return (os.path.exists('obj/libcxxabi/lib/libc++abi.a') and
                os.path.exists('obj/libcxx/lib/libc++.a') and
                os.path.exists('obj/libunwind/lib/libunwind.a'))

    def _apply_patches(self, ctx):
        os.chdir(self.path(ctx, 'src'))
        config_root = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_root, path)
            if apply_patch(ctx, path, 1):
                ctx.log.warning('applied patch %s to libcxx '
                                'directory' % path)
        os.chdir(self.path(ctx))

    def build(self, ctx):
        ctx = ctx.copy()
        self._apply_patches(ctx)

        self.musl.llvm.configure(ctx)

        os.makedirs('obj/libunwind', exist_ok=True)
        os.makedirs('obj/libcxxabi', exist_ok=True)
        os.makedirs('obj/libcxx', exist_ok=True)

        # out of some reason ranlib and ar need to be supplied as complete paths
        ranlib = run(ctx, 'which llvm-ranlib').stdout.rstrip()
        ar = run(ctx, 'which llvm-ar').stdout.rstrip()

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

        os.chdir(self.path(ctx, 'obj/libunwind'))
        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install/libunwind')
            run(ctx, [
                'cmake', '../../src/libunwind',
                '-DCMAKE_INSTALL_PREFIX=' + prefix,
                '-DLLVM_PATH=' + self.musl.llvm.path(ctx, 'src'),
                '-DLIBUNWIND_ENABLE_SHARED=OFF',
                '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY',
                '-DCMAKE_C_COMPILER='+ctx.cc,
                '-DCMAKE_CXX_COMPILER='+ctx.cxx,
                '-DCMAKE_AR='+ar,
                '-DCMAKE_RANLIB='+ranlib,
            ])

        os.chdir(self.path(ctx, 'obj/libcxxabi'))
        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install/libcxxabi')
            run(ctx, [
                'cmake', '../../src/libcxxabi',
                '-DCMAKE_INSTALL_PREFIX=' + prefix,
                '-DLLVM_PATH=' + self.musl.llvm.path(ctx, 'src'),
                '-DLIBCXXABI_LIBCXX_PATH='+self.path(ctx, 'src/libcxx'),
                '-DLIBCXXABI_USE_LLVM_UNWINDER=ON',
                '-DLIBCXXABI_ENABLE_SHARED=OFF',
                '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY',
                '-DCMAKE_C_COMPILER='+ctx.cc,
                '-DCMAKE_CXX_COMPILER='+ctx.cxx,
                '-DCMAKE_AR='+ar,
                '-DCMAKE_RANLIB='+ranlib,
                '-DLIBCXXABI_SILENT_TERMINATE=ON' # to avoid dragging in the demangling code
            ])

        self.musl.configure(ctx)
        ctx.cxxflags += ['-isystem', self.path(ctx, 'install/libcxx')+'/include/c++/v1']

        os.chdir(self.path(ctx, 'obj/libcxx'))
        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install/libcxx')
            libcxxabisrc = self.path(ctx, 'src/libcxxabi')
            libcxxabiobj = self.path(ctx, 'obj/libcxxabi')
            run(ctx, [
                'cmake', '../../src/libcxx',
                '-DCMAKE_INSTALL_PREFIX=' + prefix,
                '-DLLVM_PATH=' + self.musl.llvm.path(ctx, 'src'),
                '-DLIBCXX_CXX_ABI=libcxxabi',
                '-DLIBCXX_CXX_ABI_INCLUDE_PATHS='+libcxxabisrc+'/include',
                '-DLIBCXX_CXX_ABI_LIBRARY_PATH='+libcxxabiobj+'/lib',
                '-DLIBCXX_ENABLE_SHARED=OFF',
                '-DLIBCXX_ENABLE_EXPERIMENTAL_LIBRARY=OFF',
                '-DLIBCXX_ENABLE_EXCEPTIONS=ON',
                '-DLIBCXX_ENABLE_STATIC_ABI_LIBRARY=ON',
                '-DLIBCXX_HAS_MUSL_LIBC=ON',
                '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY',
                '-DCMAKE_C_COMPILER='+ctx.cc,
                '-DCMAKE_CXX_COMPILER='+ctx.cxx,
                '-DCMAKE_AR='+ar,
                '-DCMAKE_RANLIB='+ranlib,
                '-DCMAKE_C_FLAGS='+qjoin(ctx.cflags),
                '-DCMAKE_CXX_FLAGS='+qjoin(ctx.cxxflags),
            ])

        os.chdir(self.path(ctx, 'obj/libcxxabi'))
        run(ctx, 'make -j%d' % ctx.jobs)

        os.chdir(self.path(ctx, 'obj/libcxx'))
        run(ctx, 'make -j%d' % ctx.jobs)

        os.chdir(self.path(ctx, 'obj/libunwind'))
        run(ctx, 'make -j%d' % ctx.jobs)

    def is_installed(self, ctx):
        return (os.path.exists('install/libcxxabi/lib/libc++abi.a') and
                os.path.exists('install/libcxx/lib/libc++.a') and
                os.path.exists('install/libunwind/lib/libunwind.a'))

    def install(self, ctx):
        os.chdir(self.path(ctx, 'obj/libcxxabi'))
        run(ctx, 'make install')
        os.chdir(self.path(ctx, 'obj/libcxx'))
        run(ctx, 'make install')
        os.chdir(self.path(ctx, 'obj/libunwind'))
        run(ctx, 'make install')

    def configure(self, ctx: Namespace):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        :param ctx: the configuration context
        """
        ctx.cxxflags += ['-isystem', self.path(ctx, 'install/libcxx')+'/include/c++/v1']

        ldflags = [
            '-L'+self.path(ctx, 'install/libunwind')+'/lib',
            '-L'+self.path(ctx, 'install/libcxxabi')+'/lib',
            '-L'+self.path(ctx, 'install/libcxx')+'/lib',
        ]
        ldflags += ctx.ldflags
        ctx.ldflags = ldflags

