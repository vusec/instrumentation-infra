=====
About
=====

**instrumentation-infra** is an infrastructure for program instrumentation. It
builds benchmark programs with custom instrumentation flags (e.g., LLVM passes)
and runs them. The design is modular, so it is designed to be extended by users.


Overview
========

The infrastructure uses three high-level concepts to specify benchmarks and
build flags:

1. A *target* is a benchmark program (or a collection of programs) that is to be
   instrumented. An example is :class:`SPEC-CPU2006 <infra.targets.SPEC2006>`.

2. An *instance* specifies how to build a target. An example is
   :class:`infra.instances.Clang` which builds targets using the Clang compiler.
   For SPEC2006, one of the resulting binaries would be called
   ``400.perlbench-clang``.

3. Targets and instances can specify dependencies in the form of *packages*,
   which are built automatically before the target is built.

The infrastructure provides a number of common targets and their dependencies as
packages. It also defines baseline instances for LLVM, along with packages for
its build dependencies. There are some utility passes and a source patch for
LLVM that lets you develop instrumentation passes in a shared object, without
having to link them into the compiler after every rebuild.

A typical use case is a programmer that has implemented some security feature in
an LLVM pass, and wants to apply this pass to real-world benchmarks to measure
its performance impact. He/she would create an instance that adds the relevant
arguments to CFLAGS, create a setup script that registers this instance in the
infrastructure, and run the setup script with the ``build`` and ``run`` commands
to quickly see if things work on the builtin targets (e.g., SPEC).


Getting started
===============

The easiest way to get started with the framework is to clone and adapt our
`skeleton repository <https://github.com/vusec/instrumentation-skeleton>`_ which
creates an example target and instrumentation instance. Consult the :doc:`API
docs <api>` for extensive documentation on the functions used. Read the
:doc:`usage guide <usage>` to find our how to set up your own project otherwise,
and for examples of how to invoke build and run commands.


.. toctree::
   :maxdepth: 2
   :hidden:

   self
   usage
   api
   targets
   instances
   packages
   passes
