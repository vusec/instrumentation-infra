import os
from ..package import Package
from ..util import run, FatalError


class LLVMPasses(Package):
    def __init__(self, llvm, custom_srcdir, build_suffix, use_builtins):
        self.llvm = llvm
        self.custom_srcdir = os.path.abspath(custom_srcdir) \
                             if custom_srcdir else None
        self.build_suffix = build_suffix
        self.builtin_passes = BuiltinLLVMPasses(llvm) if use_builtins else None

    def ident(self):
        # FIXME: would be nice to have access to `ctx.paths.root` here and
        #        autodetect the build suffix from the srcdir
        return 'llvm-passes-' + self.build_suffix

    def _srcdir(self, ctx):
        if not os.path.exists(self.custom_srcdir):
            raise FatalError('llvm-passes dir "%s" does not exist' %
                             self.custom_srcdir)
        return self.custom_srcdir

    def dependencies(self):
        yield self.llvm
        if self.builtin_passes:
            yield self.builtin_passes

    def fetch(self, ctx):
        pass

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir(self._srcdir(ctx))
        self._run_make(ctx, '-j%d' % ctx.jobs)

    def install(self, ctx):
        os.chdir(self._srcdir(ctx))
        self._run_make(ctx, 'install')

    def _run_make(self, ctx, *args, **kwargs):
        return run(ctx, [
            'make', *args,
            'OBJDIR=' + self.path(ctx, 'obj'),
            'PREFIX=' + self.path(ctx, 'install')
        ], **kwargs)

    def is_fetched(self, ctx):
        return True

    def is_built(self, ctx):
        return False

    def is_installed(self, ctx):
        return False

    def pkg_config_options(self, ctx):
        yield ('--objdir',
               'absolute build path',
               self.path(ctx, 'obj'))
        yield from Package.pkg_config_options(self, ctx)

    def configure(self, ctx):
        libpath = self.path(ctx, 'install/libpasses.so')
        ctx.cflags += ['-flto']
        ctx.cxxflags += ['-flto']
        ctx.ldflags += ['-flto', '-Wl,-plugin-opt=-load=' + libpath]

    def runtime_cflags(self, ctx):
        """
        """
        if self.builtin_passes:
            return self.builtin_passes.runtime_cflags(ctx)
        return []


class BuiltinLLVMPasses(LLVMPasses):
    def __init__(self, llvm):
        LLVMPasses.__init__(self, llvm, None, 'builtin-' + llvm.version, False)

    def _srcdir(self, ctx, *subdirs):
        return os.path.join(ctx.paths.infra, 'llvm-passes',
                            self.llvm.version, *subdirs)

    def is_built(self, ctx):
        files = ('libpasses-builtin.a', 'libpasses.so', 'libpasses-opt.so')
        return all(os.path.exists('obj/' + f) for f in files)

    def is_installed(self, ctx):
        files = ('libpasses-builtin.a', 'libpasses.so', 'libpasses-opt.so')
        return all(os.path.exists('install/' + f) for f in files)

    def pkg_config_options(self, ctx):
        yield ('--cxxflags',
               'pass compile flags',
               ['-I', self._srcdir(ctx)])
        yield ('--runtime-cflags',
               'runtime compile flags',
               self.runtime_cflags(ctx))
        yield ('--target-cflags',
               'target compile flags for instrumentation helpers',
               ['-I', self._srcdir(ctx, 'include')])
        yield from LLVMPasses.pkg_config_options(self, ctx)

    def runtime_cflags(self, ctx):
        """
        """
        return ['-I', self._srcdir(ctx, 'include')]
