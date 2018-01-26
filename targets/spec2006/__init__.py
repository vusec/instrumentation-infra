from ...target import Target
#from ...packages import Perl


class SPEC2006(Target):
    name = 'spec2006'
    perl_version = '5.8.8'

    def add_build_args(self, parser):
        parser.add_argument('--spec2006-benchmarks',
                nargs='+', metavar='BENCHMARK',
                help='which SPEC2006 benchmarks to build')

    def dependencies(self):
        yield from []
        #yield Perl(self.perl_version)

    def is_fetched(self, ctx):
        raise NotImplementedError

    def fetch(self, ctx):
        raise NotImplementedError

    def build(self, ctx, instance):
        raise NotImplementedError

    def link(self, ctx, instance):
        raise NotImplementedError

    def binary_paths(self, ctx, instance):
        raise NotImplementedError
