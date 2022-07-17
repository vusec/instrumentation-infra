import os
import logging

from ...package import Package
from ...util import Namespace, run
from ..prelink import Prelink


class Relink(Package):
    """
    Relink Allows you to relocate the programs text/data to a certain address
    (if compiled with -pie):

    :identifier: relink
    :param addr: address where everything should be relocated to
    """

    def __init__(self, addr: str):
        self.addr = addr
        self.prelink = Prelink('1.0', cross_prelink_aarch64=True)

    def ident(self):
        return 'relink'

    def dependencies(self):
        yield self.prelink

    def fetch(self, ctx):
        pass

    def build(self, ctx):
        pass

    def install(self, ctx):
        pass

    def is_fetched(self, ctx):
        return True

    def is_built(self, ctx):
        return True

    def is_installed(self, ctx):
        return self.is_built(ctx)

    def configure(self, ctx: Namespace):
        """
        Set build/link flags in **ctx**. Should be called from the
        ``configure`` method of an instance. Uses post-build hooks, so any
        target compiled with this libary must implement
        :func:`infra.Target.binary_paths`.

        :param ctx: the configuration context
        """
        ctx.hooks.post_build += [self._relink_binary]

    def _relink_binary(self, ctx, binary):
        config_root = os.path.dirname(os.path.abspath(__file__))
        relink = f'{config_root}/relink.py'
        cmd = f'{relink} --static --force --binary {binary} --dest {binary} --addr={self.addr}'
        print_output = ctx.loglevel == logging.DEBUG
        run(ctx, cmd, teeout=print_output)
