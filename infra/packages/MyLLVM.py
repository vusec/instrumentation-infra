"""Class for holding LLVM object"""

import os

from ..packages import LLVM


class MyLLVM(LLVM):
    """Class for holding LLVM object"""

    def __init__(self, force_new_llvm: bool = False):
        self.llvm_dir = os.getenv("LLVM_DIR")
        self.sys_llvm = not force_new_llvm and self.llvm_dir and os.path.exists(self.llvm_dir)

        # Initialse and locally install LLVM if no system LLVM exists/can be found
        if self.sys_llvm:
            self.name = "SystemLLVM"
            self.version = "13.0.1"
            self.compiler_rt = False
            self.lld = True

        else:
            self.name = "InfraLLVM"
            super().__init__(
                "13.0.1",
                True,
                patches=["lld-new-pass-manager-option", "new-pass-manager-lto"],
                build_flags=[
                    "-DLLVM_ENABLE_PROJECTS=clang;lld;compiler-rt",
                    "-DLLVM_ENABLE_PLUGINS=ON",
                    "-DLLVM_REQUIRES_RTTI=ON",
                    "DLLVM_ENABLE_ZLIB=FORCE_ON",
                ],
            )

    def ident(self) -> str:
        return self.name

    def path(self, ctx, *args):
        return os.path.join(self.llvm_dir, *args) if self.sys_llvm else super().path(ctx, args)

    def goto_rootdir(self, ctx):
        if self.sys_llvm:
            os.chdir(self.path(ctx))
        else:
            super().goto_rootdir(ctx)

    def dependencies(self):
        return [] if self.sys_llvm else super().dependencies()

    def is_fetched(self, ctx):
        return self.sys_llvm or super().is_fetched(ctx)

    def is_built(self, ctx):
        return self.sys_llvm or super().is_built(ctx)

    def is_installed(self, ctx):
        return self.sys_llvm or super().is_installed(ctx)

    def is_clean(self, ctx):
        return self.sys_llvm or super().is_clean(ctx)

    def fetch(self, ctx):
        if self.sys_llvm:
            ctx.log.info("Using system LLVM (from $LLVM_DIR); skipping fetch()")
        else:
            ctx.log.info("Fetching new LLVM package")
            super().fetch(ctx)

    def build(self, ctx):
        if self.sys_llvm:
            ctx.log.info("Using system LLVM (from $LLVM_DIR); skipping build()")
        else:
            ctx.log.info("Building new LLVM package")
            super().build(ctx)

    def install(self, ctx):
        if self.sys_llvm:
            ctx.log.info("Using system LLVM (from $LLVM_DIR); skipping install()")
        else:
            ctx.log.info("Installing new LLVM package")
            super().install(ctx)

    def configure(self, ctx):
        if self.sys_llvm:
            ctx.log.info("Configuring system LLVM (from $LLVM_DIR) into CTX")
            ctx.log.debug(f"Current CTX: {ctx}")

            ctx.cc = self.path(ctx, "bin", "clang")
            ctx.cxx = self.path(ctx, "bin", "clang++")
            ctx.ld = self.path(ctx, "bin", "ld.lld")
            ctx.ar = self.path(ctx, "bin", "llvm-ar")
            ctx.nm = self.path(ctx, "bin", "llvm-nm")
            ctx.ranlib = self.path(ctx, "bin", "llvm-ranlib")

            ctx.log.info("System LLVM configuration completed")
            ctx.log.debug(f"System LLVM configured: new CTX: {ctx}")

        else:
            ctx.log.info("Configuring new LLVM package")
            super().configure(ctx)

    def install_env(self, ctx):
        if self.sys_llvm:
            ctx.log.info(f"Installing system LLVM ({self.llvm_dir}) into running env")
            ctx.log.debug(f"Current running env: {ctx.runenv}")

            # Get current user's environment variables for $PATH and $LD_LIBRARY_PATH
            bin_path = os.getenv("PATH", "").split(":")
            lib_path = os.getenv("LD_LIBRARY_PATH", "").split(":")
            ctx.log.debug(f"Environment $PATH: {bin_path}")
            ctx.log.debug(f"Environment $LD_LIBRARY_PATH: {lib_path}")

            # Set LLVM libraries to insert into environment
            llvm_bin = self.path(ctx, "bin")
            llvm_lib = self.path(ctx, "lib")
            ctx.log.debug(f"LLVM_BIN: {llvm_bin}\nLLVM_LIB: {llvm_lib}")

            if not os.path.exists(llvm_bin) or not os.path.exists(llvm_lib):
                raise Exception("Using system LLVM no $LLVM_DIR/bin or $LLVM_DIR/lib")

            # Set variables in running environment
            ctx.runenv.setdefault("LLVM_DIR", self.llvm_dir)
            ctx.runenv.setdefault("LLVM_HOME", os.getenv("LLVM_HOME"))
            ctx.runenv.setdefault("LLVM_OBJ", os.getenv("LLVM_OBJ"))
            ctx.runenv.setdefault("LLVM_SRC", os.getenv("LLVM_SRC"))
            ctx.runenv.setdefault("PATH", bin_path).insert(0, llvm_bin)
            ctx.runenv.setdefault("LD_LIBRARY_PATH", lib_path).insert(0, llvm_lib)
            ctx.log.debug(f"System LLVM installed: new runenv: {ctx.runenv}")

        else:
            ctx.log.info("Installing new LLVM package into environment")
            super().install_env(ctx)

    def clean(self, ctx):
        if self.sys_llvm:
            ctx.log.info("Using system LLVM (from $LLVM_DIR) -- skipping clean()")
        else:
            ctx.log.info("Cleaning new LLVM package")
            super().clean(ctx)
