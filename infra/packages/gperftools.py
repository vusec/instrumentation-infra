import os
import shutil
from ..package import Package
from ..util import Namespace, run, download
from .gnu import AutoMake


class LibUnwind(Package):
    """
    :identifier: libunwind-<version>
    :param version: version to download
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self):
        return 'libunwind-' + self.version

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def fetch(self, ctx):
        urlbase = 'http://download.savannah.gnu.org/releases/libunwind/'
        dirname = self.ident()
        tarname = dirname + '.tar.gz'
        download(ctx, urlbase + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move(dirname, 'src')
        os.remove(tarname)

    def is_built(self, ctx):
        return os.path.exists('obj/src/.libs/libunwind.so')

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        if not os.path.exists('Makefile'):
            run(ctx, ['../src/configure', '--prefix=' + self.path(ctx, 'install')])
        run(ctx, 'make -j%d' % ctx.jobs)

    def is_installed(self, ctx):
        return os.path.exists('install/lib/libunwind.so')

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, 'make install')

    def configure(self, ctx):
        ctx.ldflags += ['-L' + self.path(ctx, 'install/lib'), '-lunwind']


class Gperftools(Package):
    """
    :identifier: gperftools-<version>
    :param commit: git branch/commit to check out after cloning
    :param libunwind_version: libunwind version to use
    """

    def __init__(self, commit: str, libunwind_version='1.2-rc1'):
        self.commit = commit
        self.libunwind = LibUnwind(libunwind_version)
        # TODO patches

    def ident(self):
        return 'gperftools-' + self.commit

    def dependencies(self):
        yield AutoMake.default()
        yield self.libunwind

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def fetch(self, ctx):
        run(ctx, 'git clone https://github.com/gperftools/gperftools.git src')
        os.chdir('src')
        run(ctx, ['git', 'checkout', self.commit])

    def is_built(self, ctx):
        return os.path.exists('obj/.libs/libtcmalloc.so')

    def build(self, ctx):
        if not os.path.exists('src/configure') or not os.path.exists('src/INSTALL'):
            os.chdir('src')
            run(ctx, 'autoreconf -vfi')
            self.goto_rootdir(ctx)

        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install')
            run(ctx, ['../src/configure', '--prefix=' + prefix])
        run(ctx, 'make -j%d' % ctx.jobs)

    def is_installed(self, ctx):
        return os.path.exists('install/lib/libtcmalloc.so')

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, 'make install')

    def configure(self, ctx: Namespace):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        Sets the necessary ``-I/-L/-l`` flags, and additionally adds
        ``-fno-builtin-{malloc,calloc,realloc,free}`` to CFLAGS.

        :param ctx: the configuration context
        """
        self.libunwind.configure(ctx)
        cflags = ['-fno-builtin-' + fn
                  for fn in ('malloc', 'calloc', 'realloc', 'free')]
        cflags += ['-I', self.path(ctx, 'install/include/gperftools')]
        ctx.cflags += cflags
        ctx.cxxflags += cflags
        ctx.ldflags += ['-L' + self.path(ctx, 'install/lib'),
                        '-ltcmalloc', '-lpthread']
