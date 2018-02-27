import os
import shutil
from ..package import Package
from ..util import run, download


class PatchElf(Package):
    def __init__(self, version):
        self.version = version

    def ident(self):
        return 'patchelf-' + self.version

    def fetch(self, ctx):
        tarname = 'patchelf-%s.tar.bz2' % self.version
        download('https://nixos.org/releases/patchelf/patchelf-0.9/' + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move('patchelf-' + self.version, 'src')
        os.remove(tarname)

    def build(self, ctx):
        if not os.path.exists('src/configure'):
            os.chdir('src')
            run(ctx, ['bash', 'bootstrap.sh'])
            os.chdir('..')

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
        return os.path.exists('obj/src/patchelf')

    def is_installed(self, ctx):
        return os.path.exists('install/bin/patchelf')
