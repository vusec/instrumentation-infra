import os
import subprocess
from ..package import Package
from ..util import run, download


class CMake(Package):
    url = 'https://cmake.org/files/v{s.major}.{s.minor}/' \
          'cmake-{s.major}.{s.minor}.{s.revision}.tar.gz'

    def __init__(self, version):
        self.version = version

        version_parts = tuple(map(int, version.split('.')))
        assert len(version_parts) == 3
        self.major, self.minor, self.revision = version_parts

    def ident(self):
        return 'cmake-' + self.version

    def fetch(self, ctx):
        if not os.path.exists(self.src_path(ctx)) and not self.installed(ctx):
            os.chdir(ctx.paths.packsrc)
            tarname = 'cmake-%s.tar.gz' % self.version
            download(self.url.format(s=self), tarname)
            run(['tar', '-xzf', tarname])
            os.remove(tarname)

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
        return os.path.exists(self.build_path(ctx, 'bin', 'cmake'))

    def installed(self, ctx):
        proc = subprocess.run(['cmake', '--version'],
                stdout=subprocess.PIPE, universal_newlines=True)
        if proc.returncode == 0 and 'version ' + self.version in proc.stdout:
            return True
        return os.path.exists(self.install_path(ctx, 'bin', 'cmake'))
