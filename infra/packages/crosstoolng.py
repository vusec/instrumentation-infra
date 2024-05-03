import os
import shutil
from typing import Iterator

from ..context import Context
from ..package import Package
from ..util import run


class CrosstoolNG(Package):
    """
    crosstool-ng is used to generate full toolchains, either for cross compiling
    or for example to run a custom glibc or another custom component.
    """

    def __init__(self, version: str = "1.25.0"):
        self.version = version

    def ident(self) -> str:
        return "crosstool-ng-" + self.version

    def dependencies(self) -> Iterator[Package]:
        yield from super().dependencies()

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists(self.path(ctx, "src"))

    def fetch(self, ctx: Context) -> None:
        run(
            ctx,
            [
                "git",
                "clone",
                "--branch",
                "crosstool-ng-" + self.version,
                "https://github.com/crosstool-ng/crosstool-ng",
                "src",
            ],
        )

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists(self.path(ctx, "install"))

    def build(self, ctx: Context) -> None:
        install_dir = self.path(ctx, "install")

        os.chdir("src")
        run(ctx, ["./bootstrap"])
        run(ctx, ["autoupdate"])
        run(ctx, ["./bootstrap"])
        run(ctx, ["./configure", f"--prefix={install_dir}"])
        run(ctx, ["make", "-j%d" % ctx.jobs])

        os.makedirs(install_dir, exist_ok=True)

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists(self.path(ctx, "install/bin/ct-ng"))

    def install(self, ctx: Context) -> None:
        os.chdir("src")

        run(ctx, ["make", "install"])


