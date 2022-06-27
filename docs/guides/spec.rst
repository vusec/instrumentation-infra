SPEC CPU benchmarking
=====================

SPEC benchmarking 101
---------------------

The SPEC CPU benchmarking suites contain a number of C, C++ and Fortran
benchmarks. Each benchmark is based on an existing, real-world, program (e.g.,
the Perl interpreter, the GCC compiler, etc), and has different characteristics.
Some programs might be very CPU/FPU intensive, some might be memory intensive,
and so on. It is widely used for paper evaluations because of this.

The latest version is SPEC CPU2017, although SPEC CPU2006 is also still in wide
use (partly to compare against older systems and papers). SPEC CPU2000 has
mostly fallen out of use except for comparing against very old papers, and
CPU95/CPU92 are not used at all anymore. The infra currently supports SPEC
CPU2006 and CPU2017. The concepts are mostly the same between these two, most
information here is applicable to both versions unless otherwise stated. This
guide will refer to both as "SPEC" for convenience.

Benchmarks in each SPEC version are often grouped in several (overlapping) sets.
For example, CPU2006 has the CINT and CFP sets (for integer and floating-point
respectively), but also had sets like all_c, all_cpp, all_fortan and all_mixed
(grouping the benchmark per language). When running and reporting SPEC results,
you should pick a suitable/established set, and you should **not** cherry-pick
or leave out certain benchmarks. Typically, you'll want to run the full suite,
although running only CINT or CFP is acceptable in some cases. However, Fortran
support is currently still lacking in compilers such as LLVM. Therefore, most
papers omit (pure or mixed) Fortran benchmarks. For CPU2006, running all C and
C++ benchmarks (19 in total) is the most common configuration, and the default
for the infra.

Adding to the infra
^^^^^^^^^^^^^^^^^^^

While the infra contains SPEC :ref:`targets <targets-spec>`, it does *not*
include SPEC itself, as it is a commercial product that we are not allowed to
redistribute. Therefore, step one is to acquire a copy of SPEC and point the
infra to this.

 .. note::

    If you are a student in VUSec, you should contact your supervisor for
    access to a copy of SPEC.

The infra supports several different formats of the SPEC installation: the raw
.iso file, an extracted version of the .iso file, or a manually installed
version. A single SPEC installation can be used between different infra
instances, and generally has the preference::

    mkdir speciso
    sudo mount -o loop spec2006.iso speciso
    cd speciso
    ./install.sh -f -d /path/to/install/spec2006  # E.g., /home/$USER/spec2006

Then, open setup.py and add at bottom (but before setup.main())::

    setup.add_target(infra.targets.SPEC2006(
        source = '/path/to/spec',
        source_type = 'installed'
    ))

If you use any other type of ``source_type`` (``isofile``, ``mounted``,
``tarfile``, ``git``), the infra will install spec for you inside its own build
directory.

Building and running
^^^^^^^^^^^^^^^^^^^^

You can :ref:`build <usage-build>` and :ref:`run <usage-run>` SPEC in the infra
like any other target, e.g.::

    ./setup.py run spec2006 baseline deltapointers --build

However, some special flags that are relevant here:

 - ``--benchmark BENCHMARK[,BENCHMARK]``
    This option allows you to run only a subset of the benchmarks. This option
    is especially useful when testing or debugging a single benchmark. E.g.,
    ``--benchmark 400.perlbench``
 - ``--test``
    SPEC comes with multiple "input sets" -- inputs that are fed into the
    aforementioned benchmark programs. By default it uses the "*ref*" set, which
    are pretty big and run for a long time. With the ``--test``  option it
    instead uses the "*test*" input set, which consist of smaller inputs, so all
    of SPEC can run within a minute. This **cannot be used for benchmarks**, but
    is useful for checking if everything is OK before starting a full *ref* run.
    Note that a system might work on one input set fine, but not on the other,
    because one input set might stress different part of the programs. One
    common example is the *test* set of ``400.perlbench``, which is the only
    SPEC benchmark that executes a ``fork()``.
 - ``--iterations=N``
    To reduce noise when benchmarking, you want to do multiple runs of each
    benchmark to take the median runtime. On most systems 3 or 5 runs are
    sufficient, but if high standard deviations are observed more are required.
 - ``--parallel=proc --parallelmax=1``
    By passing ``--parallel=<something>`` the infra will produce/process the
    output of SPEC. Here, ``--parallel=proc`` means run it as processes on the
    local machine (instead of distributing the jobs over a cluster or remote
    machines). The ``--parallelmax=1`` means only one benchmark runs at a time,
    so they don't interfere with each other. For testing runs, where you don't
    care about measuring performance, you can set ``--parallelmax`` to your CPU
    count for example.

So overall, for running full spec and measure overhead, you'd use::

    ./setup.py run spec2006 baseline --iterations=3 --parallel=proc --parallelmax=1

This will produce a new directory in the ``results/`` directory. To keep track
of different runs, it's convenient to rename these directories manually after
it's done (e.g., from ``results/run-2020-04-16.10-15-55`` to
``results/baseline``).

.. note::

    You need to pass the ``--parallel=proc`` argument to actually generate
    results that can be reported.

Parsing the results
^^^^^^^^^^^^^^^^^^^

