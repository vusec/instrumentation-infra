import os
import shutil
from ...package import Package
from ...util import run, download, apply_patch


class LibElf(Package):
    def __init__(self, version):
        self.version = version

    def ident(self):
        return 'libelf-' + self.version

    def fetch(self, ctx):
        tarname = 'libelf-%s.tar.gz' % self.version
        download(ctx, 'http://www.mr511.de/software/' + tarname)
        run(ctx, ['tar', '-xzf', tarname])
        shutil.move('libelf-' + self.version, 'src')
        os.remove(tarname)

        if self.version == '0.7.0':
            os.chdir('src')
            base_path = os.path.dirname(os.path.abspath(__file__))
            apply_patch(ctx, base_path, 'libelf-0.7.0-prelink', 1)
            apply_patch(ctx, base_path, 'libelf-0.7.0-hash-prelink', 1)
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
    def __init__(self, version):
        self.version = version
        #assert version == '209'
        self.libelf = LibElf('0.7.0')

    def ident(self):
        return 'prelink-' + self.version

    def dependencies(self):
        yield self.libelf

    def fetch(self, ctx):
        run(ctx, ['svn', 'co', '-r' + self.version,
                  'svn://sourceware.org/svn/prelink/trunk', 'src'])
        os.chdir('src')
        base_path = os.path.dirname(os.path.abspath(__file__))
        apply_patch(ctx, base_path, 'prelink-execstack-link-fix', 0)

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        env = {
            'C_INCLUDE_PATH': self.libelf.path(ctx, 'install/include'),
            'ac_cv_lib_selinux_is_selinux_enabled': 'no',
            'ac_cv_header_gelf_h': 'no',
        }
        config_env = {
            **env,
            'CPPFLAGS': '-I' + self.libelf.path(ctx, 'install/include/libelf'),
            'LDFLAGS': '-L' + self.libelf.path(ctx, 'install/lib'),
        }
        run(ctx, [
            '../src/configure',
            '--prefix=' + self.path(ctx, 'install'),
            '--sbindir=' + self.path(ctx, 'install/bin')
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
