"""This file holds the configuration & build commands for the TargetAnalysis library"""

import os
from ..package import Package
from ..util import run


class TargetAnalysis(Package):
    """Package container for the TargetAnalysis."""

    name = "TargetAnalysis"
    utils_lib = "libTargetAnalysis.so"
    rebuild = False
    reinstall = False

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
        os.chdir(self.root_dir(ctx))
        run(
            ctx,
            [
                "cmake",
                "-G",
                "Unix Makefiles",
                "-B",
                "build",
                f"-DCMAKE_INSTALL_PREFIX={self.root_dir(ctx)}",
            ],
        )
        run(ctx, ["cmake", "--build", "build", "--", f"-j{ctx.jobs}"])

    def is_built(self, ctx):
        return not self.rebuild

    def install(self, ctx):
        os.chdir(self.root_dir(ctx))
        run(ctx, ["cmake", "--install", "build"])
        ctx.ldflags += [f"-L{os.path.join(self.root_dir(ctx), 'lib')}"]

    def install_env(self, ctx):
        prevlibpath = os.getenv("LD_LIBRARY_PATH", "").split(":")
        libpath = os.path.join(self.root_dir(ctx), "lib")
        if os.path.exists(libpath):
            ctx.runenv.setdefault("LD_LIBRARY_PATH", prevlibpath).insert(0, libpath)

    def is_installed(self, ctx):
        return not self.reinstall

    def clean(self, ctx):
        os.chdir(self.root_dir(ctx))
        run(ctx, ["cmake ", "--build", "build", "--target", "clean"], allow_error=True)

    def is_clean(self, ctx):
        """False if package path still exists"""
        return False
