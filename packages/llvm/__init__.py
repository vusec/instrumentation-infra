import os
import shutil
from ...package import Package
from ...util import run, apply_patch, download, FatalError
from ..gnu import Bash, CoreUtils, BinUtils, Make, \
        M4, AutoConf, AutoMake, LibTool
from ..cmake import CMake


class LLVM(Package):
    supported_versions = ('3.8.0', '4.0.0', '5.0.0')
    binutils = BinUtils('2.26.1', gold=True)

    def __init__(self, version, compiler_rt, patches, build_flags=[]):
        if version not in self.supported_versions:
            raise FatalError('LLVM version must be one of %s' %
                    '/'.join(self.supported_versions))

        self.version = version
        self.compiler_rt = compiler_rt
        self.patches = patches
        self.build_flags = build_flags

    def ident(self):
        return 'llvm-' + self.version

    def prefix(self, ctx):
        return os.path.join(ctx.paths.installroot, self.ident())

    def dependencies(self):
        yield Bash('4.3')
        yield CoreUtils('8.22')
        yield self.binutils
        yield Make('4.1')
        yield M4('1.4.18')
        yield AutoConf('2.69')
        yield AutoMake('1.15')
        yield LibTool('2.4.6')

        if self.version == '3.8.0':
            yield CMake('3.4.1')
        elif self.version == '4.0.0':
            yield CMake('3.8.2')
        elif self.version == '5.0.0':
            #yield CMake('3.4.3')
            yield CMake('3.8.2')

    def is_fetched(self, ctx):
        return os.path.exists(self.path(ctx, 'src'))

    def is_built(self, ctx):
        return os.path.exists(self.path(ctx, 'obj'))

    def is_installed(self, ctx):
        return os.path.exists(self.path(ctx, 'install', 'bin', 'llvm-config'))

    def fetch(self, ctx):
        def get(repo, clonedir):
            os.makedirs(os.path.dirname(clonedir), exist_ok=True)

            #url = 'http://llvm.org/svn/llvm-project/%s/trunk' % repo
            #run(['svn', 'co', '-r' + ctx.params.commit, url, clonedir])

            dirname = '%s-%s.src' % (repo, self.version)
            tarname = dirname + '.tar.xz'
            url = 'https://releases.llvm.org/%s/%s' % (self.version, tarname)
            download(url, tarname)
            run(['tar', '-xf', tarname])
            shutil.move(dirname, clonedir)
            os.remove(tarname)

        # download and unpack sources
        srcpath = self.src_path(ctx)

        # only clone if not installed yet (meaning that someone removed the
        # source to preserve space)
        if self.is_installed(ctx):
            ctx.log.debug('  skip fetch, already installed')
            return

        srcdir = os.path.dirname(srcpath)
        srcbase = os.path.basename(srcpath)
        os.makedirs(srcdir, exist_ok=True)
        os.chdir(srcdir)

        get('llvm', srcbase)
        get('clang', srcbase + '/tools/clang')
        if self.compiler_rt:
            get('compiler-rt', srcbase + '/projects/compiler-rt')

    def build(self, ctx):
        # apply patches from the directory this file is in
        # do this in build() instead of fetch() to make sure patches are applied
        # with --force-rebuild
        os.chdir(self.path(ctx, 'src'))
        base_path = os.path.dirname(os.path.abspath(__file__))
        for patch_name in self.patches:
            ctx.log.debug('  applying patch %s' % patch_name)
            apply_patch(base_path, patch_name + '-' + self.version, 0)

        # only build if not installed yet (meaning that someone removed the
        # objects to preserve space)
        if self.is_installed(ctx):
            ctx.log.debug('  skip build, already installed')
            return

        # build if build dir does not exist yet
        objdir = self.path(ctx, 'obj')
        os.makedirs(objdir)
        os.chdir(objdir)
        if not os.path.exists('Makefile'):
            run([
                'cmake',
                '-DCMAKE_INSTALL_PREFIX=' + self.path(ctx, 'install'),
                '-DLLVM_BINUTILS_INCDIR=' + self.binutils.src_path('include'),
                '-DCMAKE_BUILD_TYPE=Release',
                '-DLLVM_ENABLE_ASSERTIONS=On',
                '-DLLVM_OPTIMIZED_TABLEGEN=On',
                '-DLLVM_ENABLE_DOXYGEN=On',
                *self.build_flags,
                srcdir
            ])
        run(['make', '-j%d' % ctx.nproc])

    def install(self, ctx):
        # only install if llvm-config was not installed yet
        if not self.installed(ctx) or self.patched:
            os.chdir(self.build_path(ctx))
            run(['make', 'install'])

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx, 'bin', 'llvm-config'))

    def configure(self, ctx, lto=False):
        # TODO: set path?
        ctx.cc = 'clang'
        ctx.cxx = 'clang++'
        ctx.ar = 'llvm-ar'
        ctx.nm = 'llvm-nm'
        ctx.ranlib = 'llvm-ranlib'
        ctx.cflags = []
        ctx.ldflags = []

        if lto:
            path = '%s/lib/libplugins.so' % ctx.paths.prefix
            ctx.cflags += ['-flto']
            ctx.ldflags += ['-flto', '-Wl,-plugin-opt=-load=%s' % path]

            if ctx.disable_opt:
                ctx.cflags += ['-g3', '-O0']
                ctx.ldflags += ['-g3', '-O0']
                ctx.ldflags.append('-Wl,-plugin-opt=-disable-opt')
