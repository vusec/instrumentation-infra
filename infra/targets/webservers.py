import os
import shutil
import random
import time
from abc import ABCMeta, abstractmethod
from hashlib import md5
from urllib.request import urlretrieve
from ..packages import Bash, BenchmarkUtils
from ..parallel import ProcessPool, PrunPool
from ..target import Target
from ..util import run, require_program, download, qjoin, param_attrs, \
                   FatalError



class WebServer(Target, metaclass=ABCMeta):
    def __init__(self):
        self.butils = BenchmarkUtils(self)

    def dependencies(self):
        yield Bash('4.3')
        yield self.butils

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

    def add_report_args(self, parser):
        self.butils.add_report_args(parser)
        # TODO

    def run(self, ctx, instance, pool=None):
        runner = WebServerRunner(self, ctx, instance, pool)

        if ctx.args.run_type == 'serve':
            runner.run_serve()
        elif ctx.args.run_type == 'test':
            runner.run_test()
        elif ctx.args.run_type == 'ab':
            runner.run_ab()

    @abstractmethod
    def populate_rundir(self, ctx, instance):
        pass

    @abstractmethod
    def start_script(self, ctx, instance):
        pass

    @abstractmethod
    def stop_script(self, ctx, instance):
        pass


class WebServerRunner:
    @param_attrs
    def __init__(self, server, ctx, instance, pool):
        if self.pool:
            self.rundir = self.server.butils.outfile_path(ctx, instance, 'tmp')
        else:
            self.rundir = self.server.path(ctx, instance.name, 'run')
        self.rootdir = os.path.join(self.rundir, 'www')

    def outfile_path(self, outfile):
        return self.server.butils.outfile_path(self.ctx, self.instance, outfile)

    def run_serve(self):
        if self.pool:
            raise FatalError('can not serve interactively in parallel mode')

        self.create_rundir()
        self.start_server()

        try:
            self.ctx.log.info('press ctrl-C to kill the server')
            while True:
                time.sleep(100000)
        except KeyboardInterrupt:
            pass

        self.stop_server()
        self.remove_rundir()

    def run_test(self):
        self.create_rundir()

        if self.pool:
            server_command = self.bash_command(self.server_script())
            outfile = self.outfile_path('server.out')
            self.ctx.log.debug('server will log to ' + outfile)
            self.pool.run(self.ctx, server_command, jobid='server', nnodes=1,
                          outfile=outfile)

            client_command = self.bash_command(self.test_client_script())
            outfile = self.outfile_path('client.out')
            self.ctx.log.debug('client will log to ' + outfile)
            self.pool.run(self.ctx, client_command, jobid='client', nnodes=1,
                          outfile=outfile)
        else:
            self.start_server()
            self.request_and_check_index()
            self.stop_server()
            self.remove_rundir()

    def run_ab(self):
        if not self.pool:
            raise FatalError('need prun to run ApacheBench')
        elif isinstance(self.pool, ProcessPool):
            self.ctx.log.warn('ab should not run on the same machine as the '
                              'server, use prun instead for benchmarking')

        self.ctx.log.info('running ab test on two separate nodes')
        require_program('ab', 'ApacheBench is not installed')
        raise NotImplementedError

    def start_server(self):
        self.ctx.log.info('starting server')
        script = self.server.start_script(self.ctx, self.instance)
        run(self.ctx, self.bash_command(script), teeout=True)

    def stop_server(self):
        self.ctx.log.info('stopping server')
        script = self.server.stop_script(self.ctx, self.instance)
        run(self.ctx, self.bash_command(script), teeout=True)

    def bash_command(self, script):
        return ['bash', '-c', 'set -e; cd %s; %s' % (self.rundir, script)]

    def create_rundir(self):
        if os.path.exists(self.rundir):
            self.ctx.log.debug('removing old run directory ' + self.rundir)
            shutil.rmtree(self.rundir)

        self.ctx.log.debug('creating temporary run directory ' + self.rundir)
        os.makedirs(self.rundir)
        os.chdir(self.rundir)

        self.ctx.log.debug('populating run directory')
        self.server.populate_rundir(self.ctx, self.instance)
        self.populate_rootdir()

    def remove_rundir(self):
        assert not self.pool
        self.ctx.log.debug('removing run directory ' + self.rundir)
        shutil.rmtree(self.rundir)

    def populate_rootdir(self):
        require_program(self.ctx, 'dd', 'required to generate index.html')
        self.ctx.log.info('creating index.html of size %s with random '
                          'contents' % self.ctx.args.filesize)
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
        self.ctx.log.info('contents of index.html are correct')

    def server_script(self):
        start_script = self.server.start_script(self.ctx, self.instance)
        stop_script = self.server.stop_script(self.ctx, self.instance)

        if isinstance(self.pool, PrunPool):
            domain_command = 'hostname'
        else:
            domain_command = 'echo localhost'

        return '''
        set -x
        {domain_command} > domain
        {start_script}

        echo "waiting for another process to create stop_server file"
        while [ ! -e stop_server ]; do sleep 0.2; done

        echo "stopping server"
        {stop_script}
        '''.format(**locals())

    def test_client_script(self):
        domain = 'localhost'
        port = self.ctx.args.port


        return '''
        while [ ! -e {self.rundir}/nginx.pid ]; do sleep 0.1; done
        url="http://$(cat domain):{port}/index.html"
        echo "requesting $url"
        wget -q -O requested_index.html "$url"

        echo "creating stop_server to stop web server"
        touch stop_server

        if diff -q requested_index.html {self.rootdir}/index.html; then
            echo "contents of index.html are correct"
        else
            echo "ERROR: fetched content does not match generated index.html"
            exit 1
        fi
        '''.format(**locals())


