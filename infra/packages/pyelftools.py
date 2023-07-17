import os
from typing import Iterator

from ..context import Context
from ..package import Package
from ..util import run
from .python import Python


class PyElfTools(Package):
    """
    :identifier: pyelftools-<version>
    :param version: version to download
    :param python_version: which Python version to install the package for
    """

    def __init__(self, version: str, python_version: str):
        self.version = version
        self.python = Python(python_version)

    def ident(self) -> str:
        return "pyelftools-" + self.version

    def dependencies(self) -> Iterator[Package]:
        yield self.python

    def fetch(self, ctx: Context) -> None:
        run(
            ctx,
            [
                "git",
                "clone",
                "--branch",
                "v" + self.version,
                "https://github.com/eliben/pyelftools.git",
                "src",
            ],
        )

    def build(self, ctx: Context) -> None:
        os.chdir("src")
        run(ctx, [self.python.binary(), "setup.py", "build"])

    def install(self, ctx: Context) -> None:
        os.chdir("src")
        run(
            ctx,
            [
                self.python.binary(),
                "setup.py",
                "install",
                "--skip-build",
                "--prefix=" + self.path(ctx, "install"),
            ],
        )

    def install_env(self, ctx: Context) -> None:
        relpath = f"install/lib/python{self.python.version}/site-packages"
        abspath = self.path(ctx, relpath)
        syspypath = os.getenv("PYTHONPATH", "").split(":")
        pypath = ctx.runenv.setdefault("PYTHONPATH", syspypath)
        assert isinstance(pypath, list)
        pypath.insert(0, abspath)

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists("src/build")

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists(
            f"install/lib/{self.python.binary()}/site-packages/elftools"
        )