The infra can produce tables of the results for you with the normal
:ref:`report <usage-report>` command::

    ./setup.py report spec2006 results/baseline -f runtime:median:stdev_percent

The thing at the end means "give me the median and standard deviation of the
runtimes per benchmark". You can similarly do ``-f maxrss:median`` to print the
memory overhead. You can give it multiple result directories. If you pass in
``--overhead baseline`` it will calculate everything as normalized overheads
relative to the baseline instance.


SPEC CPU2017
------------

SPEC CPU2017 comes with two distinct sets of benchmarks: the *speed* and the
*rate* suites. The *speed* set is similar to older versions of SPEC, where a
single benchmark is started and its execution time is measured. The new *rate*
metric, on the other hand, launches multiple binaries at the same time (matching
the number of CPU cores) and measures throughput. More information is available
in the `SPEC documentation
<https://www.spec.org/cpu2017/Docs/overview.html#Q15>`__. Each of these to sets
as its own list of benchmark programs: *speed* benchmarks start with ``6xx``,
whereas *rate* benchmarks start with ``5xx``.

Typically we only use the *speed* set for our papers.

Running on a cluster
--------------------

.. note::

    The following information is specific to the `DAS
    <https://www.cs.vu.nl/das/>`__ clusters offered by dutch universities,
    although it can be used on any cluster that uses ``prun`` to issue jobs to
    nodes. The DAS clusters can generally be used by any (BSc, MSc or PhD)
    student at the VU, LU, UvA, and TUD.

On a cluster, it is possible to run multiple SPEC benchmarks in parallel for
much faster end-to-end benchmarking. The infra has full support for clusters
that utilize the ``prun`` command to issue jobs, as described on the :ref:`usage
page <usage-parallel>`. For running SPEC we recommend the `DAS-5
<https://www.cs.vu.nl/das5/>`__ over the `DAS-6 <https://www.cs.vu.nl/das/>`__
cluster, as it features more nodes (instead of fewer more powerful nodes).

You will first need to `request an account
<https://www.cs.vu.nl/das5/accounts.shtml>`__. When doing so as a student, you
should mention your supervisor.

Some additional notes on using the DAS cluster:

 - Your homedir is limited is space, so use ``/var/scratch/$USER`` instead (for
   both the infra and the spec install dir).
 - Use ``--parallel=prun``. You can omit ``--parallelmax``, since defaults to 64
   to match DAS-5 cluster.
 - By default jobs are killed after 15min. This is usually fine (baseline
   longest benchmark, ``464.h264ref``, takes 8 minutes) but if you have a super
   slow defense it might exceed it. For those cases, you can *outside office
   hours* use ``--prun-opts="-asocial -t 30:00"``
 - The results on the DAS-5 are much noisier since we cannot control things like
   CPU frequency scaling. Therefore you should do 11 iterations (instead of 5)
   and take median. Do also take note of the stddev: if that's crazy high it
   might indicate some defective nodes. Contact the DAS sysadmin or your
   supervisor if that's becoming a serious problem, since a reboot fixes these
   issues. Note that we have scripts to find these defective nodes based on
   benchmarking results.

So overall, in most cases you'd simply use something like::

    ./setup.py run spec2006 baseline asan --iterations=11 --parallel=prun



Debugging
---------

When debugging issues with a particular instance, it is often required to run a
SPEC benchmark under a debugger such as GDB. The infra itself launches spec
benchmarks via the ``specrun`` command, which in turn invokes the binary of the
particular benchmark several times with different command line arguments. For
example, ``400.perlbench`` runs the perl binary several times with different
perl scripts. In this example we use ``400.perlbench`` from CPU2006, but this
procedure is the same for any benchmarks of any SPEC version.

To run one of these tests manually with gdb, we bypass both the infra and
``specrun``. To determine the correct command line arguments for the benchmark
(and to set up the relevant input files), the first step is to run the
particular benchmark via the infra normally (see above). This will set up the
correct run directory, for example in
``$SPEC/benchspec/CPU2006/400.perlbench/run/run_base_ref_infra-baseline.0000``,
where the last directory name depends on the instance (here ``baseline``) and
input set (``ref`` or ``test``).

Inside this directory should be a ``speccmds.cmd`` file, which contains the run
environment and arguments of the binary, and is normally parsed by
``specinvoke``. Lines starting with ``-E`` and ``-C`` define the environment
variables and working directory, respectively, and can be ignored. The lines
starting with ``-o`` define the actual runs of the binary, and might for example
look like::

    -o checkspam.2500.5.25.11.150.1.1.1.1.out -e checkspam.2500.5.25.11.150.1.1.1.1.err ../run_base_ref_infra-baseline.0000/perlbench_base.infra-baseline -I./lib checkspam.pl 2500 5 25 11 150 1 1 1 1

The first two bits (``-o`` and ``-e``) tell ``specinvoke`` where to redirect
stdout/stderr, and we don't need. Then comes the binary (including a relative
path into the current directory), and thus is ``perlbench_base.infra-baseline``
in our case. After that follow all actual arguments, which we need to pass.

If we want to run this under gdb, we can thus call is as follows::

    gdb --args perlbench_base.infra-baseline -I./lib checkspam.pl 2500 5 25 11 150 1 1 1 1
