import os
import shutil
from ...util import run, apply_patch, FatalError
from ...target import Target


class SPEC2006(Target):
    name = 'spec2006'

    def __init__(self, specdir=None, giturl=None, patches=[]):
        if not specdir and not giturl:
            raise FatalError('should specify one of specdir or giturl')

        if specdir and giturl:
            raise FatalError('cannot specify specdir AND giturl')

        self.specdir = specdir
        self.giturl = giturl
        self.patches = patches

    def add_build_args(self, parser):
        parser.add_argument('--spec2006-benchmarks',
                nargs='+', metavar='BENCHMARK',
                help='which SPEC2006 benchmarks to build')

    def is_fetched(self, ctx):
        return os.path.exists('version.txt')

    def fetch(self, ctx):
        if self.giturl:
            ctx.log.debug('cloning SPEC2006 repo')
            run(ctx, ['git', 'clone', '--depth', 1, self.giturl, 'cloned-repo'])
            os.chdir('cloned-repo')
        else:
            os.chdir(self.specdir)

        install_path = self.path(ctx)
        ctx.log.debug('installing SPEC2006 into ' + install_path)
        run(ctx, ['./install.sh', '-f', '-d', install_path],
            env={'PERL_TEST_NUMCONVERTS': 1})

        if self.giturl:
            os.chdir('..')
            ctx.log.debug('removing cloned SPEC2006 repo to save disk space')
            shutil.rmtree('cloned-repo')

    def build(self, ctx, instance):
        # apply any pending patches (doing this at build time allows adding
        # patches during instance development)
        config_path = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_path, path)
            apply_patch(ctx, path, 1)

        # TODO: build instance

    def link(self, ctx, instance):
        pass

    def binary_paths(self, ctx, instance):
        raise NotImplementedError
