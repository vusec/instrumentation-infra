from ..context import Context
from ..package import Package
from ..util import FatalError, run


class Python(Package):
    """
    Artificial dependency package for arbitrary Python version. Just checks if
    the version is installed.

    :param version: which version to check for
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self) -> str:
        return "python-" + self.version

    def binary(self) -> str:
        """
        Returns the name of the binary that should be in the PATH.
        """
        return "python" + self.version

    def fetch(self, ctx: Context) -> None:
        if not self.is_installed(ctx):
            raise FatalError(self.binary() + " not found, please install it")

    def build(self, ctx: Context) -> None:
        pass

    def install(self, ctx: Context) -> None:
        pass

    def is_fetched(self, ctx: Context) -> bool:
        return False

    def is_built(self, ctx: Context) -> bool:
        return False

    def is_installed(self, ctx: Context) -> bool:
        proc = run(ctx, [self.binary(), "--version"], allow_error=True, silent=True)
        return proc.returncode == 0
