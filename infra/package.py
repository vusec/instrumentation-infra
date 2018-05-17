import os
import shutil
from abc import ABCMeta, abstractmethod
from typing import Iterable, Iterator, Tuple
from .util import Namespace


class Package(metaclass=ABCMeta):
    """
    Abstract base class for package definitions. Built-in derived classes are
    listed :doc:`here <packages>`.

    Each package must define a :func:`ident` method that returns a unique ID
    for the package instance. This is similar to :py:attr:`Target.name`, except
    that each instantiation of a package can return a different ID depending on
    its parameters. For example a ``Bash`` package might be initialized with a
    version number and be identified as ``bash-4.1`` and ``bash-4.3``, which
    are different packages with different build directories.

    A dependency is built in three steps:

    #. :func:`fetch` downloads the source code, typically to
       ``build/packages/<ident>/src``.
    #. :func:`build` builds the code, typically in
       ``build/packages/<ident>/obj``.
    #. :func:`install` installs the built binaries/libraries, typically into
       ``build/packages/<ident>/install``.

    The functions above are only called if :func:`is_fetched`, :func:`is_built`
    and :func:`is_installed` return ``False`` respectively. Additionally, if
    :func:`is_installed` returns ``True``, fetching and building is skipped
    altogether. All these methods are abstract and thus require an
    implementation in a pacakge definition.

    :func:`clean` removes all generated package files when the :ref:`clean
    <usage-clean>` command is run. By default, this removes
    ``build/packages/<ident>``.

    The package needs to be able to install itself into ``ctx.runenv`` so that
    it can be used by targets/instances/packages that depend on it. This is
    done by :func:`install_env`, which by default adds
    ``build/packages/<ident>/install/bin`` to the ``PATH`` and
    ``build/packages/<ident>/install/lib`` to the ``LD_LIBRARY_PATH``.

    Finally, the setup script has a :ref:`pkg-config <usage-pkg-config>`
    command that prints package information such as the installation prefix of
    compilation flags required to build software that uses the package. These
    options are configured by :func:`pkg_config_options`.
    """

    def __eq__(self, other):
        return isinstance(other, self.__class__) and \
               other.ident() == self.ident()

    def __hash__(self):
        return hash('package-' + self.ident())

    @abstractmethod
    def ident(self) -> str:
        """
        Returns a unique identifier to this package instantiation.

        Two packages are considered identical if their identifiers are equal.
        This means that if multiple targets/instances/packages return different
        instantiations of a package as dependency that share the same
        identifier, they are assumed to be equal and only the first will be
        built. This way, different implementations of :func:`dependencies` can
        instantiate the same class in order to share a dependency.
        """
        pass

    def dependencies(self) -> Iterator['Package']:
        """
        Specify dependencies that should be built and installed in the run
        environment before building this package.
        """
        yield from []

    @abstractmethod
    def is_fetched(self, ctx: Namespace) -> bool:
        """
        Returns ``True`` if :func:`fetch` should be called before building.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def is_built(self, ctx: Namespace) -> bool:
        """
        Returns ``True`` if :func:`build` should be called before installing.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def is_installed(self, ctx: Namespace) -> bool:
        """
        Returns ``True`` if the pacakge has not been installed yet, and thus
        needs to be fetched, built and installed.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def fetch(self, ctx: Namespace):
        """
        Fetches the source code for this package. This step is separated from
        :func:`build` because the ``build`` command first fetches all packages
        and targets before starting the build process.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def build(self, ctx: Namespace):
        """
        Build the package. Usually amounts to running ``make -j<ctx.jobs>``
        using :func:`util.run`.

        :param ctx: the configuration context
        """
        pass

    @abstractmethod
    def install(self, ctx: Namespace):
        """
        Install the package. Usually amounts to running ``make install`` using
        :func:`util.run`. It is recommended to install to ``self.path(ctx,
        'install')``, which results in ``build/packages/<ident>/install``.
        Assuming that a `bin` and/or `lib` directories are generated in the
        install directory, the default behaviour of :func:`install_env` will
        automatically add those to ``[LD_LIBRARY_]PATH``.

        :param ctx: the configuration context
        """
        pass

    def is_clean(self, ctx: Namespace) -> bool:
        """
        Returns ``True`` if :func:`clean` should be called before cleaning.

        :param ctx: the configuration context
        """
        return not os.path.exists(self.path(ctx))

    def clean(self, ctx: Namespace):
        """
        Clean generated files for this target, called by the :ref:`clean
        <usage-clean>` command. By default, this removes
        ``build/packages/<ident>``.

        :param ctx: the configuration context
        """
        shutil.rmtree(self.path(ctx))

    def path(self, ctx: Namespace, *args: Iterable[str]) -> str:
        """
        Get the absolute path to the build directory of this package,
        optionally suffixed with a subpath.

        :param ctx: the configuration context
        :param args: additional subpath to pass to :func:`os.path.join`
        :returns: ``build/packages/<ident>[/<subpath>]``
        """
        return os.path.join(ctx.paths.packages, self.ident(), *args)

    def install_env(self, ctx: Namespace):
        """
        Install the package into ``ctx.runenv`` so that it can be used in
        subsequent calls to :func:`util.run`. By default, it adds
        ``build/packages/<ident>/install/bin`` to the ``PATH`` and
        ``build/packages/<ident>/install/lib`` to the ``LD_LIBRARY_PATH`` (but
        only if the directories exist).

        :param ctx: the configuration context
        """
        # XXX rename 'install_env' to 'load'?
        prevbinpath = os.getenv('PATH', '').split(':')
        binpath = self.path(ctx, 'install/bin')
        if os.path.exists(binpath):
            ctx.runenv.setdefault('PATH', prevbinpath).insert(0, binpath)

        prevlibpath = os.getenv('LD_LIBRARY_PATH', '').split(':')
        libpath = self.path(ctx, 'install/lib')
        if os.path.exists(libpath):
            ctx.runenv.setdefault('LD_LIBRARY_PATH', prevlibpath).insert(0, libpath)

    def goto_rootdir(self, ctx):
        path = self.path(ctx)
        os.makedirs(path, exist_ok=True)
        os.chdir(path)

    def pkg_config_options(self, ctx: Namespace) -> Iterator[Tuple[str, str, str]]:
        """
        Yield options for the :ref:`pkg-config <usage-pkg-config>` command.
        Each option is an (option, description, value) triple. The defaults are
        ``--root`` which returns the root directory ``build/packages/<ident>``,
        and ``--prefix`` which returns the install directory populated by
        :func:`install`: ``build/packages/<ident>/install``.

        When reimplementing this method in a derived package class, it is
        recommended to end the implementation with ``yield from
        super().pkg_config_options(ctx)`` to add the two default options.

        :param ctx: the configuration context
        """
        yield ('--root',
               'absolute root path',
               self.path(ctx))
        yield ('--prefix',
               'absolute install path',
               self.path(ctx, 'install'))
