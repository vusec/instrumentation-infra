import os
from typing import Any, Iterable, Iterator

from ...context import Context
from ...package import Package, PkgConfigOption
from ...util import FatalError, Process, run
from ..llvm import LLVM


class LLVMPasses(Package):
    """
    LLVM passes dependency. Use this to add your own passes as a dependency to
    your own instances. In your own passes directory, your Makefile should look
    like this (see the `skeleton
    <https://github.com/vusec/instrumentation-skeleton/blob/master/llvm-passes/Makefile>`_
    for an example)::

        BUILD_SUFFIX = <build_suffix>
        LLVM_VERSION = <llvm_version>
        SETUP_SCRIPT = <path_to_setup.py>
        SUBDIRS      = <optional list of subdir names containing passes>
        include <path_to_infra>/infra/packages/llvm_passes/Makefile

    The makefile can be run as-is using ``make`` in your passes directory
    during development, without invoking the setup script directly. It creates
    two shared objects in
    ``build/packages/llvm-passes-<build_suffix>/install``:

    - ``libpasses-gold.so``: used to load the passes at link time in Clang.
      This is the default usage.

    - ``libpasses-opt.so``: used to run the passes with LLVM's ``opt`` utility.
      Can be used in a customized build system or for debugging.

    The passes are invoked at link time by a patched LLVM gold plugin. The
    **gold-plugin** patch of the :class:`LLVM` package adds an option to load
    custom passes into the plugin. Passes are invoked by adding their
    registered names to the flags passed to the LLVM gold plugin by the linker.
    In other words, by adding ``-Wl,-plugin-opt=<passname>`` to ``ctx.ldflags``
    in the ``configure`` method of your instance. The
    :func:`LLVM.add_plugin_flags` helper does exactly that. Before using
    passes, you must call ``llvm_passes.configure(ctx)`` to load the passes
    into the plugin. See the `skeleton LibcallCount instance
    <https://github.com/vusec/instrumentation-skeleton/blob/master/setup.py>`_
    for an example.

    For the :ref:`pkg-config <usage-pkg-config>` command of this package, the
    ``--objdir`` option points to the build directory.

    :identifier: llvm-passes-<build_suffix>
    :param llvm: LLVM package to link against
    :param srcdir: source directory containing your LLVM passes
    :param build_suffix: identifier for this set of passes
    :param use_builtins: whether to include :doc:`built-in LLVM passes
                         <passes>` in the shared object
    :param debug: enable to compile passes with ``-O0 -ggdb``
    :todo: extend this to support compile-time plugins
    """

    def __init__(
        self,
        llvm: LLVM,
        srcdir: str,
        build_suffix: str,
        use_builtins: bool,
        debug: bool = False,
        gold_passes: bool = True,
    ):
        self.llvm = llvm
        self.custom_srcdir = os.path.abspath(srcdir)
        self.build_suffix = build_suffix
        self.builtin_passes = (
            BuiltinLLVMPasses(llvm, gold_passes=gold_passes) if use_builtins else None
        )
        self.debug = debug
        self.gold_passes = gold_passes

    def ident(self) -> str:
        suffix = "-gold" if self.gold_passes else ""
        return "llvm-passes-" + self.build_suffix + suffix

    def _srcdir(self, ctx: Context) -> str:
        if not os.path.exists(self.custom_srcdir):
            raise FatalError(f"llvm-passes dir '{self.custom_srcdir}' does not exist")
        return self.custom_srcdir

    def dependencies(self) -> Iterator[Package]:
        yield self.llvm
        yield self.llvm.binutils  # for ld.gold
        if self.builtin_passes:
            yield self.builtin_passes

    def fetch(self, ctx: Context) -> None:
        pass

    def build(self, ctx: Context) -> None:
        os.makedirs("obj", exist_ok=True)
        os.chdir(self._srcdir(ctx))
        self._run_make(ctx, f"-j{ctx.jobs}")

    def install(self, ctx: Context) -> None:
        os.chdir(self._srcdir(ctx))
        self._run_make(ctx, "install")

    def _run_make(self, ctx: Context, *args: str, **kwargs: Any) -> Process:
        return run(
            ctx,
            [
                "make",
                *args,
                "OBJDIR=" + self.path(ctx, "obj"),
                "PREFIX=" + self.path(ctx, "install"),
                "USE_BUILTINS=" + str(bool(self.builtin_passes)).lower(),
                "USE_GOLD_PASSES=" + str(bool(self.gold_passes)).lower(),
                "DEBUG=" + str(self.debug).lower(),
            ],
            **kwargs,
        )

    def is_fetched(self, ctx: Context) -> bool:
        return True

    def is_built(self, ctx: Context) -> bool:
        return False

    def is_installed(self, ctx: Context) -> bool:
        return False

    def pkg_config_options(self, ctx: Context) -> Iterator[PkgConfigOption]:
        yield ("--objdir", "absolute build path", self.path(ctx, "obj"))
        yield from super().pkg_config_options(ctx)

    def configure(
        self, ctx: Context, *, linktime: bool = True, compiletime: bool = True
    ) -> None:
        """
        Set build/link flags in **ctx**. Should be called from the ``configure``
        method of an instance.

        **linktime** and **compiletime** can be set to false to avoid loading
        the pass libraries at link time and at compile time, respectively.
        Loading passes at link time requires LLVM to be built with the
        **gold-plugin** patch.

        :param ctx: the configuration context
        :param linktime: are the passes used at link time?
        :param compiletime: are the passes used at compile time?
        """
        if compiletime:
            libpath = self.path(ctx, "install/libpasses-opt.so")
            cflags = ["-Xclang", "-load", "-Xclang", libpath]
            ctx.cflags += cflags
            ctx.cxxflags += cflags

        if linktime:
            libpath = self.path(ctx, "install/libpasses-gold.so")
            ctx.cflags += ["-flto"]
            ctx.cxxflags += ["-flto"]
            ctx.ldflags += ["-flto"]
            if self.gold_passes:
                ctx.ldflags += ["-Wl,-plugin-opt=-load=" + libpath]
            else:
                ctx.ldflags += ["-fuse-ld=lld", "-Wl,-mllvm=-load=" + libpath]
            ctx.lib_ldflags += ["-flto"]

    def runtime_cflags(self, ctx: Context) -> Iterable[str]:
        """
        Returns a list of CFLAGS to pass to a runtime library that depends on
        features from passes. These set include directories for header includes
        of built-in pass functionalities such as the ``NOINSTRUMENT`` macro.

        :param ctx: the configuration context
        """
        if self.builtin_passes:
            return self.builtin_passes.runtime_cflags(ctx)
        return []


