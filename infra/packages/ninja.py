import os
import shutil
from ..package import Package
from ..util import run, download


class Ninja(Package):
    """
    :identifier: ninja-<version>
    :param version: version to download
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self):
        return 'ninja-' + self.version

    def fetch(self, ctx):
        tarname = 'v%s.tar.gz' % self.version
        download(ctx, 'https://github.com/ninja-build/ninja/archive/' + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move('ninja-' + self.version, 'src')
        os.remove(tarname)

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        run(ctx, '../src/configure.py --bootstrap')

    def install(self, ctx):
        os.makedirs('install/bin', exist_ok=True)
        shutil.copy('obj/ninja', 'install/bin')

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/ninja')

    def is_installed(self, ctx):
        return os.path.exists('install/bin/ninja')
