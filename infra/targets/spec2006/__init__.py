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
from collections import defaultdict
from typing import List
from ...util import Namespace, FatalError, run, apply_patch, qjoin, geomean
from ...target import Target
from ...packages import Bash, Nothp, BenchmarkUtils
from ...parallel import PrunPool
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
    need to check the output for errors manually** using the ``report``
    command.

    The ``--iterations`` option of the :ref:`run <usage-run>` command is
    translated into the number of nodes per job when ``--parallel`` is
    specified, and to ``--runspec-args -n <iterations>`` otherwise.

    The :ref:`report <usage-report>` command analyzes logs in the results
    directory and reports the aggregated data in a table. It receives a list of
    run directories (``results/run.X``) as positional arguments to traverse for
    log files. By default, the columns list runtimes, memory usages, overheads,
    standard deviations and iterations. The computed values are appended to
    each log file with the prefix ``[setup-report]``, and read from there by
    subsequent report commands if available (see also :class:`BenchmarkUtils`).
    This makes log files portable to different machines without copying over
    the entire SPEC directory. The script depends on a couple of Python
    libraries for its output::

        pip install [--user] terminaltables termcolor

    Some useful command-line options change what is displayed by ``report``:

    #. ``--baseline`` changes the baseline for overhead computation. By
       default, the script looks for **baseline**, **clang-lto** and **clang**
       (in that order).
    #. ``--csv``/``--tsv`` change the output from human-readable to
       comma/tab-separated for script processing. E.g., use in conjunction with
       ``cut`` to obtain a column of values.
    #. ``--nodes`` adds a (possibly very large) table of runtimes of individual
       nodes. This is useful for identifying bad nodes on the DAS-5 when
       some standard deviations are high while using ``--parallel prun``.
    #. ``--brief`` only prints the overhead columns of the table.
    #. ``--ascii`` disables UTF-8 output so that output can be saved to a log
       file or piped to ``less``.

    Finally, you may specify a list of patches to apply before building. These
    may be paths to .patch files that will be applied with ``patch -p1``, or
    choices from the following built-in patches:

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

        self.butils = BenchmarkUtils(self)

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
        group.add_argument('--runspec-args',
                nargs=argparse.REMAINDER, default=[],
                help='additional arguments for runspec')

    def add_report_args(self, parser):
        self.butils.add_report_args(parser)
        parser.add_argument('--baseline', metavar='INSTANCE',
                help='baseline instance for overheads')
        parser.add_argument('--brief', action='store_true',
                help='only show overhead numbers')
        parser.add_argument('--ascii', action='store_true',
                help='print ASCII tables instead of UTF-8 formatted ones')
        parser.add_argument('--nodes', action='store_true',
                help='show a table with performance per DAS-5 node')
        parser.add_argument('-x', '--exclude', action='append',
                default=[], choices=self.benchmarks['all'],
                help='benchmarks to exclude from results')
        parser.add_argument('-f', '--field', nargs='+', metavar='FIELD',
                dest='add_fields', default=[],
                help='add column for result field (use to report counters), '
                     'e.g.: _max_rss_kb, _sum_page_faults, _sum_io_operations, '
                     '_sum_context_switches, _sum_estimated_runtime_sec')

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
        yield self.butils

    def is_fetched(self, ctx):
        return self.source_type == 'installed' or os.path.exists('install/shrc')

    def fetch(self, ctx):
        if self.source_type == 'mounted':
            os.chdir(self.source)
        elif self.source_type == 'tarfile':
            ctx.log.debug('extracting SPEC-CPU2006 source files')
            run(ctx, ['tar', 'xf', self.source])
            srcdir = re.sub(r'(\.tar\.gz|\.tgz)$', '', os.path.basename(self.source))
            if not os.path.exists(srcdir):
                raise FatalError('extracted SPEC tarfile in %s, could not find '
                                 '%s/ afterwards' % (os.getcwd(), srcdir))
            shutil.move(srcdir, 'src')
            os.chdir('src')
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
            # make removed files writable to avoid permission errors
            srcdir = self.path(ctx, 'src')
            run(ctx, ['chmod', '-R', 'u+w', srcdir])
            shutil.rmtree(srcdir)

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

        # add flags to compile with runtime support for benchmark utils
        self.butils.configure(ctx)

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

        cmd = '{wrapper} runspec --config={config} --nobuild {runargs} {{bench}}'
        cmd = cmd.format(**locals())

        benchmarks = self._get_benchmarks(ctx, instance)

        if pool:
            if isinstance(pool, PrunPool):
                # prepare output dir on local disk before running,
                # and move output files to network disk after completion
                cmd = _unindent('''
                set -ex

                benchdir="benchspec/CPU2006/{{bench}}"
                localrun="{output_root}/$benchdir/run"
                scratchrun="{specdir}/$benchdir/run"

                # set up local copy of results dir with binaries and logdir
                rm -rf "{output_root}"
                mkdir -p "{output_root}"
                mkdir -p "{specdir}/result"
                ln -s "{specdir}/result" "{output_root}"
                if [ -d "{specdir}/$benchdir/exe" ]
                then
                    mkdir -p "{output_root}/$benchdir"
                    cp -r "{specdir}/$benchdir/exe" "{output_root}/$benchdir"
                fi

                # make empty run directories to reserve their names
                if [ -d "$scratchrun" ]
                then
                    mkdir -p "$localrun"
                    sed "s,{specdir}/,{output_root}/,g" \\
                            "$scratchrun/list" > "$localrun/list"
                    for subdir in "$scratchrun"/run_*
                    do
                        base="$(basename "$subdir")"
                        mkdir "$localrun/$base"
                    done
                fi

                # run runspec command
                {{{{ {cmd}; }}}} | sed "s,{output_root}/result/,{specdir}/result/,g"

                # copy output files back to headnode for analysis, use a
                # directory lock to avoid simultaneous writes and TOCTOU bugs
                while ! mkdir "{specdir}/$benchdir/copylock" 2>/dev/null; do sleep 0.1; done
                release_lock() {{{{
                    rm -rf "{specdir}/$benchdir/copylock"
                }}}}
                trap release_lock INT TERM EXIT

                if [ -d "$scratchrun" ]
                then
                    # copy over any new run directories
                    cp -r "$localrun"/run_* "$scratchrun/"

                    # merge list files to keep things consistent
                    sed -i /__END__/d "$scratchrun/list"
                    sed "s,{output_root},{specdir}," "$localrun/list" | \\
                            diff - "$scratchrun/list" | \\
                            sed "/^[^<]/d;s/^< //" >> "$scratchrun/list"

                else
                    # no run directory in scratch yet, just copy it over
                    # entirely and patch the paths
                    cp -r "$localrun" "$scratchrun"
                    sed -i "s,{output_root}/,{specdir}/,g" "$scratchrun/list"
                fi

                rmdir "{specdir}/$benchdir/copylock"

                # clean up
                rm -rf "{output_root}"
                ''').format(**locals())

                # the script is passed like this: prun ... bash -c '<script>'
                # this means that some escaping is necessary: use \$ instead of
                # $ for bash variables and \" instead of "
                cmd = cmd.replace('$', '\$').replace('"', '\\"')

            for bench in benchmarks:
                jobid = 'run-%s-%s' % (instance.name, bench)
                outfile = self.butils.outfile_path(ctx, instance, bench)
                self._run_bash(ctx, cmd.format(bench=bench), pool, jobid=jobid,
                               outfile=outfile, nnodes=ctx.args.iterations)
        else:
            self._run_bash(ctx, cmd.format(bench=qjoin(benchmarks)),
                           teeout=True)

    def _run_bash(self, ctx, command, pool=None, **kwargs):
        config_root = os.path.dirname(os.path.abspath(__file__))
        cmd = [
            'bash', '-c',
            '\n' + _unindent('''
            cd %s
            source shrc
            source "%s/scripts/kill-tree-on-interrupt.inc"
            %s
            ''' % (self._install_path(ctx), config_root, command))
        ]
        runfn = pool.run if pool else run
        return runfn(ctx, cmd, **kwargs)

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

    def parse_outfile(self, ctx, instance_name, outfile):
        # called by self.butils.parse_logs() in report() below

        def fix_specpath(path):
            if not os.path.exists(path):
                benchspec_dir = self._install_path(ctx, 'benchspec')
                path = re.sub(r'.*/benchspec', benchspec_dir, path)
            assert os.path.exists(path), 'invalid path ' + path
            return path

        def get_logpaths(contents):
            matches = re.findall(r'The log for this run is in (.*)$',
                                 contents, re.M)
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

            pat = re.compile(r'([^ ]+) ([^ ]+) base (\w+) ratio=(-?[0-9.]+), '
                             r'runtime=([0-9.]+).*', re.M)
            m = pat.search(logcontents)
            while m:
                status, benchmark, workload, ratio, runtime = m.groups()

                # find per-input logs by benchutils staticlib
                rpat = r'Running %s.+?-C (.+?$)(.+?)^Specinvoke:' % benchmark
                rundir, arglist = re.search(rpat, logcontents, re.M | re.S).groups()
                errfiles = re.findall(r'-e ([^ ]+err) \.\./run_', arglist)
                inputres = []
                for errfile in errfiles:
                    path = os.path.join(fix_specpath(rundir), errfile)
                    ctx.log.debug('fetching staticlib results from errfile ' + path)
                    res = list(BenchmarkUtils.parse_results(ctx, path))
                    if not res:
                        ctx.log.warning('no staticlib results in %s, there '
                                        'was probably an error' % path)
                    inputres += res

                # merge counter results found in different invocations,
                # computing aggregates based on keys
                counters = BenchmarkUtils.merge_results(inputres)

                yield {
                    'benchmark': benchmark,
                    'success': status == 'Success',
                    'workload': workload,
                    'runtime_sec': float(runtime),
                    'hostname': hostname,
                    'inputs': len(errfiles),
                    **counters
                }
                error_benchmarks.remove(benchmark)
                m = pat.search(logcontents, m.end())

            for benchmark in error_benchmarks:
                yield {
                    'benchmark': benchmark,
                    'success': False,
                    'hostname': hostname
                }

            ctx.log.debug('done parsing')

        with open(outfile) as f:
            outfile_contents = f.read()

        logpaths = list(get_logpaths(outfile_contents))
        if logpaths:
            for logpath in get_logpaths(outfile_contents):
                yield from parse_logfile(logpath)
        else:
            yield {
                'benchmark': re.sub(r'\.\d+$', '', os.path.basename(outfile)),
                'success': False,
                'timeout': True
            }

    def report(self, ctx, instances, outfile, args):
        results = self.butils.parse_logs(ctx, instances, args.rundirs)
        only_overhead = args.brief
        show_nodes_table = args.nodes

        # in --nodes table, highlight runtimes whose deviation from the mean
        # exceeds 3 times the variance, but only if the percentage deviation is
        # at least 2%
        highlight_variance_deviation = 3
        highlight_percent_threshold = 0.02

        # TODO: move runtimes table to a utility module

        # only use fancy UTF-8 table if writing to a compatible terminal
        fancy = sys.stdout.encoding == 'UTF-8' and \
                sys.stdout.name == '<stdout>' and \
                not args.ascii
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
            def colored(text, *args, **kwargs):
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

        else:
            ctx.log.debug('no baseline found, not computing overheads')

        # sort instance names to avoid random table order
        instances = sorted(results)

        # compute aggregates
        benchdata = defaultdict(lambda: defaultdict(Namespace))
        have_memdata = False
        workload = None
        node_zscores = defaultdict(lambda: defaultdict(list))
        node_runtimes = defaultdict(list)

        for iname, iresults in results.items():
            grouped = {}
            for result in iresults:
                grouped.setdefault(result['benchmark'], []).append(result)
                if workload is None:
                    workload = result.get('workload', None)
                elif result.get('workload', workload) != workload:
                    raise FatalError('%s uses %s workload whereas previous '
                                     'benchmarks use %s (logfile %s)' %
                                     (result['benchmark'], result['workload'],
                                      workload, result['outfile']))

            for bench, bresults in grouped.items():
                if bench in args.exclude:
                    continue

                assert len(bresults)
                entry = benchdata[bench][iname]
                if all(r['success'] for r in bresults):
                    entry.status = colored('OK', 'green')

                    # runtime
                    runtimes = [r['runtime_sec'] for r in bresults]
                    entry.rt_median = statistics.median(runtimes)
                    entry.rt_mean = statistics.mean(runtimes)

                    # memory usage
                    if '_max_rss_kb' in bresults[0]:
                        have_memdata = True
                        memdata = [r['_max_rss_kb'] for r in bresults]
                        entry.mem_max = max(memdata)

                    # standard deviations
                    if len(bresults) > 1:
                        entry.rt_stdev = stdev = statistics.pstdev(runtimes)
                        entry.rt_variance = statistics.pvariance(runtimes)
                        if have_memdata:
                            entry.mem_stdev = statistics.stdev(memdata)

                        # z-score per node
                        mean_rt = statistics.mean(runtimes)
                        for r in bresults:
                            node = r['hostname']
                            runtime = r['runtime_sec']
                            zscore = (runtime - mean_rt) / stdev
                            node_zscores[node][bench].append(zscore)
                            node_runtimes[(node, bench, iname)].append(
                                    (runtime, zscore, r['outfile']))
                    else:
                        entry.rt_stdev = '-'
                        if have_memdata:
                            entry.mem_stdev = '-'

                    # custom counters
                    for field in args.add_fields:
                        field_data = (r[field] for r in bresults)
                        entry[field] = BenchmarkUtils.aggregate_values(field_data, field)

                    # benchmark iterations
                    entry.iters = len(bresults)

                elif any(r.get('timeout', False) for r in bresults):
                    entry.status = colored('TIMEOUT', 'red', attrs=['bold'])
                else:
                    entry.status = colored('ERROR', 'red', attrs=['bold'])

        ctx.log.debug('all benchmarks used the %s workload' % workload)

        # compute overheads compared to baseline
        if baseline:
            overheads_rt = defaultdict(list)
            overheads_mem = defaultdict(list)

            for bench, index in benchdata.items():
                for iname, entry in index.items():
                    baseline_entry = benchdata.get(bench, {}).get(baseline, None)

                    if not baseline_entry:
                        entry.rt_overhead = '-'
                        entry.mem_overhead = '-'
                        continue

                    if 'rt_median' in entry and 'rt_median' in baseline_entry:
                        baseline_runtime = baseline_entry.rt_median
                        rt_overhead = entry.rt_median / baseline_runtime
                        entry.rt_overhead = rt_overhead
                        overheads_rt[iname].append(rt_overhead)
                    else:
                        entry.rt_overhead = '-'

                    if have_memdata:
                        if 'mem_max' in entry and 'mem_max' in baseline_entry:
                            baseline_mem = baseline_entry.mem_max
                            mem_overhead = entry.mem_max / baseline_mem
                            entry.mem_overhead = mem_overhead
                            overheads_mem[iname].append(mem_overhead)
                        else:
                            entry.mem_overhead = '-'

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
        for field in args.add_fields:
            columns.append(field)

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
                    head = column_heads.get(key, '\n' + key)
                    header_full.append(head + '\n' + iname)
                    header_keys.append(key + '-' + iname)

        # data rows
        def cell(value):
            if isinstance(value, float):
                return '%.3f' % value
            return value

        body = []
        for bench, index in sorted(benchdata.items()):
            row = [bench]
            for key in columns:
                for iname in instances:
                    if not key.endswith('_overhead') or iname != baseline:
                        row.append(cell(index.get(iname, {}).get(key, '')))
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
        with redirect_stdout(outfile):
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
                title = 'overheads-%s' if only_overhead \
                        else 'aggregated data (%s workload)'
                title = ' %s %s ' % (self.name, title % workload)
                table = Table([header_full] + body, title)
                table.inner_column_border = False
                if baseline:
                    table.inner_footing_row_border = True
                for col in range(len(instances) + 1, len(header_full)):
                    table.justify_columns[col] = 'right'
                print(table.table)

        if show_nodes_table:
            # order nodes such that the one with the highest z-scores (the most
            # deviating) come first
            zmeans = {}
            for hostname, benchscores in node_zscores.items():
                allscores = []
                for bscores in benchscores.values():
                    for score in bscores:
                        allscores.append(score)
                zmeans[hostname] = statistics.mean(allscores)
            nodes = sorted(zmeans, key=lambda n: zmeans[n], reverse=True)

            # create table with runtimes per node
            header = [' node:\n mean z-score:', '']
            for node in nodes:
                nodename = node.replace('node', '')
                zscore = ('%.1f' % zmeans[node]).replace('0.', '.')
                header.append(nodename + '\n' + zscore)
            rows = [header]

            high_devs = []

            for bench, index in sorted(benchdata.items()):
                for iname, entry in index.items():
                    row = [' ' + bench, iname]
                    for node in nodes:
                        runtimes = node_runtimes[(node, bench, iname)]
                        runtimes.sort(reverse=True)

                        # highlight outliers to easily identify bad nodes
                        highlighted = []
                        for runtime, zscore, ofile in runtimes:
                            rt = '%d' % round(runtime)
                            deviation = runtime - entry.rt_mean
                            deviation_ratio = abs(deviation) / entry.rt_mean

                            if deviation ** 2 > entry.rt_variance * highlight_variance_deviation and \
                                    deviation_ratio > highlight_percent_threshold:
                                rt = colored(rt, 'red')
                                high_devs.append((bench, iname, runtime, ofile))
                            elif runtime == entry.rt_median:
                                rt = colored(rt, 'blue', attrs=['bold'])

                            highlighted.append(rt)

                        row.append(','.join(highlighted))

                    rows.append(row)

            title = ' node runtimes '
            if fancy:
                title += '(red = high deviation, blue = median) '
            table = Table(rows, title)
            table.inner_column_border = False
            table.padding_left = 0
            print('\n'+ table.table)

            # show measurements with high deviations in separate table with log
            # file paths for easy access
            if high_devs:
                rows = [['benchmark', 'instance', 'runtime', 'log file']]
                for bench, iname, runtime, ofile in high_devs:
                    opath = re.sub('^%s/' % ctx.workdir, '', ofile)
                    rows.append([bench, iname, '%.3f' % runtime, opath])
                table = Table(rows, ' high deviations ')
                table.inner_column_border = False
                print('\n'+ table.table)

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in SPEC benchmarks.
    custom_allocs_flags = ['-allocs-custom-funcs=' + '.'.join((
        # 400.perlbench
        'Perl_safesysmalloc'  ':malloc'  ':0',
        'Perl_safesyscalloc'  ':calloc'  ':1:0',
        'Perl_safesysrealloc' ':realloc' ':1',
        'Perl_safesysfree'    ':free'    ':-1',

        # 403.gcc
        'ggc_alloc'           ':malloc'  ':0',
        'alloc_anon'          ':malloc'  ':1',
        'xmalloc'             ':malloc'  ':0',
        'xcalloc'             ':calloc'  ':1:0',
        'xrealloc'            ':realloc' ':1',
    ))]


def _unindent(cmd):
    stripped = re.sub(r'^\n|\n *$', '', cmd)
    indent = re.search('^ +', stripped, re.M)
    if indent:
        return re.sub(r'^' + indent.group(0), '', stripped, 0, re.M)
    return stripped
