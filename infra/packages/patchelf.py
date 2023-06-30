import os
import shutil
from ..context import Context
from ..package import Package
from ..util import run, download


class PatchElf(Package):
    """
    :identifier: patchelf-<version>
    :param version: version to download
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self) -> str:
        return 'patchelf-' + self.version

    def fetch(self, ctx: Context) -> None:
        tarname = f'patchelf-{self.version}.tar.bz2'
        download(ctx, 'https://nixos.org/releases/patchelf/patchelf-0.9/' + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move('patchelf-' + self.version, 'src')
        os.remove(tarname)

    def build(self, ctx: Context) -> None:
        if not os.path.exists('src/configure'):
            os.chdir('src')
            run(ctx, ['bash', 'bootstrap.sh'])
            os.chdir('..')

        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        run(ctx, ['../src/configure', '--prefix=' + self.path(ctx, 'install')])
        run(ctx, ['make', f'-j{ctx.jobs}'])

    def install(self, ctx: Context) -> None:
        os.chdir('obj')
        run(ctx, ['make', 'install'])

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists('src')

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists('obj/src/patchelf')

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists('install/bin/patchelf')
