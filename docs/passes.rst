====================
Built-in LLVM passes
====================

The framework features a number of useful analysis/transformation passes that
you can use in your own instances/passes. The passes are listed below, with the
supported LLVM versions in parentheses.


Transform passes
================

``-dump-ir`` (3.8.0/4.0.0): Dumps the current module IR of the program that is
being linked in human-readable bitcode file with the ".ll" extension. Prints the
location of the created file to ``stderr``. Optionally, the target filename can
be set by calling ``DEBUG_MODULE_NAME("myname");`` after including
``dump-ir-helper.h`` from the built-in passes.

``-custominline`` (3.8.0/4.0.0): Custom inliner for helper functions from
statically linked runtime libraries. Inlines calls to functions that have
``__attribute__((always_inline))`` and functions whose name starts with
``__noinstrument__inline_``.

``-defer-global-init`` (3.8.0): Changes all global initializers to
zero-initializers and adds a global constructor function that initializes the
globals instead. In combination with ``-expand-const-global-uses``, this is
useful for instrumenting globals without having to deal with constant
expressions (but only with instructions).

``-expand-const-global-uses`` (3.8.0): Expands all uses of constant expressions
(``ConstExpr``) in functions to equivalent instructions. This limts edge cases
during instrumentation, and can be undone with ``-instcombine``.

TODO: Combine ``-defer-global-init`` and ``-expand-const-global-uses`` into a
single ``-expand-constexprs`` pass that expands *all* constant expressions to
instructions.


Analysis passes
===============

``-sizeof-types`` (3.8.0): Finds allocated types for calls to ``malloc`` based
on ``sizeof`` expression in the source code. Must be used in conjunction with
the accompanying compiler wrapper and compile-time pass. See `header file
<https://github.com/vusec/instrumentation-infra/blob/master/llvm-passes/3.8.0/SizeofTypes.h>`_
for usage.


Utility headers
===============

Utilities to be used in custom LLVM pass implementations. These require
``use_builtins=True`` to be passed to :class:`infra.packages.LLVM`. See the
`source code
<https://github.com/vusec/instrumentation-infra/blob/master/llvm-passes/3.8.0/include/builtin>`_
for a complete reference.

``builtin/Common.h`` (3.8.0/4.0.0): Includes a bunch of much-used LLVM headers and
defines some helper functions.

``builtin/Allocation.h`` (3.8.0/4.0.0): Hlpers to populate an ``AllocationSite``
struct with standardized information about any stack/heap allocations.

TODO: rewrite ``builtin/Allocation.h`` to an ``-allocs`` analysis pass.

``builtin/CustomFunctionPass.h`` (3.8.0/4.0.0): Defines the ``CustomFunctionPass``
class which serves as a drop-in replacement for LLVM's ``FunctionPass``, but
really is a ``ModulePass``. This is necessary because the link-time passes
plugin does not support function passes because of things and reasons.
