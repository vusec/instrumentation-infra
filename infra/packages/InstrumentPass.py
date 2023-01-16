"""This file defines the configuration, compilation, building, installing, etc. infrastructure for
the instrumentation pass LLVM pass. The pass uses LLVM's new pass manager & inserts sanitiser
(UBStar) calls and configurations into a given module."""

import os
from ..package import Package
from ..util import run, Namespace


class InstrumentPass(Package):
    """Package container for the InstrumentPass LTO pass. This pass operates on LLVM IR as a final
    LTO pass and inserts calls and management operations to the runtime sanitiser library, like
    inserting check calls around memory operations. The pass is loaded using LLVM's new pass
    manager and includes the target analysis pass for targeted sanitisation. This relies on SVF.

    Args:
        infra (infra.Package): Parent class

    Returns:
        InstrumentPass instance: Instance of InstrumentPass package

    Yields:
        Dependency (infra.Package): LLVM Package as dependency"""

    name = "InstrumentPass"
    instrumentpass_dir = "InstrumentPass"
    pass_lib = "libInstrumentPass.so"
    build_dir = "build"
    install_dir = "install"

    def repo_path(self, ctx):
        """Retrieve the path to the git submodule path"""
        return os.path.join(ctx.paths.root, "VeriPatch", self.name)

    def lib_path(self, ctx):
        """Get full path to dynamic library file"""
        return os.path.join(self.repo_path(ctx), "lib", self.pass_lib)

    def ident(self):
        """Return package's name"""
        return self.name

    def __init__(self, SVF, settings: Namespace):
        """Depends on SVF & store default settings dictionary"""
        super().__init__()
        self.SVF = SVF
        self.settings = settings

    def dependencies(self):
        """InstrumentPass depends on SVF"""
        yield self.SVF

    def fetch(self, ctx):
        """Synchronising & updating git submodules"""
        run(ctx, ["git", "submodule", "sync"])  # Sync submodules
        run(ctx, ["git", "submodule", "update", "--init"])  # Git pull submodules

    def is_fetched(self, ctx):
        """Check existence of source based on CMakeLists.txt, but again, should always exist!"""
        return os.path.exists(os.path.join(self.repo_path(ctx), "CMakeLists.txt"))

    def build(self, ctx):
        """Generate build files using cmake; then use those to build the instrumentation pass"""
        # Create CMake build files into infrastructure tree & build
        os.chdir(self.repo_path(ctx))
        run(
            ctx,
            [
                "cmake",
                "-G",
                "Unix Makefiles",
                "-B",
                self.build_dir,
                f"-DCMAKE_INSTALL_PREFIX={self.path(ctx, self.install_dir)}",
                f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={self.path(ctx, 'lib')}",
                f"-DCMAKE_ARCHIVE_OUTPUT_DIRECTORY={self.path(ctx, 'lib')}",
                "-DNO_DEBUG_LOG=ON" if self.settings.no_debug_build_log else "-DNO_DEBUG_LOG=OFF",
                "-DNO_WARN_LOG=ON" if self.settings.no_warning_build_log else "-DNO_WARN_LOG=OFF",
            ],
        )
        run(ctx, ["cmake", "--build", self.build_dir])

    def is_built(self, ctx):
        """Check if .so file exists"""
        return (
            False
            if self.settings.rebuild
            else os.path.exists(os.path.join(self.repo_path(ctx), self.build_dir))
        )

    def install(self, ctx):
        """Install into infrastructure install directory"""
        os.chdir(self.repo_path(ctx))
        run(ctx, ["cmake", "--install", self.build_dir])

    def is_installed(self, ctx):
        """Check if dynamic library file exist in the infrastructure's build tree"""
        return (
            False if self.settings.reinstall else os.path.exists(self.path(ctx, self.install_dir))
        )

    def configure(self, ctx):
        """Not standard in infrastructure. Should be called by the parent instance (here,
        VeriPatch) and inserts the pass library into the BUILD environment for the target."""
        ctx.log.debug("Configuring Instrumentation pass")

        # First configure the always-needed stuff for running InstrumentPass on a target
        ctx.ldflags += [
            f"-L{self.path(ctx, self.install_dir, 'lib')}",
            f"-Wl,--load-pass-plugin={self.path(ctx, self.install_dir, 'lib', self.pass_lib)}",
            f"-Wl,-mllvm=-load={self.path(ctx, self.install_dir, 'lib', self.pass_lib)}",
            f"-Wl,-mllvm=--redzone-size={self.settings.redzone_size}",
            f"-Wl,-mllvm=--target-config-file={self.settings.target_config_file}",
            f"-Wl,-mllvm=--target-analyis-config-file={self.settings.target_config_file}",
            f"-Wl,-mllvm=--num-random-targets={self.settings.num_random_targets}",
        ]

        # Now pass the command-line-dependent configuration onto the InstrumentPass
        if self.settings.clean_analysis:
            ctx.ldflags += ["-Wl,-mllvm=--clean-analysis"]
        if self.settings.targeted_sanitisation:
            ctx.ldflags += ["-Wl,-mllvm=--targeted-sanitisation"]
        if self.settings.no_check_loads:
            ctx.ldflags += ["-Wl,-mllvm=--no-check-loads"]
        if self.settings.no_check_stores:
            ctx.ldflags += ["-Wl,-mllvm=--no-check-stores"]
        if self.settings.no_check_libcalls:
            ctx.ldflags += ["-Wl,-mllvm=--no-check-libcalls"]
        if self.settings.no_check_stack:
            ctx.ldflags += ["-Wl,-mllvm=--no-check-stack"]
        if self.settings.no_check_globals:
            ctx.ldflags += ["-Wl,-mllvm=--no-check-globals"]
        if self.settings.strip_markers:
            ctx.ldflags += ["-Wl,-mllvm=--strip-markers"]
        if self.settings.simple_error_reports:
            ctx.ldflags += ["-Wl,-mllvm=--simple-error-reports"]
