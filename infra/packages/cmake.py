import os
import shutil

from ..context import Context
from ..package import Package
from ..util import download, run


class CMake(Package):
    """
    :identifier: cmake-<version>
    :param version: version to download
    """

    url = (
        "https://cmake.org/files/v{s.major}.{s.minor}/"
        "cmake-{s.major}.{s.minor}.{s.revision}.tar.gz"
    )

    def __init__(self, version: str):
        self.version = version

        version_parts = tuple(map(int, version.split(".")))
        assert len(version_parts) == 3
        self.major, self.minor, self.revision = version_parts

    def ident(self) -> str:
        return "cmake-" + self.version

    def fetch(self, ctx: Context) -> None:
        download(ctx, self.url.format(s=self), "src.tar.gz")
        run(ctx, ["tar", "-xzf", "src.tar.gz"])
        shutil.move("cmake-" + self.version, "src")
        os.remove("src.tar.gz")

    def build(self, ctx: Context) -> None:
        os.makedirs("obj", exist_ok=True)
        os.chdir("obj")
        if not os.path.exists("Makefile"):
            run(ctx, ["../src/configure", "--prefix=" + self.path(ctx, "install")])
        run(ctx, ["make", f"-j{ctx.jobs}"])

    def install(self, ctx: Context) -> None:
        os.chdir("obj")
        run(ctx, ["make", "install"])

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists("obj/bin/cmake")

    def is_installed(self, ctx: Context) -> bool:
        if os.path.exists("install/bin/cmake"):
            return True
        proc = run(ctx, ["cmake", "--version"], allow_error=True)
        return proc.returncode == 0 and "version " + self.version in proc.stdout