class BuiltinLLVMPasses(LLVMPasses):
    """
    Subclass of :class:`LLVMPasses` for :doc:`built-in passes <passes>`. Use
    this if you don't have any custom passes and just want to use the built-in
    passes. Configuration happens in the same way as described above: by
    calling the :func:`configure` method.

    In addition to the shared objects listed above, this package also produces
    a static library called ``libpasses-builtin.a`` which is used by the
    :class:`LLVMPasses` to include built-in passes when ``use_builtins`` is
    ``True``.

    For the :ref:`pkg-config <usage-pkg-config>` command of this package, the
    following options are added in addition to
    ``--root``/``--prefix``/``--objdir``:

    - ``--cxxflags`` lists compilation flags for custom passes that depend on
      built-in analysis passes (sets include path for headers).

    - ``--runtime-cflags`` prints the value of
      :func:`LLVMPasses.runtime_cflags`.

    :identifier: llvm-passes-builtin-<llvm.version>
    :param llvm: LLVM package to link against
    """

    def __init__(self, llvm: LLVM, gold_passes: bool = True):
        super().__init__(
            llvm, ".", "builtin-" + llvm.version, False, gold_passes=gold_passes
        )
        self.custom_srcdir = ""

    def _srcdir(self, ctx: Context, *subdirs: str) -> str:
        return os.path.join(ctx.paths.infra, "llvm-passes", self.llvm.version, *subdirs)

    def is_built(self, ctx: Context) -> bool:
        files = ("libpasses-builtin.a", "libpasses-gold.so", "libpasses-opt.so")
        return all(os.path.exists("obj/" + f) for f in files)

    def is_installed(self, ctx: Context) -> bool:
        files = ("libpasses-builtin.a", "libpasses-gold.so", "libpasses-opt.so")
        return all(os.path.exists("install/" + f) for f in files)

    def pkg_config_options(self, ctx: Context) -> Iterator[PkgConfigOption]:
        yield ("--cxxflags", "pass compile flags", ["-I", self._srcdir(ctx, "include")])
        yield ("--runtime-cflags", "runtime compile flags", self.runtime_cflags(ctx))
        yield from super().pkg_config_options(ctx)

    def runtime_cflags(self, ctx: Context) -> Iterable[str]:
        return ["-I", self._srcdir(ctx, "include/runtime")]
