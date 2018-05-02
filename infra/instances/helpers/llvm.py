def configure(ctx, version):
    ctx.cc = 'clang'
    ctx.cxx = 'clang++'
    ctx.ar = 'llvm-ar'
    ctx.nm = 'llvm-nm'
    ctx.ranlib = 'llvm-ranlib'
    ctx.cflags = []
    ctx.ldflags = []
