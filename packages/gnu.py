import os
import shutil
import subprocess
from abc import ABCMeta, abstractmethod
from ..package import Package
from ..util import run, download


class GNUTarPackage(Package, metaclass=ABCMeta):
    @property
    @abstractmethod
    def name(self):
        pass

    @property
    @abstractmethod
    def built_path(self):
        pass

    @property
    @abstractmethod
    def installed_path(self):
        pass

    def __init__(self, version):
        self.version = version

    def ident(self):
        return '%s-%s' % (self.name, self.version)

    def fetch(self, ctx):
        if not os.path.exists(self.src_path(ctx)) and not self.installed(ctx):
            os.chdir(ctx.paths.packsrc)
            tarname = '%s-%s.tar.gz' % (self.name, self.version)
            download('http://ftp.gnu.org/gnu/%s/%s' % (self.name, tarname))
            run(['tar', '-xzf', tarname])
            os.remove(tarname)

    def build(self, ctx):
        if not self.built(ctx) and not self.installed(ctx):
            objdir = self.build_path(ctx)
            os.makedirs(objdir, exist_ok=True)
            os.chdir(objdir)
            run([self.src_path(ctx, 'configure'),
                 '--prefix=' + self.install_path(ctx)])
            run(['make', '-j%d' % ctx.nproc])

    def install(self, ctx):
        if not self.installed(ctx):
            os.chdir(self.build_path(ctx))
            run(['make', 'install'])

    def built(self, ctx):
        return os.path.exists(self.build_path(ctx, self.built_path))

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx, self.installed_path))


class Bash(GNUTarPackage):
    name = 'bash'
    built_path = 'bash'
    installed_path = 'bin/bash'

    def installed(self, ctx):
        proc = subprocess.run(['bash', '--version'],
                stdout=subprocess.PIPE, universal_newlines=True)
        if proc.returncode == 0 and 'version ' + self.version in proc.stdout:
            return True
        return GNUTarPackage.installed(self, ctx)


class Make(GNUTarPackage):
    name = 'make'
    built_path = 'make'
    installed_path = 'bin/make'

    def installed(self, ctx):
        proc = subprocess.run(['make', '--version'], stdout=PIPE)
        if proc.returncode == 0 and proc.stdout.startswith('GNU Make ' + self.version):
            return True
        return GNUTarPackage.installed(self, ctx)


class CoreUtils(GNUTarPackage):
    name = 'coreutils'
    built_path = 'src/yes'
    installed_path = 'bin/yes'


class M4(GNUTarPackage):
    name = 'm4'
    built_path = 'src/m4'
    installed_path = 'bin/m4'


class AutoConf(GNUTarPackage):
    name = 'autoconf'
    built_path = 'bin/autoconf'
    installed_path = 'bin/autoconf'


class AutoMake(GNUTarPackage):
    name = 'automake'
    built_path = 'bin/automake'
    installed_path = 'bin/automake'


class LibTool(GNUTarPackage):
    name = 'libtool'
    built_path = 'libtool'
    installed_path = 'bin/libtool'


class BinUtils(Package):
    def __init__(self, version, gold=True):
        self.version = version
        self.gold = gold

    def ident(self):
        s = 'binutils-' + self.version
        if self.gold:
            s += '-gold'
        return s

    def fetch(self, ctx):
        os.chdir(ctx.paths.packsrc)

        if not os.path.exists(self.ident()):
            tarname = 'binutils-%s.tar.bz2' % self.version
            download('http://ftp.gnu.org/gnu/binutils/' + tarname)
            run(['tar', '-xf', tarname])
            shutil.move('binutils-' + self.version, self.ident())
            os.remove(tarname)

    def build(self, ctx):
        if not self.built(ctx) and not self.installed(ctx):
            objdir = self.build_path(ctx)
            os.makedirs(objdir, exist_ok=True)
            os.chdir(objdir)

            configure = [self.src_path(ctx, 'configure'),
                         '--enable-gold', '--enable-plugins',
                         '--disable-werror',
                         '--prefix=' + self.install_path(ctx)]

            # match system setting to avoid 'this linker was not configured to
            # use sysroots' error or failure to find libpthread.so
            if run(['gcc', '--print-sysroot']).stdout:
                configure.append('--with-sysroot')

            run(configure)
            run(['make', '-j%d' % ctx.nproc])
            if self.gold:
                run(['make', '-j%d' % ctx.nproc, 'all-gold'])

    def install(self, ctx):
        if not self.installed(ctx):
            os.chdir(self.build_path(ctx))
            run(['make', 'install'])

            # replace ld with gold
            if self.gold:
                os.chdir(self.install_path(ctx, 'bin'))
                os.remove('ld')
                shutil.copy('ld.gold', 'ld')

    def built(self, ctx):
        subdir = 'gold' if self.gold else 'ld'
        return os.path.exists(self.build_path(ctx, subdir, 'ld-new'))

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx, 'bin', 'ld'))
