import sys
import os
import shutil
import logging
import argparse
import getpass
import re
import statistics
import csv
from contextlib import redirect_stdout
from typing import List
from ...util import FatalError, run, apply_patch, qjoin, geomean
from ...target import Target
from ...packages import Bash, Nothp
from ...parallel import PrunPool
from ...report import BenchmarkRunner, parse_results, log_result
from .benchmark_sets import benchmark_sets


class SPEC2006(Target):
    """
    The `SPEC-CPU2006 <https://www.spec.org/cpu2006/>`_ benchmarking suite.

    Since SPEC may not be redistributed, you need to provide your own copy in
    ``source``. We support the following types for ``source_type``:

    - ``mounted``:    mounted/extracted ISO directory
    - ``installed``:  pre-installed SPEC directory in another project
    - ``tarfile``:    compressed tarfile with ISO contents
    - ``git``:        git repo containing extracted ISO

    The ``--spec2006-benchmarks`` command-line argument is added for the
    :ref:`build <usage-build>` and :ref:`run <usage-run>` commands. It supports
    full individual benchmark names such as '400.perlbench', and the following
    benchmark sets defined by SPEC:

    - ``all_c``: C benchmarks
    - ``all_cpp``: C++ benchmarks
    - ``all_fortran``: Fortran benchmarks
    - ``all_mixed``: C/Fortran benchmarks
    - ``int``: `integer benchmarks <https://spec.org/cpu2006/CINT2006/>`_
    - ``fp``: `floating-point benchmarks <https://spec.org/cpu2006/CFP2006/>`_

    Mutiple sets and individual benchmarks can be specified, duplicates are
    removed and the list is sorted automatically. When unspecified, the
    benchmarks default to ``all_c all_cpp``.

    The following options are added only for the :ref:`run <usage-run>`
    command:

    - ``--benchmarks``: alias for ``--spec2006-benchmarks``
    - ``--test``: run the test workload
    - ``--measuremem``: use an alternative runscript that bypasses ``runspec``
      to measure memory usage
    - ``--runspec-args``: passed directly to ``runspec``

    Parallel builds and runs using the ``--parallel`` option are supported.
    Command output will end up in the ``results/`` directory in that case.
    Note that even though the parallel job may finish successfully, **you still
    need to check the output for errors manually**. Here is a useful oneliner
    for that::

        grep -rh 'Success:\|Error:' results/run-<timestamp>

    The ``--iterations`` option of the :ref:`run <usage-run>` command is
    translated into the number of nodes per job when ``--parallel`` is
    specified, and to ``--runspec-args -n <iterations>`` otherwise.

    You may specify a list of patches to apply before building. These may be
    paths to .patch files that will be applied with ``patch -p1``, or choices
    from the following built-in patches:

    - **dealII-stddef** Fixes error in dealII compilation on recent compilers
      when ``ptrdiff_t`` is used without including ``stddef.h``. (you basically
      always want this)

    - **asan** applies the AddressSanitizer patch, needed to make
      ``-fsanitize=address`` work on LLVM.

    - **gcc-init-ptr** zero-initializes a pointer on the stack so that type
      analysis at LTO time does not get confused.

    - **omnetpp-invalid-ptrcheck** fixes a code copy-paste bug in an edge case
      of a switch statement, where a pointer from a union is used while it is
      initialized as an int.

    TODO: document output of ``report`` command

    :name: spec2006
    :param source_type: see above
    :param source: where to install spec from
    :param patches: patches to apply after installing
    :param nothp: run without transparent huge pages (they tend to introduce
                  noise in performance measurements), implies :class:`Nothp`
                  dependency if ``True``
    :param force_cpu: bind runspec to this cpu core (-1 to disable)
    """

    name = 'spec2006'

    def __init__(self, source_type: str,
                       source: str,
                       patches: List[str] = [],
                       nothp: bool = True,
                       force_cpu: int = 0):
        if source_type not in ('mounted', 'installed', 'tarfile', 'git'):
            raise FatalError('invalid source type "%s"' % source_type)

        if source_type == 'installed':
            shrc = source + '/shrc'
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

    def add_report_args(self, parser):
        parser.add_argument('--baseline', metavar='INSTANCE',
                help='baseline instance for overheads')
        parser.add_argument('--only-overhead', action='store_true',
                help='only show overhead numbers')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--csv',
                action='store_const', const='csv', dest='nonhuman',
                help='print table in CSV format')
        group.add_argument('--tsv',
                action='store_const', const='tsv', dest='nonhuman',
                help='print table as tab-separated values')

    def dependencies(self):
        yield Bash('4.3')
        if self.nothp:
            yield Nothp()
        yield from BenchmarkRunner.dependencies()

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

        install_path = self._install_path(ctx)
        ctx.log.debug('installing SPEC-CPU2006 into ' + install_path)
        run(ctx, ['./install.sh', '-f', '-d', install_path],
            env={'PERL_TEST_NUMCONVERTS': 1})

        if self.source_type in ('tarfile', 'git'):
            ctx.log.debug('removing SPEC-CPU2006 source files to save disk space')
            shutil.rmtree(self.path(ctx, 'src'))

    def _install_path(self, ctx, *args):
        if self.source_type == 'installed':
            return os.path.join(self.source, *args)
        return self.path(ctx, 'install', *args)

    def _apply_patches(self, ctx):
        os.chdir(self._install_path(ctx))
        config_root = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_root, path)
            if apply_patch(ctx, path, 1) and self.source_type == 'installed':
                ctx.log.warning('applied patch %s to external SPEC-CPU2006 '
                                'directory' % path)

    def build(self, ctx, instance, pool=None):
        # apply any pending patches (doing this at build time allows adding
        # patches during instance development, and is needed to apply patches
        # when self.source_type == 'installed')
        self._apply_patches(ctx)

        # add flags to compile with runtime support for benchmark runner
        BenchmarkRunner.configure(ctx)

        os.chdir(self.path(ctx))
        config = self._make_spec_config(ctx, instance)
        print_output = ctx.loglevel == logging.DEBUG

        for bench in self._get_benchmarks(ctx, instance):
            cmd = 'killwrap_tree runspec --config=%s --action=build %s' % \
                  (config, bench)
            if pool:
                jobid = 'build-%s-%s' % (instance.name, bench)
                outdir = os.path.join(ctx.paths.pool_results, 'build',
                                      self.name, instance.name)
                os.makedirs(outdir, exist_ok=True)
                outfile = os.path.join(outdir, bench)
                self._run_bash(ctx, cmd, pool, jobid=jobid,
                              outfile=outfile, nnodes=1)
            else:
                ctx.log.info('building %s-%s %s' %
                             (self.name, instance.name, bench))
                self._run_bash(ctx, cmd, teeout=print_output)

    def run(self, ctx, instance, pool=None):
        config = 'infra-' + instance.name
        config_root = os.path.dirname(os.path.abspath(__file__))

        if not os.path.exists(self._install_path(ctx, 'config', config + '.cfg')):
            raise FatalError('%s-%s has not been built yet!' %
                             (self.name, instance.name))

        runargs = []

        if ctx.args.test:
            runargs += ['--size', 'test']

        # the pool scheduler will pass --iterations as -np to prun, so only run
        # one iteration in runspec
        runargs += ['--iterations', '1' if pool else '%d' % ctx.args.iterations]

        # set output root to local disk when using prun to avoid noise due to
        # network lag when writing output files
        specdir = self._install_path(ctx)
        if isinstance(pool, PrunPool):
            output_root = '/local/%s/cpu2006-output-root' % getpass.getuser()
            runargs += ['--define', 'output_root=' + output_root]
        else:
            output_root = specdir

        # apply wrapper in macro for monitor_wrapper in config
        if 'target_run_wrapper' in ctx:
            runargs += ['--define', 'run_wrapper=' + ctx.target_run_wrapper]

        # don't stop running if one benchmark from the list crashes
        if not pool:
            runargs += ['--ignore_errors']

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

        benchmarks = self._get_benchmarks(ctx, instance)

        if pool:
            if isinstance(pool, PrunPool):
                # prepare output dir on local disk before running,
                # and move output files to network disk after completion

                # FIXME the 'sleep 5' is needed to give NFS some time to sync
                # logfile contents to /var/scratch, which is read by
                # log_results below which will fail if the file is incomplete.
                # This is a temporary fix and should be fixed with a proper
                # sync command or by analyzing log files at report time
                benchdir = 'benchspec/CPU2006/{bench}'
                cmd = _unindent('''
                set -ex

                # set up local copy of results dir with binaries and logdir
                rm -rf "{output_root}"
                mkdir -p "{output_root}"
                mkdir -p "{specdir}/result"
                ln -s "{specdir}/result" "{output_root}"
                if [ -d "{specdir}/{benchdir}/exe" ]; then
                    mkdir -p "{output_root}/{benchdir}"
                    cp -r "{specdir}/{benchdir}/exe" "{output_root}/{benchdir}"
                fi

                # make empty run directories to reserve their names
                if [ -d "{specdir}/{benchdir}/run" ]; then
                    mkdir -p "{output_root}/{benchdir}/run"
                    sed "s,{specdir}/,{output_root}/,g" \\
                            "{specdir}/{benchdir}/run/list" > \\
                            "{output_root}/{benchdir}/run/list"
                    for subdir in "{specdir}/{benchdir}/run/*"; do
                        base=\$(basename "\$subdir")
                        if [ "\$base" != list ]; then
                            mkdir "{output_root}/{benchdir}/run/\$base"
                        fi
                    done
                fi

                # run runspec command
                {{{{ {cmd}; }}}} | sed "s,{output_root}/result/,{specdir}/result/,g"

                # copy output files back to headnode for analysis, use a
                # directory lock to avoid simultaneous writes and TOCTOU bugs
                while ! mkdir "{specdir}/{benchdir}/runlock" 2>/dev/null; do sleep 0.1; done
                release_lock() {{{{
                    rm -rf "{specdir}/{benchdir}/runlock"
                }}}}
                trap release_lock INT TERM EXIT

                if [ -d "{specdir}/{benchdir}/run" ]; then
                    lastdir=\$(tail -2 "{output_root}/{benchdir}/run/list" | grep -oP 'dir=.+?\.[0-9]+' | sed s/dir=//)
                    cp -r "\$lastdir" "{specdir}/{benchdir}/run"
                    cp "{output_root}/{benchdir}/run/list" "{specdir}/{benchdir}/run/list"
                else
                    cp -r "{output_root}/{benchdir}/run" "{specdir}/{benchdir}/run"
                fi
                sed -i "s,{output_root}/,{specdir}/,g" "{specdir}/{benchdir}/run/list"

                rmdir "{specdir}/{benchdir}/runlock"

                # clean up
                rm -rf "{output_root}"

                # wait for file sync
                sleep 5
                ''').format(**locals())

            for bench in benchmarks:
                jobid = 'run-%s-%s' % (instance.name, bench)
                benchcmd = self._bash_command(ctx, cmd.format(bench=bench))
                runner = BenchmarkRunner(ctx, self, instance, bench)
                runner.run(benchcmd, pool=pool, jobid=jobid,
                           nnodes=ctx.args.iterations)
        else:
            benchcmd = self._bash_command(ctx, cmd.format(bench=qjoin(benchmarks)))
            runner = BenchmarkRunner(ctx, self, instance, 'all')
            runner.run(benchcmd, teeout=True)

    def _bash_command(self, ctx, command):
        config_root = os.path.dirname(os.path.abspath(__file__))
        return [
            'bash', '-c',
            '\n' + _unindent('''
            cd %s
            source shrc
            source "%s/scripts/kill-tree-on-interrupt.inc"
            %s
            ''' % (self._install_path(ctx), config_root, command))
        ]

    def _run_bash(self, ctx, command, pool=None, **kwargs):
        runfn = pool.run if pool else run
        return runfn(ctx, self._bash_command(ctx, command), **kwargs)

    def _make_spec_config(self, ctx, instance):
        config_name = 'infra-' + instance.name
        config_path = self._install_path(ctx, 'config/%s.cfg' % config_name)
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

    def _get_benchmarks(self, ctx, instance):
        benchmarks = set()
        for bset in ctx.args.spec2006_benchmarks:
            for bench in self.benchmarks[bset]:
                if not hasattr(instance, 'exclude_spec2006_benchmark') or \
                        not instance.exclude_spec2006_benchmark(bench):
                    benchmarks.add(bench)
        return sorted(benchmarks)

    # define benchmark sets, generated using scripts/parse-benchmarks-sets.py
    benchmarks = benchmark_sets

    def log_results(self, ctx, job_output, instance, runner, outfile):
        def fix_specpath(path):
            if not os.path.exists(path):
                benchspec_dir = self._install_path(ctx, 'benchspec')
                path = re.sub(r'.*/benchspec', benchspec_dir, path)
            assert os.path.exists(path), 'invalid path ' + path
            return path

        def get_logpaths(contents):
            matches = re.findall(r'The log for this run is in (.*)$', contents, re.M)
            assert matches
            for match in matches:
                logpath = match.replace('The log for this run is in ', '')
                yield logpath

        def parse_logfile(logpath):
            ctx.log.debug('parsing log file ' + logpath)

            with open(logpath) as f:
                logcontents = f.read()

            m = re.match(r'^runspec .+ started at .+ on "(.*)"', logcontents)
            assert m, 'could not find hostname'
            hostname = m.group(1)

            m = re.search(r'^Benchmarks selected: (.+)$', logcontents, re.M)
            assert m, 'could not find benchmark list'
            error_benchmarks = set(m.group(1).split(', '))

            pat = re.compile(r'([^ ]+) ([^ ]+) base (\w+) ratio=(-?[0-9.]+), runtime=([0-9.]+).*', re.M)
            m = pat.search(logcontents)
            while m:
                status, benchmark, workload, ratio, runtime = m.groups()

                # find per-input logs by benchutils staticlib
                rpat = r'Running %s.+?-C (.+?$)(.+?)^Specinvoke:' % benchmark
                rundir, arglist = re.search(rpat, logcontents, re.M | re.S).groups()
                errfiles = re.findall(r'-e ([^ ]+)', arglist)
                inputres = []
                for errfile in errfiles:
                    path = os.path.join(fix_specpath(rundir), errfile)
                    ctx.log.debug('fetching staticlib results from errfile ' + path)
                    res = list(parse_results(ctx, path))
                    assert len(res)
                    inputres += res

                # report only the worst case of all input sets
                maxrss = max(r.maxrss_kb for r in inputres)
                maxpagefaults = max(r.page_faults for r in inputres)
                maxcswitch = max(r.context_switches for r in inputres)

                log_result({
                    'benchmark': benchmark,
                    'success': status == 'Success',
                    'workload': workload,
                    'runtime_sec': float(runtime),
                    'maxrss_kb': maxrss,
                    'maxpagefaults': maxpagefaults,
                    'maxcontextswitches': maxcswitch,
                    'hostname': hostname
                }, outfile)
                error_benchmarks.remove(benchmark)
                m = pat.search(logcontents, m.end())

            for benchmark in error_benchmarks:
                log_result({
                    'benchmark': benchmark,
                    'success': False,
                    'hostname': hostname
                }, outfile)

            ctx.log.debug('done parsing')

        for logpath in get_logpaths(job_output):
            parse_logfile(logpath)

    def report(self, ctx, results, args):
        only_overhead = args.only_overhead

        # only use fancy UTF-8 table if writing to a compatible terminal
        fancy = sys.stdout.encoding == 'UTF-8' and sys.stdout.name == '<stdout>'
        if fancy:
            from terminaltables import SingleTable as Table
        else:
            from terminaltables import AsciiTable as Table

        # optional support for colored text
        try:
            if not fancy or args.nonhuman:
                raise ImportError
            from termcolor import colored
        except ImportError:
            def colored(text, *args):
                return text

        # determine baseline
        baseline = None

        if args.baseline:
            baseline = args.baseline
        elif len(results) > 1:
            default_baselines = ('baseline', 'clang-lto', 'clang')

            for iname in default_baselines:
                if iname in results:
                    baseline = iname
                    break

        if baseline:
            ctx.log.debug('using %s instance as baseline' % baseline)
        elif only_overhead:
            raise FatalError('no baseline found for overhead computation')
        else:
            ctx.log.debug('no baseline found, not computing overheads')

        # sort instance names to avoid non-deterministic table order
        instances = sorted(results)

        # compute aggregates
        benchdata = {}
        have_memdata = False

        for iname, iresults in results.items():
            grouped = {}
            for result in iresults:
                grouped.setdefault(result['benchmark'], []).append(result)

            for bench, bresults in grouped.items():
                assert len(bresults)
                entry = benchdata.setdefault(bench, {}).setdefault(iname, {})
                if all(r['success'] for r in bresults):
                    entry['status'] = colored('OK', 'green')

                    runtimes = [r['runtime_sec'] for r in bresults]
                    entry['rt_median'] = statistics.median(runtimes)

                    if 'maxrss_kb' in bresults[0]:
                        have_memdata = True
                        memdata = [r['maxrss_kb'] for r in bresults]
                        entry['mem_max'] = max(memdata)

                    if len(bresults) > 1:
                        entry['rt_stdev'] = statistics.stdev(runtimes)
                        if have_memdata:
                            entry['mem_stdev'] = statistics.stdev(memdata)
                    else:
                        entry['rt_stdev'] = '-'
                        if have_memdata:
                            entry['mem_stdev'] = '-'

                    entry['iters'] = len(bresults)
                else:
                    entry['status'] = colored('ERROR', 'red', attrs=['bold'])

        # compute overheads compared to baseline
        if baseline:
            overheads_rt = {}
            overheads_mem = {}

            for bench, index in benchdata.items():
                for iname, entry in index.items():
                    baseline_entry = benchdata[bench][baseline]
                    if 'rt_median' in baseline_entry:
                        baseline_runtime = baseline_entry['rt_median']
                        rt_overhead = entry['rt_median'] / baseline_runtime
                        entry['rt_overhead'] = rt_overhead
                        overheads_rt.setdefault(iname, []).append(rt_overhead)
                    else:
                        entry['rt_overhead'] = '-'

                    if have_memdata:
                        if 'mem_max' in baseline_entry:
                            baseline_mem = baseline_entry['mem_max']
                            mem_overhead = entry['mem_max'] / baseline_mem
                            entry['mem_overhead'] = mem_overhead
                            overheads_mem.setdefault(iname, []).append(mem_overhead)
                        else:
                            entry['mem_overhead'] = '-'

            geomeans_rt = {iname: geomean(oh) for iname, oh in overheads_rt.items()}
            geomeans_mem = {iname: geomean(oh) for iname, oh in overheads_mem.items()}

        # build column list based on which data is present
        columns = []
        if not only_overhead:
            columns.append('status')
        if baseline:
            columns.append('rt_overhead')
            if have_memdata:
                columns.append('mem_overhead')
        if not only_overhead:
            columns += ['rt_median', 'rt_stdev']
            if have_memdata:
                columns += ['mem_max', 'mem_stdev']
            columns.append('iters')

        column_heads = {
            'status': '\nstatus',
            'rt_overhead': 'runtime\noverhead',
            'mem_overhead': 'memory\noverhead',
            'rt_median': 'runtime\nmedian',
            'rt_stdev': 'runtime\nstdev',
            'mem_max': 'memory\nmax',
            'mem_stdev': 'memory\nstdev',
            'iters': '\niters'
        }

        # header row
        header_full = ['\n\nbenchmark']
        header_keys = ['benchmark']
        for key in columns:
            for iname in instances:
                if not key.endswith('_overhead') or iname != baseline:
                    header_full.append(column_heads[key] + '\n' + iname)
                    header_keys.append(key + '-' + iname)

        # data rows
        def cell(value):
            if isinstance(value, float):
                return '%.3f' % value
            return value

        body = []
        for bench, index in sorted(benchdata.items(), key=lambda p: p[0]):
            row = [bench]
            for key in columns:
                for iname in instances:
                    if not key.endswith('_overhead') or iname != baseline:
                        row.append(cell(index[iname].get(key, '')))
            body.append(row)

        # geomean row
        if baseline and not args.nonhuman:
            lastrow = ['geomean:']
            if not only_overhead:
                lastrow += [''] * len(instances)
            lastrow += [cell(geomeans_rt.get(iname, '-'))
                        for iname in instances if iname != baseline]
            lastrow += [cell(geomeans_mem.get(iname, '-'))
                        for iname in instances if iname != baseline]
            lastrow += [''] * (len(header_full) - len(lastrow))
            body.append(lastrow)

        # write table
        if args.nonhuman == 'csv':
            writer = csv.writer(sys.stdout, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header_keys)
            for row in body:
                writer.writerow(row)
        elif args.nonhuman == 'tsv':
            print('\t'.join(header_keys))
            for row in body:
                print('\t'.join(str(cell) for cell in row))
        else:
            title = 'overheads' if only_overhead else 'aggregated data'
            table = Table([header_full] + body, ' %s %s ' % (self.name, title))
            table.inner_column_border = False
            if baseline:
                table.inner_footing_row_border = True
            for col in range(len(instances) + 1, len(header_full)):
                table.justify_columns[col] = 'right'
            print(table.table)


def _unindent(cmd):
    stripped = re.sub(r'^\n|\n *$', '', cmd)
    indent = re.search('^ +', stripped, re.M)
    if indent:
        return re.sub(r'^' + indent.group(0), '', stripped, 0, re.M)
    return stripped
