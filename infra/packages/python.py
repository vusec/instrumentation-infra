from ..package import Package
from ..util import run, FatalError


class Python(Package):
    def __init__(self, version):
        self.version = version

    def ident(self):
        return 'python-' + self.version

    def _binary(self):
        return 'python' + self.version

    def fetch(self, ctx):
        if not self.is_installed(ctx):
            raise FatalError(self._binary() + ' not found, please install it')

    def build(self, ctx):
        pass

    def install(self, ctx):
        pass

    def is_fetched(self, ctx):
        return False

    def is_built(self, ctx):
        return False

    def is_installed(self, ctx):
        proc = run(ctx, [self._binary(), '--version'], allow_error=True, silent=True)
        return proc and proc.returncode == 0
