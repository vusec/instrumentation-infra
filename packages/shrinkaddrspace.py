import os
from ..package import Package
from ..util import run
from . import Prelink, PatchElf, PyElfTools


class ShrinkAddrSpace(Package):
    git_url = ''

    def __init__(self, addrspace_bits, commit='master', srcdir=None):
        self.addrspace_bits = addrspace_bits
        self.commit = commit
        self.srcdir = os.path.abspath(srcdir) if srcdir else None

    def ident(self):
        return 'shrinkaddrspace-%d' % self.addrspace_bits

    def dependencies(self):
        yield Prelink('209')
        yield PatchElf('0.9')
        yield PyElfTools('0.24', '2.7')

    def src_path(self, ctx, *args):
        return os.path.join(ctx.paths.packsrc, 'shrinkaddrspace', *args)

    def fetch(self, ctx):
        if not self.srcdir:
            self.srcdir = self.src_path(ctx)

            if not os.path.exists(self.srcdir):
                os.makedirs(ctx.paths.packsrc)
                os.chdir(ctx.paths.packsrc)
                run(['git', 'clone', self.git_url, 'shrinkaddrspace'])
                os.chdir('shrinkaddrspace')
                run(['git', 'checkout', self.commit])

    def build(self, ctx):
        os.chdir(self.srcdir)
        run(['make', '-j%d' % ctx.nproc, 'OBJDIR=' + self.build_path(ctx)])

    def configure(self, ctx, static):
        if static:
            # linker flags
            ctx.ldflags += [
                '-L' + self.build_path(ctx),
                '-Wl,-whole-archive',
                '-lshrink-static',
                '-Wl,-no-whole-archive',
                '-ldl'
            ]

            # patch binary and prelink libraries after build
            ctx.hooks.post_build += [self.prelink_binary, self.fix_preinit]

            # runtime settings
            ctx.run_wrapper = self.src_path(ctx, 'rpath_wrapper.sh')
        else:
            raise NotImplementedError

    def prelink_binary(self, ctx, binary):
        os.chdir(os.dirname(binary))
        run([
            self.src_path(ctx, 'prelink_binary.py'),
            '--set-rpath', '--in-place', '--static-lib',
            '--out-dir', 'prelink',
            '--library-path', ':'.join(ctx.ld_library_path),
            '--addrspace-bits', self.addrspace_bits,
            binary
        ])

    def fix_preinit(self, ctx, binary):
        run([
            self.src_path(ctx, 'fix_preinit.py'),
            '--preinit-name', '__shrinkaddrspace_preinit',
            binary
        ])
