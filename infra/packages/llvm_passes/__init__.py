import os
from ...package import Package
from ...util import FatalError, run
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
        include <path_to_infra>/packages/llvm-passes/Makefile

    The makefile can be run as-is using ``make`` in your passes directory
    during development, without invoking the setup script directly. It creates
    two shared objects in
    ``build/packages/llvm-passes-<build_suffix>/install``:

    - ``libpasses-gold.so``: used to load the passes at link time in Clang.

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

    :identifier: llvm-passes-<build_suffix>
    :param llvm: LLVM package to link against
    :param srcdir: source directory containing your LLVM passes
    :param build_suffix: identifier for this set of passes
    :param use_builtins: whether to include :ref:`built-in LLVM passes
                         <builtin-passes>` in the shared object
    :todo: extend this to support compile-time plugins
    """

    def __init__(self, llvm: LLVM,
                       srcdir: str,
                       build_suffix: str,
                       use_builtins: bool):
        self.llvm = llvm
        self.custom_srcdir = os.path.abspath(srcdir)
        self.build_suffix = build_suffix
        self.builtin_passes = BuiltinLLVMPasses(llvm) if use_builtins else None

    def ident(self):
        return 'llvm-passes-' + self.build_suffix

    def _srcdir(self, ctx):
        if not os.path.exists(self.custom_srcdir):
            raise FatalError('llvm-passes dir "%s" does not exist' %
                             self.custom_srcdir)
        return self.custom_srcdir

    def dependencies(self):
        yield self.llvm
        if self.builtin_passes:
            yield self.builtin_passes

    def fetch(self, ctx):
        pass

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir(self._srcdir(ctx))
        self._run_make(ctx, '-j%d' % ctx.jobs)

    def install(self, ctx):
        os.chdir(self._srcdir(ctx))
        self._run_make(ctx, 'install')

    def _run_make(self, ctx, *args, **kwargs):
        return run(ctx, [
            'make', *args,
            'OBJDIR=' + self.path(ctx, 'obj'),
            'PREFIX=' + self.path(ctx, 'install'),
            'USE_BUILTINS=' + ('true' if self.builtin_passes else 'false')
        ], **kwargs)

    def is_fetched(self, ctx):
        return True

    def is_built(self, ctx):
        return False

    def is_installed(self, ctx):
        return False

    def pkg_config_options(self, ctx):
        yield ('--objdir',
               'absolute build path',
               self.path(ctx, 'obj'))
        yield from super().pkg_config_options(ctx)

    def configure(self, ctx):
        libpath = self.path(ctx, 'install/libpasses-gold.so')
        ctx.cflags += ['-flto']
        ctx.cxxflags += ['-flto']
        ctx.ldflags += ['-flto', '-Wl,-plugin-opt=-load=' + libpath]

    def runtime_cflags(self, ctx):
        """
        """
        if self.builtin_passes:
            return self.builtin_passes.runtime_cflags(ctx)
        return []


class BuiltinLLVMPasses(LLVMPasses):
    """
    """

    def __init__(self, llvm: LLVM):
        super().__init__(llvm, '.', 'builtin-' + llvm.version, False)
        self.custom_srcdir = None

    def _srcdir(self, ctx, *subdirs):
        return os.path.join(ctx.paths.infra, 'llvm-passes',
                            self.llvm.version, *subdirs)

    def is_built(self, ctx):
        files = ('libpasses-builtin.a', 'libpasses-gold.so', 'libpasses-opt.so')
        return all(os.path.exists('obj/' + f) for f in files)

    def is_installed(self, ctx):
        files = ('libpasses-builtin.a', 'libpasses-gold.so', 'libpasses-opt.so')
        return all(os.path.exists('install/' + f) for f in files)

    def pkg_config_options(self, ctx):
        yield ('--cxxflags',
               'pass compile flags',
               ['-I', self._srcdir(ctx)])
        yield ('--runtime-cflags',
               'runtime compile flags',
               self.runtime_cflags(ctx))
        yield ('--target-cflags',
               'target compile flags for instrumentation helpers',
               ['-I', self._srcdir(ctx, 'include')])
        yield from super().pkg_config_options(ctx)

    def runtime_cflags(self, ctx):
        """
        """
        return ['-I', self._srcdir(ctx, 'include')]
