import io
import os
import logging
import argparse
import platform
import dataclasses

from typing import Any, Callable, Iterable
from datetime import datetime
from dataclasses import dataclass, field
from multiprocessing import cpu_count


@dataclass(frozen=True)
class ContextPaths:
    """
    Absolute, read-only, paths used throughout the infra.

    Normally instances, targets, and packages do not need to consult these
    pathsdirectly, but instead use their respective ``path`` method.
    """

    #: Root dir of the infra itself.
    infra: str

    #: Path to the user's script that invoked the infra.
    setup: str

    #: Working directory when the infra was started.
    workdir: str

    @property
    def root(self) -> str:
        """Root directory, that contains the user's script invoking the infra."""
        return os.path.dirname(self.setup)

    @property
    def buildroot(self) -> str:
        """Build directory."""
        return os.path.join(self.root, "build")

    @property
    def log(self) -> str:
        """Directory containing all logs."""
        return os.path.join(self.buildroot, "log")

    @property
    def debuglog(self) -> str:
        """Path to the debug log."""
        return os.path.join(self.log, "debug.txt")

    @property
    def runlog(self) -> str:
        """Path to the log of all executed commands."""
        return os.path.join(self.log, "commands.txt")

    @property
    def packages(self) -> str:
        """Build directory for packages."""
        return os.path.join(self.buildroot, "packages")

    @property
    def targets(self) -> str:
        """Build directory for targets."""
        return os.path.join(self.buildroot, "targets")

    @property
    def pool_results(self) -> str:
        """Directory containing all results of running targets."""
        return os.path.join(self.root, "results")


@dataclass(slots=True)
class ContextHooks:
    """Hooks (i.e., functions) that are executed at various stages during the
    building and running of targets."""

    #: Hooks to execute before building a target.
    pre_build: List[Callable] = field(default_factory=list)

    #: Hooks to execute after a target is built.
    #:
    #: This can be used to do additional post-processing on the generated binaries.
    post_build: List[Callable] = field(default_factory=list)


@dataclass(slots=True)
class Context:
    """
    The global configuration context, used by all targets, instances, etc.

    For example, an instance can configure its compiler flags in this
    context, which are then used by targets.
    """

    #: Absolute paths to be used (readonly) throughout the framework.
    paths: ContextPaths

    #: The logging object used for status updates.
    log: logging.Logger

    #: The logging level as requested by the user.
    #:
    #: Note that is differs from the logging object's log level, since all debug output
    #: is written to a file regardless of the requested loglevel.
    loglevel: int = logging.NOTSET

    #: Populated with processed command-line arguments. Targets and instances can add
    #: additional command-line arguments, which can be accessed through this object.
    args: argparse.Namespace = field(default_factory=argparse.Namespace)

    #: An object with hooks for various points in the building/running process.
    hooks: ContextHooks = field(default_factory=ContextHooks)

    #: Environment variables that are used when running a target.
    runenv: dict[str, str | list[str]] = field(default_factory=dict)

    #: When the current run of the infra was started.
    starttime: datetime = field(default_factory=datetime.now)

    #: Command(s) to prepend in front of the target's run command (executed directly on
    #: the command line). This can be set to a custom shell script, or for example
    #: ``perf`` or ``valgrind``.
    target_run_wrapper: str = ""  # TODO: merge this with Tools?

    #: File object used for writing all executed commands, if enabled.
    runlog_file: io.TextIOWrapper | None = None

    #: The amount of parallel jobs to use. Contains the value of the ``-j`` command-line
    #: option, defaulting to the number of CPU cores returned by
    #: :func:`multiprocessing.cpu_count`.
    jobs: int = 8

    #: Architecture to build targets for. Initialized to :func:`platform.machine`.
    #: Valid values include ``x86_64`` and ``arm64``/``aarch64``; for more, refer to
    #: ``uname -m`` and :func:`platform.machine`.
    arch: str = "unknown"

    #: C compiler to use when building targets.
    cc: str = "cc"

    #: C++ compiler to use for building targets.
    cxx: str = "cxx"

    #: Fortran compiler to use for building targets.
    fc: str = "fc"

    #: Command for creating static library archives.
    ar: str = "ar"

    #: Command to read an object's symbols.
    nm: str = "nm"

    #: Command to generate the index of an archive.
    ranlib: str = "ranlib"

    #: C compilation flags to use when building targets.
    cflags: list[str] = field(default_factory=list)

    #: C++ compilation flags to use when building targets.
    cxxflags: list[str] = field(default_factory=list)

    #: Fortran compilation flags to use when building targets.
    fcflags: list[str] = field(default_factory=list)

    #: Linker flags to use when building targets.
    ldflags: list[str] = field(default_factory=list)

    #: Special set of linker flags set by some packages, and is passed when linking
    #: target libraries that will later be (statically) linked into the binary.
    #:
    #: In practice it is either empty or ``['-flto']`` when compiling with LLVM.
    lib_ldflags: list[str] = field(default_factory=list)

    def add_flags(
        self,
        flags: Iterable[str] | str,
        cc: bool = False,
        cxx: bool = False,
        ld: bool = False,
        lib_ld: bool = False,
        dups: bool = True,
    ) -> None:
        """Helper function to add one or more flags to the context's cflags/cxxflags/ldflags/lib_ldflags
        lists conveniently with a single call. By default, allows insertion of duplicate flags. Can
        be disabled by setting :param:`dups` to `False`.

        :param Iterable[str] | str flags: a single flag or an iterable containing flags
        :param bool cc: add the flag(s) to the C compiler flags, defaults to False
        :param bool cxx: add the flag(s) to the C++ compiler flags, defaults to False
        :param bool ld: add the flag(s) to the linker flags, defaults to False
        :param bool lib_ld: add the flag(s) to the lib_linker flags, defaults to False
        :param bool dups: always add flag(s) even if they are already added, defaults to True
        """
        flags = [flags] if isinstance(flags, str) else list(flags)
        for flag in flags:
            if cc and (flag not in self.cflags or dups):
                self.cflags.append(flag)
            if cxx and (flag not in self.cxxflags or dups):
                self.cxxflags.append(flag)
            if ld and (flag not in self.ldflags or dups):
                self.ldflags.append(flag)
            if lib_ld and (flag not in self.lib_ldflags or dups):
                self.lib_ldflags.append(flag)

    def copy(self) -> "Context":
        """
        Make a partial deepcopy of this Context, copying only fields of type
        ``ContextPaths|list|dict``.
        """
        changes: dict[str, Any] = {"paths": dataclasses.replace(self.paths)}
        for attr in dir(self):
            if attr.startswith("_"):
                continue
            attr_val = getattr(self, attr)
            if isinstance(attr_val, (list, dict)):
                changes[attr] = attr_val.copy()
        return dataclasses.replace(self, **changes)
