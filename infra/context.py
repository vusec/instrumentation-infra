import argparse
import dataclasses
import io
import logging
import os
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
)

T = TypeVar("T")


def slotted(cls: Type[T]) -> Type[T]:
    """
    Decorator to create a dataclass where only defined fields can be
    assigned to.

    This is purely for backwards compatibility; Once support for Python
    version below 3.10 is dropped, replace uses of this decorator with
    @dataclass(slots=True).
    """

    if TYPE_CHECKING:
        from _typeshed import DataclassInstance

    def slotted_setattr(self: "DataclassInstance", key: str, value: Any) -> None:
        if key not in (f.name for f in fields(self)):
            raise Exception(
                f"cannot set '{key}' in ctx: dynamically adding extra "
                "fields to ctx is deprecated"
            )
        super(type(self), self).__setattr__(key, value)

    setattr(cls, "__setattr__", slotted_setattr)
    return cls


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


@slotted
@dataclass
class ContextHooks:
    """Hooks (i.e., functions) that are executed at various stages during the
    building and running of targets."""

    #: Hooks to execute before building a target.
    pre_build: List[Callable] = field(default_factory=list)

    #: Hooks to execute after a target is built.
    #:
    #: This can be used to do additional post-processing on the generated binaries.
    post_build: List[Callable] = field(default_factory=list)


@slotted
@dataclass
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
    runenv: Dict[str, Union[str, List[str]]] = field(default_factory=dict)

    #: When the current run of the infra was started.
    starttime: datetime = field(default_factory=datetime.now)

    #: Command(s) to prepend in front of the target's run command (executed directly on
    #: the command line). This can be set to a custom shell script, or for example
    #: ``perf`` or ``valgrind``.
    target_run_wrapper: str = ""  # TODO: merge this with Tools?

    #: File object used for writing all executed commands, if enabled.
    runlog_file: Optional[io.TextIOWrapper] = None

    #: Object used to redirect the output of executed commands to a file and stdout.
    runtee: Optional[io.IOBase] = None

    #: The amount of parallel jobs to use. Contains the value of the ``-j`` command-line
    #: option, defaulting to the number of CPU cores returned by
    #: :func:`multiprocessing.cpu_count`.
    jobs: int = 8

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
    cflags: List[str] = field(default_factory=list)

    #: C++ compilation flags to use when building targets.
    cxxflags: List[str] = field(default_factory=list)

    #: Fortran compilation flags to use when building targets.
    fcflags: List[str] = field(default_factory=list)

    #: Linker flags to use when building targets.
    ldflags: List[str] = field(default_factory=list)

    #: Special set of linker flags set by some packages, and is passed when linking
    #: target libraries that will later be (statically) linked into the binary.
    #:
    #: In practice it is either empty or ``['-flto']`` when compiling with LLVM.
    lib_ldflags: List[str] = field(default_factory=list)

    def copy(self) -> "Context":
        """
        Make a partial deepcopy of this Context, copying only fields of type
        ``ContextPaths|list|dict``.
        """
        changes: Dict[str, Any] = {"paths": dataclasses.replace(self.paths)}
        for attr in dir(self):
            if attr.startswith("_"):
                continue
            attr_val = getattr(self, attr)
            if isinstance(attr_val, (list, dict)):
                changes[attr] = attr_val.copy()
        return dataclasses.replace(self, **changes)
