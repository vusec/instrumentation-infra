import sys
import os
import shutil
import logging
import argparse
import getpass
import re
import statistics
import csv
import random
import shlex
from contextlib import redirect_stdout
from collections import defaultdict
from typing import List
from ...util import Namespace, FatalError, run, download, apply_patch, qjoin, geomean
from ...target import Target
from ...packages import Bash, Nothp, BenchmarkUtils
from ...parallel import PrunPool


class nginx(Target):
    def __init__(self, version: str = '1.15.3'):
        self.version = version
        self.name = 'nginx-%s' % version


    # build/targets/nginx-1.15.3/src/
    def srcdir(self, ctx):
        return self.path(ctx, 'src')
    
    # build/targets/nginx-1.15.3/<instance>/obj
    def objdir(self, ctx, instance):
        return self.path(ctx, instance.name, 'obj')
    
    # build/targets/nginx-1.15.3/<instance>/run.<timestamp>
    def rundir(self, ctx, instance):
        return self.path(ctx, instance.name, 'run.%s' % ctx.starttime.timestamp())

    # build/targets/nginx-1.15.3/<instance>/run.<timestamp>/files/
    def fildir(self, ctx, instance):
        return self.path(ctx, instance.name, 'run.%s' % ctx.starttime.timestamp(), 'files')

    # build/targets/nginx-1.15.3/<instance>/install
    def bindir(self, ctx, instance):
        return self.path(ctx, instance.name, 'install')

    # build/targets/nginx-1.15.3/<instance>/install/sbin/nginx
    def binary(self, ctx, instance):
        return self.path(ctx, instance.name, 'install', 'sbin', 'nginx')


    def fetch(self, ctx):
        tarname = '%s.tar.gz' % (self.name)
        
        ctx.log.debug('fetching %s' % tarname)
        download(ctx, 'http://nginx.org/download/%s' % (tarname))
        
        ctx.log.debug('extracting %s' % tarname)
        run(ctx, ['tar', '-xf', tarname])

        shutil.move(self.name, self.srcdir(ctx))
        os.remove(tarname)

    def is_fetched(self, ctx):
        return os.path.exists(self.srcdir(ctx))

    def build(self, ctx, instance):
        _srcdir = self.srcdir(ctx)
        _objdir = self.objdir(ctx, instance)
        _bindir = self.bindir(ctx, instance)

        if not os.path.exists(_objdir):
            ctx.log.debug('copying nginx src to %s' % _objdir)
            shutil.copytree(_srcdir, _objdir)

        os.chdir(_objdir)
        ctx.log.debug('configuring %s' % self.name)
        if not os.path.exists('Makefile'):
            ctx.log.debug (ctx.cflags)
            ctx.log.debug (qjoin(ctx.cflags))
            ctx.log.debug (shlex.quote(qjoin(ctx.cflags)))
            cc_opt = (qjoin(ctx.cflags))
            ld_opt = (qjoin(ctx.ldflags))
            run(ctx, ['./configure', '--prefix=%s' % _bindir, '--with-cc-opt=%s' % cc_opt, '--with-ld-opt=%s' % ld_opt], env={'CC': ctx.cc})

        ctx.log.debug('building %s' % self.name)
        run(ctx, ['make', '-j%d' % ctx.jobs])

        ctx.log.debug('installing %s' % self.name)
        run(ctx, ['make', 'install'])

    def link(self, ctx, instance):
        pass

    def binary_paths(self, ctx, instance):
        return []

    def run(self, ctx, instance):
        _binary = self.binary(ctx, instance)
        _rundir = self.rundir(ctx, instance)
        _fildir = self.fildir(ctx, instance)
        os.makedirs(_rundir, exist_ok=True)
        os.makedirs(_fildir, exist_ok=True)
        os.chdir(_rundir)

        # /build/targets/nginx-1.15.3/
        #   src/
        #   <instance>/
        #       install/
        #           bin/
        #               nginx
        #       run.<timestamp>/
        #           files/
        #               index.html
        #           nginx.conf

        workers = 1
        port = random.randint(20000,30000)
        filesize = 1024
        conf = os.path.join(_rundir,'nginx.conf')
        html = os.path.join(_fildir,'index.html')

        # write nginx.conf to (temporary) run directory
        template = """
error_log {_rundir}/error.log;
lock_file {_rundir}/nginx.lock;
pid {_rundir}/nginx.pid;
worker_processes {workers};
events {{ }}
http {{
  server {{
    listen {port};
    server_name localhost;
    location / {{
      root {_rundir}/files;
    }}
  }}
}}
"""
        with open(conf,'w') as f:
            f.write(template.format(**locals()))
            
        # populate files/index.html
        run(ctx, ['dd', 'if=/dev/urandom', 'of=%s' % html, 'bs=1', 'count=%d' % filesize])


        ctx.log.debug('starting %s' % self.name)
        run(ctx, [_binary, '-c', conf], teeout=True, allow_error=True)

        ctx.log.debug('requesting index.html')
        url = 'http://localhost:%d/index.html' % port
        run(ctx, ['wget', url], teeout=True, allow_error=True)

        ctx.log.debug('stopping %s' % self.name)
        run(ctx, [_binary, '-c', conf, '-s', 'quit'], teeout=True, allow_error=True)

        # remove run directory
        shutil.rmtree(_rundir)
