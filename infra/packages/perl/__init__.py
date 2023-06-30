import os
import shutil
from typing import Iterator

from ...context import Context
from ...package import Package
from ...util import FatalError, apply_patch, download, run
from ..gnu import Bash


class Perl(Package):
    def __init__(self, version: str):
        if not version.startswith("5."):
            raise FatalError("only perl5 is supported")
        self.version = version
        self.bash = Bash("4.3")

    def ident(self) -> str:
        return "perl-" + self.version

    def dependencies(self) -> Iterator[Package]:
        yield self.bash

    def fetch(self, ctx: Context) -> None:
        ident = "perl-" + self.version
        tarname = ident + ".tar.gz"
        download(ctx, "http://www.cpan.org/src/5.0/" + tarname)
        run(ctx, ["tar", "-xf", tarname])
        shutil.move(ident, "src")
        os.remove(tarname)

    def build(self, ctx: Context) -> None:
        os.chdir("src")
        if not os.path.exists("Makefile"):
            prefix = self.path(ctx, "install")
            run(ctx, ["bash", "./Configure", "-des", "-Dprefix=" + prefix])
        run(ctx, ["make", f"-j{ctx.jobs}"])

    def install(self, ctx: Context) -> None:
        os.chdir("src")
        run(ctx, ["make", "install"])

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def is_built(self, ctx: Context) -> bool:
        return os.path.exists("src/perl")

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists("install/bin/perl")


class SPECPerl(Perl):
    def __init__(self) -> None:
        Perl.__init__(self, "5.8.8")

    def ident(self) -> str:
        return f"perl-{self.version}-spec"

    def fetch(self, ctx: Context) -> None:
        Perl.fetch(self, ctx)

        # apply patches (includes quote fix from
        # https://rt.perl.org/Public/Bug/Display.html?id=44581)
        os.chdir("src")
        config_path = os.path.dirname(os.path.abspath(__file__))
        for patch_name in ("makedepend", "pagesize"):
            path = f"{config_path}/{patch_name}-{self.version}.patch"
            apply_patch(ctx, path, 1)

        if not os.path.exists(".patched-Configure-paths"):
            libmfile = run(ctx, "gcc -print-file-name=libm.so").stdout.rstrip()
            libpath = os.path.dirname(libmfile)

            if libpath != ".":
                ctx.log.debug("applying paths patch on Configure")
                run(
                    ctx,
                    ["sed", "-i", f"s|^xlibpth='|xlibpth='{libpath} |", "Configure"],
                )
                open(".patched-Configure-paths", "w").close()

    def build(self, ctx: Context) -> None:
        os.chdir("src")
        if not os.path.exists("Makefile"):
            prefix = self.path(ctx, "install")
            run(ctx, ["bash", "./Configure", "-des", "-Dprefix=" + prefix])
            run(ctx, "sed -i '/<command-line>/d' makefile x2p/makefile")
        run(ctx, ["make", f"-j{ctx.jobs}"])


class Perlbrew(Package):
    def __init__(self, perl: Perl):
        self.perl = perl

    def ident(self) -> str:
        return "perlbrew-" + self.perl.ident()

    def dependencies(self) -> Iterator[Package]:
        yield self.perl

    def fetch(self, ctx: Context) -> None:
        # download installer
        download(ctx, "http://install.perlbrew.pl", "perlbrew-installer")

        # patch it to use the local perl installation
        perl = self.perl.path(ctx, "install/bin/perl")
        run(ctx, f'sed -i "s|/usr/bin/perl|{perl}|g" perlbrew-installer')

    def build(self, ctx: Context) -> None:
        pass

    def install(self, ctx: Context) -> None:
        run(
            ctx,
            "bash perlbrew-installer",
            env={
                "PERLBREW_ROOT": self.path(ctx, "install"),
                "PERLBREW_HOME": self.path(ctx, "home"),
            },
        )

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("perlbrew-installer")

    def is_built(self, ctx: Context) -> bool:
        return True

    def is_installed(self, ctx: Context) -> bool:
        return os.path.exists(self.path(ctx, "install/bin/perlbrew"))

    # def install_env(self, ctx):
    #    Package.install_env(self, ctx)

    #    bins = self.path(ctx, f'install/perls/perl-{self.perl.version}/bin')
    #    assert os.path.exists(bins)
    #    ctx.runenv.PATH.insert(0, bins)

    #    # FIXME source install/etc/bashrc in SPEC
