"""This file contains class definition for the infrastructure-compatible FFMalloc unit"""
import os
import shutil
from ..package import Package
from ..util import run


class FFMalloc(Package):
    """Define package for FFMalloc fast-forward one-time allocator"""

    name = "FFMalloc"
    ffmalloc_lib = "libffmallocst.so"
    rebuild = False
    reinstall = False
    clean_first = False

    def root_dir(self, ctx):
        """Retrieve the path to the git submodule path"""
        return os.path.join(ctx.paths.root, "external", self.name)

    def ident(self):
        return self.name

    def fetch(self, ctx):
        pass

    def is_fetched(self, ctx):
        return True

    def build(self, ctx):
        """Use the provided build makefile"""
        # If cleaning before building remove all build files and such
        if self.clean_first:
            self.clean(ctx)

        os.chdir(self.root_dir(ctx))
        run(ctx, ["make", "sharedst"])

    def is_built(self, ctx):
        return not self.rebuild

    def install(self, ctx):
        os.chdir(self.root_dir(ctx))
        run(ctx, ["make", "install_st", f"INSTALL_TARGET={self.root_dir(ctx)}"])
        ctx.ldflags += [f"-L{os.path.join(self.root_dir(ctx), 'lib')}"]

    def install_env(self, ctx):
        prevlibpath = os.getenv("LD_LIBRARY_PATH", "").split(":")
        libpath = os.path.join(self.root_dir(ctx), "lib")
        if os.path.exists(libpath):
            ctx.runenv.setdefault("LD_LIBRARY_PATH", prevlibpath).insert(0, libpath)

    def is_installed(self, ctx):
        return not self.reinstall

    def prepare_run(self, ctx):
        """Insert FFMalloc into LD_PRELOAD"""
        ld_preload = os.getenv("LD_PRELOAD", "").split(":")
        ctx.log.debug(f"Old LD_PRELOAD value: {ld_preload}")
        ctx.runenv.setdefault("LD_PRELOAD", ld_preload).insert(0, self.ffmalloc_lib)

    def clean(self, ctx):
        os.chdir(self.root_dir(ctx))
        run(ctx, ["make", "clean"], allow_error=True)
        shutil.rmtree(os.path.join(self.root_dir(ctx), "lib"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.root_dir(ctx), "obj"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.root_dir(ctx), self.ffmalloc_lib), ignore_errors=True)

    def is_clean(self, ctx):
        return not self.clean_first or (
            not os.path.exists(os.path.join(self.root_dir(ctx), "lib"))
            and not os.path.exists(os.path.join(self.root_dir(ctx), "obj"))
            and not os.path.exists(os.path.join(self.root_dir(ctx), self.ffmalloc_lib))
        )
