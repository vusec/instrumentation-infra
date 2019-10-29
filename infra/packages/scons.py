import os
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

    def ident(self):
        return self.name + '-' + self.version

    def fetch(self, ctx):
        os.makedirs('src')
        os.chdir('src')
        tarname = 'scons-local-%s.tar.gz' % self.version
        download(ctx, 'http://prdownloads.sourceforge.net/scons/' + tarname)
        untar(ctx, tarname)

    def build(self, ctx):
        pass

    def install(self, ctx):
        os.makedirs('install/bin', exist_ok=True)
        os.chdir('install/bin')
        link = 'scons'
        target = '../../src/scons.py'
        if os.path.exists(link):
            assert os.readlink(link) == target
        else:
            os.symlink(target, link)

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return self.is_fetched(ctx)

    def is_installed(self, ctx):
        return os.path.exists('install/bin/scons')

    @classmethod
    def default(cls):
        return cls('3.1.1')
