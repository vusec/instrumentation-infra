class Instance:
    def add_build_args(self, parser):
        pass

    def dependencies(self):
        yield from []

    def configure(self, ctx):
        raise NotImplementedError
