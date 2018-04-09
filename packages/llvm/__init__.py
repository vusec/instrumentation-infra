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
        yield AutoMake('1.15.1')
        yield LibTool('2.4.6')
        yield CMake('3.8.2')

    def fetch(self, ctx):
        def get(repo, clonedir):
            basedir = os.path.dirname(clonedir)
            if basedir:
                os.makedirs(basedir, exist_ok=True)

            #url = 'http://llvm.org/svn/llvm-project/%s/trunk' % repo
            #run(ctx, ['svn', 'co', '-r' + ctx.params.commit, url, clonedir])

            dirname = '%s-%s.src' % (repo, self.version)
            tarname = dirname + '.tar.xz'
            download(ctx, 'https://releases.llvm.org/%s/%s' % (self.version, tarname))
            run(ctx, ['tar', '-xf', tarname])
            shutil.move(dirname, clonedir)
            os.remove(tarname)

        # download and unpack sources
        get('llvm', 'src')
        get('cfe', 'src/tools/clang')
        if self.compiler_rt:
            get('compiler-rt', 'src/projects/compiler-rt')

    def build(self, ctx):
        # TODO: verify that any applied patches are in self.patches, error
        # otherwise

        # apply patches from the directory this file is in
        # do this in build() instead of fetch() to make sure patches are applied
        # with --force-rebuild
        os.chdir('src')
        config_path = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s-%s.patch' % (config_path, path, self.version)
            apply_patch(ctx, path, 1)
        os.chdir('..')

        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        generator = 'Ninja' if self.ninja_supported(ctx) else 'Unix Makefiles'
        run(ctx, [
            'cmake',
            '-G', generator,
            '-DCMAKE_INSTALL_PREFIX=' + self.path(ctx, 'install'),
            '-DLLVM_BINUTILS_INCDIR=' + self.binutils.path(ctx, 'src/include'),
            '-DCMAKE_BUILD_TYPE=Release',
            '-DLLVM_ENABLE_ASSERTIONS=On',
            '-DLLVM_OPTIMIZED_TABLEGEN=On',
            *self.build_flags,
            '../src'
        ])
        run(ctx, ['cmake', '--build', '.'])

    def ninja_supported(self, ctx):
        proc = run(ctx, ['ninja', '--version'], allow_error=True)
        return proc and proc.returncode == 0

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, ['cmake', '--build', '.', '--target', 'install'])

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/bin/llvm-config')

    def is_installed(self, ctx):
        if not self.patches:
            # allow preinstalled LLVM if version matches
            # TODO: do fuzzy matching on version?
            proc = run(ctx, ['llvm-config', '--version'], allow_error=True)
            if proc and proc.returncode == 0:
                installed_version = proc.stdout.strip()
                if installed_version == self.version:
                    return True
                else:
                    ctx.log.debug('installed llvm-config version %s is '
                                  'different from required %s' %
                                  (installed_version, self.version))

        return os.path.exists('install/bin/llvm-config')

    def configure(self, ctx, lto=False):
        ctx.cc = self.path(ctx, 'install/bin/clang')
        ctx.cxx = 'clang++'
        ctx.ar = 'llvm-ar'
        ctx.nm = 'llvm-nm'
        ctx.ranlib = 'llvm-ranlib'
        ctx.cflags = []
        ctx.ldflags = []
