import argparse
import getpass
import logging
import os
from pathlib import Path
import re
import shutil
from collections import defaultdict
from contextlib import redirect_stdout
from typing import Any, Iterable, Iterator, Mapping

from ...commands.report import outfile_path
from ...context import Context
from ...instance import Instance
from ...package import Package
from ...packages import Bash, Nothp, ReportableTool, RusageCounters
from ...parallel import Pool, PrunPool
from ...target import Target
from ...util import ResultDict, apply_patch, qjoin, require_program, run, untar
from .benchmark_sets import benchmark_sets


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

    @property
    def name(self) -> str:
        return "spec2006"

    @property
    def aggregation_field(self) -> str:
        return "benchmark"

    def __init__(
        self,
        source_type: str,
        source_path: Path | str,
        patches: list[str] = [],
        toolsets: list[str] = [],
        default_benchmarks: list[str] = ["all_c", "all_cpp"],
        reporters: list[ReportableTool | type[ReportableTool]] = [RusageCounters()],
        no_thp: bool = True,
        bind_cpu: int = -1,
    ) -> None:
        self.source_type = source_type
        self.source_path = Path(source_path)
        self.patches = patches
        self.toolsets = toolsets
        self.default_benchmarks = default_benchmarks
        self.reporters = reporters
        self.no_thp = no_thp
        self.bind_cpu = bind_cpu

        # Define benchmark sets, generated using scripts/parse-benchmarks-sets.py
        self.benchmarks = benchmark_sets

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
        )

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--benchmarks",
            nargs="+",
            metavar="BENCHMARK",
            default=self.default_benchmarks,
            choices=self.benchmarks,
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
        yield RusageCounters()
        if self.no_thp:
            yield Nothp()

    def root_dir(self, ctx: Context, *args: str | Path) -> Path:
        return Path(self.path(ctx)).joinpath(*args)

    def mount_dir(self, ctx: Context, *args: str | Path) -> Path:
        return self.root_dir(ctx, "mount").joinpath(*args)

    def source_dir(self, ctx: Context, *args: str | Path) -> Path:
        return self.root_dir(ctx, "src").joinpath(*args)

    def install_dir(self, ctx: Context, *args: str | Path) -> Path:
        return (
            self.root_dir(ctx, "install").joinpath(*args)
            if self.source_type != "installed"
            else self.source_path.joinpath(*args)
        )

    def config_dir(self, ctx: Context, *args: str | Path) -> Path:
        return self.install_dir(ctx, "config").joinpath(*args)

    def benchspec_dir(self, ctx: Context, *args: str | Path) -> Path:
        return self.install_dir(ctx, "benchspec").joinpath(*args)

    def benches_dir(self, ctx: Context, *args: str | Path) -> Path:
        return self.benchspec_dir(ctx, "CPU2006").joinpath(*args)

    def is_clean(self, ctx: Context) -> bool:
        if self.source_type == "installed":
            return True
        return Path(self.path(ctx)).is_dir() and any(Path(self.path(ctx)).iterdir())

    def clean(self, ctx: Context) -> None:
        match self.source_type:
            case "mounted":
                if Path(self.source_path).is_relative_to(self.path(ctx)):
                    raise RuntimeError(f"Mounted directory is subdirectory of target ({self.path(ctx)}); cannot clean!")
                shutil.rmtree(self.path(ctx), ignore_errors=True)
            case "installed":
                ctx.log.info(f"Cannot clean pre-installed SPEC2006 instance at {self.source_path}")
            case "git" | "remote":
                shutil.rmtree(self.path(ctx), ignore_errors=True)
            case "tar" | "tarfile" | "archive":
                if Path(self.source_path).is_relative_to(self.path(ctx)):
                    raise RuntimeError(f"Cleaning target would erase source archive: {self.source_path}!")
                shutil.rmtree(self.path(ctx), ignore_errors=True)
            case "iso" | "isofile" | "image":
                if Path(self.source_path).is_relative_to(self.path(ctx)):
                    raise RuntimeError(f"Cleaning target would erase source image: {self.source_path}!")
                shutil.rmtree(self.path(ctx), ignore_errors=True)
            case _:
                raise ValueError(f"Invalid {self.name} source type: {self.source_type}!")

    def is_fetched(self, ctx: Context) -> bool:
        return self.install_dir(ctx, "shrc").exists()

    def install_spec(self, ctx: Context, source_dir: Path, target_dir: Path) -> None:
        for toolset in self.toolsets:
            ctx.log.debug(f"Extracting SPEC2006 toolset: {toolset}")
            untar(ctx, toolset, remove=False)

        ctx.log.info(f"Installing SPEC2006 to {target_dir} (from source directory: {source_dir})")
        run(
            ctx,
            [source_dir / "install.sh", "-f", "-d", target_dir],
            env={"PERL_TEST_NUMCONVERTS": "1"},
            cwd=source_dir,
            teeout=ctx.loglevel <= logging.DEBUG,
        )

    def fetch(self, ctx: Context) -> None:
        match self.source_type:
            case "installed":
                raise FileNotFoundError(f"No 'shrc' in existing SPEC2006 installation at {self.source_path}!")

            case "git" | "remote":
                require_program(ctx, "git", "Cannot get SPEC2006 sources without git!")

                ls_remote = run(ctx, ["git", "ls-remote", self.source_path], allow_error=True)
                if ls_remote.returncode != 0:
                    raise RuntimeError(f"Could not read from git remote: {self.source_path}!")

                ctx.log.info(f"Cloning SPEC2006 sources into {self.source_dir(ctx)}")
                run(ctx, ["git", "clone", "--depth", 1, self.source_path, self.source_dir(ctx)])

                self.install_spec(ctx, self.source_dir(ctx), self.install_dir(ctx))

                ctx.log.info(f"Erasing SPEC2006 source files from {self.source_dir(ctx)} to save disk space")
                shutil.rmtree(self.source_dir(ctx), ignore_errors=True)

            case "mounted" | "extracted":
                if not self.source_path.exists() or not self.source_path.is_dir():
                    raise FileNotFoundError(f"Failed to find valid SPEC2006 mount/sources at {self.source_path}!")
                if not (self.source_path / "install.sh").exists():
                    raise FileNotFoundError(f"Failed to find install script in source/mount: {self.source_path}!")

                self.install_spec(ctx, self.source_path, self.install_dir(ctx))

            case "tar" | "tarfile" | "archive":
                untar(ctx, tarname=str(self.source_path), dest=str(self.source_dir(ctx)), remove=False)

                self.install_spec(ctx, self.source_dir(ctx), Path(self.install_dir(ctx)))

                ctx.log.info(f"Removing extract SPEC2006 source files at {self.source_dir(ctx)} to save disk space")
                shutil.rmtree(self.source_dir(ctx), ignore_errors=True)

            case "iso" | "isofile" | "image":
                require_program(ctx, "fuseiso", "'fuseiso' not found; cannot mount image without fuseiso!")
                require_program(ctx, "fusermount", "'fusermount' not found; cannot unmount image without fusermount!")

                if not self.source_path.exists():
                    raise FileNotFoundError(f"Can't find {self.source_path}; cannot mount to {self.mount_dir(ctx)}!")
                if not self.source_path.is_file():
                    raise RuntimeError(f"Cannot mount ISO file {self.source_path}; invalid file type!")

                if self.mount_dir(ctx).exists():
                    if self.mount_dir(ctx).is_mount():
                        ctx.log.info(f"Existing mount found at {self.mount_dir(ctx)}; unmounting...")
                        run(ctx, ["fusermount", "-u", self.mount_dir(ctx)])

                    ctx.log.info(f"Clearing existing objects at mount point: {self.mount_dir(ctx)}")
                    shutil.rmtree(self.mount_dir(ctx))

                # Ensure the mount point actually exists (requirement)
                os.makedirs(self.mount_dir(ctx))

                ctx.log.info(f"Mounting ISO image ({self.source_path}) to {self.mount_dir(ctx)}")
                run(ctx, ["fuseiso", self.source_path, self.mount_dir(ctx)])

                self.install_spec(ctx, self.mount_dir(ctx), Path(self.install_dir(ctx)))

                ctx.log.debug(f"Unmounting mount point: {self.mount_dir(ctx)}")
                run(ctx, ["fusermount", "-u", self.mount_dir(ctx)])
                shutil.rmtree(self.mount_dir(ctx))

            case _:
                raise ValueError(f"Invalid {self.name} source type: {self.source_type}!")

    def patch_spec(self, ctx: Context) -> None:
        """
        Applies any pending patches; done at build-time to allow patching SPEC without having to
        reinstall SPEC entirely (also allows for patching pre-installed SPEC instances).
        """
        ctx.log.debug(f"Patching SPEC installation at {self.install_dir(ctx)}")
        os.chdir(self.install_dir(ctx))

        for patch in self.patches:
            patch_path = Path(patch)
            if not patch_path.is_absolute():
                patch_path = Path(__file__).absolute().parent / patch_path

            ctx.log.debug(f"Applying patch at {patch_path}")
            if self.source_type == "installed":
                ctx.log.warning(f"Patching existing SPEC2006 installation ({self.source_path}) with {patch_path}")
            apply_patch(ctx, str(patch_path), 1)

    def init_build(self, ctx: Context, instance: Instance) -> str:
        """
        Performs general pre-build initialisation steps like configuring RusageCounters and such
        """
        # apply any pending patches (doing this at build time allows adding
        # patches during instance development, and is needed to apply patches
        # when self.source_type == 'installed')
        self.patch_spec(ctx)

        # add flags to compile with runtime support for benchmark utils
        RusageCounters().configure(ctx)

        # Create the SPEC configuration for this instance & return it
        return self._make_spec_config(ctx, instance)

    def build(self, ctx: Context, instance: Instance, pool: Pool | None = None) -> None:
        config = self.init_build(ctx, instance)

        os.chdir(self.install_dir(ctx))
        for bench in self._get_benchmarks(ctx, instance):
            cmd = f"killwrap_tree runspec --config={config} --action=build {bench}"
            if pool:
                jobid = f"build-{instance.name}-{bench}"
                outdir = os.path.join(ctx.paths.pool_results, "build", self.name, instance.name)
                os.makedirs(outdir, exist_ok=True)
                outfile = os.path.join(outdir, bench)
                self._run_bash(ctx, cmd, pool, jobid=jobid, outfile=outfile, nnodes=1)
            else:
                ctx.log.info(f"building {self.name}-{instance.name} {bench}")
                self._run_bash(ctx, cmd, teeout=ctx.loglevel == logging.DEBUG)

    def run(self, ctx: Context, instance: Instance, pool: Pool | None = None) -> None:
        config_name = f"infra-{instance.name}"
        config_file = self.config_dir(ctx, f"{config_name}.cfg")
        if not config_file.exists():
            raise FileNotFoundError(f"Config {config_file} not found; {self.name}-{instance.name} not built!")

        runargs = []

        if ctx.args.test:
            runargs += ["--size", "test"]

        # the pool scheduler will pass --iterations as -np to prun, so only run
        # one iteration in runspec
        runargs += ["--iterations", "1" if pool else str(ctx.args.iterations)]

        # set output root to local disk when using prun to avoid noise due to
        # network lag when writing output files
        specdir = str(self.install_dir(ctx))
        if isinstance(pool, PrunPool):
            output_root = f"/local/{getpass.getuser()}/cpu2006-output-root"
            runargs += ["--define", "output_root=" + output_root]
        else:
            output_root = specdir

        # apply wrapper in macro for monitor_wrapper in config
        if ctx.target_run_wrapper:
            runargs += ["--define", f"run_wrapper={ctx.target_run_wrapper}"]

        # don't stop running if one benchmark from the list crashes
        if not pool:
            runargs += ["--ignore_errors"]

        runargs += ctx.args.runspec_args

        wrapper = "killwrap_tree"
        if self.no_thp:
            wrapper += " nothp"
        if self.bind_cpu >= 0:
            wrapper += f" taskset -c {self.bind_cpu}"

        cmd = f"{wrapper} runspec --config={config_name} --nobuild {qjoin(runargs)} {{bench}}"

        benchmarks = self._get_benchmarks(ctx, instance)

        if pool:
            if isinstance(pool, PrunPool):
                # prepare output dir on local disk before running,
                # and move output files to network disk after completion
                cmd = _unindent(
                    f"""
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
                """
                )

                # the script is passed like this: prun ... bash -c '<script>'
                # this means that some escaping is necessary: use \$ instead of
                # $ for bash variables and \" instead of "
                cmd = cmd.replace("$", r"\$").replace('"', '\\"')

            for bench in benchmarks:
                jobid = f"run-{instance.name}-{bench}"
                outfile = outfile_path(ctx, self, instance, bench)
                self._run_bash(
                    ctx,
                    cmd.format(bench=bench),
                    pool,
                    jobid=jobid,
                    outfile=outfile,
                    nnodes=ctx.args.iterations,
                )
        else:
            self._run_bash(ctx, cmd.format(bench=qjoin(benchmarks)), teeout=True)

    def _run_bash(self, ctx: Context, command: str, pool: Pool | None = None, **kwargs: Any) -> None:
        config_root = Path(__file__).absolute().parent
        cmd = [
            "bash",
            "-c",
            "\n"
            + _unindent(
                f"""
            cd {self.install_dir(ctx)}
            source shrc
            source "{config_root}/scripts/kill-tree-on-interrupt.inc"
            {command}
            """
            ),
        ]
        if pool:
            pool.run(ctx, cmd, **kwargs)
        else:
            run(ctx, cmd, **kwargs)

    def _make_spec_config(self, ctx: Context, instance: Instance) -> str:
        config_name = f"infra-{instance.name}"
        config_path = self.config_dir(ctx, f"{config_name}.cfg")
        ctx.log.debug(f"Writing SPEC2006 config to {config_path}")

        with open(config_path, "w") as f:
            with redirect_stdout(f):
                print(f"tune        = base")
                print(f"ext         = {config_name}")
                print(f"reportable  = no")
                print(f"teeout      = yes")
                print(f"teerunout   = no")
                print(f"makeflags   = -j{ctx.jobs}")
                print(f"strict_rundir_verify = no")

                # allow different output root to be set using
                # --define output_root=...
                print(f"%ifdef %{{output_root}}")
                print(f"  output_root = %{{output_root}}")
                print(f"%endif")

                print(f"")
                print(f"default=default=default=default:")

                # see https://www.spec.org/cpu2006/Docs/makevars.html#nofbno1
                # for flags ordering
                print(f"CC          = {ctx.cc} {qjoin(ctx.cflags)}")
                print(f"CXX         = {ctx.cxx} {qjoin(ctx.cxxflags)}")
                print(f"FC          = {ctx.fc} {qjoin(ctx.fcflags)}")
                print(f"CLD         = {ctx.cc} {qjoin(ctx.ldflags)}")
                print(f"CXXLD       = {ctx.cxx} {qjoin(ctx.ldflags)}")
                print(f"COPTIMIZE   = -std=gnu89")
                print(f"CXXOPTIMIZE = -std=c++98")

                # post-build hooks call back into the setup script
                if ctx.hooks.post_build:
                    print(f"")
                    print(
                        f"build_post_bench = {ctx.paths.setup} exec-hook post-build "
                        f"{instance.name} `echo ${{commandexe}} "
                        f'| sed "s/_\\[a-z0-9\\]\\\\+\\\\.{config_name}\\\\\\$//"`'
                    )
                    print("")

                # allow run wrapper to be set using --define run_wrapper=...
                print(f"%ifdef %{{run_wrapper}}")
                print(f"  monitor_wrapper = %{{run_wrapper}} $command")
                print(f"%endif")

                # configure benchmarks for 64-bit Linux (hardcoded for now)
                print(f"")
                print(f"default=base=default=default:")
                print(f"PORTABILITY    = -DSPEC_CPU_LP64")
                print(f"")

                arch_perlbench_portability = {
                    "x86_64": "SPEC_CPU_LINUX_X64",
                    "aarch64": "SPEC_CPU_LINUX",  # Not officially supported
                    "arm64": "SPEC_CPU_LINUX",  # Not officially supported
                }
                if ctx.arch not in arch_perlbench_portability:
                    raise RuntimeError(
                        f"Architecture '{ctx.arch}' is not supported by SPEC06 target"
                        " currently; please consult the example configs, specify the"
                        " right arch_perlbench_portability, and add any additional"
                        " required changes."
                    )

                benchmark_flags = {
                    "400.perlbench=default=default=default": {"CPORTABILITY": [f"-D{arch_perlbench_portability[ctx.arch]}"]},
                    "403.gcc=default=default=default": {"CPORTABILITY": ["-DSPEC_CPU_LINUX"]},
                    "462.libquantum=default=default=default": {"CPORTABILITY": ["-DSPEC_CPU_LINUX"]},
                    "464.h264ref=default=default=default": {"CPORTABILITY": ["-fsigned-char"]},
                    "482.sphinx3=default=default=default": {"CPORTABILITY": ["-fsigned-char"]},
                    "483.xalancbmk=default=default=default": {"CXXPORTABILITY": ["-DSPEC_CPU_LINUX"]},
                    "481.wrf=default=default=default": {
                        "extra_lines": ["wrf_data_header_size = 8"],
                        "CPORTABILITY": ["-DSPEC_CPU_CASE_FLAG", "-DSPEC_CPU_LINUX"],
                    },
                }
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
                path = self.benches_dir(ctx, bench)
                os.chdir(path)
                for hook in ctx.hooks.pre_build:
                    ctx.log.info(f"Running hook {hook} on {bench} in {path}")
                    hook(ctx, str(path))

    # override post-build hook runner rather than defining `binary_paths` since
    # we add hooks to the generated SPEC config file and call them through the
    # exec-hook setup command instead
    def run_hooks_post_build(self, ctx: Context, instance: Instance) -> None:
        pass

    def _get_benchmarks(self, ctx: Context, instance: Instance) -> Iterable[str]:
        benchmarks = set()
        for bset in ctx.args.benchmarks:
            for bench in self.benchmarks[bset]:
                if not hasattr(instance, "exclude_spec2006_benchmark") or not getattr(instance, "exclude_spec2006_benchmark")(
                    bench
                ):
                    benchmarks.add(bench)
        return sorted(benchmarks)

    # define benchmark sets, generated using scripts/parse-benchmarks-sets.py
    benchmarks = benchmark_sets

    def parse_outfile(self, ctx: Context, outfile: str) -> Iterator[ResultDict]:
        def fix_specpath(path: str) -> str:
            if not os.path.exists(path):
                benchspec_dir = str(self.benchspec_dir(ctx))
                path = re.sub(r".*/benchspec", benchspec_dir, path)
            assert os.path.exists(path), "invalid path " + path
            return path

        def get_logpaths(contents: str) -> Iterator[str]:
            matches = re.findall(r"The log for this run is in (.*)$", contents, re.M)
            for match in matches:
                logpath = match.replace("The log for this run is in ", "")
                yield logpath

        def parse_logfile(logpath: str) -> Iterator[dict[str, Any]]:
            ctx.log.debug("parsing log file " + logpath)

            with open(logpath) as f:
                logcontents = f.read()

            m = re.match(r'^runspec .+ started at .+ on "(.*)"', logcontents)
            assert m, "could not find hostname"
            hostname = m.group(1)

            m = re.search(r"^Benchmarks selected: (.+)$", logcontents, re.M)
            assert m, "could not find benchmark list"
            error_benchmarks = set(m.group(1).split(", "))

            pat = re.compile(
                r"([^ ]+) ([^ ]+) base (\w+) ratio=(-?[0-9.]+), " r"runtime=([0-9.]+).*",
                re.M,
            )
            m = pat.search(logcontents)
            while m:
                status, benchmark, workload, ratio, runtime = m.groups()
                runtime_results: dict[str, int | float] = defaultdict(int)

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
                        ctx.log.error(f"missing errfile {path}, there was probably an error")
                        benchmark_error = True
                        continue

                    for reporter in self.reporters:
                        for counter, value in reporter.parse_results(ctx, path).items():
                            assert isinstance(value, (int, float))
                            runtime_results[counter] += value

                if benchmark_error:
                    ctx.log.warning(f"cancel processing benchmark {benchmark} in log " f"file {logpath} because of errors")
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
    custom_allocs_flags = [
        "-allocs-custom-funcs="
        + ".".join(
            (
                # 400.perlbench
                "Perl_safesysmalloc:malloc:0",
                "Perl_safesyscalloc:calloc:1:0",
                "Perl_safesysrealloc:realloc:1",
                "Perl_safesysfree:free:-1",
                # 403.gcc
                "ggc_alloc:malloc:0",
                "alloc_anon:malloc:1",
                "xmalloc:malloc:0",
                "xcalloc:calloc:1:0",
                "xrealloc:realloc:1",
            )
        )
    ]


def _unindent(cmd: str) -> str:
    stripped = re.sub(r"^\n|\n *$", "", cmd)
    indent = re.search("^ +", stripped, re.M)
    if indent:
        return re.sub(r"^" + indent.group(0), "", stripped, 0, re.M)
    return stripped
