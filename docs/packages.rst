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


Tools
=====

.. autoclass:: infra.packages.Nothp
