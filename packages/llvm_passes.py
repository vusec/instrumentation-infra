import os
from ..package import Package
from ..util import run, FatalError


def strip_prefix(prefix, full):
    return full[len(prefix):] if full.startswith(prefix) else full


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

    def srcdir(self, ctx):
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
        os.chdir(self.srcdir(ctx))
        self.run_make(ctx, '-j%d' % ctx.jobs)

    def install(self, ctx):
        os.chdir(self.srcdir(ctx))
        self.run_make(ctx, 'install')

    def run_make(self, ctx, *args, **kwargs):
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

    def run_pkg_config(self, ctx, parser, args):
        pgroup = parser.add_mutually_exclusive_group(required=True)
        pgroup.add_argument('--objdir', action='store_true',
                help='print absolute build path')
        pgroup.add_argument('--prefix', action='store_true',
                help='print absolute install path')
        args = parser.parse_args(args)

        if args.objdir:
            print(self.path(ctx, 'obj'))
        elif args.prefix:
            print(self.path(ctx, 'install'))

    def configure(self, ctx):
        libpath = self.path(ctx, 'install/libpasses.so')
        ctx.cflags += ['-flto']
        ctx.ldflags += ['-flto', '-Wl,-plugin-opt=-load=' + libpath]


class BuiltinLLVMPasses(LLVMPasses):
    def __init__(self, llvm):
        LLVMPasses.__init__(self, llvm, None, 'builtin-' + llvm.version, False)

    def srcdir(self, ctx, *subdirs):
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
               'compile flags',
               ['-I', self.srcdir(ctx)])
        yield ('--ldflags',
               'link flags',
               ['-L', self.path(ctx, 'install'), '-lpasses-builtin'])
        yield ('--target-cflags',
               'target compile flags for instrumentation helpers',
               ['-I', self.srcdir(ctx, 'include')])
        yield from Package.pkg_config_options(self, ctx)
