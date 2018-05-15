import os
import stat
import shutil
from ...package import Package
from ...util import run, download, apply_patch, FatalError
from ..gnu import Bash


class Perl(Package):
    def __init__(self, version):
        if not version.startswith('5.'):
            raise FatalError('only perl5 is supported')
        self.version = version
        self.bash = Bash('4.3')

    def ident(self):
        return 'perl-' + self.version

    def dependencies(self):
        yield self.bash

    def fetch(self, ctx):
        ident = 'perl-' + self.version
        tarname = ident + '.tar.gz'
        download(ctx, 'http://www.cpan.org/src/5.0/' + tarname)
        run(ctx, ['tar', '-xf', tarname])
        shutil.move(ident, 'src')
        os.remove(tarname)

    def build(self, ctx):
        os.chdir('src')
        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install')
            run(ctx, ['bash', './Configure', '-des', '-Dprefix=' + prefix])
        run(ctx, ['make', '-j%d' % ctx.jobs])

    def install(self, ctx):
        os.chdir('src')
        run(ctx, ['make', 'install'])

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def is_built(self, ctx):
        return os.path.exists('src/perl')

    def is_installed(self, ctx):
        return os.path.exists('install/bin/perl')


class SPECPerl(Perl):
    def __init__(self):
        Perl.__init__(self, '5.8.8')

    def ident(self):
        return 'perl-%s-spec' % self.version

    def fetch(self, ctx):
        Perl.fetch(self, ctx)

        # apply patches (includes quote fix from
        # https://rt.perl.org/Public/Bug/Display.html?id=44581)
        os.chdir('src')
        config_path = os.path.dirname(os.path.abspath(__file__))
        for patch_name in ('makedepend', 'pagesize'):
            path = '%s/%s-%s.patch' % (config_path, patch_name, self.version)
            apply_patch(ctx, path, 1)

        if not os.path.exists('.patched-Configure-paths'):
            libmfile = run(ctx, 'gcc -print-file-name=libm.so').stdout.rstrip()
            libpath = os.path.dirname(libmfile)

            if libpath != '.':
                ctx.log.debug('applying paths patch on Configure')
                run(ctx, ['sed', '-i', "s|^xlibpth='|xlibpth='%s |" % libpath,
                          'Configure' ])
                open('.patched-Configure-paths', 'w').close()

    def build(self, ctx):
        os.chdir('src')
        if not os.path.exists('Makefile'):
            prefix = self.path(ctx, 'install')
            run(ctx, ['bash', './Configure', '-des', '-Dprefix=' + prefix])
            run(ctx, "sed -i '/<command-line>/d' makefile x2p/makefile")
        run(ctx, ['make', '-j%d' % ctx.jobs])


class Perlbrew(Package):
    def __init__(self, perl):
        self.perl = perl

    def ident(self):
        return 'perlbrew-' + self.perl.ident()

    def dependencies(self):
        yield self.perl

    def fetch(self, ctx):
        # download installer
        download(ctx, 'http://install.perlbrew.pl', 'perlbrew-installer')

        # patch it to use the local perl installation
        perl = self.perl.path(ctx, 'install/bin/perl')
        run(ctx, 'sed -i "s|/usr/bin/perl|%s|g" perlbrew-installer' % perl)

    def build(self, ctx):
        pass

    def install(self, ctx):
        run(ctx, 'bash perlbrew-installer', env={
            'PERLBREW_ROOT': self.path(ctx, 'install'),
            'PERLBREW_HOME': self.path(ctx, 'home')
        })

    def is_fetched(self, ctx):
        return os.path.exists('perlbrew-installer')

    def is_built(self, ctx):
        return True

    def is_installed(self, ctx):
        return os.path.exists(self.path(ctx, 'install/bin/perlbrew'))

    #def install_env(self, ctx):
    #    Package.install_env(self, ctx)

    #    bins = self.path(ctx, 'install/perls/perl-%s/bin' % self.perl.version)
    #    assert os.path.exists(bins)
    #    ctx.runenv.PATH.insert(0, bins)

    #    # FIXME source install/etc/bashrc in SPEC
