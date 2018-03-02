from . import llvm


def configure(ctx):
    llvm.configure(ctx)
    path = '%s/lib/libplugins.so' % ctx.paths.prefix
    ctx.cflags += ['-flto']
    ctx.ldflags += ['-flto', '-Wl,-plugin-opt=-load=%s' % path]

    if ctx.disable_opt:
        ctx.cflags += ['-g3', '-O0']
        ctx.ldflags += ['-g3', '-O0']
        add_lto_args(ctx, '-disable-opt')


def add_lto_args(ctx, *args):
    for arg in args:
        ctx.ldflags.append('-Wl,-plugin-opt=' + str(arg))


def add_stats_pass(ctx, pass_name, *args):
    if not pass_name.startswith('-'):
        pass_name = '-' + pass_name

    add_lto_args(ctx, pass_name, '-stats-only=' + pass_name, *args)
