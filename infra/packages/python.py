from ..package import Package
from ..util import run, FatalError


class Python(Package):
    """
    Artificial dependency package for arbitrary Python version. Just checks if
    the version is installed.

    :param version: which version to check for
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self):
        return 'python-' + self.version

    def binary(self) -> str:
        """
        Returns the name of the binary that should be in the PATH.
        """
        return 'python' + self.version

    def fetch(self, ctx):
        if not self.is_installed(ctx):
            raise FatalError(self.binary() + ' not found, please install it')

    def build(self, ctx):
        pass

    def install(self, ctx):
        pass

    def is_fetched(self, ctx):
        return False

    def is_built(self, ctx):
        return False

    def is_installed(self, ctx):
        proc = run(ctx, [self.binary(), '--version'], allow_error=True, silent=True)
        return proc and proc.returncode == 0
