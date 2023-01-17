"""This file holds the configuration & build commands for the UBStar sanitiser library"""

import os
from ..package import Package
from ..util import run, Namespace, qjoin


class UBStar(Package):
    """Package container for the UBStar sanitiser. Encapsulates both the dynamic runtime library
    as well as the interceptor library for catching malloc() and friends through LD_PRELOAD.

    Args:
        infra (infra.Package): Parent class

    Returns:
        UBStar instance: Instance of UBStar package"""

    name = "UBStar"
    ubstar_dir = "UBStar"
    rtlib = "librt.so"
    wraplib = "libwrap.so"
    install_target = "install"

    def repo_path(self, ctx):
        """Retrieve the path to the git submodule path"""
        return os.path.join(ctx.paths.root, "external", self.name)

    def rtlib_path(self, ctx):
        """Get full path to runtime library"""
        return os.path.join(self.repo_path(ctx), "dist", "lib", self.rtlib)

    def wraplib_path(self, ctx):
        """Get full path to interceptor library"""
        return os.path.join(self.repo_path(ctx), "dist", "lib", self.wraplib)

    def ident(self):
        """Return package's name"""
        return self.name

    def __init__(self, ffmalloc, settings: Namespace):
        """Depends on FFMalloc and receives defaults settings dictionary"""
        super().__init__()
        self.ffmalloc = ffmalloc
        self.settings = settings

    def dependencies(self):
        """UBStar depends on FFMalloc"""
        yield self.ffmalloc

    def fetch(self, ctx):
        """Synchronising & updating git submodules"""
        run(ctx, ["git", "submodule", "sync"])  # Sync submodules
        run(ctx, ["git", "submodule", "update", "--init"])  # Git pull submodules

    def is_fetched(self, ctx):
        """Check if Makefile exists"""
        return os.path.exists(os.path.join(self.repo_path(ctx), "Makefile"))

    def build(self, ctx):
        """Compile the C source files (only compile, don't generate/verify)"""
        # Handle command line configuration
        cflags = [f"-DHEAP_REDZONE_SIZE={self.settings.redzone_size}"]  # Always pass
        if self.settings.use_ffmalloc:
            cflags += ["-DUSE_FFMALLOC"]  # Use ffmalloc instead of system alloc
        if self.settings.rt_debug_prints:
            cflags += ["-DDEBUG_PRINTS"]  # Enable verbose debug printing at runtime
        if self.settings.no_check_libcalls:
            cflags += ["-DSKIP_LIBCALLS"]  # Don't instrument library functions
        if self.settings.no_whitelist_args:
            cflags += ["-DSKIP_ARGS"]  # Don't whitelist command line args passed to target
        if self.settings.no_whitelist_libs:
            cflags += ["-DSKIP_WHITELIST"]  # Don't whitelist shared library static memory space

        # Go to root dir and call "make only-compile"
        os.chdir(self.repo_path(ctx))
        run(ctx, ["make", "only-compile", f"CFLAGS={qjoin(cflags)}"])

    def is_built(self, ctx):
        """Check if library .so files exist"""
        return (
            False
            if self.settings.rebuild
            else (os.path.exists(self.rtlib_path(ctx)) and os.path.exists(self.wraplib_path(ctx)))
        )

    def install(self, ctx):
        """Run make install to the infrastructure's target"""
        os.chdir(self.repo_path(ctx))
        run(ctx, ["make", "install", f"INSTALL_TARGET={self.path(ctx, self.install_target)}"])

    def is_installed(self, ctx):
        """Check if dynamic library files exist in the infrastructure's build tree"""
        return (
            False
            if self.settings.reinstall
            else os.path.exists(self.path(ctx, self.install_target))
        )

    def prepare_run(self, ctx):
        """Insert UBStar (and optionally FFMalloc) into LD_PRELOAD"""
        ld_preload = os.getenv("LD_PRELOAD", "").split(":")
        ctx.log.debug(f"Old LD_PRELOAD value: {ld_preload}")
        ctx.runenv.setdefault("LD_PRELOAD", ld_preload).insert(0, self.wraplib)

        if self.settings.use_ffmalloc:
            self.ffmalloc.prepare_run(ctx)
