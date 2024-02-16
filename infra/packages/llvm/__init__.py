from itertools import chain
import logging
import os
import shutil
import subprocess
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


class SimpleProc(NamedTuple):
    """Class for holding the return code and stripped stderr/stdout streams for a subprocess"""

    ret_code: int | None = None
    stderr: str = ""
    stdout: str = ""


class Version:
    """Object for holding version numbers in a nicely presented way"""

    def __init__(self, version: str) -> None:
        """Split the given string into the different version numbers and store them"""
        versions = version.split(".")
        if len(versions) > 3:
            raise ValueError(f"Version format parsing error; cannot split {version} into X[.Y[.Z]]")

        self.major = int(versions[0])
        self.minor = int(versions[1]) if len(versions) >= 2 else None
        self.patch = int(versions[2]) if len(versions) >= 3 else None

    def __repr__(self) -> str:
        """Returns a string combining the version numbers with periods"""
        if self.minor is not None and self.patch is not None:
            return f"{self.major}.{self.minor}.{self.patch}"
        if self.minor is not None:
            return f"{self.major}.{self.minor}"
        return f"{self.major}"

    def __eq__(self, v: object) -> bool:
        """True if the versions match (no supplied type is seen as 0 (e.g. 4.0 == 4.0.0))"""
        if not isinstance(v, (Version, str, int)):
            return False
        if isinstance(v, (str, int)):
            v = Version(str(v))
        if self.major != v.major:
            return False
        if (self.minor or 0) != (v.minor or 0):
            return False
        if (self.patch or 0) != (v.patch or 0):
            return False
        return True


def run_llvm_config(bin: Path, flag: str, check: bool = False) -> SimpleProc:
    """
    Run `llvm-config` with the given flag and return the exit code, `stderr`, and `stdout` (stripped)

    :param Path bin: path to the `llvm-config` binary to execute
    :param str flag: which flag to pass to the `llvm-config` binary
    :return tuple[int, str, str]: the return code, `stderr` (stripped), and `stdout` (stripped)
    """
    if not bin.exists():
        raise FileNotFoundError(f"Failed to find {bin}")

    if not os.access(bin, os.X_OK):
        raise PermissionError(f"{bin} is not executable")

    proc = subprocess.run([bin, flag], capture_output=True, text=True, check=check)
    return SimpleProc(ret_code=proc.returncode, stderr=proc.stderr.strip(), stdout=proc.stdout.strip())


def find_global_llvm(version: Version) -> Path | None:
    """
    Scan $PATH and find all `llvm-config[-X]` binaries (where X is the major version number) and, if
    such a binary exists for the correct LLVM version, return its path, otherwise return None

    :param str version: the desired LLVM version
    :return Path | None: path to `llvm-config` for a correctly versioned LLVM instance, None otherwise
    """
    for path in [Path(sub) for sub in os.environ.get("PATH", "").split(":")]:
        if not path.is_dir():
            continue
        for f in path.iterdir():
            if f.name == "llvm-config" or f.name == f"llvm-config-{version.major}":
                proc = run_llvm_config(f, "--version")
                if proc.ret_code != 0:
                    continue
                if version == Version(proc.stdout):
                    return f
    return None


