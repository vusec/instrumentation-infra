import os
import subprocess
from ..package import Package
from ..util import FatalError


class Python(Package):
    def __init__(self, version):
        self.version = version

    def ident(self):
        return 'python-' + self.version

    def fetch(self, ctx):
        if not self.installed(ctx):
            raise FatalError(self.binary() + ' not found, please install it')

    def build(self, ctx):
        pass

    def install(self, ctx):
        pass

    def installed(self, ctx):
        try:
            subprocess.call([self.binary(), '--version'])
        except OSError as e:
            if e.errno == os.errno.ENOENT:
                return False
            raise
        return True

    def binary(self):
        return 'python' + self.version
