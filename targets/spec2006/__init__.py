from ...target import Target


class SPEC2006(Target):
    name = 'spec2006'
    perl_version = '5.8.8'

    def add_build_args(self, parser):
        pass

    def dependencies(self):
        yield from []
        #yield Perl(self.perl_version)

    def fetch(self, ctx, instance):
        raise NotImplementedError

    def build(self, ctx, instance):
        raise NotImplementedError

    def binary_paths(self, ctx, instance):
        raise NotImplementedError

