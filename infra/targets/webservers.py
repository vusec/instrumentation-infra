import os
import shutil
import random
import time
from abc import ABCMeta, abstractmethod
from hashlib import md5
from urllib.request import urlretrieve
from ..util import run, require_program, download, qjoin, param_attrs, \
                   FatalError
from ..target import Target


class WebServer(Target, metaclass=ABCMeta):
    def add_run_args(self, parser):
        parser.add_argument('-t', '-type',
                dest='run_type', required=True, choices=('serve', 'test', 'ab'),
                help='serve: just run the web server until it is killed\n'
                     'test: test a single fetch of randomized index.html\n'
                     'ab: run server and ApacheBench client on separate nodes '
                     '(needs prun)')

        # common options
        parser.add_argument('--port', type=int,
                default=random.randint(20000, 30000),
                help='web server port (random by default)')
        parser.add_argument('--filesize', type=str, default='64K',
                help='filesize for generated index.html '
                     '(supports suffixes compatible with dd)')

        # ApacheBench options
        parser.add_argument('--ab-duration',
                metavar='SECONDS', default=10, type=int,
                help='ab test duration in seconds (default 10)')
        parser.add_argument('--ab-concurrency',
                nargs='+', type=int, metavar='N',
                help='a list of concurrency levels to run ab with (ab -c); '
                     'start low and increment until the server is saturated')

    def run(self, ctx, instance, pool=None):
        runner = WebServerRunner(self, ctx, instance, pool)

        if ctx.args.run_type == 'serve':
            runner.run_serve()
        elif ctx.args.run_type == 'test':
            runner.run_test()
        elif ctx.args.run_type == 'ab':
            runner.run_ab()

    def rundir(self, ctx, instance, *args):
        return self.path(ctx, instance.name, 'run', *args)

    def rootdir(self, ctx, instance, *args):
        return self.rundir(ctx, instance, 'www', *args)

    @abstractmethod
    def start_server(self, ctx, instance):
        """
        Prepare the run directory and create a command to start the server.
        """
        pass

    @abstractmethod
    def stop_server(self, ctx, instance):
        """
        Kill the server previously started by :ref:`start_server`.
        """
        pass


class WebServerRunner:
    @param_attrs
    def __init__(self, server, ctx, instance, pool):
        self.rundir = server.rundir(ctx, instance)
        self.rootdir = server.rootdir(ctx, instance)

    def run_serve(self):
        if self.pool:
            raise FatalError('serve mode does not support parallel runs')
        self.ctx.log.info('running web server until killed')
        self.create_rundir()
        self.populate_rootdir()
        self.start_server()
        try:
            self.ctx.log.info('press ctrl-C to kill the server')
            while True:
                time.sleep(100000)
        except KeyboardInterrupt:
            self.stop_server()
            self.remove_rundir()

    def run_test(self):
        self.ctx.log.info('running simple local test by fetching index.html')
        if self.pool:
            raise NotImplementedError
        else:
            self.create_rundir()
            self.populate_rootdir()
            self.start_server()
            self.request_and_check_index()
            self.stop_server()
            self.remove_rundir()

    def run_ab(self):
        self.ctx.log.info('running ab test on two saparate nodes')
        if not self.pool:
            raise FatalError('need prun to run ApacheBench')
        require_program('ab', 'ApacheBench is not installed')
        raise NotImplementedError

    def create_rundir(self):
        if os.path.exists(self.rundir):
            self.ctx.log.debug('removing old run directory ' + self.rundir)
            shutil.rmtree(self.rundir)
        self.ctx.log.debug('creating temporary run directory ' + self.rundir)
        os.makedirs(self.rundir)
        os.chdir(self.rundir)

    def remove_rundir(self):
        self.ctx.log.debug('removing run directory ' + self.rundir)
        shutil.rmtree(self.rundir)

    def populate_rootdir(self):
        require_program(self.ctx, 'dd', 'required to generate index.html')
        os.makedirs(self.rootdir, exist_ok=True)
        run(self.ctx, ['dd', 'if=/dev/urandom', 'bs=1',
                       'of=%s/index.html' % self.rootdir,
                       'count=' + self.ctx.args.filesize])

    def request_and_check_index(self):
        url = 'http://localhost:%d/index.html' % self.ctx.args.port
        self.ctx.log.info('requesting ' + url)
        urlretrieve(url, 'requested_index.html')

        with open(os.path.join(self.rootdir, 'index.html'), 'rb') as f:
            expected = f.read()
        with open('requested_index.html', 'rb') as f:
            got = f.read()

        if got != expected:
            self.stop_server()
            raise FatalError('content does not match generated index.html')
        self.ctx.log.info('content of index.html is correct')

    def start_server(self):
        self.server.start_server(self.ctx, self.instance)

    def stop_server(self):
        self.server.stop_server(self.ctx, self.instance)