class Nginx(WebServer):
    """
    The Nginx web server.

    :name: nginx
    :param version: which (open source) version to download
    """

    name = 'nginx'

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in Nginx.
    custom_allocs_flags = ['-allocs-custom-funcs=' + '.'.join((
        'ngx_alloc'        ':malloc' ':0',
        'ngx_palloc'       ':malloc' ':1',
        'ngx_palloc_small' ':malloc' ':1',
        'ngx_palloc_large' ':malloc' ':1',
    ))]

    @param_attrs
    def __init__(self, version):
        super().__init__()

    def fetch(self, ctx):
        download(ctx, 'https://nginx.org/download/' + self.tar_name())

    def is_fetched(self, ctx):
        return os.path.exists(self.tar_name())

    def tar_name(self):
        return 'nginx-' + self.version + '.tar.gz'

    def build(self, ctx, instance):
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

    def populate_rundir(self, ctx, instance):
        port = ctx.args.port
        rundir = os.getcwd()
        ctx.log.debug('writing config to nginx.conf')
        config_template = '''
lock_file {rundir}/nginx.lock;
pid {rundir}/nginx.pid;
worker_processes {ctx.args.workers};
events {{ }}
http {{
    server {{
        listen {port};
        server_name localhost;
        location / {{
        root {rundir}/www;
        }}
    }}
}}
'''
        with open('nginx.conf', 'w') as f:
            f.write(config_template.format(**locals()))

        # need logs/ directory for error+access logs
        os.mkdir('logs')

    def start_script(self, ctx, instance):
        rundir = os.getcwd()
        objdir = self.path(ctx, instance.name, 'objs')
        return '''
        {objdir}/nginx -p {rundir} -c nginx.conf
        echo "started server on port {ctx.args.port}, pid $(cat nginx.pid)"
        '''.format(**locals())

    def stop_script(self, ctx, instance):
        rundir = os.getcwd()
        objdir = self.path(ctx, instance.name, 'objs')
        #return objdir + '/nginx -c nginx.conf -s quit'
        return '''
        {objdir}/nginx -p {rundir} -c nginx.conf -s quit
        '''.format(**locals())
