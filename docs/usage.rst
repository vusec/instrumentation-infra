=====
Usage
=====

**instrumentation-infra** is meant to be used as a submodule in a git
repository. To use it, you must create a `setup script`. The setup script
specifies which targets and instances are used by the current project, including
any custom targets and instances. An example can be found in our skeleton
repository `here
<https://github.com/vusec/instrumentation-skeleton/blob/master/setup.py>`_. The
setup script (which we will call ``setup.py`` from now on) is an executable
Python script that calls :func:`Setup.main() <infra.Setup.main>`. The script has
a number of subcommands of which the basic usage is discussed below. Each
subcommand has an extensive ``--help`` option that shows all of its knobs and
bells.


Installing dependencies
=======================

The infrastructure's only hard dependency is Python 3.5. If you intend to use
LLVM, however, there are some build dependencies. This is what you need for
LLVM on a fresh Ubuntu 16.04 installation::

    sudo apt-get install bison build-essential gettext git pkg-config python ssh

For nicer command-line usage, install the following Python packages (optional)::

    pip3 install --user coloredlogs argcomplete

``argcomplete`` enables command-line argument completion, but it needs to be
activated first (optional)::

    sudo activate-global-python-argcomplete --complete-arguments -o nospace -o default
    # OR, in user space (add to ~/.bashrc):
    eval "$(register-python-argcomplete setup.py --complete-arguments -o nospace -o default)"


Cloning the framework in your project
=====================================

First add the infrastructure as a git submodule. This creates a ``.gitmodules``
file that you should commit::

    git submodule add infra git@github.com:vusec/instrumentation-infra.git
    git add instrumentation-infra .gitmodules  # implicit on recent git versions
    git commit -m "Clone instrumentation infrastructure"

Next, create a setup script (recommended name ``setup.py``) in your project root
that invokes the infrastructure's main function.  Consult the `skeleton example
<https://github.com/vusec/instrumentation-skeleton/blob/master/setup.py>`_ and
:class:`API docs <infra.Setup>` for this step.

Finally, write any :class:`target <infra.Target>`, :class:`instance
<infra.Instance>` and :class:`package <infra.Package>` definitions needed or
your project so that you can use them in the commands below.


.. _usage-build:

The ``build`` command
=====================

::

    ./setup.py build [-j JOBS] --targets TARGET ... --instances INSTANCE ...
    ./setup.py build [-j JOBS] --packages PACKAGE ...

``build`` builds one or more target programs, using one or more instances. Only
registered targets/instances are valid. The :class:`API docs <infra.Setup>`
explain how to register them. Each target and instance specifies which packages
it depends on. For example, an instance that runs LLVM passes depends on LLVM,
which in turn depends on some libraries depending on the version used. Before
building a target programs, ``build`` lists its dependencies, downloads and
builds them, and adds their installation directories to the PATH. All generated
build files are put in a directory called ``build/`` in the root of your
project.

Each package specifies a simple test for the setup script to see if it has
already been built (e.g., it checks if ``install/bin/<binary>`` exists). If so,
the build is skipped. This avoids having to run ``make`` all the time for each
dependency, but sometimes you do want to force-run ``make``, for example while
debugging a custom package, or when you hackfixed the source code of a package.
In this case, you can use ``--force-rebuild-deps`` to skip the checks and
rebuild everything, and optionally ``--clean`` to first remove all generated
files for all built packages and targets (this behaves as if you just cloned the
project, use it with care). You can also force-build a single package using
``--packages`` for more fine-grained control.

The ``-j`` option is similar to that of ``make``, allowing parallel builds of
object files. It defaults to the number of cores available on the machine, with
a maximum of 16 (but you can manually set it to larger values if you think
enough RAM is available).


.. _usage-clean:

The ``clean`` command
=====================

::

    ./setup.py clean [--targets TARGET ...] [--packages PACKAGE ...]

``clean`` removes all generated files for a target program or package. This is
the opposite of ``build``. You can overwrite the behavior for your own targets
and packages (see the :func:`API docs <infra.Package.clean>`), but by default it
removes the entire ``build/{targets,packages}/<name>`` directory.

``clean`` is particularly useful for cleaning build files of a custom package,
such as a runtime library with source code embedded in your project, before
running ``build`` on a target that depends on the runtime library.


.. _usage-run:

The ``run`` command
===================

::

    ./setup.py run [--build] [--iterations=N] TARGET INSTANCE ... [<target-options>]

``run`` runs one or more instances of a single target program. When ``--build``
is passed, it first runs the ``build`` command for that target. Valid values for
``<target-options>`` differ per target, the :func:`API docs
<infra.Target.add_run_args>` explain how to add options for your own targets.

The example below builds and runs the test workload of `401.bzip2` from the
SPEC2006 suite, both compiled with Clang but with link-time optimizations
disabled and disabled respectively::

    ./setup.py run --build spec2006 clang clang-LTO --test --benchmarks 401.bzip2

The ``--iterations`` option specifies the number of times to run the target, to
be able to compute a median and standard deviation for the runtime.


Parallel builds and runs
========================

``build`` and ``run`` both have the ``--parallel`` option that divides the
workload over multiple cores or machines. The amount of parallelism is
controlled with ``--parallelmax=N``. There are two types:

- ``--parallel=proc`` spawns jobs as processes on the current machine. ``N`` is
  the number of parallel processes running at any given time, and defaults to
  the number of cores. This is particularly useful for local development of
  link-time passes where single-threaded linking is the bottleneck. Do use this
  in conjunction with ``-j`` to limit the amount of forked processes per job.

- ``--parallel=prun`` schedules jobs as ``prun`` jobs on different machines on
  the `DAS-5 cluster <https://www.cs.vu.nl/das5/jobs.shtml>`_. Here ``N``
  indicates the maximum number of node reservations of simultaneously scheduled
  jobs (both running and pending), defaulting to 64 (tailored to the VU
  cluster).  Additional options such as job time can be passed directly to
  ``prun`` using ``--prun-opts``.

The example below builds and runs the C/C++ subset of SPEC2006 with the test
workload, in order to test if the ``myinst`` instance breaks anything. The
machine has 8 cores, so we limit the number of parallel program builds to 8
(which is also the default) and limit the number of build processes per program
using ``-j 2`` to avoid excessive context switching::

    ./setup.py run --build --parallel proc --parallelmax 8 -j 2 \
        spec2006 myinst --test --benchmarks all_c all_cpp


.. _usage-config:

The ``config`` command
======================

::

    ./setup.py config --targets
    ./setup.py config --instances
    ./setup.py config --packages

``config`` prints information about the setup configuration, such as the
registered targets, instances and packages (the union of all registered
dependencies).


.. _usage-pkg-config:

The ``pkg-config`` command
==========================

::

    ./setup.py pkg-config PACKAGE <package-options>

``pkg-config`` prints information about a single package, such as its
installation prefix or, in the case of a library package, the CFLAGS needed to
compile a program that uses the library. Each package can define its own options
here (see :func:`API docs <infra.Package.pkg_config_options>`), but there are
two defaults:

- ``--root`` returns ``build/packages/<package>``.
- ``--prefix`` returns ``build/packages/<package>/install``.

``pkg-config`` is intended to be used build systems of targets that need to call
into the setup script from a different process than the ``./setup.py build ...``
invocation. For example, our skeleton repository uses this to make the `Makefile
<https://github.com/vusec/instrumentation-skeleton/blob/master/llvm-passes/Makefile>`_
for its LLVM passes stand-alone, allowing developers to run ``make`` directly in
the ``llvm-passes/`` directory rather than ``../setup.py build --packages llvm-passes-skeleton``.
