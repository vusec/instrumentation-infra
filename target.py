import os


class Target:
    def add_build_args(self, parser):
        pass

    def dependencies(self):
        yield from []

    def fetch(self, ctx, instances):
        return NotImplemented

    def build(self, ctx, instance):
        raise NotImplementedError

    def link(self, ctx, instance):
        return NotImplemented

    def binary_paths(self, ctx, instance):
        raise NotImplementedError

    def run_hooks_post_build(self, ctx, instance):
        for binary in self.binary_paths(ctx, instance):
            for hook in ctx.hooks.post_build:
                os.chdir(os.path.dirname(binary))
                hook(ctx, binary)
