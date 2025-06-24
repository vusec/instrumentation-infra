import io
import os
import logging
import argparse
import platform
import dataclasses

from typing import Any, Callable, Iterable, TypeAlias
from datetime import datetime
from dataclasses import dataclass, field
from multiprocessing import cpu_count

HookFunc: TypeAlias = Callable[["Context", str], None]

LOG_LVL_CRT = logging.CRITICAL
LOG_LVL_FTL = logging.FATAL
LOG_LVL_ERR = logging.ERROR
LOG_LVL_WRN = logging.WARNING
LOG_LVL_INF = logging.INFO
LOG_LVL_VRB = (logging.INFO + logging.DEBUG) // 2
LOG_LVL_DBG = logging.DEBUG
LOG_LVL_TRC = (logging.DEBUG + logging.NOTSET) // 2
LOG_LVL_NST = logging.NOTSET

LOG_LEVEL_ABBREVIATIONS = {
    "NST": "NST",
    "TRC": "TRC",
    "DBG": "DBG",
    "VRB": "VRB",
    "INF": "INF",
    "WRN": "WRN",
    "ERR": "ERR",
    "FTL": "FTL",
    "CRT": "CRT",
    "NOTSET": "NST",
    "TRACE": "TRC",
    "DEBUG": "DBG",
    "VERBOSE": "VRB",
    "INFO": "INF",
    "WARN": "WRN",
    "WARNING": "WRN",
    "ERROR": "ERR",
    "FATAL": "FTL",
    "CRITICAL": "CRT",
}

LOG_LEVEL_NAMES = {
    LOG_LVL_CRT: {"CRT", "CRITICAL"},
    LOG_LVL_FTL: {"FTL", "FATAL"},
    LOG_LVL_ERR: {"ERR", "ERROR"},
    LOG_LVL_WRN: {"WRN", "WARN", "WARNING"},
    LOG_LVL_INF: {"INF", "INFO"},
    LOG_LVL_VRB: {"VRB", "VERBOSE"},
    LOG_LVL_DBG: {"DBG", "DEBUG"},
    LOG_LVL_TRC: {"TRC", "TRACE"},
    LOG_LVL_NST: {"NST", "NOTSET"},
}


def add_custom_log_levels() -> None:
    for lvl, names in LOG_LEVEL_NAMES.items():
        for name in names:
            logging.addLevelName(lvl, name)


class ExtLogger(logging.getLoggerClass()):
    def __init__(self, name: str, level: int | str = logging.NOTSET) -> None:
        super().__init__(name, level)

    def verbose(self, message, *args, **kwargs):
        if self.isEnabledFor(LOG_LVL_VRB):
            self._log(LOG_LVL_VRB, message, args, **kwargs)

    def trace(self, message, *args, **kwargs):
        if self.isEnabledFor(LOG_LVL_TRC):
            self._log(LOG_LVL_TRC, message, args, **kwargs)


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
        return os.path.join(self.log, "debug.log")

    @property
    def runlog(self) -> str:
        """Path to the log of all executed commands."""
        return os.path.join(self.log, "commands.log")

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

    #: Hooks to execute before building a target
    pre_build: list[HookFunc] = field(default_factory=list)

    #: Hooks to execute after a target is built (e.g. for additional post-processing)
    post_build: list[HookFunc] = field(default_factory=list)

    #: Hooks to execute before running a target (called for each binary-to-run)
    pre_run: list[HookFunc] = field(default_factory=list)

    #: Hooks to execute after running a target (called for each binary that was ran)
    post_run: list[HookFunc] = field(default_factory=list)

    @staticmethod
    def hook_name(hook: Callable[["Context", str], None]) -> str:
        return getattr(hook, "__name__", repr(hook))


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
    log: ExtLogger

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
    target_run_wrapper: str = ""

    #: File object used for writing all executed commands, if enabled.
    runlog_file: io.TextIOWrapper | None = None

    #: The amount of parallel jobs to use. Contains the value of the ``-j``
    #: command-line option, defaulting to the number of CPU cores returned by
    #: :func:`multiprocessing.cpu_count` (limited to 64 at most)
    jobs: int = field(default=min(cpu_count(), 64))

    #: Architecture to build targets for. Initialized to :func:`platform.machine`.
    #: Valid values include ``x86_64`` and ``arm64``/``aarch64``; for more, refer to
    #: ``uname -m`` and :func:`platform.machine`.
    arch: str = field(default=platform.machine())

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

    def getEnvironment(self, include_flags: bool = False) -> dict[str, str | list[str]]:
        """
        Returns the environment stored in the context; sets variables like :var:`$CC` to default
        programs stored in :var:`ctx.cc` and such. Optionally includes environment variables for
        the stored flags, e.g. :var:`$CFLAGS` for flags in :var:`ctx.cflags`.

        :param bool include_flags: whether to include environment variables for flags, defaults to False
        :return dict[str, str | list[str]]: the environment for the current context
        """
        return (
            self.runenv
            | {
                "CC": self.cc,
                "CXX": self.cxx,
                "FC": self.fc,
                "AR": self.ar,
                "NM": self.nm,
                "RANLIB": self.ranlib,
            }
            | (
                {
                    "CFLAGS": " ".join(self.cflags),
                    "CXXFLAGS": " ".join(self.cxxflags),
                    "LDFLAGS": " ".join(self.ldflags),
                    "LIB_LDFLAGS": " ".join(self.lib_ldflags),
                }
                if include_flags
                else {}
            )
        )

    def add_flags(
        self,
        *flags: str,
        scopes: Iterable[str] | None = None,
        allow_dups: bool = False,
    ) -> "Context":
        """
        Add one or more flags (auto-splitting on whitespace) to the given scopes.

        :param flags:       One or more flag-strings; e.g. "-g3", "-Xclang -verify"
        :param scopes:      Which scopes to populate; any of "cc","cxx","ld","lib_ld".
                            Defaults to all.
        :param allow_dups:  If False, skip flags already present.
        :returns:           self (for chaining)
        """
        # Default to all scopes if not passed
        scopes = scopes or ("cc", "cxx", "ld", "lib_ld")

        # Split each raw flag on whitespace, strip, drop empties
        clean = [flag for raw in flags if (parts := raw.split()) for part in parts if (flag := part.strip())]

        # Append each cleaned flag to the selected flag arrays (match scope)
        for flag in clean:
            for scope in scopes:
                match scope:
                    case "cc":
                        target = self.cflags
                    case "cxx":
                        target = self.cxxflags
                    case "ld":
                        target = self.ldflags
                    case "lib_ld":
                        target = self.lib_ldflags
                    case _:
                        raise ValueError(f"Unknown scope selected: {scope}")
                if allow_dups or flag not in target:
                    target.append(flag)

        return self

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
