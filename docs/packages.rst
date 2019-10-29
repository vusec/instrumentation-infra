=================
Built-in packages
=================

LLVM
====

.. autoclass:: infra.packages.LLVM
   :members: configure, add_plugin_flags

Dependencies
------------
.. autoclass:: infra.packages.AutoConf
.. autoclass:: infra.packages.AutoMake
   :members: default
.. autoclass:: infra.packages.Bash
.. autoclass:: infra.packages.BinUtils
.. autoclass:: infra.packages.CMake
.. autoclass:: infra.packages.CoreUtils
.. autoclass:: infra.packages.LibElf
.. autoclass:: infra.packages.LibTool
.. autoclass:: infra.packages.M4
.. autoclass:: infra.packages.Make
.. autoclass:: infra.packages.Ninja


LLVM passes
===========

.. autoclass:: infra.packages.LLVMPasses
   :members: configure, runtime_cflags
.. autoclass:: infra.packages.BuiltinLLVMPasses


Address space shrinking
=======================

.. autoclass:: infra.packages.LibShrink
   :members: configure, run_wrapper

Dependencies
------------
.. autoclass:: infra.packages.PatchElf
.. autoclass:: infra.packages.Prelink
.. autoclass:: infra.packages.PyElfTools


TCmalloc
========

.. autoclass:: infra.packages.Gperftools
   :members: configure

Dependencies
------------
.. autoclass:: infra.packages.LibUnwind


Tools
=====

.. autoclass:: infra.packages.Nothp
.. autoclass:: infra.packages.BenchmarkUtils
   :members:


Apache benchmark (ab)
=====================

.. autoclass:: infra.packages.ApacheBench

Dependencies
------------
.. autoclass:: infra.packages.APR
.. autoclass:: infra.packages.APRUtil


Wrk benchmark
=============

.. autoclass:: infra.packages.Wrk
.. autoclass:: infra.packages.Wrk2


Scons
=====

.. autoclass:: infra.packages.Scons
