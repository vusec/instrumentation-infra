"""This file holds the configuration & build commands for the UBStar sanitiser library"""

import os
import shutil
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
    rtlib = "librt.so"
    wraplib = "libwrap.so"
    rebuild = False
    reinstall = False
    clean_first = False

    def root_dir(self, ctx):
        """Retrieve the path to the git submodule path"""
        return os.path.join(ctx.paths.root, "external", self.name)

    def ident(self):
        return self.name

    def __init__(self, settings: Namespace):
        super().__init__()
        self.settings = settings

    def fetch(self, ctx):
        pass

    def is_fetched(self, ctx):
        return True

    def build(self, ctx):
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
        os.chdir(self.root_dir(ctx))
        run(ctx, ["make", "only-compile", f"CFLAGS={qjoin(cflags)}"])

    def is_built(self, ctx):
        return not self.settings.rebuild and not self.rebuild

    def install(self, ctx):
        os.chdir(self.root_dir(ctx))
        run(
            ctx,
            [
                "make",
                "install",
                f"INSTALL_TARGET={self.root_dir(ctx)}",
            ],
        )
        ctx.ldflags += [f"-L{os.path.join(self.root_dir(ctx), 'lib')}"]

    def install_env(self, ctx):
        prevlibpath = os.getenv("LD_LIBRARY_PATH", "").split(":")
        libpath = os.path.join(self.root_dir(ctx), "lib")
        if os.path.exists(libpath):
            ctx.runenv.setdefault("LD_LIBRARY_PATH", prevlibpath).insert(0, libpath)

    def is_installed(self, ctx):
        return not self.settings.reinstall and not self.reinstall

    def prepare_run(self, ctx):
        """Insert UBStar (and optionally FFMalloc) into LD_PRELOAD"""
        ld_preload = os.getenv("LD_PRELOAD", "").split(":")
        ctx.log.debug(f"Old LD_PRELOAD value: {ld_preload}")
        ctx.runenv.setdefault("LD_PRELOAD", ld_preload).insert(0, self.wraplib)

    def clean(self, ctx):
        """Call make clean & delete symlink"""
        os.chdir(self.root_dir(ctx))
        run(ctx, ["make", "clean-c"], allow_error=True)
        shutil.rmtree(os.path.join(self.root_dir(ctx), "lib"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.root_dir(ctx), "dist", "lib"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.root_dir(ctx), "dist", "obj"), ignore_errors=True)
        if os.path.exists(os.path.join(self.root_dir(ctx), "dist", "Makefile.basic")):
            os.remove(os.path.join(self.root_dir(ctx), "dist", "Makefile.basic"))

    def is_clean(self, ctx):
        """False if package path still exists"""
        return (
            not os.path.exists(os.path.join(self.root_dir(ctx), "lib"))
            and not os.path.exists(os.path.join(self.root_dir(ctx), "dist", "lib"))
            and not os.path.exists(os.path.join(self.root_dir(ctx), "dist", "obj"))
            and not os.path.exists(os.path.join(self.root_dir(ctx), "dist", "Makefile.basic"))
        )
