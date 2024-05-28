from itertools import chain
import itertools
import logging
import os
import shutil
import subprocess
import requests
import platform
import tarfile

from urllib import request

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from requests import patch
from sympy import Q

from ...context import Context
from ...package import Package
from ...util import FatalError, apply_patch, download, run, untar
from ..cmake import CMake
from ..gnu import AutoMake, Bash, BinUtils, CoreUtils, Make
from ..ninja import Ninja


@dataclass(repr=False, eq=True, order=True, frozen=True, slots=True)
class Version:
    """Simple class for parsing & holding a version number from a string"""

    major: int
    minor: int
    patch: int

    @staticmethod
    def parse(v_str: str) -> "Version":
        v = [int(_v) for _v in v_str.split(".")]
        if not v:
            raise ValueError(f"Invalid version; cannot parse: {v_str}")
        return Version(v[0], v[1] if len(v) >= 2 else 0, v[2] if len(v) >= 3 else 0)

    def __repr__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def getGlobalLLVM(version: Version | None = None) -> Path | None:
    """Try to get a llvm-config binary for the requested version from the system ($PATH)"""

    # If no version was given, return the version of whatever llvm-config binary is found by default
    if version is None:
        conf_bin = shutil.which("llvm-config")
        return Path(conf_bin) if conf_bin is not None else None

    # Helper function to verify the version reported by llvm-config
    def _checkVersion(_bin: Path, _v: Version | None = None) -> Version | None:
        try:
            _p = subprocess.run([_bin, "--version"], capture_output=True, text=True)

            # Check the version iff it was given; otherwise return the reported version
            if _v is None:
                return Version.parse(_p.stdout.strip())
            return _v if _v == Version.parse(_p.stdout.strip()) else None
        except:
            return None

    # Try to find a versioned llvm-config binary in the system (if any) for the requested version
    v_bin = shutil.which(f"llvm-config-{version.major}")
    if v_bin is not None and _checkVersion(Path(v_bin), version):
        return Path(v_bin)

    # Search dirs in $PATH for llvm-config/llvm-config-X; if version matches, return it
    for dir in [Path(_dir) for _dir in os.environ.get("PATH", "").split(":")]:
        for bin in itertools.chain(dir.glob(f"llvm-config-{version.major}"), dir.glob("llvm-config")):
            if _checkVersion(bin, version):
                return bin

    # No llvm-config executables found for the correct version
    return None


def llvmHasBin(conf: Path, bin: str) -> bool:
    """Check if the given llvm-config instance's bindir contains the given program"""
    if not conf.is_file() or not bin:
        return False
    bindir = Path(subprocess.run([conf, "--bindir"], capture_output=True, text=True).stdout.strip())
    return any(bindir.glob(bin)) if bindir.is_dir() else False


def llvmHasLib(conf: Path, lib: str) -> bool:
    """Check if the given llvm-config instance's libdir recursively contains the given library"""
    if not conf.is_file() or not lib:
        return False
    libdir = Path(subprocess.run([conf, "--libdir"], capture_output=True, text=True).stdout.strip())
    return any(libdir.rglob(lib)) if libdir.is_dir() else False


