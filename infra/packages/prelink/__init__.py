import os
import shutil
from typing import List

from ...package import Package
from ...util import run, download, apply_patch


class LibElf(Package):
    """
    :identifier: libelf-<version>
    :param str version: version to download
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self):
        return 'libelf-' + self.version

    def fetch(self, ctx):
        tarname = 'libelf-%s.tar.gz' % self.version
        download(ctx, 'https://web.archive.org/web/20160505164756if_/http://www.mr511.de/software/' + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move('libelf-' + self.version, 'src')
        os.remove(tarname)

        if self.version == '0.7.0':
            os.chdir('src')
            config_path = os.path.dirname(os.path.abspath(__file__))
            apply_patch(ctx, config_path + '/libelf-0.7.0-prelink.patch', 1)
            apply_patch(ctx, config_path + '/libelf-0.7.0-hash-prelink.patch', 1)
        else:
            ctx.log.debug('could not patch libelf version %s for prelink' %
                          self.version)

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        run(ctx, ['../src/configure', '--prefix=' + self.path(ctx, 'install')])
        run(ctx, ['make', '-j%d' % ctx.jobs])

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, ['make', 'install'])

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/lib/libelf.a')

    def is_installed(self, ctx):
        return os.path.exists('install/lib/libelf.a')


class Prelink(Package):
    """
    :identifier: prelink-<version>
    :param str version: version to download
    """

    def __init__(self, version, cross_prelink_aarch64 = False):
        self.version = version
        #assert version == '209'
        self.cross_prelink_aarch64 = cross_prelink_aarch64
        if not self.cross_prelink_aarch64:
            self.libelf = LibElf('0.7.0')

    def ident(self):
        return 'prelink-' + self.version

    def dependencies(self):
        if self.cross_prelink_aarch64:
            yield from []
        else:
            yield self.libelf

    def fetch(self, ctx):
        if self.cross_prelink_aarch64:
            run(ctx, ['git', 'clone', '--branch', 'cross_prelink_aarch64',
                    'https://git.yoctoproject.org/prelink-cross', 'src'])
            os.chdir('src')
        else:
            run(ctx, ['svn', 'co', '-r' + self.version,
                    'svn://sourceware.org/svn/prelink/trunk', 'src'])
            os.chdir('src')
            config_path = os.path.dirname(os.path.abspath(__file__))
            apply_patch(ctx, config_path + '/prelink-execstack-link-fix.patch', 0)

    def build(self, ctx):
        if not os.path.exists('src/configure') or not os.path.exists('src/INSTALL'):
            os.chdir('src')
            run(ctx, 'autoreconf -vfi')
            self.goto_rootdir(ctx)

        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')

        env: dict[str, str | List[str]] = { }
        config_env: dict[str, str | List[str]] = { }

        if not self.cross_prelink_aarch64:
            env: dict[str, str | List[str]] = {
                'C_INCLUDE_PATH': self.libelf.path(ctx, 'install/include'),
                'ac_cv_lib_selinux_is_selinux_enabled': 'no',
                'ac_cv_header_gelf_h': 'no',
            }
            config_env: dict[str, str | List[str]] = {
                **env,
                'CPPFLAGS': '-I' + self.libelf.path(ctx, 'install/include/libelf'),
                'LDFLAGS': '-L' + self.libelf.path(ctx, 'install/lib'),
            }
        run(ctx, [
            '../src/configure',
            '--prefix=' + self.path(ctx, 'install'),
            '--sbindir=' + self.path(ctx, 'install/bin'),
        ], env=config_env)
        run(ctx, ['make', '-j%d' % ctx.jobs, '-C', 'gelf'], env=env)
        run(ctx, ['make', '-j%d' % ctx.jobs, '-C', 'src'], env=env)

    def install(self, ctx):
        run(ctx, 'make install -C obj/src')

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/src/prelink')

    def is_installed(self, ctx):
        return os.path.exists('install/bin/prelink')
