import os
from ..package import Package
from ..util import Namespace, run
from . import Prelink, PatchElf, PyElfTools


class LibShrink(Package):
    """
    Dependency package for `libshrink <https://github.com/vusec/libshrink>`_.

    Libshrink shrinks the application address space to a maximum number of
    bits. It moves the stack and TLS to a memory region that is within the
    allowed bitrange, and prelinks all shared libraries as well so that they do
    not exceed the address space limitations. It also defines a
    :func:`run_wrapper` that should be put in ``ctx.target_run_wrapper`` by an
    instance that uses libshrink.

    :identifier: libshrink-<addrspace_bits>
    :param addrspace_bits: maximum number of nonzero bits in any pointer
    :param commit: branch or commit to clone
    :param debug: whether to compile with debug symbols
    """

    git_url = 'https://github.com/vusec/libshrink.git'

    def __init__(self, addrspace_bits: int,
                       commit = 'master',
                       debug = False):
        self.addrspace_bits = addrspace_bits
        self.commit = commit
        self.debug = debug

    def ident(self):
        return 'libshrink-%d' % self.addrspace_bits

    def dependencies(self):
        yield Prelink('209')
        yield PatchElf('0.9')
        yield PyElfTools('0.24', '2.7')

    def fetch(self, ctx):
        run(ctx, ['git', 'clone', self.git_url, 'src'])
        os.chdir('src')
        run(ctx, ['git', 'checkout', self.commit])

    def build(self, ctx):
        os.chdir('src')
        run(ctx, ['make', '-j%d' % ctx.jobs,
                  'OBJDIR=' + self.path(ctx, 'obj'),
                  'DEBUG=' + ('1' if self.debug else '0')])

    def install(self, ctx):
        pass

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/libshrink-static.a') and \
               os.path.exists('obj/libshrink-preload.so')

    def is_installed(self, ctx):
        return self.is_built(ctx)

    def configure(self, ctx: Namespace, static=True):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance. Uses post-build hooks, so any
        target compiled with this libary must implement
        :func:`infra.Target.binary_paths`.

        :param ctx: the configuration context
        :param static: use the static library? (shared library otherwise)
        :raises NotImplementedError: if **static** is not ``True`` (TODO)
        """
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
            ctx.hooks.post_build += [self._prelink_binary, self._fix_preinit]
        else:
            raise NotImplementedError('libshrink does not have dynamic library support')

    def _prelink_binary(self, ctx, binary):
        libpath = ctx.runenv.join_paths().get('LD_LIBRARY_PATH', '')
        run(ctx, [
            self.path(ctx, 'src/prelink_binary.py'),
            '--set-rpath', '--in-place', '--static-lib',
            '--out-dir', 'prelink-' + os.path.basename(binary),
            '--library-path', libpath,
            '--addrspace-bits', self.addrspace_bits,
            binary
        ])

    def _fix_preinit(self, ctx, binary):
        run(ctx, [
            self.path(ctx, 'src/fix_preinit.py'),
            '--preinit-name', '__shrinkaddrspace_preinit',
            binary
        ])

    def run_wrapper(self, ctx: Namespace) -> str:
        """
        Run wrapper for targets. Links to a script that sets the ``rpath``
        before any libraries are loaded, so that any dependencies of shared
        libraries loaded by the applications are also loaded from the directory
        of prelinked libraries (which is created by a post-build hook).

        :param ctx: the configuration context
        """
        return self.path(ctx, 'src/rpath_wrapper.sh')
