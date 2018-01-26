import os
from ...package import Package
from ...util import run, download, apply_patch


class LibElf(Package):
    def __init__(self, version):
        self.version = version

    def ident(self):
        return 'libelf-' + self.version

    def fetch(self, ctx):
        if not os.path.exists(self.src_path(ctx)) and not self.installed(ctx):
            os.chdir(ctx.paths.packsrc)
            tarname = 'libelf-%s.tar.gz' % self.version
            download('http://www.mr511.de/software/' + tarname)
            run(['tar', '-xzf', tarname])
            os.remove(tarname)

            if self.version == '0.7.0':
                base_path = os.path.dirname(os.path.abspath(__file__))
                apply_patch(base_path, 'libelf-0.7.0-prelink', 1)
                apply_patch(base_path, 'libelf-0.7.0-hash-prelink', 1)

    def build(self, ctx):
        if not self.built(ctx) and not self.installed(ctx):
            objdir = self.build_path(ctx)
            os.makedirs(objdir, exist_ok=True)
            os.chdir(objdir)
            run([self.src_path(ctx, 'configure'),
                 '--prefix=' + self.install_path(ctx)])
            run(['make', '-j%d' % ctx.nproc])

    def install(self, ctx):
        if not self.installed(ctx):
            os.chdir(self.build_path(ctx))
            run(['make', 'install'])

    def built(self, ctx):
        return os.path.exists(self.build_path(ctx, 'lib', 'libelf.a'))

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx, 'lib', 'libelf.a'))


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
        if not os.path.exists(self.src_path(ctx)):
            os.chdir(ctx.paths.packsrc)
            run(['svn', 'co', '-r' + self.version,
                 'svn://sourceware.org/svn/prelink/trunk', self.ident()])

            os.chdir(self.ident())
            base_path = os.path.dirname(os.path.abspath(__file__))
            apply_patch(base_path, 'prelink-execstack-link-fix', 0)

    def build(self, ctx):
        if not self.built(ctx) and not self.installed(ctx):
            objdir = self.build_path(ctx)
            os.makedirs(objdir, exist_ok=True)
            os.chdir(objdir)
            env = {
                'C_INCLUDE_PATH': self.install_path(ctx, 'include'),
                'ac_cv_lib_selinux_is_selinux_enabled': 'no',
                'ac_cv_header_gelf_h': 'no',
            }
            config_env = {
                **env,
                'CPPFLAGS': '-I' + self.libelf.install_path(ctx, 'include/libelf'),
                'LDFLAGS': '-L' + self.libelf.install_path(ctx, 'lib'),
            }
            run([self.src_path(ctx, 'configure'),
                 '--prefix=' + self.install_path(ctx),
                 '--sbindir=' + self.install_path(ctx, 'bin')],
                env=config_env)
            run(['make', '-j%d' % ctx.nproc, '-C', 'gelf'], env=env)
            run(['make', '-j%d' % ctx.nproc, '-C', 'src'], env=env)

    def install(self, ctx):
        if not self.installed(ctx):
            os.chdir(self.build_path(ctx))
            run(['make', 'install-exec', 'install-data'])

    def built(self, ctx):
        return os.path.exists(self.build_path(ctx, 'src', 'prelink'))

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx, 'bin', 'prelink'))