class LLVM(Package):
    """
    LLVM: [LLVM compiler infrastructure project](https://llvm.org/) dependency package

    Ensures an LLVM instance of the requested version exists and configures the infrastructure's\
    configuration context to use it for compiling and linking dependent targets/packages/instances\
    and their dependencies.

    Note: calling ``configure()`` on an object of this class will populate the configuration\
    context's variables to use the tools from this package (i.e. variables like `ctx.cc` or\
    `ctx.cxx` will be set to the `clang` and `clang++` binaries from this package's LLVM instance).

    By default, this package will look for and attempt to reuse an existing instance of LLVM. If\
    `force_local` is set, this package will ignore existing LLVM instances and always build a local\
    copy in the infrastructure's build tree. If unset, this package will search for an `llvm-config`\
    executable (also extended with the requested version) in the user's system. If the version\
    reported by `llvm-config` matches the requested version, the locations and settings reported\
    by `llvm-config` will be used.

    Alternatively, a path to a specific `llvm-config` executable can be given, which will be used\
    to populate this package's configuration (note: the LLVM version of this binary must match).

    The [clang](https://clang.llvm.org/) project is always enabled. Additional projects to\
    enable can be specified through `projects` (e.g. the [lld](https://lld.llvm.org/) linker).

    Runtimes to enable can be specified through `runtimes` (e.g. the compiler rutime\
    [compiler-rt](https://compiler-rt.llvm.org/) needed to run ASan).

    Also supports providing a set of patches which will be :func:`applied<util.apply_patch>` to the\
    LLVM source tree before building. Patches are applied using the `patch` command and expect a\
    difference listings (e.g. produced by `diff -c` or `diff -u`). Patches can be provided as follows:

    1. ``path``: where path is a path-like object holding an absolute path to the patch file or a\
                relative path from the context's root directory (i.e. relative to ctx.paths.root).
    2. ``(rundir, path)``: where `rundir` is the working directory to use when applying the patch (i.e.\
                        `chdir(rundir)` before calling `patch`), and `path` is the same as above.
    3. ``builtin``: name of one of the supported built-in patches. Available built-in patches are:
            * `gold-plugins` (3.8.0/3.9.1/4.0.0/5.0.0/7.0.0): adds a `-load` option to load passes\
                    from a shared object file during link-time optimisations; best used in combination\
                    with :class:`LLVMPasses`
            * `statsfilter` (3.8.0/3.9.1/5.0.0/7.0.0): adds a `-stats-only` options which relates to\
                    `-stats` like `-debug-only` relates to `-debug`
            * `lto-nodiscard-value-names` (7.0.0): preserves value names when producing bitcode for\
                    LTO (useful for debugging passes)
            * `safestack` (3.8.0): adds the `-fsanitize=safestack` option for old versions of LLVM
            * `compiler-rt-typefix` (4.0.0): fixes a bug in `compiler-rt` version 4.0.0 so that it\
                    compiles for recent versions of glibc (applied automatically if the `compiler-rt`\
                    runtime is passed)
    """

    # LLVM >= 8 are fully available through GitHub; older versions only through LLVM's own website
    git_url = "https://github.com/llvm/llvm-project.git"
    src_url = "https://releases.llvm.org"

    # Hold the dependencies for building LLVM
    # TODO: Let the versions of the dependencies depend on the requested LLVM version
    make: Make = Make("4.4.1")
    bash: Bash = Bash("5.2.21")
    cmake: CMake = CMake("3.27.9")
    ninja: Ninja = Ninja("1.11.1")
    binutils: BinUtils = BinUtils("2.41")
    coreutils: CoreUtils = CoreUtils("9.4")
    automake: AutoMake = AutoMake.default(
        automake_version="1.16.5",
        autoconf_version="2.71",
        m4_version="1.4.19",
        libtool_version="2.4.7",
    )

    def __init__(
        self,
        version: str,
        *,
        llvm_config: Path | None = None,
        force_local: bool = False,
        commit: str | None = None,
        projects: Iterable[str] = [],
        runtimes: Iterable[str] = [],
        build_flags: Iterable[str] = [],
        patches: Iterable[str | tuple[str, str]] = [],
    ) -> None:
        """
        Set the base configuration for the LLVM package object; also checks to see if any global LLVM
        instances can be found that match the requested version and can be reused.

        Note: enabled projects/runtimes are found automatically when reusing an existing LLVM instance

        :param str version: the desired version to use in the infrastructure
        :param Path | None llvm_config: use LLVM instance from this llvm-config binary, defaults to None
        :param bool force_local: ignore any global LLVM instances & always build locally, defaults to False
        :param str | None commit: build LLVM from this specific commit/hash/tag/release, defaults to None
        :param Iterable[str] | None projects: enable these projects (clang always enabled), defaults to None
        :param Iterable[str] | None runtimes: enable these projects, defaults to None
        :param Iterable[str] | None build_flags: additional build flags for building LLVM, defaults to None
        :param Iterable[str | tuple[str, str]] | None patches: patches to apply before building LLVM, defaults to None
        """
        self.version = Version(version)
        self.llvm_config = llvm_config
        self.commit = commit
        self.projects = set(projects)
        self.runtimes = set(runtimes)
        self.build_flags = set(build_flags)
        self.patches = set(patches)
        self.name = f"llvm-{self.version}"

        # Set to None for now, will be set from `llvm-config` if reusing an LLVM instance
        self.rootdir: Path | None = None
        self.bindir: Path | None = None
        self.libdir: Path | None = None
        self.incdir: Path | None = None

        # Will also be set later
        self.use_extern: bool = False
        self.lld: bool = False
        self.compiler_rt: bool = False

        # Sanity check
        if self.llvm_config is not None:
            assert not (force_local or len(self.build_flags | self.patches) > 0)

        # If not forcing a local build and not explicitly re-using an LLVM build, find global LLVM instances
        if not force_local and len(self.build_flags | self.patches) == 0:
            self.llvm_config = self.llvm_config or find_global_llvm(self.version)

        # None iff forcing local build/no global LLVM found/modifying LLVM build environment
        if self.llvm_config is None:
            # Always enable the clang project
            self.projects.add("clang")

            # Support legacy uses of this package
            self.lld = "lld" in self.projects
            self.compiler_rt = "compiler-rt" in self.runtimes

            # Append -lld and/or -compiler-rt to name iff enabled
            if self.lld:
                self.name = self.name + "-lld"
            if self.compiler_rt:
                self.name = self.name + "-compiler-rt"

            # A bug in LLVM version 4.0.0 requires the compiler-rt-typefix patch to be applied
            if self.compiler_rt and self.version == Version("4.0.0"):
                self.patches.add("compiler-rt-typefix")
        else:
            self.rootdir = Path(run_llvm_config(self.llvm_config, "--obj-root").stdout)
            self.bindir = Path(run_llvm_config(self.llvm_config, "--bindir").stdout)
            self.libdir = Path(run_llvm_config(self.llvm_config, "--libdir").stdout)
            self.incdir = Path(run_llvm_config(self.llvm_config, "--includedir").stdout)

            # Ensure clang/clang++ are always available
            assert shutil.which("clang", path=self.bindir) is not None
            assert shutil.which("clang++", path=self.bindir) is not None

            # Check for lld by looking for the `ld.lld` binary in LLVM's binary directory
            self.lld = shutil.which("ld.lld", path=self.bindir) is not None

            # Compiler-rt includes the `hwasan_symbolize` so check for its existence
            self.compiler_rt = shutil.which("hwasan_symbolize", path=self.bindir) is not None

            # Append -lld and/or -compiler-rt to name iff enabled
            if self.lld:
                self.name = self.name + "-lld"
            if self.compiler_rt:
                self.name = self.name + "-compiler-rt"

            # Boolean flag to mark that this package is using an external LLVM instance
            self.use_extern = True

    def ident(self) -> str:
        return self.name

    def dependencies(self) -> Iterator[Package]:
        yield from (
            []
            if self.use_extern
            else [
                self.make,
                self.bash,
                self.cmake,
                self.ninja,
                self.binutils,
                self.coreutils,
                self.automake,
            ]
        )

    def is_fetched(self, ctx: Context) -> bool:
        if self.use_extern:
            return True
        rootdir = Path(self.path(ctx))
        return rootdir.is_dir() and any(rootdir.rglob("CMakeLists.txt"))

    def is_built(self, ctx: Context) -> bool:
        if self.use_extern:
            return True
        builddir = Path(self.path(ctx, "build"))
        return builddir.is_dir() and any(builddir.rglob("*.so"))

    def is_installed(self, ctx: Context) -> bool:
        if self.use_extern:
            return True
        installdir = Path(self.path(ctx, "install"))
        return installdir.is_dir() and any(installdir.rglob("*.so"))

    def is_clean(self, ctx: Context) -> bool:
        if self.use_extern:
            return True
        rootdir = Path(self.path(ctx))
        return not rootdir.is_dir()

    def fetch_from_git(self, ctx: Context, shallow: bool = True) -> None:
        """
        For LLVM versions >= 8 the entire source tree can be retrieved from git. By default
        only a shallow clone is done but this can be disabled.

        :param Context ctx: the configuration context
        :param bool shallow: clone the entire git source tree or do a shallow clone (faster/smaller)
        """
        assert self.version.major >= 8
        ctx.log.debug(f"Fetching LLVM {self.version} from git ({self.git_url})")

        self.goto_rootdir(ctx)
        rootdir = Path(self.path(ctx))

        ctx.log.debug(f"Clearing root directory (empty: {rootdir.is_dir() and any(rootdir.iterdir())}): {rootdir}")
        for path in rootdir.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        commit = self.commit or f"llvmorg-{self.version}"
        ctx.log.info(f"Cloning LLVM {self.version} sources into {rootdir}; checking out: {commit}")

        run(
            ctx,
            ["git", "clone", "--depth", "1", "--single-branch", "--branch", commit, self.git_url],
            teeout=ctx.loglevel <= logging.DEBUG,
        )

    def fetch_from_releases(self, ctx: Context, project: str, dest: Path) -> None:
        """
        Fetch LLVM source archives directly from the LLVM releases page, extract them, and place
        them in the proper locations for building.

        :param Context ctx: the configuration context
        :param str project: the project to retrieve (e.g. llvm, clang, lld, etc)
        :param Path dest: which directory to place the extracted files into
        """
        # Old clang is named cfe for w/e reason
        if project == "clang":
            project = "cfe"

        src_name = f"{project}-{self.version}.src"
        tar_name = f"{src_name}.tar.xz"
        archive = Path(self.path(ctx, tar_name))
        url = f"{self.src_url}/{self.version}/{tar_name}"
        ctx.log.debug(f"Downloading {tar_name} from {url} (destination: {archive})")

        # Clear old source files/trees
        ctx.log.debug(f"Clearing destination (empty: {dest.is_dir() and any(dest.iterdir())}): {dest}")
        if dest.is_dir() and dest.exists():
            shutil.rmtree(dest)
        os.makedirs(dest.parent, exist_ok=True)

        # Download & extract
        download(ctx, url, outfile=str(archive))
        untar(ctx, tar_name, str(dest), remove=True, basename=src_name)

    def fetch(self, ctx: Context) -> None:
        if self.use_extern:
            ctx.log.warning("fetch() called for external LLVM instance")
            return

        if self.version.major >= 8:
            self.fetch_from_git(ctx, shallow=True)
        else:
            # Main LLVM sources
            self.fetch_from_releases(ctx, "llvm", Path(self.path(ctx, "src")))

            # Versions before LLVM 8 require downloading all projects & runtimes separately
            for project in self.projects:
                self.fetch_from_releases(ctx, project, Path(self.path(ctx, "src", "tools", project)))
            for runtime in self.runtimes:
                self.fetch_from_releases(ctx, runtime, Path(self.path(ctx, "src", "tools", runtime)))

            # To enable consistent build layout, move everything in the "src" directory one level up
            srcdir = Path(self.path(ctx, "src"))
            for path in srcdir.iterdir():
                dest = srcdir.parent.joinpath(path.name)

                # If the destination is a directory that exists it must first be removed
                if dest.is_dir():
                    shutil.rmtree(dest)

                # This is the same as `mv path dest`
                path.replace(dest)

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
        if self.use_extern:
            ctx.log.warning("build() called for external LLVM instance")
            return

        # If necessary, apply the patches to the source tree first
        self.apply_patches(ctx)

        # Configure the build first; use default settings and append the user-supplied build flags
        self.goto_rootdir(ctx)
        run(
            ctx,
            [
                "cmake",
                "-S",
                self.path(ctx, "llvm-project/llvm") if self.version.major >= 8 else self.path(ctx),
                "-B",
                self.path(ctx, "build"),
                "-G",
                "Ninja",
                "--install-prefix",
                self.path(ctx, "install"),
                "-DCMAKE_BUILD_TYPE=Debug",
                "-DLLVM_OPTIMIZED_TABLEGEN=ON",
                f"-DLLVM_PARALLEL_LINK_JOBS={ctx.jobs}",
                f"-DLLVM_PARALLEL_COMPILE_JOBS={ctx.jobs}",
                f"-DLLVM_BINUTILS_INCDIR={self.binutils.path(ctx, 'install', 'include')}",
                f"-DLLVM_ENABLE_PROJECTS={';'.join(self.projects)}" if self.version.major >= 8 else "",
                f"-DLLVM_ENABLE_RUNTIMES={';'.join(self.runtimes)}" if self.version.major >= 8 else "",
                *self.build_flags,
            ],
            teeout=ctx.loglevel <= logging.DEBUG,
        )

        # Actually build LLVM
        run(
            ctx,
            ["cmake", "--build", self.path(ctx, "build"), "--parallel", ctx.jobs],
            teeout=ctx.loglevel <= logging.DEBUG,
        )

    def install(self, ctx: Context) -> None:
        if self.use_extern:
            ctx.log.warning("install() called for external LLVM instance")
            return

        # Install the locally built LLVM
        run(
            ctx,
            ["cmake", "--install", self.path(ctx, "build"), "--prefix", self.path(ctx, "install")],
            teeout=ctx.loglevel <= logging.DEBUG,
        )

    def install_env(self, ctx: Context) -> None:
        def set_def_and_add(var: str, val: Path) -> None:
            """
            Set the default value of the given variable in the running environment and prepend the
            given value to the running environment.

            :param str var: which variable to take from the system environment as default & prepend to
            :param Path val: the actual value to prepend
            """
            if not val.exists():
                ctx.log.warning(f"Cannot add '{val}' to '{var}'; '{val}' does not exist!")
                return

            # Get current value from runenv and take the OS' value of the variable as the default
            current = ctx.runenv.setdefault(var, os.environ.get(var, "").split(":"))

            # If the environment variable is a list, add the value to FRONT of the list (so it's used first)
            if isinstance(current, list):
                current.insert(0, str(val))
                ctx.log.debug(f"New '{var}': '{current}'")
            else:
                ctx.log.warning(f"Cannot add '{val}' to '{var}'; '{var}' is not a list (got: '{type(current)})")

        if self.use_extern:
            # Sanity checks; these should've been set in the __init__ function
            assert self.bindir is not None and self.libdir is not None

            # Add the binary directory to the PATH variable
            set_def_and_add("PATH", self.bindir)

            # Get all directories with library objects (since compiler-rt and others can be put in lib/.../*.so)
            for libdir in {d.parent for d in chain(self.libdir.rglob("*.so"), self.libdir.rglob("*.a"))}:
                set_def_and_add("LD_LIBRARY_PATH", libdir)

        else:
            # Add the binary directory to the PATH variable
            set_def_and_add("PATH", Path(self.path(ctx, "install", "bin")))

            # Get all directories with library objects (since compiler-rt and others can be put in lib/.../*.so)
            libdir = Path(self.path(ctx, "install", "lib"))
            for _libdir in {d.parent for d in chain(libdir.rglob("*.so"), libdir.rglob("*.a"))}:
                set_def_and_add("LD_LIBRARY_PATH", _libdir)

        # Ensure required variables are loaded & set into the configuration context
        self.load(ctx, False)

    def load(self, ctx: Context, reset_flags: bool = True) -> None:
        """
        Loads the binaries/libraries from this specific LLVM instance into the configuration
        context such that any targets/packages built after calling this will be built using
        this LLVM instance.

        Typically called from a pre-build hook when only using this LLVM instance to build
        a specific target, or from an instance's configure function/at the end of the
        install_env function such that this LLVM instance is used immediately.

        By default, this function will reset the cflags/cxxflags/ldflags to avoid flags used
        for building LLVM itself (with possibly a different compiler) conflicting with flags
        for LLVM tools when building dependencies/targets with this LLVM toolchain. The
        `llvm-config` tool is used to get appropriate initial values for [c/cxx/ld]flags.

        :param Context ctx: the configuration context
        :param bool reset_flags: clear flags used to build LLVM itself, defaults to True
        """
        # If using an external LLVM, use its bindir, otherwise relative to local build
        if self.use_extern:
            # Sanity checks; these should've been set in the __init__ function
            assert isinstance(self.bindir, Path) and self.bindir.exists()
            assert isinstance(self.libdir, Path) and self.libdir.exists()

            ctx.cc = str(self.bindir.joinpath("clang"))
            ctx.cxx = str(self.bindir.joinpath("clang++"))
            ctx.ar = str(self.bindir.joinpath("llvm-ar"))
            ctx.nm = str(self.bindir.joinpath("llvm-nm"))
            ctx.ranlib = str(self.bindir.joinpath("llvm-ranlib"))
        else:
            ctx.cc = str(self.path(ctx, "install", "bin", "clang"))
            ctx.cxx = str(self.path(ctx, "install", "bin", "clang++"))
            ctx.ar = str(self.path(ctx, "install", "bin", "llvm-ar"))
            ctx.nm = str(self.path(ctx, "install", "bin", "llvm-nm"))
            ctx.ranlib = str(self.path(ctx, "install", "bin", "llvm-ranlib"))

        # If resetting [c/cxx/ld]flags, clear the values currently in ctx.[...]flags
        if reset_flags:
            if not len(ctx.cflags) == 0:
                ctx.log.warning(f"Clearing non-empty argument list 'ctx.cflags'; old flags: {ctx.cflags}")
            if not len(ctx.cxxflags) == 0:
                ctx.log.warning(f"Clearing non-empty argument list 'ctx.cxxflags'; old flags: {ctx.cxxflags}")
            if not len(ctx.ldflags) == 0:
                ctx.log.warning(f"Clearing non-empty argument list 'ctx.ldflags'; old flags: {ctx.ldflags}")
            if not len(ctx.lib_ldflags) == 0:
                ctx.log.warning(f"Clearing non-empty argument list 'ctx.lib_ldflags'; old flags: {ctx.lib_ldflags}")
            if not len(ctx.fcflags) == 0:
                ctx.log.warning(f"Clearing non-empty argument list 'ctx.fcflags'; old flags: {ctx.fcflags}")
            ctx.cflags.clear()
            ctx.cxxflags.clear()
            ctx.ldflags.clear()
            ctx.lib_ldflags.clear()
            ctx.fcflags.clear()

    def configure(self, ctx: Context) -> None:
        """
        Configures a clean context that will be used for building this LLVM instance;
        clears the set of cflags/cxxflags/etc

        :param Context ctx: the configuration context
        """
        # Initialise the flags but if there's already values in them, print a warning before clearing
        if len(ctx.cflags) > 0 or len(ctx.cxxflags) > 0 or len(ctx.ldflags) > 0 or len(ctx.lib_ldflags) > 0:
            ctx.log.warning("Non-empty cflags/cxxflags/ldflags/lib_ldflags while configuring LLVM!")

        # Initialise the flags to be empty initially
        ctx.cflags = []
        ctx.cxxflags = []
        ctx.ldflags = []
        ctx.fcflags = []
        ctx.lib_ldflags = []

    def clean(self, ctx: Context) -> None:
        if self.use_extern:
            ctx.log.warning("clean() called for external LLVM instance")
            return

        super().clean(ctx)

    def path(self, ctx: Context, *args: str) -> str:
        if self.use_extern:
            assert isinstance(self.rootdir, Path)
            return str(self.rootdir.joinpath(*args))
        return super().path(ctx, *args)

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


