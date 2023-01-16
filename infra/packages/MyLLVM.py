from .. import util
from .. import packages

import os


class MyLLVM(packages.LLVM):
    def __init__(self):
        super().__init__(
            "13.0.0",
            True,
            patches=["lld-new-pass-manager-option", "new-pass-manager-lto"],
            build_flags=[
                "-DLLVM_ENABLE_PROJECTS=clang;lld;compiler-rt",
                "-DLLVM_ENABLE_PLUGINS=ON",
                "-DLLVM_REQUIRES_RTTI=ON",
                "DLLVM_ENABLE_ZLIB=FORCE_ON",
            ],
        )

    def is_fetched(self, ctx):
        if os.path.exists("/home/ubuntu/llvm"):
            return True
        return super().is_fetched(ctx)

    def is_built(self, ctx):
        if os.path.exists("/home/ubuntu/llvm"):
            return True
        return super().is_built(ctx)

    def dependencies(self):
        if not os.path.exists("/home/ubuntu/llvm"):
            return super().dependencies()
        return []

    def is_installed(self, ctx):
        if os.path.exists("/home/ubuntu/llvm"):
            return True
        return super().is_installed(ctx)

    def fetch(self, ctx):
        def get(clonedir):
            major_version = int(self.version.split(".")[0])
            util.run(ctx, ["git", "clone", "git@github.com:llvm/llvm-project.git", clonedir])
            os.chdir(clonedir)
            util.run(ctx, ["git", "checkout", "release/%d.x" % major_version])

        get("src")

    def build(self, ctx):
        os.chdir("src")
        config_path = ctx.paths.root + "/patches/"
        for path in self.patches:
            if "/" not in path:
                path = "%s/%s.patch" % (config_path, path)
            ctx.log.debug(f"[Path] {path}")
            util.apply_patch(ctx, path, 1)
        os.chdir("..")

        os.makedirs("obj", exist_ok=True)
        os.chdir("obj")
        util.run(
            ctx,
            [
                "cmake",
                "-G",
                "Ninja",
                "-DCMAKE_INSTALL_PREFIX=" + self.path(ctx, "install"),
                "-DLLVM_BINUTILS_INCDIR=" + self.binutils.path(ctx, "install/include"),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DLLVM_ENABLE_ASSERTIONS=On",
                "-DLLVM_OPTIMIZED_TABLEGEN=On",
                "-DCMAKE_C_COMPILER=gcc",
                "-DCMAKE_CXX_COMPILER=g++",  # must be the same as used for compiling passes
                *self.build_flags,
                "../src/llvm",
            ],
        )
        util.run(ctx, "cmake --build . -- -j %d" % ctx.jobs)

    def install_env(self, ctx):
        llvmDir = os.getenv("LLVM_DIR", "").split(":")
        useStandardPath = False
        if len(llvmDir) == 0:
            useStandardPath = True
        if len(llvmDir[0]) == 0 or not (os.path.exists(llvmDir[0])):
            useStandardPath = True
        if not useStandardPath:
            llvmDir = llvmDir[0]
            prevbinpath = os.getenv("PATH", "").split(":")
            binpath = self.path(ctx, llvmDir + "/bin")
            if os.path.exists(binpath):
                ctx.runenv.setdefault("PATH", prevbinpath).insert(0, binpath)

            prevlibpath = os.getenv("LD_LIBRARY_PATH", "").split(":")
            libpath = self.path(ctx, llvmDir + "/lib")
            if os.path.exists(libpath):
                ctx.runenv.setdefault("LD_LIBRARY_PATH", prevlibpath).insert(0, libpath)
        else:
            super().install_env(ctx)
            llvmDir = self.path(ctx) + "/install"

        if self.compiler_rt:
            prevlibpath = os.getenv("LD_LIBRARY_PATH", "").split(":")
            compiler_rt_path = self.path(ctx, f"install/lib/clang/13.0.1/lib/linux/")
            if os.path.exists(compiler_rt_path):
                ctx.runenv.setdefault("LD_LIBRARY_PATH", prevlibpath).insert(0, compiler_rt_path)

        ctx.runenv.setdefault("LLVM_DIR", llvmDir)
        ctx.runenv.setdefault("CC", f"{llvmDir}/bin/clang")
        ctx.runenv.setdefault("CXX", f"{llvmDir}/bin/clang++")
        ctx.runenv.setdefault("LD", f"{llvmDir}/bin/lld")
        ctx.cc = f"{llvmDir}/bin/clang"
