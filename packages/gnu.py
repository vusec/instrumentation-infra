import os
import shutil
from subprocess import PIPE
from abc import ABCMeta, abstractmethod
from ..package import Package
from ..util import run, download, FatalError


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

    @property
    @abstractmethod
    def tar_compression(self):
        pass

    def __init__(self, version):
        self.version = version

    def ident(self):
        return '%s-%s' % (self.name, self.version)

    def fetch(self, ctx):
        ident = '%s-%s' % (self.name, self.version)
        tarname = ident + '.tar.' + self.tar_compression
        download(ctx, 'http://ftp.gnu.org/gnu/%s/%s' % (self.name, tarname))
        run(ctx, ['tar', '-xf', tarname])
        shutil.move(ident, 'src')
        os.remove(tarname)

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')
        run(ctx, ['../src/configure', '--prefix=' + self.path(ctx, 'install')])
        run(ctx, ['make', '-j%d' % ctx.jobs])

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, ['make', 'install'])

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/' + self.built_path)

    def is_installed(self, ctx):
        return os.path.exists('install/' + self.installed_path)


class Bash(GNUTarPackage):
    name = 'bash'
    built_path = 'bash'
    installed_path = 'bin/bash'
    tar_compression = 'gz'

    def is_installed(self, ctx):
        if GNUTarPackage.is_installed(self, ctx):
            return True
        proc = run(ctx, ['bash', '--version'], allow_error=True, silent=True)
        return proc and proc.returncode == 0 and \
                'version ' + self.version in proc.stdout


class Make(GNUTarPackage):
    name = 'make'
    built_path = 'make'
    installed_path = 'bin/make'
    tar_compression = 'gz'

    def is_installed(self, ctx):
        if GNUTarPackage.is_installed(self, ctx):
            return True
        proc = run(ctx, ['make', '--version'], allow_error=True, silent=True)
        return proc and proc.returncode == 0 and \
                proc.stdout.startswith('GNU Make ' + self.version)


class CoreUtils(GNUTarPackage):
    name = 'coreutils'
    built_path = 'src/yes'
    installed_path = 'bin/yes'
    tar_compression = 'xz'


class M4(GNUTarPackage):
    name = 'm4'
    built_path = 'src/m4'
    installed_path = 'bin/m4'
    tar_compression = 'gz'


class AutoConf(GNUTarPackage):
    name = 'autoconf'
    built_path = 'bin/autoconf'
    installed_path = 'bin/autoconf'
    tar_compression = 'gz'


class AutoMake(GNUTarPackage):
    name = 'automake'
    built_path = 'bin/automake'
    installed_path = 'bin/automake'
    tar_compression = 'gz'


class LibTool(GNUTarPackage):
    name = 'libtool'
    built_path = 'libtool'
    installed_path = 'bin/libtool'
    tar_compression = 'gz'


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
        tarname = 'binutils-%s.tar.bz2' % self.version
        download(ctx, 'http://ftp.gnu.org/gnu/binutils/' + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move('binutils-' + self.version, 'src')
        os.remove(tarname)

    def build(self, ctx):
        os.makedirs('obj', exist_ok=True)
        os.chdir('obj')

        if not self.bison_installed(ctx):
            raise FatalError('bison not found (required to build binutils)')

        configure = ['../src/configure',
                     '--enable-gold', '--enable-plugins',
                     '--disable-werror',
                     '--prefix=' + self.path(ctx, 'install')]

        # match system setting to avoid 'this linker was not configured to
        # use sysroots' error or failure to find libpthread.so
        if run(ctx, ['gcc', '--print-sysroot']).stdout:
            configure.append('--with-sysroot')

        run(ctx, configure)
        run(ctx, ['make', '-j%d' % ctx.jobs])
        if self.gold:
            run(ctx, ['make', '-j%d' % ctx.jobs, 'all-gold'])

    def install(self, ctx):
        os.chdir('obj')
        run(ctx, ['make', 'install'])

        # replace ld with gold
        if self.gold:
            os.chdir('../install/bin')
            os.remove('ld')
            shutil.copy('ld.gold', 'ld')

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('obj/%s/ld-new' % ('gold' if self.gold else 'ld'))

    def is_installed(self, ctx):
        return os.path.exists('install/bin/ld')

    def bison_installed(self, ctx):
        proc = run(ctx, ['bison', '--version'], allow_error=True, silent=True)
        return proc and proc.returncode == 0