@dataclass
class LLVMBinDist(Package):
    """
    LLVM + Clang binary distribution package.

    Fetches and extracts a tarfile from http://releases.llvm.org.

    :identifier: llvm-<version>
    :param version: the full LLVM version to download, like X.Y.Z
    :param target: target machine in tarfile name, e.g., "x86_64-linux-gnu-ubuntu-16.10"
    :param suffix: if nonempty, create {clang,clang++,opt,llvm-config}<suffix> binaries
    """

    version: str
    target: str
    bin_suffix: str

    def ident(self) -> str:
        return "llvmbin-" + self.version

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def fetch(self, ctx: Context) -> None:
        ident = f"clang+llvm-{self.version}-{self.target}"
        tarname = ident + ".tar.xz"
        download(ctx, f"http://releases.llvm.org/{self.version}/{tarname}")
        run(ctx, ["tar", "-xf", tarname])
        shutil.move(ident, "src")
        os.remove(tarname)

    def is_built(self, ctx: Context) -> bool:
        return True

    def build(self, ctx: Context) -> None:
        pass

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists("install")

    def install(self, ctx: Context) -> None:
        shutil.move("src", "install")
        os.chdir("install/bin")

        if self.bin_suffix:
            for src in ("clang", "clang++", "opt", "llvm-config"):
                tgt = src + self.bin_suffix
                if os.path.exists(src) and not os.path.exists(tgt):
                    ctx.log.debug(f"creating symlink {tgt} -> {src}")
                    os.symlink(src, tgt)
