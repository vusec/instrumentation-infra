import os
from ..context import Context
from ..package import Package
from ..util import download, untar


class Scons(Package):
    """
    The scons build tool (replacement for make).

    :identifier: scons-<version>
    :param version: version to download
    """

    name = 'scons'

    def __init__(self, version: str):
        self.version = version

    def ident(self) -> str:
        return self.name + '-' + self.version

    def fetch(self, ctx: Context) -> None:
        os.makedirs('src')
        os.chdir('src')
        tarname = f'scons-local-{self.version}.tar.gz'
        download(ctx, 'http://prdownloads.sourceforge.net/scons/' + tarname)
        untar(ctx, tarname)

    def build(self, ctx: Context) -> None:
        pass

    def install(self, ctx: Context) -> None:
        os.makedirs('install/bin', exist_ok=True)
        os.chdir('install/bin')
        link = 'scons'
        target = '../../src/scons.py'
        if os.path.exists(link):
            assert os.readlink(link) == target
        else:
            os.symlink(target, link)

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists('src')

    def is_built(self, ctx: Context) -> bool:
        return self.is_fetched(ctx)

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists('install/bin/scons')

    @classmethod
    def default(cls) -> 'Scons':
        return cls('3.1.1')
