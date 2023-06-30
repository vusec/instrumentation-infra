import argparse
import getpass
import logging
import os
import re
import shutil
from collections import defaultdict
from contextlib import redirect_stdout
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Type,
    Union,
)

from ...commands.report import outfile_path, process_log
from ...context import Context
from ...instance import Instance
from ...package import Package
from ...packages import Bash, Nothp, ReportableTool, RusageCounters
from ...parallel import Job, Pool, ProcessPool, PrunPool
from ...target import Target
from ...util import FatalError, ResultDict, apply_patch, qjoin, require_program, run
from .benchmark_sets import benchmark_sets


class SPEC2017(Target):
    """
    The `SPEC-CPU2017 <https://www.spec.org/cpu2017/>`_ benchmarking suite.

    Since SPEC may not be redistributed, you need to provide your own copy in
    ``source``. We support the following types for ``source_type``:

    - ``isofile``:    ISO file to mount (requires ``fuseiso`` to be installed)
    - ``mounted``:    mounted/extracted ISO directory
    - ``installed``:  pre-installed SPEC directory in another project
    - ``tarfile``:    compressed tarfile with ISO contents
    - ``git``:        git repo containing extracted ISO

    The following options are added only for the :ref:`run <usage-run>`
    command:

    - ``--benchmarks``: alias for ``--spec2017-benchmarks``
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

    :name: spec2017
    :param source_type: see above
    :param source: where to install spec from
    :param patches: patches to apply after installing
    :param nothp: run without transparent huge pages (they tend to introduce
                  noise in performance measurements), implies :class:`Nothp`
                  dependency if ``True``
    :param force_cpu: bind runspec to this cpu core (-1 to disable)
    :param default_benchmarks: specify benchmarks run by default
    """

    name = "spec2017"

    aggregation_field = "benchmark"

    def __init__(
        self,
        source_type: str,
        source: str,
        patches: List[str] = [],
        nothp: bool = True,
        force_cpu: int = 0,
        default_benchmarks: List[str] = [
            "intspeed_pure_c",
            "intspeed_pure_cpp",
            "fpspeed_pure_c",
        ],
        reporters: List[Union[ReportableTool, Type[ReportableTool]]] = [RusageCounters],
    ):
        if source_type not in ("isofile", "mounted", "installed", "tarfile", "git"):
            raise FatalError(f"invalid source type '{source_type}'")

        if source_type == "installed":
            shrc = source + "/shrc"
            if not os.path.exists(shrc):
                shrc = os.path.abspath(shrc)
                raise FatalError(shrc + " is not a valid SPEC installation")

        self.source = source
        self.source_type = source_type
        self.patches = patches
        self.nothp = nothp
        self.force_cpu = force_cpu
        self.default_benchmarks = default_benchmarks
        self.reporters = reporters

    def reportable_fields(self) -> Mapping[str, str]:
        fields = {
            "benchmark": "benchmark program",
            "status": "whether the benchmark finished successfully",
            "runtime": "total runtime in seconds",
            "hostname": "machine hostname",
            "workload": "run workload (test / ref / train)",
            "inputs": "number of different benchmark inputs",
        }
        for reporter in self.reporters:
            fields.update(reporter.reportable_fields())
        return fields

    def add_build_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--benchmarks",
            nargs="+",
            metavar="BENCHMARK",
            default=self.default_benchmarks,
            choices=self.benchmarks,
            help="which benchmarks to build",
        )

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--benchmarks",
            nargs="+",
            metavar="BENCHMARK",
            default=self.default_benchmarks,
            choices=self.benchmarks,
            help="which benchmarks to run",
        )
        parser.add_argument(
            "--test",
            action="store_true",
            help="run a single iteration of the test workload",
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--runspec-args",
            nargs=argparse.REMAINDER,
            default=[],
            help="additional arguments for runspec",
        )

    def dependencies(self) -> Iterator[Package]:
        yield Bash("4.3")
        if self.nothp:
            yield Nothp()
        yield RusageCounters()

    def is_fetched(self, ctx: Context) -> bool:
        return self.source_type == "installed" or os.path.exists("install/shrc")

    def fetch(self, ctx: Context) -> None:
        def do_install(srcdir: str) -> None:
            os.chdir(srcdir)
            install_path = self._install_path(ctx)
            ctx.log.debug("installing SPEC-CPU2017 into " + install_path)
            run(
                ctx,
                ["./install.sh", "-f", "-d", install_path],
                env={"PERL_TEST_NUMCONVERTS": "1"},
            )

        if self.source_type == "isofile":
            require_program(ctx, "fuseiso", "required to mount SPEC iso")
            require_program(ctx, "fusermount", "required to mount SPEC iso")
            mountdir = self.path(ctx, "mount")
            ctx.log.debug("mounting SPEC-CPU2017 ISO to " + mountdir)
            os.mkdir(mountdir)
            run(ctx, ["fuseiso", self.source, mountdir])
            do_install(mountdir)
            ctx.log.debug("unmounting SPEC-CPU2017 ISO")
            os.chdir(self.path(ctx))
            run(ctx, ["fusermount", "-u", mountdir])
            os.rmdir(mountdir)

        elif self.source_type == "mounted":
            do_install(self.source)

        elif self.source_type == "tarfile":
            ctx.log.debug("extracting SPEC-CPU2017 source files")
            run(ctx, ["tar", "xf", self.source])
            srcdir = re.sub(r"(\.tar\.gz|\.tgz)$", "", os.path.basename(self.source))
            if not os.path.exists(srcdir):
                raise FatalError(
                    f"extracted SPEC tarfile in {os.getcwd()}, could not "
                    f"find {srcdir}/ afterwards"
                )
            shutil.move(srcdir, "src")
            do_install("src")
            ctx.log.debug("removing SPEC-CPU2017 source files to save disk space")
            # make removed files writable to avoid permission errors
            srcdir = self.path(ctx, "src")
            run(ctx, ["chmod", "-R", "u+w", srcdir])
            shutil.rmtree(srcdir)

        elif self.source_type == "git":
            require_program(ctx, "git")
            ctx.log.debug("cloning SPEC-CPU2017 repo")
            run(ctx, ["git", "clone", "--depth", 1, self.source, "src"])
            do_install("src")

    def _install_path(self, ctx: Context, *args: str) -> str:
        if self.source_type == "installed":
            return os.path.join(self.source, *args)
        return self.path(ctx, "install", *args)

    def _apply_patches(self, ctx: Context) -> None:
        os.chdir(self._install_path(ctx))
        config_root = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if "/" not in path:
                path = f"{config_root}/{path}.patch"
            if apply_patch(ctx, path, 1) and self.source_type == "installed":
                ctx.log.warning(
                    f"applied patch {path} to external SPEC-CPU2017 directory"
                )

    def build(
        self, ctx: Context, instance: Instance, pool: Optional[Pool] = None
    ) -> None:
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
            cmd = f"killwrap_tree runcpu --config={config} --action=build {bench}"
            if pool:
                jobid = f"build-{instance.name}-{bench}"
                outdir = os.path.join(
                    ctx.paths.pool_results, "build", self.name, instance.name
                )
                os.makedirs(outdir, exist_ok=True)
                outfile = os.path.join(outdir, bench)
                self._run_bash(ctx, cmd, pool, jobid=jobid, outfile=outfile, nnodes=1)
            else:
                ctx.log.info(f"building {self.name}-{instance.name} {bench}")
                self._run_bash(ctx, cmd, teeout=print_output)

    def run(
        self, ctx: Context, instance: Instance, pool: Optional[Pool] = None
    ) -> None:
        config = "infra-" + instance.name

        if not os.path.exists(self._install_path(ctx, "config", config + ".cfg")):
            raise FatalError(f"{self.name}-{instance.name} has not been built yet!")

        runargs = []

        if ctx.args.test:
            runargs += ["--size", "test"]

        # the pool scheduler will pass --iterations as -np to prun, so only run
        # one iteration in runspec
        runargs += ["--iterations", "1" if pool else str(ctx.args.iterations)]

        # set output root to local disk when using prun to avoid noise due to
        # network lag when writing output files
        specdir = self._install_path(ctx)
        if isinstance(pool, PrunPool):
            output_root = f"/local/{getpass.getuser()}/cpu2017-output-root"
            runargs += ["--define", "output_root=" + output_root]
        else:
            output_root = specdir

        # apply wrapper in macro for monitor_wrapper in config
        if ctx.target_run_wrapper:
            runargs += ["--define", "run_wrapper=" + ctx.target_run_wrapper]

        # don't stop running if one benchmark from the list crashes
        if not pool:
            runargs += ["--ignore_errors"]

        runargs += ctx.args.runspec_args

        wrapper = "killwrap_tree"
        if self.nothp:
            wrapper += " nothp"
        if self.force_cpu >= 0:
            if isinstance(pool, ProcessPool) and pool.parallelmax > 1:
                ctx.log.warning(
                    f"Ignoring force_cpu={self.force_cpu} for "
                    "SPEC2017 because using parallel=proc with "
                    "parallelmax > 1"
                )
            else:
                wrapper += f" taskset -c {self.force_cpu}"

        cmd = f"{wrapper} runcpu --config={config} --nobuild {qjoin(runargs)} {{bench}}"

        benchmarks = self._get_benchmarks(ctx, instance)

        if pool:
            if isinstance(pool, PrunPool):
                # prepare output dir on local disk before running,
                # and move output files to network disk after completion
                cmd = _unindent(f"""
                set -ex

                benchdir="benchspec/CPU2017/{{bench}}"
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
                while ! mkdir "{specdir}/$benchdir/copylock" 2>/dev/null; do
                    sleep 0.1;
                done
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
                """)

                # the script is passed like this: prun ... bash -c '<script>'
                # this means that some escaping is necessary: use \$ instead of
                # $ for bash variables and \" instead of "
                cmd = cmd.replace("$", "\\$").replace('"', '\\"')

            for bench in benchmarks:
                jobid = f"run-{instance.name}-{bench}"
                outfile = outfile_path(ctx, self, instance, bench)

                def onsuccess_parse_log(job: Job) -> None:
                    for job_outfile in job.outfiles:
                        process_log(ctx, job_outfile, self, write_cache=True)

                self._run_bash(
                    ctx,
                    cmd.format(bench=bench),
                    pool,
                    jobid=jobid,
                    outfile=outfile,
                    nnodes=ctx.args.iterations,
                    onsuccess=onsuccess_parse_log,
                )
        else:
            self._run_bash(ctx, cmd.format(bench=qjoin(benchmarks)), teeout=True)

    def _run_bash(
        self,
        ctx: Context,
        command: str,
        pool: Optional[Pool] = None,
        onsuccess: Optional[Callable] = None,
        **kwargs: Any,
    ) -> None:
        config_root = os.path.dirname(os.path.abspath(__file__))
        cmd = [
            "bash",
            "-c",
            "\n" + _unindent(f"""
            cd {self._install_path(ctx)}
            source shrc
            source "{config_root}/scripts/kill-tree-on-interrupt.inc"
            {command}
            """),
        ]
        if pool:
            pool.run(ctx, cmd, onsuccess=onsuccess, **kwargs)
        else:
            assert onsuccess is None, "onsuccess not supported without pool"
            run(ctx, cmd, **kwargs)

    def _make_spec_config(self, ctx: Context, instance: Instance) -> str:
        config_name = "infra-" + instance.name
        config_path = self._install_path(ctx, f"config/{config_name}.cfg")
        ctx.log.debug("writing SPEC2017 config to " + config_path)

        with open(config_path, "w") as f:
            with redirect_stdout(f):
                print(f"#--------- Global Settings -----------")
                print(f"label                 = {config_name}")
                print(f"makeflags             = -j{ctx.jobs}")
                print(f"reportable            = no")
                print(f"strict_rundir_verify  = no")
                print(f"teeout                = yes")
                print(f"tune                  = base")
                print(f"")

                print(f"#--------- How Many CPUs? ------------")
                print(f"intrate,fprate:")
                print(f"   copies            = 1")
                print(f"intspeed,fpspeed:")
                print(f"   threads           = 1")
                print(f"")

                print(f"#--------- Compilers -----------------")
                print(f"default:")
                print(f"    CC                 = {ctx.cc} {qjoin(ctx.cflags)}")
                print(f"    CXX                = {ctx.cxx} {qjoin(ctx.cxxflags)}")
                print(f"    FC                 = {ctx.fc} {qjoin(ctx.fcflags)}")
                print(f"    CLD                = {ctx.cc} {qjoin(ctx.ldflags)}")
                print(f"    CXXLD              = {ctx.cxx} {qjoin(ctx.ldflags)}")
                print(f"    COPTIMIZE          = -std=c99")
                print(f"    CXXOPTIMIZE        = -std=c++03")
                print(f"    CC_VERSION_OPTION  = --version")
                print(f"    CXX_VERSION_OPTION = --version")
                print(f"    FC_VERSION_OPTION  = --version")
                print(f"")

                print(f"#--------- Portability -----------------")
                print(f"default:")
                print(f"     EXTRA_PORTABILITY = -DSPEC_LP64")
                print(f"")

                benchmark_flags = {
                    "500.perlbench_r,600.perlbench_s": {
                        "PORTABILITY": ["-DSPEC_LINUX_X64"],
                    },
                    "523.xalancbmk_r,623.xalancbmk_s": {
                        "PORTABILITY": ["-DSPEC_LINUX"],
                    },
                    "502.gcc_r,602.gcc_s=peak": {
                        "LDOPTIMIZE": ["-z", "muldefs"],
                    },
                    # Baseline Tuning Flags
                    # 'default=base': {
                    #     'OPTIMIZE': ['-flto', '-g', '%{olevel}', '-march=native'],
                    # },
                    "intrate,intspeed": {
                        "LDCFLAGS": ["-z", "muldefs"],
                    },
                }

                # if 'benchmark_flags' in ctx:
                #    for benchmark, flags in ctx.benchmark_flags.items():
                #        if benchmark not in benchmark_flags:
                #            benchmark_flags[benchmark] = {}
                #        for flag, value in flags.items():
                #            if flag not in benchmark_flags[benchmark]:
                #                benchmark_flags[benchmark][flag] = []
                #            benchmark_flags[benchmark][flag].extend(value)

                for benchmark, flags in benchmark_flags.items():
                    print(f"{benchmark}:")
                    for flag, value in flags.items():
                        if flag == "extra_lines":
                            for line in value:
                                print(line)
                        else:
                            print(f"{flag}   = {qjoin(value)}")
                    print("")

        return config_name

    def run_hooks_pre_build(self, ctx: Context, instance: Instance) -> None:
        if ctx.hooks.pre_build:
            for bench in self._get_benchmarks(ctx, instance):
                path = self._install_path(ctx, "benchspec", "CPU2006", bench)
                os.chdir(path)
                for hook in ctx.hooks.pre_build:
                    ctx.log.info(f"Running hook {hook} on {bench} in {path}")
                    hook(ctx, path)

    # override post-build hook runner rather than defining `binary_paths` since
    # we add hooks to the generated SPEC config file and call them through the
    # exec-hook setup command instead
    def run_hooks_post_build(self, ctx: Context, instance: Instance) -> None:
        pass

    def _get_benchmarks(self, ctx: Context, instance: Instance) -> Iterable[str]:
        benchmarks = set()
        for bset in ctx.args.benchmarks:
            for bench in self.benchmarks[bset]:
                if not hasattr(instance, "exclude_spec2017_benchmark") or not getattr(
                    instance, "exclude_spec2017_benchmark"
                )(bench):
                    benchmarks.add(bench)
        return sorted(benchmarks)

    # define benchmark sets, generated using scripts/parse-benchmarks-sets.py
    benchmarks = benchmark_sets

    def parse_outfile(self, ctx: Context, outfile: str) -> Iterator[ResultDict]:
        def fix_specpath(path: str) -> str:
            if not os.path.exists(path):
                benchspec_dir = self._install_path(ctx, "benchspec")
                path = re.sub(r".*/benchspec", benchspec_dir, path)
            assert os.path.exists(path), "invalid path " + path
            return path

        def get_logpaths(contents: str) -> Iterator[str]:
            matches = re.findall(r"The log for this run is in (.*)$", contents, re.M)
            for match in matches:
                logpath = match.replace("The log for this run is in ", "")
                yield logpath

        def parse_logfile(logpath: str) -> Iterator[Dict[str, Any]]:
            ctx.log.debug("parsing log file " + logpath)

            with open(logpath) as f:
                logcontents = f.read()

            m = re.match(r'^runcpu .+ started at .+ on "(.*)"', logcontents)
            assert m, "could not find hostname"
            hostname = m.group(1)

            m = re.search(r"^Benchmarks selected: (.+)$", logcontents, re.M)
            assert m, "could not find benchmark list"
            error_benchmarks = set(m.group(1).split(", "))

            pat = re.compile(
                r"([^ ]+) ([^ ]+) base (\w+) ratio=(-?[0-9.]+), "
                r"runtime=([0-9.]+).*",
                re.M,
            )
            m = pat.search(logcontents)
            while m:
                status, benchmark, workload, ratio, runtime = m.groups()
                runtime_results: Dict[str, Union[int, float]] = defaultdict(int)

                # find per-input logs by benchutils staticlib
                rpat = r"Running %s.+?-C (.+?$)(.+?)^Specinvoke:" % benchmark
                match = re.search(rpat, logcontents, re.M | re.S)
                assert match is not None
                rundir, arglist = match.groups()
                errfiles = re.findall(r"-e ([^ ]+err) \.\./run_", arglist)
                benchmark_error = False
                for errfile in errfiles:
                    path = os.path.join(fix_specpath(rundir), errfile)
                    if not os.path.exists(path):
                        ctx.log.error(
                            f"missing errfile {path}, there was probably an error"
                        )
                        benchmark_error = True
                        continue

                    for reporter in self.reporters:
                        for counter, value in reporter.parse_results(ctx, path).items():
                            assert isinstance(value, (int, float))
                            runtime_results[counter] += value

                if benchmark_error:
                    ctx.log.warning(
                        f"cancel processing benchmark {benchmark} in log "
                        f"file {logpath} because of errors"
                    )
                else:
                    yield {
                        "benchmark": benchmark,
                        "status": "ok" if status == "Success" else "invalid",
                        "workload": workload,
                        "hostname": hostname,
                        "runtime": float(runtime),
                        "inputs": len(errfiles),
                        **runtime_results,
                    }
                    error_benchmarks.remove(benchmark)

                m = pat.search(logcontents, m.end())

            for benchmark in error_benchmarks:
                yield {
                    "benchmark": benchmark,
                    "status": "error",
                    "hostname": hostname,
                }

            ctx.log.debug("done parsing")

        with open(outfile) as f:
            outfile_contents = f.read()

        logpaths = list(get_logpaths(outfile_contents))
        if logpaths:
            for logpath in logpaths:
                yield from parse_logfile(logpath)
        else:
            yield {
                "benchmark": re.sub(r"\.\d+$", "", os.path.basename(outfile)),
                "status": "timeout",
            }

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in SPEC benchmarks.
    custom_allocs_flags: Sequence[str] = []


def _unindent(cmd: str) -> str:
    stripped = re.sub(r"^\n|\n *$", "", cmd)
    indent = re.search("^ +", stripped, re.M)
    if indent:
        return re.sub(r"^" + indent.group(0), "", stripped, 0, re.M)
    return stripped