class Nginx(WebServer):
    """
    The Nginx web server.

    :name: nginx
    :param version: which (open source) version to download
    """

    name = 'nginx'
    config_file = 'config.conf'

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in Nginx.
    custom_allocs_flags = ['-allocs-custom-funcs=' + '.'.join((
        'ngx_alloc'        ':malloc' ':0',
        'ngx_palloc'       ':malloc' ':1',
        'ngx_palloc_small' ':malloc' ':1',
        'ngx_palloc_large' ':malloc' ':1',
    ))]

    def __init__(self, version):
        self.version = version

    def fetch(self, ctx):
        download(ctx, 'https://nginx.org/download/' + self.tar_name())

    def is_fetched(self, ctx):
        return os.path.exists(self.tar_name())

    def tar_name(self):
        return 'nginx-' + self.version + '.tar.gz'

    def build(self, ctx, instance, pool=None):
        if not os.path.exists(instance.name):
            require_program(ctx, 'tar', 'required to unpack source tarfile')
            ident = 'nginx-' + self.version
            ctx.log.debug('unpacking ' + ident)
            shutil.rmtree(ident, ignore_errors=True)
            run(ctx, ['tar', '-xf', self.tar_name()])
            shutil.move(ident, instance.name)

        # Configure if there is no Makefile or if flags changed
        os.chdir(instance.name)
        if self.should_configure(ctx):
            ctx.log.debug('no Makefile or flags changed, reconfiguring')
            run(ctx, ['./configure',
                      '--prefix=' + self.rundir(ctx, instance),
                      '--with-cc=' + ctx.cc,
                      '--with-cc-opt=' + qjoin(ctx.cflags),
                      '--with-ld-opt=' + qjoin(ctx.ldflags)])
        else:
            ctx.log.debug('same flags as before, skip reconfigure')

        run(ctx, ['make', '-j%d' % ctx.jobs, '--always-make'])

    def should_configure(self, ctx):
        try:
            with open('flags_hash') as f:
                old_hash = f.read()
        except FileNotFoundError:
            old_hash = None

        new_hash = self.hash_flags(ctx)
        if new_hash == old_hash:
            return False

        with open('flags_hash', 'w') as f:
            f.write(new_hash)
        return True

    def hash_flags(self, ctx):
        h = md5()
        h.update(b'CC=' + ctx.cc.encode('ascii'))
        h.update(b'\nCFLAGS=' + qjoin(ctx.cflags).encode('ascii'))
        h.update(b'\nLDFLAGS=' + qjoin(ctx.ldflags).encode('ascii'))
        return h.hexdigest()

    def server_bin(self, ctx, instance):
        return self.path(ctx, instance.name, 'objs', 'nginx')

    def binary_paths(self, ctx, instance):
        yield self.server_bin(ctx, instance)

    def add_run_args(self, parser):
        super().add_run_args(parser)
        parser.add_argument('--workers', type=int, default=1,
                help='number of worker processes')

    def start_server(self, ctx, instance):
        # write config file
        port = ctx.args.port
        rundir = self.rundir(ctx, instance)
        rootdir = self.rootdir(ctx, instance)
        ctx.log.debug('writing config to ' + self.config_file)
        config_template = """
error_log {rundir}/error.log;
lock_file {rundir}/nginx.lock;
pid {rundir}/nginx.pid;
worker_processes {ctx.args.workers};
events {{ }}
http {{
    server {{
        listen {port};
        server_name localhost;
        location / {{
        root {rootdir};
        }}
    }}
}}
"""
        with open(self.config_file, 'w') as f:
            f.write(config_template.format(**locals()))

        # start server
        os.mkdir('logs')
        ctx.log.debug('starting server on port %d' % port)
        run(ctx, ['../objs/nginx', '-c', self.config_file], teeout=True)
        with open('nginx.pid') as f:
            self.pid = int(f.read())
        ctx.log.info('started server on port %d, pid %d' % (port, self.pid))

    def stop_server(self, ctx, instance):
        ctx.log.info('stopping server')
        run(ctx, ['../objs/nginx', '-s', 'quit', '-c', self.config_file],
            teeout=True)
