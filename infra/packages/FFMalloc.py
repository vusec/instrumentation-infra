"""This file contains class definition for the infrastructure-compatible FFMalloc unit"""
import os
from ..package import Package
from ..util import run


class FFMalloc(Package):
    """Define package for FFMalloc fast-forward one-time allocator"""

    name = "FFMalloc"
    ffmalloc_lib = "libffmallocst.so"
    install_target = "install"

    def repo_path(self, ctx):
        """Retrieve the path to the git submodule path"""
        return os.path.join(ctx.paths.root, "VeriPatch", self.name)

    def lib_path(self, ctx):
        """Return full path to library file"""
        return os.path.join(self.repo_path(ctx), "lib", self.ffmalloc_lib)

    def ident(self):
        """Return FFMalloc name"""
        return self.name

    def fetch(self, ctx):
        """Update & synchronise the git submodules"""
        run(ctx, ["git", "submodule", "sync"])  # Sync submodules
        run(ctx, ["git", "submodule", "update", "--init"])  # Git pull submodules

    def is_fetched(self, ctx):
        """Check if git submodules are synchronised & pulled"""
        return os.path.exists(os.path.join(self.repo_path(ctx), "Makefile"))

    def build(self, ctx):
        """Use the provided build script"""
        os.chdir(self.repo_path(ctx))
        run(ctx, ["make", "sharedst"])

    def is_built(self, ctx):
        """Check if static library file (libSvf.a) exists in release-build directory."""
        return os.path.exists(self.lib_path(ctx))

    def install(self, ctx):
        """Copies built library files to infrastructure expected directories"""
        os.chdir(self.repo_path(ctx))
        run(
            ctx,
            [
                "make",
                "install_st",
                f"INSTALL_TARGET={self.path(ctx, self.install_target)}",
            ],
        )

    def is_installed(self, ctx):
        """Check if install directory exists"""
        return os.path.exists(self.path(ctx, self.install_target))

    def prepare_run(self, ctx):
        """Insert FFMalloc into LD_PRELOAD"""
        ld_preload = os.getenv("LD_PRELOAD", "").split(":")
        ctx.log.debug(f"Old LD_PRELOAD value: {ld_preload}")
        ctx.runenv.setdefault("LD_PRELOAD", ld_preload).insert(0, self.ffmalloc_lib)
