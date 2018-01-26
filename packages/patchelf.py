import os
from ..package import Package
from ..util import run, download


class PatchElf(Package):
    def __init__(self, version):
        self.version = version

    def ident(self):
        return 'patchelf-' + self.version

    def fetch(self, ctx):
        os.chdir(ctx.paths.packsrc)

        if not os.path.exists(self.ident()):
            tarname = 'patchelf-%s.tar.bz2' % self.version
            download('https://nixos.org/releases/patchelf/patchelf-0.9/' + tarname)
            run(['tar', '-xf', tarname])
            os.remove(tarname)

    def build(self, ctx):
        if not self.built(ctx) and not self.installed(ctx):
            if not os.path.exists(self.src_path(ctx, 'configure')):
                os.chdir(self.src_path(ctx))
                run(['bash', 'bootstrap.sh'])

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
        return os.path.exists(self.build_path(ctx, 'src', 'patchelf'))

    def installed(self, ctx):
        return os.path.exists(self.install_path(ctx, 'bin', 'patchelf'))
