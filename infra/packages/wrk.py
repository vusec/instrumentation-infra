import os
import shutil
from ..context import Context
from ..package import Package
from ..util import run


class Wrk(Package):
    """
    The wrk benchmark.

    :identifier: wrk-<version>
    :param version: version to download
    """

    name = 'wrk'
    gitrepo = 'https://github.com/wg/wrk.git'

    def __init__(self, version: str = 'master'):
        self.version = version

    def ident(self) -> str:
        return self.name + '-' + self.version

    def fetch(self, ctx: Context) -> None:
        run(ctx, ['git', 'clone', self.gitrepo, 'src'])
        os.chdir('src')
        run(ctx, ['git', 'checkout', self.version])

    def build(self, ctx: Context) -> None:
        os.chdir('src')
        if not os.path.exists('Makefile'):
            run(ctx, ['../src/configure',
                      '--prefix=' + self.path(ctx, 'install')])
        run(ctx, f'make -j{ctx.jobs}')

    def install(self, ctx: Context) -> None:
        os.makedirs('install/bin', exist_ok=True)
        shutil.copy('src/wrk', 'install/bin')

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists('src')

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists('src/wrk')

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists('install/bin/wrk')

    def get_binary_path(self, ctx: Context) -> str:
        return self.path(ctx, 'src', 'wrk')


class Wrk2(Wrk):
    """
    The wrk2 benchmark.

    :identifier: wrk2-<version>
    :param version: version to download
    """
    name = 'wrk2'
    gitrepo = 'https://github.com/giltene/wrk2.git'
