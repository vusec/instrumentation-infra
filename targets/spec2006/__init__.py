import os
import shutil
from contextlib import redirect_stdout
from ...util import run, apply_patch, qjoin, FatalError
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
                nargs='+', metavar='BENCHMARK', default=['bzip2'], # FIXME
                help='which SPEC2006 benchmarks to build')

    def is_fetched(self, ctx):
        return os.path.exists('shrc')

    def fetch(self, ctx):
        if self.giturl:
            ctx.log.debug('cloning SPEC2006 repo')
            run(ctx, ['git', 'clone', '--depth', 1, self.giturl, 'src'])
            os.chdir('src')
        else:
            os.chdir(self.specdir)

        install_path = self.path(ctx, 'install')
        ctx.log.debug('installing SPEC2006 into ' + install_path)
        run(ctx, ['./install.sh', '-f', '-d', install_path],
            env={'PERL_TEST_NUMCONVERTS': 1})

        if self.giturl:
            ctx.log.debug('removing cloned SPEC2006 repo to save disk space')
            shutil.rmtree(self.path(ctx, 'src'))

    def build(self, ctx, instance):
        # apply any pending patches (doing this at build time allows adding
        # patches during instance development)
        os.chdir('install')
        config_path = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_path, path)
            apply_patch(ctx, path, 1)
        os.chdir('..')

        config = self.make_spec_config(ctx, instance)
        import sys
        self.runspec(ctx, '--config=' + config, '--action=build',
                     *ctx.args.spec2006_benchmarks, stdout=sys.stdout)

    def runspec(self, ctx, *args, **kwargs):
        config_path = os.path.dirname(os.path.abspath(__file__))
        run(ctx, ['bash', '-c',
            'cd %s/install;'
            'source shrc;'
            'source "%s/scripts/kill-tree-on-interrupt.inc";'
            'killwrap_tree runspec %s' %
            (self.path(ctx), config_path, qjoin(args))], **kwargs)

    def make_spec_config(self, ctx, instance):
        config_name = 'infra-' + instance.name
        config_path = self.path(ctx, 'install/config/%s.cfg' % config_name)
        ctx.log.debug('writing SPEC2006 config to ' + config_path)

        with open(config_path, 'w') as f:
            with redirect_stdout(f):
                print('tune        = base')
                print('ext         = ' + config_name)
                print('reportable  = no')
                print('teeout      = yes')
                print('teerunout   = no')
                print('makeflags   = -j%d' % ctx.jobs)
                print('strict_rundir_verify = no')
                print('')
                print('default=default=default=default:')

                # see https://www.spec.org/cpu2006/Docs/makevars.html#nofbno1
                # for flags ordering
                cflags = qjoin(ctx.cflags)
                ldflags = qjoin(ctx.ldflags)
                print('CC          = %s %s' % (ctx.cc, cflags))
                print('CXX         = %s %s' % (ctx.cxx, cflags))
                print('FC          = `which false`')
                print('CLD         = %s %s' % (ctx.cc, ldflags))
                print('CXXLD       = %s %s' % (ctx.cxx, ldflags))

                # post-build hooks call back into the setup script
                print('')
                print('build_post_bench = %s exec-hook post-build %s ${commandexe}' %
                      (ctx.paths.setup, instance.name))
                print('')

                if 'target_run_wrapper' in ctx:
                    print('')
                    print('monitor_wrapper = %s \$command' % ctx.target_run_wrapper)

                # configure benchmarks for 64-bit Linux (hardcoded for now)
                print('')
                print('default=base=default=default:')
                print('PORTABILITY    = -DSPEC_CPU_LP64')
                print('')
                print('400.perlbench=default=default=default:')
                print('CPORTABILITY   = -DSPEC_CPU_LINUX_X64')
                print('')
                print('462.libquantum=default=default=default:')
                print('CPORTABILITY   = -DSPEC_CPU_LINUX')
                print('')
                print('483.xalancbmk=default=default=default:')
                print('CXXPORTABILITY = -DSPEC_CPU_LINUX')
                print('')
                print('481.wrf=default=default=default:')
                print('wrf_data_header_size = 8')
                print('CPORTABILITY   = -DSPEC_CPU_CASE_FLAG -DSPEC_CPU_LINUX')

        return config_name

    def link(self, ctx, instance):
        pass

    # override post-build hook runner rather than defining `binary_paths` since
    # we add hooks to the generated SPEC config file and call them through the
    # exec-hook setup command instead
    def run_hooks_post_build(self, ctx, instance):
        pass
