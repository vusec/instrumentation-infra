import os
from ..package import Package
from ..util import run, prefix_paths
from . import Prelink, PatchElf, PyElfTools


class ShrinkAddrSpace(Package):
    git_url = ''

    def __init__(self, addrspace_bits, commit='master', srcdir=None):
        self.addrspace_bits = addrspace_bits
        self.commit = commit
        self.custom_srcdir = os.path.abspath(srcdir) if srcdir else None

    def ident(self):
        return 'shrinkaddrspace-%d' % self.addrspace_bits

    def dependencies(self):
        yield Prelink('209')
        yield PatchElf('0.9')
        yield PyElfTools('0.24', '2.7')

    def fetch(self, ctx):
        if self.custom_srcdir:
            os.symlink(self.custom_srcdir, 'src')
        else:
            run(ctx, ['git', 'clone', self.git_url, 'src'])
            os.chdir('src')
            run(ctx, ['git', 'checkout', self.commit])

    def build(self, ctx):
        os.chdir('src')
        run(ctx, ['make', '-j%d' % ctx.jobs, 'OBJDIR=' + self.path(ctx, 'obj')])

    def install(self, ctx):
        pass

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/libshrink-static.a') and \
               os.path.exists('obj/libshrink-preload.so')

    def is_installed(self, ctx):
        return self.is_built(ctx)

    def configure(self, ctx, static):
        if static:
            # linker flags
            ctx.ldflags += [
                '-L' + self.path(ctx, 'obj'),
                '-Wl,-whole-archive',
                '-lshrink-static',
                '-Wl,-no-whole-archive',
                '-ldl'
            ]

            # patch binary and prelink libraries after build
            ctx.hooks.post_build += [self.prelink_binary, self.fix_preinit]

            # runtime settings
            ctx.run_wrapper = self.path(ctx, 'src/rpath_wrapper.sh')
        else:
            raise NotImplementedError

    def prelink_binary(self, ctx, binary):
        libpath = prefix_paths(ctx.prefixes, '/lib',
                               os.environ.get('LD_LIBRARY_PATH', ''))
        run(ctx, [
            self.path(ctx, 'src/prelink_binary.py'),
            '--set-rpath', '--in-place', '--static-lib',
            '--out-dir', 'prelink-' + os.path.basename(binary),
            '--library-path', libpath,
            '--addrspace-bits', self.addrspace_bits,
            binary
        ])

    def fix_preinit(self, ctx, binary):
        run(ctx, [
            self.path(ctx, 'src/fix_preinit.py'),
            '--preinit-name', '__shrinkaddrspace_preinit',
            binary
        ])