class LLVM(Package):
    """
    LLVM: [LLVM compiler infrastructure project](https://llvm.org/) dependency package

    Ensures an LLVM instance of the requested version exists and configures the infrastructure's
    configuration context to use it for compiling and linking dependent targets/packages/instances
    and their dependencies.

    Note: calling ``configure()`` on an object of this class will populate the configuration
    context's variables to use the tools from this package (i.e. variables like `ctx.cc` or
    `ctx.cxx` will be set to the `clang` and `clang++` binaries from this package's LLVM instance).

    By default, this package will look for and attempt to reuse an existing instance of LLVM. If
    `force_local` is set, this package will ignore existing LLVM instances and always build a local
    copy in the infrastructure's build tree. If unset, this package will search for an `llvm-config`
    executable (also extended with the requested version) in the user's system. If the version
    reported by `llvm-config` matches the requested version, the locations and settings reported
    by `llvm-config` will be used.

    Alternatively, a path to a specific `llvm-config` executable can be given, which will be used
    to populate this package's configuration (note: the LLVM version of this binary must match).

    If no valid LLVM instance can be found, this class will download and install a matching version
    of LLVM. If :param:`allow_bins` is True, this package will try to get precompiled binary releases
    of the requested LLVM version. If :param:`allow_bins` is False or no matching binary release can
    be found, this package will build a new LLVM instance from source.

    The [clang](https://clang.llvm.org/) project is always enabled. Additional projects to
    enable can be specified through `projects` (e.g. the [lld](https://lld.llvm.org/) linker).

    Runtimes to enable can be specified through `runtimes` (e.g. the compiler rutime
    [compiler-rt](https://compiler-rt.llvm.org/) needed to run ASan).

    Also supports providing a set of patches which will be :func:`applied<util.apply_patch>` to the
    LLVM source tree before building. Patches are applied using the `patch` command and expect a
    difference listings (e.g. produced by `diff -c` or `diff -u`). Patches can be provided as follows:

    1. ``path``: where path is a path-like object holding an absolute path to the patch file or a
                relative path from the context's root directory (i.e. relative to ctx.paths.root).
    2. ``(rundir, path)``: where `rundir` is the working directory to use when applying the patch (i.e.
                        `chdir(rundir)` before calling `patch`), and `path` is the same as above.
    3. ``builtin``: name of one of the supported built-in patches. Available built-in patches are:
            * `gold-plugins` (3.8.0/3.9.1/4.0.0/5.0.0/7.0.0): adds a `-load` option to load passes
                    from a shared object file during link-time optimisations; best used in combination
                    with :class:`LLVMPasses`
            * `statsfilter` (3.8.0/3.9.1/5.0.0/7.0.0): adds a `-stats-only` options which relates to
                    `-stats` like `-debug-only` relates to `-debug`
            * `lto-nodiscard-value-names` (7.0.0): preserves value names when producing bitcode for
                    LTO (useful for debugging passes)
            * `safestack` (3.8.0): adds the `-fsanitize=safestack` option for old versions of LLVM
            * `compiler-rt-typefix` (4.0.0): fixes a bug in `compiler-rt` version 4.0.0 so that it
                    compiles for recent versions of glibc (applied automatically if the `compiler-rt`
                    runtime is passed)
    """

    @property
    def lld(self) -> bool:
        return "lld" in self.projects

    @property
    def compiler_rt(self) -> bool:
        return "compiler_rt" in self.runtimes

    def ident(self) -> str:
        return self.name

    def dependencies(self) -> Iterator[Package]:
        if self.llvm_config is not None:
            yield from []
        else:
            if shutil.which("make") is None:
                yield Make("4.3")
            if shutil.which("bash") is None:
                yield Bash("5.1.16")
            if shutil.which("cmake") is None:
                yield CMake("3.28")
            if shutil.which("ld") is None:
                yield BinUtils("2.38")
            if shutil.which("cat") is None:
                yield CoreUtils("8.32")
            if (
                shutil.which("automake") is None
                or shutil.which("autoconf") is None
                or shutil.which("m4") is None
                or shutil.which("libtoolize") is None
            ):
                yield AutoMake.default(
                    automake_version="1.16.5",
                    autoconf_version="2.71",
                    m4_version="1.4.18",
                    libtool_version="2.4.6",
                )

    def __init__(
        self,
        version: str,
        *,
        llvm_config: Path | None = None,
        force_local: bool = False,
        allow_bins: bool = True,
        commit: str | None = None,
        projects: Iterable[str] = {"clang", "lld"},
        runtimes: Iterable[str] = {"compiler-rt"},
        build_flags: Iterable[str] = [
            "-DCMAKE_BUILD_TYPE=Release",
            "-DLLVM_BUILD_LLVM_DYLIB=ON",
            "-DLLVM_ENABLE_BINDINGS=OFF",
            "-DLLVM_ENABLE_EH=OFF",
            "-DLLVM_ENABLE_FFI=OFF",
            "-DLLVM_ENABLE_PIC=ON",
            "-DLLVM_ENABLE_RTTI=ON",
            "-DLLVM_INSTALL_UTILS=ON",
            "-DLLVM_LINK_LLVM_DYLIB=ON",
            "-DLLVM_TARGETS_TO_BUILD=X86",
            "-DLLVM_OPTIMIZED_TABLEGEN=ON",
        ],
        patches: Iterable[Path | str | tuple[str, str]] = [],
    ) -> None:
        """
        Set the base configuration for the LLVM package object; also checks to see if any global LLVM
        instances can be found that match the requested version and can be reused.

        Note: enabled projects/runtimes are found automatically when reusing an existing LLVM instance

        :param str version:
            The desired LLVM version to use/get/build
        :param Path | None llvm_config:
            Path to a `llvm-config` binary of a pre-existing LLVM instance to use
        :param bool force_local:
            Ignore any global LLVM instances; will get binary releases or build from sources, defaults to False
        :param bool allow_bins:
            Allow using binary releases of the given LLVM version if they exist
        :param str | None commit:
            Build LLVM from this specific commit/hash/tag/release, defaults to "llvmorg-<version>"
        :param Iterable[str] projects:
            List of projects to build
        :param Iterable[str] runtimes:
            Enable these runtimes, defaults to compiler-rt
        :param Iterable[str] build_flags:
            Use these flags when building LLVM (used iff building from source)
        :param Iterable[str | tuple[str, str]] patches:
            Patches to apply to LLVM sources before building; can be a path to a patch file (applied
            in the root directory of the package) or a tuple of the working directory to use and the
            path to the patch file: (<work_dir>, <patch_path>); if the path to the patch file is
            relative, the patch file is first sought relative to the current working directory; if
            not found, the path is taken relative to this package's source file
        """
        self.version = Version.parse(version)
        self.name = f"llvm-{self.version}"
        self.llvm_config = llvm_config
        self.force_local = force_local
        self.allow_bins = allow_bins
        self.commit = commit if commit is not None else f"llvmorg-{self.version}"
        self.projects = set(projects)
        self.runtimes = set(runtimes)
        self.build_flags = set(build_flags)
        self.patches = set(patches)

        # Some sanity checks
        if self.llvm_config is not None and force_local:
            raise ValueError("Providing llvm-config not compatible with forcing local builds/build flags/patches")

        # Add patches based on given version
        if self.compiler_rt and self.version == Version(4, 0, 0):
            self.patches.add("compiler-rt-typefix")

        # If no llvm-config was given (and a new build isn't being forced), try to find a sytem-wide LLVM instance
        if self.llvm_config is None and not self.force_local:
            self.llvm_config = getGlobalLLVM(self.version)

        # If llvm-config exists, check if lld and compiler-rt are available
        if self.llvm_config is not None:
            if llvmHasBin(self.llvm_config, "ld.lld"):
                self.projects.add("lld")
            if llvmHasLib(self.llvm_config, "libclang_rt.*"):
                self.runtimes.add("compiler-rt")

    def root_dir(self, ctx: Context, *args) -> Path:
        return Path(self.path(ctx, *args))

    def is_fetched(self, ctx: Context) -> bool:
        if self.llvm_config is not None:
            return True
        return any(self.root_dir(ctx).iterdir()) if self.root_dir(ctx).is_dir() else False

    def is_built(self, ctx: Context) -> bool:
        if self.llvm_config is not None:
            return True
        return any(self.root_dir(ctx, "build").iterdir()) if self.root_dir(ctx, "build").is_dir() else False

    def is_installed(self, ctx: Context) -> bool:
        if self.llvm_config is not None:
            return True
        return any(self.root_dir(ctx, "install").iterdir()) if self.root_dir(ctx, "install").is_dir() else False

    def is_clean(self, ctx: Context) -> bool:
        if self.llvm_config is not None:
            return True
        return not any(self.root_dir(ctx).iterdir()) if self.root_dir(ctx).is_dir() else True

    def __get_bins(self, ctx: Context) -> Path | None:
        urls: list[str] = []
        response = requests.get(f"https://api.github.com/repos/llvm/llvm-project/releases/tags/llvmorg-{self.version}")
        response.raise_for_status()

        response_json = response.json()
        if not isinstance(response_json, dict):
            ctx.log.error(f"GitHub releases API did not return a valid response JSON from {response.url}")
            return None

        assets = response_json.get("assets", {})
        ctx.log.debug(f"Found {len(assets)} downloadable assets for LLVM release {self.version}")

        # Find all downloadable precompiled binaries for Linux (x86_64)
        for asset in assets:
            # Ensure the asset is also a valid dictionary
            if not isinstance(asset, dict):
                ctx.log.warning(f"Asset object not a dictionary: {asset}")
                continue

            # Extract the asset (archive) name and its download URL; ensure they exist & are strings
            name = asset.get("name")
            url = asset.get("browser_download_url")
            if not isinstance(name, str) or not isinstance(url, str):
                ctx.log.error(f"Malformed asset object: {asset}")
                continue

            # Skip any assets that aren't archives or aren't precompiled binaries
            if not name.startswith(f"clang+llvm-{self.version}") or not name.endswith(".tar.xz"):
                continue

            # Skip assets that aren't valid for the current machine or operating system
            if not platform.machine().lower() in name.lower():
                continue
            if not platform.system().lower() in name.lower():
                continue

            ctx.log.info(f"Found asset for the current machine: {name} ({url})")
            urls.append(url)

        ctx.log.info(f"Found {len(urls)} asset URLs")
        for url in urls:
            try:
                ctx.log.info(f"Attempting to download LLVM {self.version} binary releases from {url}")
                archive = Path(request.urlretrieve(url, filename=self.path(ctx, "llvm_bins.tar.xz"))[0])
                if archive.is_file():
                    ctx.log.info(f"Successfully downloaded LLVM {self.version} binary releases into {archive}")
                    return archive

                raise FileNotFoundError(f"{archive} not found after downloading from {url}")
            except Exception as e:
                ctx.log.error(f"Failed to download asset from {url}: {e}")

        ctx.log.warning(f"Failed to get binary releases for LLVM {self.version}")
        return None

    def __extract_bins(self, ctx: Context, archive_path: Path) -> None:
        ctx.log.info(f"Unpacking archive {archive_path}")

        with tarfile.open(archive_path) as archive:
            root_mems = [mem for mem in archive.getmembers() if "/" not in mem.name and mem.isdir()]

            # If the archive contains a single directory, ignore it & extract its mems into the target
            if len(root_mems) == 1:
                root_dir = root_mems[0].name
                ctx.log.info(f"Archive contains single directory; expanding root: {root_dir}")
                for member in archive.getmembers():
                    if member.name.startswith(root_dir):
                        member.name = str(Path(member.name).relative_to(root_dir))
                        archive.extract(member, self.root_dir(ctx, "install"))
            else:
                # If files are directly in the top-level archive, just extract them all
                ctx.log.info(f"Archive directly contains files; extracting into {self.root_dir(ctx)}")
                archive.extractall(self.root_dir(ctx, "install"))

        # Cleanup the archive
        archive_path.unlink()

    def fetch(self, ctx: Context) -> None:
        if self.llvm_config is not None:
            ctx.log.info(f"Using external LLVM instance; skipping fetch...")
            return
        self.goto_rootdir(ctx)

        # If using a binary release is allowed, try to get them
        if self.allow_bins:
            ctx.log.info(f"Attempting to download pre-compiled binary releases of LLVM {self.version}")
            archive = self.__get_bins(ctx)
            if archive is not None:
                self.__extract_bins(ctx, archive)
                ctx.log.info(f"Successfully extracted binary release of LLVM {self.version}")
                return

        ctx.log.info(f"Fetching LLVM {self.version} sources...")
        run(
            ctx,
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                f"llvmorg-{self.version}",
                "https://github.com/llvm/llvm-project.git",
                self.path(ctx),
            ],
            teeout=True,
        )
        ctx.log.info(f"Cloned LLVM {self.version} sources into {self.root_dir(ctx)}")

    def apply_patches(self, ctx: Context) -> None:
        """
        Apply the patches specified in self.patches. For supported formats for this iterable, see
        this class' docstring.

        :param Context ctx: the configuration context
        """
        for patch in self.patches:
            ctx.log.debug(f"Parsing and applying patch: {patch}")
            self.goto_rootdir(ctx)

            # Get the working directory to use and the patchfile to apply
            if isinstance(patch, tuple):
                # The working directory for applying the patch can be specified in the first element
                workdir = Path(self.path(ctx, patch[0]))

                # The second element picks the (possibly absolute path or to a built-in) patch file
                if Path(patch[1]).is_absolute():
                    patchfile = Path(patch[1])
                else:
                    patchfile = Path(__file__).absolute().parent.joinpath(patch[1])
            else:
                # Just use the package's base directory as the working directory
                workdir = Path(self.path(ctx))

                # Get the patch file to use (either from an absolute path or a built-in patch)
                if Path(patch).is_absolute():
                    patchfile = Path(patch)
                else:
                    patchfile = Path(__file__).absolute().parent.joinpath(patch)

            # Actually apply the patch
            os.chdir(workdir)
            apply_patch(ctx, str(patchfile), 0)

    def build(self, ctx: Context) -> None:
        if self.llvm_config is not None:
            ctx.log.info(f"Using external LLVM instance; skipping build...")
            return

        # If a binary release was installed already (during fetch), skip building
        if self.is_installed(ctx):
            ctx.log.info(f"LLVM {self.version} installed from binary releases; nothing to build")
            return

        # If necessary, apply the patches to the source tree first
        self.apply_patches(ctx)

        # Go to the root directory and generate the build configuration/tree
        self.goto_rootdir(ctx)
        run(
            ctx,
            [
                "cmake",
                "-S",
                "llvm",
                "-B",
                "build",
                "-G",
                "Ninja",
                "--install-prefix",
                self.path(ctx, "install"),
                f"-DLLVM_ENABLE_PROJECTS={';'.join(self.projects)}",
                f"-DLLVM_ENABLE_RUNTIMES={';'.join(self.runtimes)}",
                f"-DLLVM_PARALLEL_COMPILE_JOBS={ctx.jobs}",
                f"-DLLVM_PARALLEL_LINK_JOBS={ctx.jobs}",
                *self.build_flags,
            ],
            teeout=True,
        )

        # Actually run the build
        run(ctx, ["cmake", "--build", "build", "--parallel", str(ctx.jobs)], teeout=True)

    def install(self, ctx: Context) -> None:
        if self.llvm_config is not None:
            ctx.log.info(f"Using external LLVM instance; skipping install...")
            return

        if self.is_installed(ctx):
            ctx.log.info(f"LLVM {self.version} installed from binary releases; nothing to install")
            return

        # Install LLVM into the previously configured prefix
        run(ctx, ["cmake", "--install", "build"], teeout=True)

    def install_env(self, ctx: Context) -> None:
        # If LLVM was built locally, use the llvm-config installed by it; else use pre-existing llvm-config
        if self.llvm_config is not None:
            conf_bin = self.llvm_config
        else:
            conf_bin = Path(self.path(ctx, "install", "bin", "llvm-config"))
        if not conf_bin.is_file():
            raise FileNotFoundError(f"Failed to found llvm-config binary: {conf_bin}!")

        root_dir = Path(run(ctx, [conf_bin, "--obj-root"]).stdout.strip())
        bins_dir = Path(run(ctx, [conf_bin, "--bindir"]).stdout.strip())
        libs_dir = Path(run(ctx, [conf_bin, "--libdir"]).stdout.strip())

        if not root_dir.is_dir() or not bins_dir.is_dir() or not libs_dir.is_dir():
            raise FileNotFoundError(f"No LLVM root/bins/libs dir: ({root_dir}:{bins_dir}:{libs_dir})")

        # Prepend LLVM's directories to the front of $PATH/$LD_LIBRARY_PATH so they're prioritised
        cur_path = ctx.runenv.setdefault("PATH", os.environ.get("PATH", "").split(":"))
        cur_libs = ctx.runenv.setdefault("LD_LIBRARY_PATH", os.environ.get("LD_LIBRARY_PATH", "").split(":"))
        if isinstance(cur_path, str):
            cur_path = cur_path.split(":")
        if isinstance(cur_libs, str):
            cur_libs = cur_libs.split(":")

        # Set the defaults; set LLVM_DIR and add bins/libs dir to $PATH & $LD_LIBRARY_PATH
        ctx.runenv["LLVM_DIR"] = str(root_dir)
        cur_path.insert(0, str(bins_dir))
        cur_libs.insert(0, str(libs_dir))

        # Also explicitly set ctx.cc/ctx.cxx/etc to point to this LLVM instance (if the bins exist)
        if (bins_dir / "clang").is_file():
            ctx.cc = str(bins_dir / "clang")
        else:
            ctx.log.warning(f"Binary not found: 'clang' ({bins_dir / 'clang'})")
        if (bins_dir / "clang++").is_file():
            ctx.cxx = str(bins_dir / "clang++")
        else:
            ctx.log.warning(f"Binary not found: 'clang++' ({bins_dir / 'clang++'})")
        if (bins_dir / "llvm-ar").is_file():
            ctx.ar = str(bins_dir / "llvm-ar")
        else:
            ctx.log.warning(f"Binary not found: 'llvm-ar' ({bins_dir / 'llvm-ar'})")
        if (bins_dir / "llvm-nm").is_file():
            ctx.nm = str(bins_dir / "llvm-nm")
        else:
            ctx.log.warning(f"Binary not found: 'llvm-nm' ({bins_dir / 'llvm-nm'})")
        if (bins_dir / "llvm-ranlib").is_file():
            ctx.ranlib = str(bins_dir / "llvm-ranlib")
        else:
            ctx.log.warning(f"Binary not found: 'llvm-ranlib' ({bins_dir / 'llvm-ranlib'})")

        # If LLD was built, use it as a linker
        if self.lld:
            ctx.add_flags("-fuse-ld=lld", cc=False, cxx=False, ld=True, lib_ld=False, dups=False)

    @staticmethod
    def load_pass_plugin(ctx: Context, libs: Iterable[str]) -> None:
        """
        For compiler pass plugins, load the compiler passes from the given shared library objects.

        :param Context ctx: the configuration context
        :param Iterable[str] libs: add a variable number of shared libraries to load
        """
        for lib in libs:
            ctx.log.debug(f"Adding compiler pass plugin: {lib}")
            ctx.cflags.append(f"-fpass-plugin={lib}")
            ctx.cxxflags.append(f"-fpass-plugin={lib}")

    @staticmethod
    def add_lto_pass(ctx: Context, libs: Iterable[str], old_mgr: bool = True, new_mgr: bool = True) -> None:
        """
        Helper function to load a given LTO pass using the old and/or new (LLVM >= 13) pass manager(s)

        :param Context ctx: the configuration context
        :param Iterable[str] libs: shared library files containing LTO passes to load
        :param bool old_mgr: load using old pass manager (i.e. -Wl,-mllvm=-load=[lib]), defaults to true
        :param bool new_mgr: load using new pass manager (i.e. -Wl,--load-pass-plugin=[lib]), defaults to true
        """
        for lib in libs:
            ctx.log.debug(f"Adding link-time optimisation pass: {lib}")
            if old_mgr:
                ctx.ldflags.append(f"-Wl,-mllvm=-load={lib}")
            if new_mgr:
                ctx.ldflags.append(f"-Wl,--load-pass-plugin={lib}")

    @staticmethod
    def add_lto_pass_flags(ctx: Context, flags: Iterable[str], gold_passes: bool = False) -> None:
        """
        Helper function to add link-time flags to the inserted LTO passes; prefixes the given flags with
        `-Wl,-plugin-opt=` when using the gold plugin; otherwise add them using the interface expected
        by ld.lld (i.e. prepends `-Wl,-mllvm=`). Requires ld.lld to be enabled.

        :param Context ctx: the configuration context
        :param Iterable[str] *flags: the LTO pass flags to add to `ctx.ldflags`
        :param bool gold_passes: insert the pass using syntax for gold plugin
        """
        for flag in flags:
            if gold_passes:
                ctx.ldflags.append(f"-Wl,-plugin-opt={str(flag)}")
            else:
                ctx.ldflags.append(f"-Wl,-mllvm={str(flag)}")
