import os
import shutil
from typing import Iterator

from ...context import Context
from ...package import Package
from ...util import apply_patch, download, run


class LibElf(Package):
    """
    :identifier: libelf-<version>
    :param str version: version to download
    """

    def __init__(self, version: str):
        self.version = version

    def ident(self) -> str:
        return "libelf-" + self.version

    def fetch(self, ctx: Context) -> None:
        tarname = f"libelf-{self.version}.tar.gz"
        download(
            ctx,
            "https://web.archive.org/web/20160505164756if_/"
            "http://www.mr511.de/software/"
            + tarname,
        )
        run(ctx, ["tar", "-xf", tarname])
        shutil.move("libelf-" + self.version, "src")
        os.remove(tarname)

        if self.version == "0.7.0":
            os.chdir("src")
            config_path = os.path.dirname(os.path.abspath(__file__))
            apply_patch(ctx, config_path + "/libelf-0.7.0-prelink.patch", 1)
            apply_patch(ctx, config_path + "/libelf-0.7.0-hash-prelink.patch", 1)
        else:
            ctx.log.debug(f"could not patch libelf version {self.version} for prelink")

    def build(self, ctx: Context) -> None:
        os.makedirs("obj", exist_ok=True)
        os.chdir("obj")
        run(ctx, ["../src/configure", "--prefix=" + self.path(ctx, "install")])
        run(ctx, ["make", f"-j{ctx.jobs}"])

    def install(self, ctx: Context) -> None:
        os.chdir("obj")
        run(ctx, ["make", "install"])

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists("obj/lib/libelf.a")

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists("install/lib/libelf.a")


class Prelink(Package):
    """
    :identifier: prelink-<version>
    :param str version: version to download
    """

    def __init__(self, version: str):
        self.version = version
        # assert version == '209'
        self.libelf = LibElf("0.7.0")

    def ident(self) -> str:
        return "prelink-" + self.version

    def dependencies(self) -> Iterator[Package]:
        yield self.libelf

    def fetch(self, ctx: Context) -> None:
        run(
            ctx,
            [
                "svn",
                "co",
                "-r" + self.version,
                "svn://sourceware.org/svn/prelink/trunk",
                "src",
            ],
        )
        os.chdir("src")
        config_path = os.path.dirname(os.path.abspath(__file__))
        apply_patch(ctx, config_path + "/prelink-execstack-link-fix.patch", 0)

    def build(self, ctx: Context) -> None:
        os.makedirs("obj", exist_ok=True)
        os.chdir("obj")
        env = {
            "C_INCLUDE_PATH": self.libelf.path(ctx, "install/include"),
            "ac_cv_lib_selinux_is_selinux_enabled": "no",
            "ac_cv_header_gelf_h": "no",
        }
        config_env = {
            **env,
            "CPPFLAGS": "-I" + self.libelf.path(ctx, "install/include/libelf"),
            "LDFLAGS": "-L" + self.libelf.path(ctx, "install/lib"),
        }
        run(
            ctx,
            [
                "../src/configure",
                "--prefix=" + self.path(ctx, "install"),
                "--sbindir=" + self.path(ctx, "install/bin"),
            ],
            env=config_env,
        )
        run(ctx, ["make", f"-j{ctx.jobs}", "-C", "gelf"], env=env)
        run(ctx, ["make", f"-j{ctx.jobs}", "-C", "src"], env=env)

    def install(self, ctx: Context) -> None:
        run(ctx, "make install -C obj/src")

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists("obj/src/prelink")

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists("install/bin/prelink")
