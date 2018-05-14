def add_lto_args(ctx, *args):
    for arg in args:
        ctx.ldflags.append('-Wl,-plugin-opt=' + str(arg))


def add_stats_pass(ctx, pass_name, *args):
    if not pass_name.startswith('-'):
        pass_name = '-' + pass_name

    add_lto_args(ctx, pass_name, '-stats-only=' + pass_name, *args)
