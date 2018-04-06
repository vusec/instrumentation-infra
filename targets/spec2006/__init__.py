from ...target import Target
from ...packages import SPECPerl, Perlbrew#, PerlPackages


class SPEC2006(Target):
    name = 'spec2006'

    def add_build_args(self, parser):
        parser.add_argument('--spec2006-benchmarks',
                nargs='+', metavar='BENCHMARK',
                help='which SPEC2006 benchmarks to build')

        self.perl = SPECPerl()
        self.perlbrew = Perlbrew(self.perl)

    def dependencies(self):
        yield self.perlbrew
        #yield self.perlpackages

    def is_fetched(self, ctx):
        raise NotImplementedError

    def fetch(self, ctx):
        raise NotImplementedError

    def build(self, ctx, instance):
        raise NotImplementedError

    def link(self, ctx, instance):
        pass

    def binary_paths(self, ctx, instance):
        raise NotImplementedError
