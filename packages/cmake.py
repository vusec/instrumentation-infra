import os
import shutil
from subprocess import PIPE
from ..package import Package
from ..util import run, run, download


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
        download(ctx, self.url.format(s=self), 'src.tar.gz')
        run(ctx, ['tar', '-xzf', 'src.tar.gz'])
        shutil.move('cmake-' + self.version, 'src')
        os.remove('src.tar.gz')

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
        return os.path.exists('obj/bin/cmake')

    def is_installed(self, ctx):
        if os.path.exists('install/bin/cmake'):
            return True
        proc = run(ctx, ['cmake', '--version'], allow_error=True)
        return proc and proc.returncode == 0 and \
                'version ' + self.version in proc.stdout