class CustomToolchain(Package):
    """
    Custom full toolchains, either for cross compiling or for example
    to run a custom glibc or another custom component.
    """

    def __init__(
        self,
        arch: str = "x86_64",
        glibc_version: str | None = None,
        min_kernel_version: str | None = None,
        glibc_path: str | None = None,
    ):
        self.arch = arch
        self.glibc_version = glibc_version
        self.glibc_path = glibc_path
        self.min_kernel_version = min_kernel_version

        self.crosstoolNG = CrosstoolNG()

    def ident(self) -> str:
        return "custom-toolchain-" + self.arch

    def dependencies(self) -> Iterator[Package]:
        yield from super().dependencies()
        yield self.crosstoolNG

    def is_fetched(self, ctx: Context) -> bool:
        return True

    def fetch(self, ctx: Context) -> None:
        pass

    def patch_config_file(self, ctx: Context) -> None:
        # CT_ZLIB_VERSION 1.2.12 download no longer exists
        run(
            ctx,
            ["sed", "-i", 's/CT_ZLIB_VERSION=.*/CT_ZLIB_VERSION="1.2.13"/', ".config"],
        )

        if self.glibc_version:
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    's/CT_GLIBC_VERSION=.*/CT_GLIBC_VERSION="%s"/' % self.glibc_version,
                    ".config",
                ],
            )

        if self.min_kernel_version:
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    's/CT_LINUX_VERSION=.*/CT_LINUX_VERSION="%s"/' % self.min_kernel_version,
                    ".config",
                ],
            )
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    's/CT_GLIBC_MIN_KERNEL=.*/CT_GLIBC_MIN_KERNEL="%s"/' % self.min_kernel_version,
                    ".config",
                ],
            )

        if self.glibc_path:
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    "s/CT_GLIBC_SRC_RELEASE=y/# CT_GLIBC_SRC_RELEASE is not set/",
                    ".config",
                ],
            )
            run(ctx, ["sed", "-i", "$ s/$/\\\nCT_GLIBC_SRC_CUSTOM=y/", ".config"])
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    '$ s/$/\\\nCT_GLIBC_CUSTOM_LOCATION="%s"/' % self.glibc_path.replace("/", "\\/"),
                    ".config",
                ],
            )

        if self.glibc_version == "2.21":
            # required for old glibc versions (currently only tested for 2.21)
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    ("s/CT_GLIBC_EXTRA_CFLAGS=.*" '/CT_GLIBC_EXTRA_CFLAGS="-Wno-error=missing-attributes"/'),
                    ".config",
                ],
            )
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    "s/CT_GLIBC_ENABLE_WERROR=y/CT_GLIBC_ENABLE_WERROR=n/",
                    ".config",
                ],
            )
            run(
                ctx,
                [
                    "sed",
                    "-i",
                    ("s/CT_GLIBC_EXTRA_CONFIG_ARRAY=.*" '/CT_GLIBC_EXTRA_CONFIG_ARRAY=("--without-selinux")/'),
                    ".config",
                ],
            )

        run(ctx, ["sed", "-i", "s/# CT_DEBUG_CT is not set/CT_DEBUG_CT=y/", ".config"])
        run(ctx, ["bash", "-c", "echo CT_DEBUG_CT_SAVE_STEPS=y >> .config"])
        run(ctx, ["bash", "-c", "echo CT_DEBUG_CT_SAVE_STEPS_GZIP=n >> .config"])

        run(ctx, ["sed", "-i", "s/CT_DEBUG_GDB=y/# CT_DEBUG_GDB is not set/", ".config"])
        run(
            ctx,
            [
                "sed",
                "-i",
                "s/CT_GDB_GDBSERVER=y/# CT_GDB_GDBSERVER is not set/",
                ".config",
            ],
        )

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists(self.path(ctx, "obj", f"{self.arch}-unknown-linux-gnu"))

    def build(self, ctx: Context) -> None:
        obj_dir = self.path(ctx, "obj")
        os.makedirs("obj", exist_ok=True)
        os.chdir("obj")

        ct_ng_bin = self.crosstoolNG.path(ctx, "install/bin/ct-ng")

        ctx = ctx.copy()
        if "LIBRARY_PATH" in ctx.runenv:
            del ctx.runenv["LIBRARY_PATH"]
        if "LD_LIBRARY_PATH" in ctx.runenv:
            del ctx.runenv["LD_LIBRARY_PATH"]

        run(ctx, [ct_ng_bin, f"{self.arch}-unknown-linux-gnu"])

        self.patch_config_file(ctx)

        run(ctx, [ct_ng_bin, "build", "CT_JOBS=%d" % ctx.jobs, f"CT_PREFIX={obj_dir}"])

        run(ctx, ["chmod", "-R", "ug+w", f"{obj_dir}/{self.arch}-unknown-linux-gnu"])

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists(self.path(ctx, "install", "sysroot"))

    def install(self, ctx: Context) -> None:
        os.makedirs("install", exist_ok=True)

        sysroot_dir = self.path(
            ctx,
            f"obj/{self.arch}-unknown-linux-gnu/{self.arch}-unknown-linux-gnu/sysroot",
        )
        cxx_include_dir = self.path(
            ctx,
            (f"obj/{self.arch}-unknown-linux-gnu/" f"{self.arch}-unknown-linux-gnu/include/c++"),
        )
        gcc_lib_dir = self.path(ctx, f"obj/{self.arch}-unknown-linux-gnu/lib/gcc")

        shutil.copytree(sysroot_dir, self.path(ctx, "install/sysroot"))
        shutil.copytree(cxx_include_dir, self.path(ctx, "install/sysroot/usr/include/c++"))
        shutil.copytree(gcc_lib_dir, self.path(ctx, "install/sysroot/lib/gcc"))

    def configure_flags(self, ctx: Context) -> None:
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        :param ctx: the configuration context
        """
        host_sysroot = self.path(ctx, "install/sysroot")
        # in the future 'target_sysroot' can be used if you are running the binaries
        # on a different device than compiling.
        target_sysroot = host_sysroot

        ld_path = f"{target_sysroot}/lib64/ld-{self.glibc_version}.so"
        if not os.path.exists(ld_path):
            if self.arch == "x86_64":
                ld_path = f"{target_sysroot}/lib64/ld-linux-{self.arch}.so.2"
            elif self.arch == "aarch64":
                ld_path = f"{target_sysroot}/lib64/ld-linux-{self.arch}.so.1"

        if self.is_installed(ctx):
            assert os.path.exists(ld_path)

        cflags = [
            f"-I{host_sysroot}/usr/include",
            f"--sysroot={host_sysroot}",
        ]
        cxxflags = [
            f"-I{host_sysroot}/usr/include",
            f"-I{host_sysroot}/usr/include/c++/11.2.0",
            f"-I{host_sysroot}/usr/include/c++/11.2.0/{self.arch}-unknown-linux-gnu",
            f"-I{host_sysroot}/usr/include/c++/11.2.0/backward",
            f"--sysroot={host_sysroot}",
        ]
        ldflags = [
            f"-L{host_sysroot}/lib64",
            f"-L{host_sysroot}/lib64/gcc/{self.arch}-unknown-linux-gnu/11.2.0",
            f"-L{host_sysroot}/usr/lib64",
            f"--sysroot={host_sysroot}",
            f"-Wl,-rpath={target_sysroot}/lib64",
            f"-Wl,--dynamic-linker={ld_path}",
        ]

        ctx.cflags += cflags
        ctx.cxxflags += cxxflags
        ctx.ldflags += ldflags

    def package_configure(self, ctx: Context) -> None:
        """
        Set build/link flags in **ctx** for packages that are to be linked against
        this glibc.
        """
        self.configure_flags(ctx)

    def configure(self, ctx: Context) -> None:
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance.

        :param ctx: the configuration context
        """
        self.configure_flags(ctx)

        # FIXME: this used to use ctx.extra_libs, whatever that is.
        ctx.ldflags += [
            "-Wl,--start-group",
            "-lgcc",
            "-lstdc++",
            "-lgcc_s",
            "-lc",
            "-lm",
            "-Wl,--end-group",
        ]

        ctx.runenv["LIBRARY_PATH"] = self.path(ctx, "install/sysroot/usr/lib")
