import os
import shutil
import logging
import argparse
import datetime
import getpass
import re
from contextlib import redirect_stdout
from ...util import run, apply_patch, qjoin, FatalError
from ...target import Target
from ...packages import Bash, Nothp
from .benchmark_sets import benchmark_sets


class SPEC2006(Target):
    name = 'spec2006'

    def __init__(self,
            source=None,      # where to install spec from
            source_type=None, # see below
            patches=[],       # patches to apply after installing
            nothp=True,       # run without transparent huge pages?
            force_cpu=0       # bind runspec to this cpu core (-1 to disable)
            ):

        # mounted    mounted/extracted ISO
        # installed  installed SPEC from other project
        # tarfile    compressed tarfile to extract
        # git        git repo containing extracted ISO
        if source_type not in ('mounted', 'installed', 'tarfile', 'git'):
            raise FatalError('invalid source type "%s"' % source_type)

        if source_type == 'installed':
            shrc = self.source + '/shrc'
            if not os.path.exists(shrc):
                shrc = os.path.abspath(shrc)
                raise FatalError(shrc + ' is not a valid SPEC installation')

        self.source = source
        self.source_type = source_type
        self.patches = patches
        self.nothp = nothp
        self.force_cpu = force_cpu

    def add_build_args(self, parser, desc='build'):
        parser.add_argument('--spec2006-benchmarks',
                nargs='+', metavar='BENCHMARK', default=['all_c', 'all_cpp'],
                choices=list(self.benchmarks.keys()),
                help='which SPEC-CPU2006 benchmarks to build')

    def add_run_args(self, parser):
        parser.add_argument('--benchmarks', '--spec2006-benchmarks',
                dest='spec2006_benchmarks',
                nargs='+', metavar='BENCHMARK', default=['all_c', 'all_cpp'],
                choices=list(self.benchmarks.keys()),
                help='which benchmarks to run')
        parser.add_argument('--test', action='store_true',
                help='run a single iteration of the test workload')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--measuremem', action='store_true',
                help='measure memory usage (single run, does not support '
                     'runspec arguments)')
        group.add_argument('--runspec-args',
                nargs=argparse.REMAINDER, default=[],
                help='additional arguments for runspec')

    def dependencies(self):
        yield Bash('4.3')
        if self.nothp:
            yield Nothp()

    def is_fetched(self, ctx):
        return self.source_type == 'installed' or os.path.exists('install/shrc')

    def fetch(self, ctx):
        if self.source_type == 'mounted':
            os.chdir(self.source)
        elif self.source_type == 'tarfile':
            ctx.log.debug('extracting SPEC-CPU2006 source files')
            os.makedirs('src', exist_ok=True)
            os.chdir('src')
            run(ctx, ['tar', 'xf', self.source])
        elif self.source_type == 'git':
            ctx.log.debug('cloning SPEC-CPU2006 repo')
            run(ctx, ['git', 'clone', '--depth', 1, self.source, 'src'])
            os.chdir('src')
        else:
            assert False

        install_path = self.path(ctx, 'install')
        ctx.log.debug('installing SPEC-CPU2006 into ' + install_path)
        run(ctx, ['./install.sh', '-f', '-d', install_path],
            env={'PERL_TEST_NUMCONVERTS': 1})

        if self.source_type in ('tarfile', 'git'):
            ctx.log.debug('removing SPEC-CPU2006 source files to save disk space')
            shutil.rmtree(self.path(ctx, 'src'))

    def apply_patches(self, ctx):
        os.chdir(self.path(ctx, 'install'))
        config_root = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_root, path)
            if apply_patch(ctx, path, 1) and self.source_type == 'installed':
                ctx.log.warning('applied patch %s to external SPEC-CPU2006 '
                                'directory' % path)

    def build_parallel(self, ctx, instance, prun):
        self.build(ctx, instance, prun=prun)

    def build(self, ctx, instance, prun=None):
        # apply any pending patches (doing this at build time allows adding
        # patches during instance development, and is needed to apply patches
        # when self.source_type == 'installed')
        self.apply_patches(ctx)

        os.chdir(self.path(ctx))
        config = self.make_spec_config(ctx, instance)
        print_output = ctx.loglevel == logging.DEBUG

        for bench in self.get_benchmarks(ctx, instance):
            cmd = 'killwrap_tree runspec --config=%s --action=build %s' % \
                  (config, bench)
            if prun:
                jobid = 'build-%s-%s' % (instance.name, bench)
                outdir = os.path.join(ctx.paths.prun_results, 'build',
                                      self.name, instance.name)
                os.makedirs(outdir, exist_ok=True)
                outfile = os.path.join(outdir, bench)
                self.run_bash(ctx, cmd, prun, jobid=jobid,
                              outfile=outfile, nnodes=1)
            else:
                ctx.log.info('building %s-%s %s' %
                             (self.name, instance.name, bench))
                self.run_bash(ctx, cmd, teeout=print_output)

    def run_parallel(self, ctx, instance, prun):
        self.run(ctx, instance, prun=prun)

    def run(self, ctx, instance, prun=None):
        config = 'infra-' + instance.name
        config_root = os.path.dirname(os.path.abspath(__file__))

        if not os.path.exists(self.path(ctx, 'install/config/%s.cfg' % config)):
            raise FatalError('%s-%s has not been built yet!' %
                             (self.name, instance.name))

        runargs = []

        if ctx.args.test:
            #if ctx.args.iterations != 1:
            #    ctx.log.warning('ignoring --iterations=%d because --test '
            #                    'is specified' % ctx.args.iterations)
            #    ctx.args.iterations = 1
            #    if prun:
            #        prun.iterations = 1

            runargs += ['--size', 'test']

        # the prun scheduler will pass --iterations as -np to prun, so only run
        # one iteration in runspec
        runargs += ['--iterations', '1' if prun else '%d' % ctx.args.iterations]

        # set output root to local disk when using prun to avoid noise due to
        # network lag when writing output files
        specdir = self.path(ctx, 'install')
        if prun:
            output_root = '/local/%s/cpu2006-output-root' % getpass.getuser()
            runargs += ['--define', 'output_root=' + output_root]
        else:
            output_root = specdir

        if 'target_run_wrapper' in ctx:
            runargs += ['--define', 'run_wrapper=' + ctx.target_run_wrapper]

        runargs += ctx.args.runspec_args
        runargs = qjoin(runargs)

        wrapper =  'killwrap_tree'
        if self.nothp:
            wrapper += ' nothp'
        if self.force_cpu >= 0:
            wrapper += ' taskset -c %d' % self.force_cpu

        if ctx.args.measuremem:
            cmd = 'runspec --config={config} --action=setup {runargs} %s\n' \
                  '{wrapper} {config_root}/measuremem.py {output_root} {config} {{bench}}'
        else:
            cmd = '{wrapper} runspec --config={config} --nobuild {runargs} {{bench}}'

        cmd = cmd.format(**locals())

        benchmarks = self.get_benchmarks(ctx, instance)

        if prun:
            timestamp = datetime.datetime.now().strftime('run-%Y-%m-%d.%H:%M:%S')

            # prepare output dir on local disk before running,
            # and move output files to network disk after completion
            cmd = unindent('''
            rm -rf "{output_root}"
            mkdir -p "{output_root}"
            mkdir -p "{specdir}/result"
            ln -s "{specdir}/result" "{output_root}"
            if [ -d "{specdir}/benchspec/CPU2006/{{bench}}/exe" ]; then
                mkdir -p "{output_root}/benchspec/CPU2006/{{bench}}"
                cp -r "{specdir}/benchspec/CPU2006/{{bench}}/exe" \\
                    "{output_root}/benchspec/CPU2006/{{bench}}"
            fi
            {{{{ {cmd}; }}}} | \\
                sed "s,{output_root}/result/,{specdir}/result/,g"
            rm -rf "{output_root}"
            ''').format(**locals())

            for bench in benchmarks:
                jobid = 'run-%s-%s' % (instance.name, bench)
                outdir = os.path.join(ctx.paths.prun_results, timestamp,
                                      self.name, instance.name)
                os.makedirs(outdir, exist_ok=True)
                outfile = os.path.join(outdir, bench)
                self.run_bash(ctx, cmd.format(bench=bench), prun,
                              jobid=jobid, outfile=outfile,
                              nnodes=ctx.args.iterations)
        else:
            self.run_bash(ctx, cmd.format(bench=qjoin(benchmarks)), teeout=True)

    def run_bash(self, ctx, commands, prun=None, **kwargs):
        config_root = os.path.dirname(os.path.abspath(__file__))
        cmd = [
            'bash', '-c',
            '\n' + unindent('''
            cd %s/install
            source shrc
            source "%s/scripts/kill-tree-on-interrupt.inc"
            %s
            ''' % (self.path(ctx), config_root, commands))
        ]
        if prun:
            return prun.run(ctx, cmd, **kwargs)
        return run(ctx, cmd, **kwargs)

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

                # allow different output root to be set using
                # --define output_root=...
                print('%ifdef %{output_root}')
                print('  output_root = %{output_root}')
                print('%endif')

                print('')
                print('default=default=default=default:')

                # see https://www.spec.org/cpu2006/Docs/makevars.html#nofbno1
                # for flags ordering
                cflags = qjoin(ctx.cflags)
                cxxflags = qjoin(ctx.cxxflags)
                ldflags = qjoin(ctx.ldflags)
                fortranc = shutil.which('gfortran') or shutil.which('false')
                print('CC          = %s %s' % (ctx.cc, cflags))
                print('CXX         = %s %s' % (ctx.cxx, cxxflags))
                print('FC          = %s' % fortranc)
                print('CLD         = %s %s' % (ctx.cc, ldflags))
                print('CXXLD       = %s %s' % (ctx.cxx, ldflags))
                print('COPTIMIZE   = -std=gnu89')
                print('CXXOPTIMIZE = -std=c++98') # fix __float128 in old clang

                # post-build hooks call back into the setup script
                if ctx.hooks.post_build:
                    print('')
                    print('build_post_bench = %s exec-hook post-build %s '
                          '`echo ${commandexe} '
                          '| sed "s/_\\[a-z0-9\\]\\\\+\\\\.%s\\\\\\$//"`' %
                          (ctx.paths.setup, instance.name, config_name))
                    print('')

                # allow run wrapper to be set using --define run_wrapper=...
                print('%ifdef %{run_wrapper}')
                print('  monitor_wrapper = %{run_wrapper} $command')
                print('%endif')

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

    def get_benchmarks(self, ctx, instance):
        benchmarks = set()
        for bset in ctx.args.spec2006_benchmarks:
            for bench in self.benchmarks[bset]:
                if not hasattr(instance, 'exclude_spec2006_benchmark') or \
                        not instance.exclude_spec2006_benchmark(bench):
                    benchmarks.add(bench)
        return sorted(benchmarks)

    # define benchmark sets, generated using scripts/parse-benchmarks-sets.py
    benchmarks = benchmark_sets


def unindent(cmd):
    stripped = re.sub(r'^\n|\n *$', '', cmd)
    indent = re.search('^ +', stripped, re.M)
    if indent:
        return re.sub(r'^' + indent.group(0), '', stripped, 0, re.M)
    return stripped
