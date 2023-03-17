import os
import shutil
import logging
import argparse
import getpass
import re
from contextlib import redirect_stdout
from collections import defaultdict
from typing import List
from ...commands.report import outfile_path
from ...util import FatalError, run, apply_patch, qjoin, require_program
from ...target import Target
from ...packages import Bash, Nothp, RusageCounters
from ...parallel import PrunPool
from .benchmark_sets import benchmark_sets
from .nodes_command import SpecFindBadPrunNodesCommand


class SPEC2006(Target):
    """
    The `SPEC-CPU2006 <https://www.spec.org/cpu2006/>`_ benchmarking suite.

    Since SPEC may not be redistributed, you need to provide your own copy in
    ``source``. We support the following types for ``source_type``:

    - ``isofile``:    ISO file to mount (requires ``fuseiso`` to be installed)
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
    subsequent report commands if available (see also :class:`RusageCounters`).
    This makes log files portable to different machines without copying over
    the entire SPEC directory. The script depends on a couple of Python
    libraries for its output::

        pip3 install [--user] terminaltables termcolor

    Some useful command-line options change what is displayed by ``report``:

    TODO: move some of these from below to general report command docs

    #. ``--fields`` changes which data fields are printed. A column is added
       for each instance for each field. The options are autocompleted and
       default to status, overheads, runtime, memory usage, stddevs and
       iterations. Custom counter fields from runtime libraries can also be
       specified (but are not autocompleted).
    #. ``--baseline`` changes the baseline for overhead computation. By
       default, the script looks for **baseline**, **clang-lto** or **clang**.
    #. ``--csv``/``--tsv`` change the output from human-readable to
       comma/tab-separated for script processing. E.g., use in conjunction with
       ``cut`` to obtain a column of values.
    #. ``--nodes`` adds a (possibly very large) table of runtimes of individual
       nodes. This is useful for identifying bad nodes on the DAS-5 when
       some standard deviations are high while using ``--parallel prun``.
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
    :param toolsets: approved toolsets to add additionally
    :param nothp: run without transparent huge pages (they tend to introduce
                  noise in performance measurements), implies :class:`Nothp`
                  dependency if ``True``
    :param force_cpu: bind runspec to this cpu core (-1 to disable)
    :param default_benchmarks: specify benchmarks run by default
    """

    name = 'spec2006'

    reportable_fields = {
        'benchmark': 'benchmark program',
        'status':    'whether the benchmark finished successfully',
        'runtime':   'total runtime in seconds',
        'hostname':  'machine hostname',
        'workload':  'run workload (test / ref / train)',
        'inputs':    'number of different benchmark inputs',
        **RusageCounters.reportable_fields,
    }
    aggregation_field = 'benchmark'

    def __init__(self, source_type: str,
                       source: str,
                       patches: List[str] = [],
                       toolsets: List[str] = [],
                       nothp: bool = True,
                       force_cpu: int = 0,
                       default_benchmarks: List[str] = ['all_c', 'all_cpp']):
        if source_type not in ('isofile', 'mounted', 'installed', 'tarfile', 'git'):
            raise FatalError('invalid source type "%s"' % source_type)

        if source_type == 'installed':
            shrc = source + '/shrc'
            if not os.path.exists(shrc):
                shrc = os.path.abspath(shrc)
                raise FatalError(shrc + ' is not a valid SPEC installation')

        self.source = source
        self.source_type = source_type
        self.patches = patches
        self.toolsets = toolsets
        self.nothp = nothp
        self.force_cpu = force_cpu
        self.default_benchmarks = default_benchmarks

    def add_build_args(self, parser):
        parser.add_argument('--benchmarks',
                nargs='+', metavar='BENCHMARK', default=self.default_benchmarks,
                choices=self.benchmarks,
                help='which benchmarks to build')

    def add_run_args(self, parser):
        parser.add_argument('--benchmarks',
                nargs='+', metavar='BENCHMARK', default=self.default_benchmarks,
                choices=self.benchmarks,
                help='which benchmarks to run')
        parser.add_argument('--test', action='store_true',
                help='run a single iteration of the test workload')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--runspec-args',
                nargs=argparse.REMAINDER, default=[],
                help='additional arguments for runspec')

    def dependencies(self):
        yield Bash('4.3')
        if self.nothp:
            yield Nothp()
        yield RusageCounters()

    def is_fetched(self, ctx):
        return self.source_type == 'installed' or os.path.exists('install/shrc')

    def fetch(self, ctx):
        def do_install(srcdir):
            os.chdir(srcdir)
            for toolset in self.toolsets:
                ctx.log.debug('extracting SPEC-CPU2006 toolset ' + toolset)
                run(ctx, ['tar', 'xf', toolset])
            install_path = self._install_path(ctx)
            ctx.log.debug('installing SPEC-CPU2006 into ' + install_path)
            run(ctx, ['./install.sh', '-f', '-d', install_path],
                env={'PERL_TEST_NUMCONVERTS': 1})

        if self.source_type == 'isofile':
            require_program(ctx, 'fuseiso', 'required to mount SPEC iso')
            require_program(ctx, 'fusermount', 'required to mount SPEC iso')
            mountdir = self.path(ctx, 'mount')
            ctx.log.debug('mounting SPEC-CPU2006 ISO to ' + mountdir)
            os.mkdir(mountdir)
            run(ctx, ['fuseiso', self.source, mountdir])
            do_install(mountdir)
            ctx.log.debug('unmounting SPEC-CPU2006 ISO')
            os.chdir(self.path(ctx))
            run(ctx, ['fusermount', '-u', mountdir])
            os.rmdir(mountdir)

        elif self.source_type == 'mounted':
            do_install(self.source)

        elif self.source_type == 'tarfile':
            ctx.log.debug('extracting SPEC-CPU2006 source files')
            run(ctx, ['tar', 'xf', self.source])
            srcdir = re.sub(r'(\.tar\.gz|\.tgz)$', '', os.path.basename(self.source))
            if not os.path.exists(srcdir):
                raise FatalError('extracted SPEC tarfile in %s, could not find '
                                 '%s/ afterwards' % (os.getcwd(), srcdir))
            shutil.move(srcdir, 'src')
            do_install('src')
            ctx.log.debug('removing SPEC-CPU2006 source files to save disk space')
            # make removed files writable to avoid permission errors
            srcdir = self.path(ctx, 'src')
            run(ctx, ['chmod', '-R', 'u+w', srcdir])
            shutil.rmtree(srcdir)

        elif self.source_type == 'git':
            require_program(ctx, 'git')
            ctx.log.debug('cloning SPEC-CPU2006 repo')
            run(ctx, ['git', 'clone', '--depth', 1, self.source, 'src'])
            do_install('src')

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
        RusageCounters().configure(ctx)

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
                    rmdir "{specdir}/$benchdir/copylock" 2>/dev/null || true
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

                release_lock

                # clean up
                rm -rf "{output_root}"
                ''').format(**locals())

                # the script is passed like this: prun ... bash -c '<script>'
                # this means that some escaping is necessary: use \$ instead of
                # $ for bash variables and \" instead of "
                cmd = cmd.replace('$', '\$').replace('"', '\\"')

            for bench in benchmarks:
                jobid = 'run-%s-%s' % (instance.name, bench)
                outfile = outfile_path(ctx, self, instance, bench)
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
                extra_libs = qjoin(ctx.extra_libs) if 'extra_libs' in ctx else None
                coptimize = qjoin(ctx.coptimize) if 'coptimize' in ctx else '-std=gnu89'
                cxxoptimize = qjoin(ctx.cxxoptimize) if 'cxxoptimize' in ctx else '-std=c++98'
                foptimize = qjoin(ctx.foptimize) if 'foptimize' in ctx else None
                f77optimize = qjoin(ctx.f77optimize) if 'f77optimize' in ctx else None
                fortranc = shutil.which('gfortran') or shutil.which('false')
                print('CC          = %s %s' % (ctx.cc, cflags))
                print('CXX         = %s %s' % (ctx.cxx, cxxflags))
                print('FC          = %s' % fortranc)
                print('CLD         = %s %s' % (ctx.cc, ldflags))
                print('CXXLD       = %s %s' % (ctx.cxx, ldflags))
                print('COPTIMIZE   = %s' % (coptimize))
                print('CXXOPTIMIZE = %s' % (cxxoptimize))
                if foptimize:
                    print('FOPTIMIZE   = %s' % (foptimize))
                if f77optimize:
                    print('F77OPTIMIZE = %s' % (f77optimize))
                if extra_libs:
                    print('EXTRA_LIBS = %s' % (extra_libs))

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

                benchmark_flags = {
                        '400.perlbench=default=default=default': {
                            'CPORTABILITY': ['-DSPEC_CPU_LINUX_X64'] if 'arch' in ctx and ctx.arch == 'x86_64' else ['-DSPEC_CPU_LINUX']
                        },
                        '403.gcc=default=default=default': {
                            'CPORTABILITY': ['-DSPEC_CPU_LINUX']
                        },
                        '462.libquantum=default=default=default': {
                            'CPORTABILITY': ['-DSPEC_CPU_LINUX']
                        },
                        '464.h264ref=default=default=default': {
                            'CPORTABILITY': ['-fsigned-char']
                        },
                        '482.sphinx3=default=default=default': {
                            'CPORTABILITY': ['-fsigned-char']
                        },
                        '482.sphinx3=default=default=default': {
                            'CPORTABILITY': ['-fsigned-char']
                        },
                        '483.xalancbmk=default=default=default': {
                            'CXXPORTABILITY': ['-DSPEC_CPU_LINUX']
                        },
                        '481.wrf=default=default=default': {
                            'extra_lines': ['wrf_data_header_size = 8'],
                            'CPORTABILITY': ['-DSPEC_CPU_CASE_FLAG', '-DSPEC_CPU_LINUX']
                        }
                }

                if 'benchmark_flags' in ctx:
                    for benchmark, flags in ctx.benchmark_flags.items():
                        if benchmark not in benchmark_flags:
                            benchmark_flags[benchmark] = {}
                        for flag, value in flags.items():
                            if flag not in benchmark_flags[benchmark]:
                                benchmark_flags[benchmark][flag] = []
                            benchmark_flags[benchmark][flag].extend(value)

                for benchmark, flags in benchmark_flags.items():
                    print('%s:' % benchmark)
                    for flag, value in flags.items():
                        if flag == 'extra_lines':
                            for line in value:
                                print(line)
                        else:
                            print('%s   = %s' % (flag, qjoin(value)))
                    print('')

        return config_name

    # override post-build hook runner rather than defining `binary_paths` since
    # we add hooks to the generated SPEC config file and call them through the
    # exec-hook setup command instead
    def run_hooks_post_build(self, ctx, instance):
        pass
    
    def run_hooks_pre_build(self, ctx, instance):
        if ctx.hooks.pre_build:
            for bench in self._get_benchmarks(ctx, instance):
                path = self._install_path(ctx, 'benchspec', 'CPU2006', bench)
                os.chdir(path)
                for hook in ctx.hooks.pre_build:
                    ctx.log.info(f"Running hook {hook} on {bench} in {path}")
                    hook(ctx, path)

    def _get_benchmarks(self, ctx, instance):
        benchmarks = set()
        for bset in ctx.args.benchmarks:
            for bench in self.benchmarks[bset]:
                if not hasattr(instance, 'exclude_spec2006_benchmark') or \
                        not instance.exclude_spec2006_benchmark(bench):
                    benchmarks.add(bench)
        return sorted(benchmarks)

    # define benchmark sets, generated using scripts/parse-benchmarks-sets.py
    benchmarks = benchmark_sets

    def parse_outfile(self, ctx, instance_name, outfile):
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
                rusage_counters = defaultdict(int)

                # find per-input logs by benchutils staticlib
                rpat = r'Running %s.+?-C (.+?$)(.+?)^Specinvoke:' % benchmark
                rundir, arglist = re.search(rpat, logcontents, re.M | re.S).groups()
                errfiles = re.findall(r'-e ([^ ]+err) \.\./run_', arglist)
                benchmark_error = False
                for errfile in errfiles:
                    path = os.path.join(fix_specpath(rundir), errfile)
                    if not os.path.exists(path):
                        ctx.log.error('missing errfile %s, there was probably '
                                      'an error' % path)
                        benchmark_error = True
                        continue

                    rusage_results = \
                        list(RusageCounters.parse_results(ctx, path))
                    if not rusage_results:
                        ctx.log.error('no staticlib results in %s, there was '
                                      'probably an error' % path)
                        benchmark_error = True
                        continue

                    for result in rusage_results:
                        for counter, value in result.items():
                            rusage_counters[counter] += value

                if benchmark_error:
                    ctx.log.warning('cancel processing benchmark %s in log file '
                                    '%s because of errors' % (benchmark, logpath))
                else:
                    yield {
                        'benchmark': benchmark,
                        'status': 'ok' if status == 'Success' else 'invalid',
                        'workload': workload,
                        'hostname': hostname,
                        'runtime': float(runtime),
                        'inputs': len(errfiles),
                        **rusage_counters
                    }
                    error_benchmarks.remove(benchmark)

                m = pat.search(logcontents, m.end())

            for benchmark in error_benchmarks:
                yield {
                    'benchmark': benchmark,
                    'status': 'error',
                    'hostname': hostname,
                }

            ctx.log.debug('done parsing')

        with open(outfile) as f:
            outfile_contents = f.read()

        logpaths = list(get_logpaths(outfile_contents))
        if logpaths:
            for logpath in logpaths:
                yield from parse_logfile(logpath)
        else:
            yield {
                'benchmark': re.sub(r'\.\d+$', '', os.path.basename(outfile)),
                'status': 'timeout',
            }

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
