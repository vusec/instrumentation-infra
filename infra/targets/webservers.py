import os
import shutil
import random
import time
import re
from contextlib import redirect_stdout
from abc import ABCMeta, abstractmethod
from hashlib import md5
from urllib.request import urlretrieve
from statistics import median, pstdev, mean
from multiprocessing import cpu_count
from ..packages import Bash, BenchmarkUtils, Wrk, Netcat, Scons
from ..parallel import ProcessPool, PrunPool
from ..target import Target
from ..util import run, download, qjoin, param_attrs, FatalError, untar


class WebServer(Target, metaclass=ABCMeta):
    reportable_fields = {
        'connections':  'concurrent client connections',
        'threads':      'number of client threads making connections',
        'throughput':   'attained throughput (reqs/s)',
        'avg_latency':  'average latency (ms)',
        '50p_latency':  '50th percentile latency (ms)',
        '75p_latency':  '75th percentile latency (ms)',
        '90p_latency':  '90th percentile latency (ms)',
        '99p_latency':  '99th percentile latency (ms)',
        'transferrate': 'network traffic (KB/s)',
        'duration':     'benchmark duration (s)',
        'cpu':          'median server CPU load during benchmark (%%)',
    }
    aggregation_field = 'connections'

    def __init__(self):
        self.butils = BenchmarkUtils(self)

    def dependencies(self):
        yield Bash('4.3')
        yield self.butils
        yield Wrk()
        yield Netcat('0.7.1')

    def add_run_args(self, parser):
        parser.add_argument('-t', '-type',
                dest='run_type', required=True,
                choices=('serve', 'test', 'bench', 'bench-server', 'bench-client'),
                help='serve: just run the web server until it is killed\n'
                     'test: test a single fetch of randomized index.html\n'
                     'bench: run server and wrk client on separate nodes '
                     '(needs prun)')

        # common options
        parser.add_argument('--port', type=int,
                default=random.randint(10000, 30000),
                help='web server port (random by default)')
        parser.add_argument('--filesize', type=str, default='64',
                help='filesize for generated index.html in bytes '
                     '(supports suffixes compatible with dd, default 64)')

        # bench options
        parser.add_argument('--duration',
                metavar='SECONDS', default=10, type=int,
                help='benchmark duration in seconds (default 10)')
        parser.add_argument('--threads',
                type=int, default=1,
                help='concurrent wrk threads (distributes client load)')
        parser.add_argument('--connections',
                nargs='+', type=int,
                help='a list of concurrent wrk connections; '
                     'start low and increment until the server is saturated')
        parser.add_argument('--cleanup-time',
                metavar='SECONDS', default=0, type=int,
                help='time to wait between benchmarks (default 3)')

        # bench-client options
        parser.add_argument('--server-ip',
                help='IP of machine running matching bench-server')

    def run(self, ctx, instance, pool=None):
        runner = WebServerRunner(self, ctx, instance, pool)

        if ctx.args.run_type == 'serve':
            runner.run_serve()
        elif ctx.args.run_type == 'test':
            runner.run_test()
        elif ctx.args.run_type == 'bench':
            runner.run_bench()
        elif ctx.args.run_type == 'bench-server':
            runner.run_bench_server()
        elif ctx.args.run_type == 'bench-client':
            runner.run_bench_client()

    @abstractmethod
    def populate_logdir(self, runner: 'WebServerRunner'):
        """
        Populate the run directory which will be mounted on both server and
        the client. E.g., write the server configuration file here. The
        configuration should store temporary files, such as access logs, in
        `runner.rundir` which will be private to each host in the run pool.

        :param runner: the web server runner instance calling this function
        """
        pass

    @abstractmethod
    def start_script(self, runner: 'WebServerRunner'):
        """
        Generate a bash script that starts the server daemon.

        :param runner: the web server runner instance calling this function
        :returns: a bash script that starts the server daemon
        """
        pass

    @abstractmethod
    def stop_script(self, runner: 'WebServerRunner'):
        """
        Generate a bash script that stops the server daemon after benchmarking.

        :param runner: the web server runner instance calling this function
        :returns: a bash script that stops the server daemon
        """
        pass

    def parse_outfile(self, ctx, instance_name, outfile):
        dirname, filename = os.path.split(outfile)
        if not filename.startswith('bench.'):
            ctx.log.debug('ignoring non-benchmark file')
            return

        with open(outfile) as f:
            outfile_contents = f.read()

        def search(regex):
            m = re.search(regex, outfile_contents, re.M)
            assert m, 'regex not found in outfile ' + outfile
            return m.group(1)

        def parse_latency(s):
            m = re.match(r'(\d+\.\d+)([mun]?s)', s)
            assert m, 'invalid latency'
            latency = float(m.group(1))
            unit = m.group(2)
            if unit == 'us':
                latency /= 1000
            elif unit == 'ns':
                latency /= 1000000
            elif unit == 's':
                latency *= 1000
            return latency

        def parse_bytesize(s):
            m = re.match(r'(\d+\.\d+)([KMG]?B)', s)
            assert m, 'invalid bytesize'
            size = float(m.group(1))
            unit = m.group(2)
            if unit == 'B':
                size /= 1000
            elif unit == 'MB':
                size *= 1000
            elif unit == 'GB':
                size *= 1000000
            return size

        cpu_outfile = os.path.join(dirname, filename.replace('bench', 'cpu'))
        with open(cpu_outfile) as f:
            try:
                cpu_usages = [float(line) for line in f]
            except ValueError:
                raise FatalError('%s contains invalid lines' % cpu_outfile)

        yield {
            'threads': int(search(r'(\d+) threads and \d+ connections')),
            'connections': int(search(r'\d+ threads and (\d+) connections')),
            'avg_latency': parse_latency(search(r'^    Latency\s+([^ ]+)')),
            '50p_latency': parse_latency(search(r'^\s+50%\s+(.+)')),
            '75p_latency': parse_latency(search(r'^\s+75%\s+(.+)')),
            '90p_latency': parse_latency(search(r'^\s+90%\s+(.+)')),
            '99p_latency': parse_latency(search(r'^\s+99%\s+(.+)')),
            'throughput': float(search(r'^Requests/sec:\s+([0-9.]+)')),
            'transferrate': parse_bytesize(search(r'^Transfer/sec:\s+(.+)')),
            'duration': float(search(r'\d+ requests in ([\d.]+)s,')),
            'cpu': median(sorted(cpu_usages))
        }


class WebServerRunner:
    comm_port = 40000

    @param_attrs
    def __init__(self, server, ctx, instance, pool):
        localdir = '/tmp/infra-%s-%s' % (server.name, instance.name)
        self.rundir = os.path.join(localdir, 'run')
        if self.pool:
            self.logdir = server.butils.outfile_path(ctx, instance)
        else:
            self.logdir = os.path.join(localdir, 'log')

    def logfile(self, outfile):
        return os.path.join(self.logdir, outfile)

    def run_serve(self):
        if self.pool:
            if not self.ctx.args.duration:
                raise FatalError('need --duration argument')

            self.populate_logdir()

            server_command = self.bash_command(self.standalone_server_script())
            outfile = self.logfile('server.out')
            self.ctx.log.debug('server will log to ' + outfile)
            self.pool.run(self.ctx, server_command, jobid='server', nnodes=1,
                          outfile=outfile)
        else:
            self.create_logdir()
            self.populate_logdir()
            self.start_server()

            try:
                self.ctx.log.info('press ctrl-C to kill the server')
                while True:
                    time.sleep(100000)
            except KeyboardInterrupt:
                pass

            self.stop_server()

    def run_test(self):
        if self.pool:
            self.populate_logdir()

            server_command = self.bash_command(self.test_server_script())
            outfile = self.logfile('server.out')
            self.ctx.log.debug('server will log to ' + outfile)
            self.pool.run(self.ctx, server_command, jobid='server', nnodes=1,
                          outfile=outfile)

            client_command = self.bash_command(self.test_client_script())
            outfile = self.logfile('client.out')
            self.ctx.log.debug('client will log to ' + outfile)
            self.pool.run(self.ctx, client_command, jobid='client', nnodes=1,
                          outfile=outfile)
        else:
            self.create_logdir()
            self.populate_logdir()
            self.start_server()
            self.request_and_check_index()
            self.stop_server()

    def run_bench(self):
        if not self.pool:
            raise FatalError('need prun to run benchmark')
        elif isinstance(self.pool, ProcessPool):
            self.ctx.log.warn('the client should not run on the same machine '
                              'as the server, use prun for benchmarking')

        if not self.ctx.args.duration:
            raise FatalError('need --duration')

        if not self.ctx.args.connections:
            raise FatalError('need --connections')

        for conn in self.ctx.args.connections:
            if conn < self.ctx.args.threads:
                raise FatalError('#connections must be >= #threads (%d < %d)' %
                                 (conn, self.ctx.args.threads))

        self.populate_logdir()
        self.write_config()

        server_script = self.wrk_server_script()
        server_command = self.bash_command(server_script)
        outfile = self.logfile('server.out')
        self.ctx.log.debug('server will log to ' + outfile)
        self.pool.run(self.ctx, server_command, outfile=outfile,
                        jobid='server', nnodes=1)

        client_command = self.bash_command(self.wrk_client_script())
        outfile = self.logfile('client.out')
        self.ctx.log.debug('client will log to ' + outfile)
        self.pool.run(self.ctx, client_command, outfile=outfile,
                        jobid='wrk-client', nnodes=1)

    def run_bench_server(self):
        if self.pool:
            raise FatalError('cannot run this command with --parallel')

        self.ctx.log.warn('another machine should run a matching bench-client')
        self.ctx.log.info('will log to %s (merge with client log)'
                          % self.logdir)

        self.populate_logdir()
        self.write_config()
        run(self.ctx, self.bash_command(self.wrk_server_script()), teeout=True)

    def run_bench_client(self):
        if self.pool:
            raise FatalError('cannot run this command with --parallel')

        if not self.ctx.args.duration:
            raise FatalError('need --duration')

        if not self.ctx.args.connections:
            raise FatalError('need --connections')

        if not self.ctx.args.server_ip:
            raise FatalError('need --server-ip and --port')

        for conn in self.ctx.args.connections:
            if conn < self.ctx.args.threads:
                raise FatalError('#connections must be >= #threads (%d < %d)' %
                                 (conn, self.ctx.args.threads))

        self.ctx.log.warn('matching bench-server should be running at %s'
                          % self.ctx.args.server_ip)
        self.ctx.log.info('will log to %s (merge with server log)'
                          % self.logdir)

        self.ctx.log.debug('creating log directory')
        os.makedirs(self.logdir, exist_ok=True)
        os.chdir(self.logdir)

        with open(self.logfile('server_host'), 'w') as f:
            f.write(self.ctx.args.server_ip + '\n')

        self.write_config()
        run(self.ctx, self.bash_command(self.wrk_client_script()), teeout=True)

    def write_config(self):
        with open(self.logfile('config.txt'), 'w') as f:
            with redirect_stdout(f):
                print('server workers:    ', self.ctx.args.workers)
                print('client threads:    ', self.ctx.args.threads)
                print('client connections:', self.ctx.args.connections)
                print('benchmark duration:', self.ctx.args.duration, 'seconds')

    def start_server(self):
        self.ctx.log.info('starting server')
        script = self.wrap_start_script()
        run(self.ctx, self.bash_command(script), teeout=True)

    def stop_server(self):
        self.ctx.log.info('stopping server')
        script = self.wrap_stop_script()
        run(self.ctx, self.bash_command(script), teeout=True)

    def bash_command(self, script):
        if isinstance(self.pool, PrunPool):
            # escape for passing as: prun ... bash -c '<script>'
            script = script.replace('$', '\$').replace('"', '\\"')

        return ['bash', '-c', 'set -e; cd %s; %s' % (self.logdir, script)]

    def create_logdir(self):
        assert not self.pool
        if os.path.exists(self.logdir):
            self.ctx.log.debug('removing old log directory ' + self.logdir)
            shutil.rmtree(self.logdir)
        self.ctx.log.debug('creating log directory ' + self.logdir)
        os.makedirs(self.logdir)

    def populate_logdir(self):
        self.ctx.log.debug('populating log directory')
        os.makedirs(self.logdir, exist_ok=True)
        os.chdir(self.logdir)
        self.server.populate_logdir(self)

    def request_and_check_index(self):
        assert not self.pool
        url = 'http://localhost:%d/index.html' % self.ctx.args.port
        self.ctx.log.info('requesting ' + url)
        urlretrieve(url, 'requested_index.html')

        with open(os.path.join(self.rundir, 'www', 'index.html'), 'rb') as f:
            expected = f.read()
        with open('requested_index.html', 'rb') as f:
            got = f.read()

        if got != expected:
            self.stop_server()
            raise FatalError('content does not match generated index.html')
        self.ctx.log.info('contents of index.html are correct')

    def wrap_start_script(self):
        start_script = self.server.start_script(self)
        host_command = 'echo localhost'
        if isinstance(self.pool, PrunPool):
            # get the infiniband network IP
            host_command = 'ifconfig ib0 2>/dev/null | grep -Po "(?<=inet )[^ ]+"'
        return '''
        echo "=== creating local run directory with index.html"
        rm -rf "{self.rundir}"
        mkdir -p "{self.rundir}/www"
        dd if=/dev/urandom bs=1 count={filesize} of="{self.rundir}/www/index.html"

        echo "=== starting web server"
        {start_script}
        server_host="$({host_command})"
        echo "=== serving at $server_host:{port}"
        '''.format(**vars(self.ctx.args), **locals())

    def wrap_stop_script(self):
        stop_script = self.server.stop_script(self)
        return '''
        echo "=== received stop signal, stopping web server"
        {stop_script}

        if [ -s "{self.rundir}/error.log" ]; then
            echo "=== there were errors, copying log to {self.logdir}/error.log"
            cp "{self.rundir}/error.log" .
        fi

        echo "=== removing local run directory"
        rm -rf "{self.rundir}"
        '''.format(**vars(self.ctx.args), **locals())

    def server_script(self, body_template, **fmt_args):
        start_script = self.wrap_start_script()
        stop_script = self.wrap_stop_script()
        return ('''
        comm_recv() {{ netcat --close -l -p {self.comm_port} || true; }}

        {start_script}

        echo "=== writing hostname to file"
        echo "$server_host" > server_host
        sync

        ''' + body_template + '''

        {stop_script}
        ''').format(**vars(self.ctx.args), **locals(), **fmt_args)

    def client_script(self, body_template, **fmt_args):
        return ('''
        comm_send() {{
            read msg
            while ! netcat --close "$server_host" {self.comm_port} \\
                    <<< "$msg" 2>/dev/null; do :; done
        }}

        echo "=== waiting for server to write its IP to file"
        while [ ! -e server_host ]; do sleep 0.1; sync; done
        server_host="$(cat server_host)"

        ''' + body_template + '''

        echo "=== sending stop signal to server"
        comm_send <<< stop
        ''').format(**vars(self.ctx.args), **locals(), **fmt_args)

    def test_server_script(self):
        return self.server_script('''
        echo "=== copying index.html to log directory for client"
        cp "{self.rundir}/www/index.html" .

        echo "=== waiting for stop signal from client"
        test "$(comm_recv)" = stop
        ''')

    def test_client_script(self):
        return self.client_script('''
        url="http://$server_host:{port}/index.html"
        echo "=== requesting $url"
        wget -q -O requested_index.html "$url"
        ''') + \
        '''
        if diff -q index.html requested_index.html; then
            echo "=== contents of index.html are correct"
        else
            echo "=== ERROR: content mismatch:"
            echo "  $(pwd)/requested_index.html"
            echo "does not match:"
            echo "  $(pwd)/index.html"
            exit 1
        fi
        '''.format(**locals())

    def wrk_server_script(self):
        return self.server_script('''
        echo "=== waiting for first work rate"
        rate="$(comm_recv)"
        while [ "$rate" != stop ]; do
            echo "=== logging cpu usage to cpu.$rate for {duration} seconds"
            {{ timeout {duration} mpstat 1 {duration} || true; }} | \\
                    awk '/^[0-9].+all/ {{print 100-$13; fflush()}}' \\
                    > "cpu.$rate"

            echo "=== waiting for next work rate"
            rate="$(comm_recv)"
        done
        ''')

    def wrk_client_script(self):
        conns = ' '.join(str(c) for c in self.ctx.args.connections)
        return self.client_script('''
        url="http://$server_host:{port}/index.html"
        echo "=== will benchmark $url for {duration} seconds for each work rate"

        echo "=== 3 second warmup run"
        wrk --duration 3s --threads {threads} --connections 400 "$url"

        for i in $(seq 1 1 {iterations}); do
            for connections in {conns}; do
                if [ {cleanup_time} -gt 0 ]; then
                    echo "=== waiting {cleanup_time} seconds for server to clean up"
                    sleep {cleanup_time}
                fi

                echo "=== sending work rate $connections.$i to server"
                comm_send <<< "$connections.$i"

                echo "=== starting benchmark"
                set -x
                wrk --duration {duration}s --connections $connections \\
                        --threads {threads} --latency "$url" \\
                        > bench.$connections.$i
                set +x
            done
        done
        ''', conns=conns)

    def standalone_server_script(self):
        return self.server_script('''
        echo "=== logging cpu usage to cpu for {duration} seconds"
        {{ timeout {duration} mpstat 1 {duration} || true; }} | \\
                awk '/^[0-9].+all/ {{print 100-$13; fflush()}}' \\
                > cpu
        ''')


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
            ctx.log.debug('unpacking nginx-' + self.version)
            shutil.rmtree('nginx-' + self.version, ignore_errors=True)
            untar(ctx, self.tar_name(), instance.name, remove=False)

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
                help='number of worker processes (default 1)')
        parser.add_argument('--worker-connections', type=int, default=1024,
                help='number of connections per worker process (default 1024)')

    def populate_logdir(self, runner):
        runner.ctx.log.debug('creating nginx.conf')
        a = runner.ctx.args
        config_template = '''
        error_log {runner.rundir}/error.log error;
        lock_file {runner.rundir}/nginx.lock;
        pid {runner.rundir}/nginx.pid;
        worker_processes {a.workers};
        worker_cpu_affinity auto;
        events {{
            worker_connections {a.worker_connections};
            use epoll;
        }}
        http {{
            server {{
                listen {a.port};
                server_name localhost;
                sendfile on;
                access_log off;
                keepalive_requests 500;
                keepalive_timeout 500ms;
                location / {{
                    root {runner.rundir}/www;
                }}
            }}
        }}
        '''
        with open('nginx.conf', 'w') as f:
            f.write(config_template.format(**locals()))

    def start_script(self, runner):
        objdir = self.path(runner.ctx, runner.instance.name, 'objs')
        return '''
        # create logs/ dir, nginx needs it to create the default error log
        # before processing the error_logs directive
        mkdir -p "{runner.rundir}/logs"

        cp nginx.conf "{runner.rundir}"

        {objdir}/nginx -p "{runner.rundir}" -c nginx.conf
        echo -n "=== started server on port {runner.ctx.args.port}, "
        echo "pid $(cat {runner.rundir}/nginx.pid)"
        '''.format(**locals())

    def stop_script(self, runner):
        objdir = self.path(runner.ctx, runner.instance.name, 'objs')
        return '''
        {objdir}/nginx -p "{runner.rundir}" -c nginx.conf -s quit
        '''.format(**locals())


class ApacheHttpd(WebServer):
    """
    Apache web server. Builds APR and APR Util libraries as binary dependencies.

    :name: apache
    :param version: apache httpd version
    :param apr_version: APR version
    :param apr_util_version: APR Util version
    :param module: a list of modules to enable (default: "few", any modules will
                   be statically linked)
    """

    name = 'apache'

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in Apache.
    custom_allocs_flags = ['-allocs-custom-funcs=' + '.'.join((
        'apr_palloc'        ':malloc' ':1',
        'apr_palloc_debug'  ':malloc' ':1',
        'apr_pcalloc'       ':calloc' ':1',
        'apr_pcalloc_debug' ':calloc' ':1',
    ))]

    @param_attrs
    def __init__(self, version: str, apr_version: str, apr_util_version: str,
                 modules=['few']):
        super().__init__()

    def fetch(self, ctx):
        _fetch_apache(ctx, 'httpd', 'httpd-' + self.version, 'src')
        _fetch_apache(ctx, 'apr', 'apr-' + self.apr_version, 'src/srclib/apr')
        _fetch_apache(ctx, 'apr', 'apr-util-' + self.apr_util_version,
                      'src/srclib/apr-util')

    def is_fetched(self, ctx):
        return os.path.exists('src')

    def build(self, ctx, instance):
        # create build directory
        objdir = os.path.join(instance.name, 'obj')
        if os.path.exists(objdir):
            ctx.log.debug('removing old object directory ' + objdir)
            shutil.rmtree(objdir)
        ctx.log.debug('creating object directory ' + objdir)
        os.makedirs(objdir)
        os.chdir(objdir)

        # set environment for configure scripts
        prefix = self.path(ctx, instance.name, 'install')
        env = {
            'CC': ctx.cc,
            'CFLAGS': qjoin(ctx.cflags),
            'LDFLAGS': qjoin(ctx.lib_ldflags),
            'HTTPD_LDFLAGS': qjoin(ctx.ldflags),
            'AR': ctx.ar,
            'RANLIB': ctx.ranlib,
        }

        # build APR
        ctx.log.info('building %s-%s-apr' % (self.name, instance.name))
        os.mkdir('apr')
        os.chdir('apr')
        run(ctx, ['../../../src/srclib/apr/configure',
                  '--prefix=' + prefix,
                  '--enable-static',
                  '--enable-shared=no'], env=env)
        run(ctx, 'make -j%d' % ctx.jobs)
        run(ctx, 'make install')
        os.chdir('..')

        # build APR-Util
        ctx.log.info('building %s-%s-apr-util' % (self.name, instance.name))
        os.mkdir('apr-util')
        os.chdir('apr-util')
        run(ctx, ['../../../src/srclib/apr-util/configure',
                  '--prefix=' + prefix,
                  '--with-apr=' + prefix], env=env)
        run(ctx, 'make -j%d' % ctx.jobs)
        run(ctx, 'make install')
        os.chdir('..')

        # build httpd web server
        ctx.log.info('building %s-%s-httpd' % (self.name, instance.name))
        os.mkdir('httpd')
        os.chdir('httpd')
        run(ctx, ['../../../src/configure',
                  '--prefix=' + prefix,
                  '--with-apr=' + prefix,
                  '--with-apr-util=' + prefix,
                  '--enable-modules=none',
                  '--enable-mods-static=' + qjoin(self.modules)], env=env)
        run(ctx, 'make -j%d' % ctx.jobs)
        run(ctx, 'make install')
        os.chdir('..')

    def server_bin(self, ctx, instance):
        return self.path(ctx, instance.name, 'install', 'bin', 'httpd')

    def binary_paths(self, ctx, instance):
        yield self.server_bin(ctx, instance)

    def add_run_args(self, parser):
        super().add_run_args(parser)
        nproc = cpu_count()
        parser.add_argument('--workers', type=int, default=nproc,
                help='number of worker processes '
                     '(ServerLimit, default %d)' % nproc)
        parser.add_argument('--worker-threads', type=int, default=25,
                help='number of connection threads per worker process '
                     '(ThreadsPerChild, default 25)')

    def populate_logdir(self, runner):
        runner.ctx.log.debug('creating httpd.conf')
        a = runner.ctx.args
        total_threads = a.workers * a.worker_threads
        config_template = '''
        Listen {a.port}
        ErrorLog error.log
        PidFile apache.pid
        DocumentRoot www
        ServerLimit {a.workers}
        StartServers {a.workers}
        ThreadsPerChild {a.worker_threads}
        ThreadLimit {a.worker_threads}
        MaxRequestWorkers {total_threads}
        MaxSpareThreads {total_threads}
        KeepAlive On
        KeepAliveTimeout 500ms
        MaxKeepAliveRequests 500
        EnableSendfile On
        Timeout 1
        '''
        with open('httpd.conf', 'w') as f:
            f.write(config_template.format(**locals()))

    def start_script(self, runner):
        rootdir = self.path(runner.ctx, runner.instance.name, 'install')
        return '''
        cp -r "{rootdir}/conf" "{runner.rundir}"
        cp httpd.conf "{runner.rundir}"/conf

        {rootdir}/bin/httpd -d "{runner.rundir}" -k start
        echo -n "=== started server on port {runner.ctx.args.port}, "
        echo "pid $(cat "{runner.rundir}/apache.pid")"
        '''.format(**locals())

    def stop_script(self, runner):
        rootdir = self.path(runner.ctx, runner.instance.name, 'install')
        return '''
        {rootdir}/bin/httpd -d "{runner.rundir}" -k stop
        '''.format(**locals())


class Lighttpd(WebServer):
    """
    TODO: docs
    """

    name = 'lighttpd'

    @param_attrs
    def __init__(self, version):
        super().__init__()

    def dependencies(self):
        yield from super().dependencies()
        yield Scons.default()

    def fetch(self, ctx):
        minor_version = re.match(r'(\d+\.\d+)\.\d+', self.version).group(1)
        download(ctx, 'https://download.lighttpd.net/lighttpd/releases-%s.x/%s' %
                      (minor_version, self.tar_name()))

    def is_fetched(self, ctx):
        return os.path.exists(self.tar_name())

    def tar_name(self):
        return 'lighttpd-' + self.version + '.tar.gz'

    def build(self, ctx, instance):
        if not os.path.exists(instance.name):
            ctx.log.debug('unpacking lighttpd-' + self.version)
            shutil.rmtree('lighttpd-' + self.version, ignore_errors=True)
            untar(ctx, self.tar_name(), instance.name, remove=False)

        os.chdir(instance.name)

        # remove old build directory to force a rebuild
        if os.path.exists('sconsbuild'):
            ctx.log.debug('removing old sconsbuild directory')
            shutil.rmtree('sconsbuild')

        path = ctx.runenv.join_paths().get('PATH', [])
        cc = shutil.which(ctx.cc, path=path)
        env = {
            'CFLAGS': qjoin(ctx.cflags),
            'LDFLAGS': qjoin(ctx.ldflags),
        }
        run(ctx, ['scons', '-j', ctx.jobs,
                  'CC=' + cc,
                  'with_pcre=no',
                  'build_static=yes',
                  'build_dynamic=no'], env=env)

    def server_bin(self, ctx, instance):
        return self.path(ctx, instance.name,
                         'sconsbuild', 'static', 'build', 'lighttpd')

    def binary_paths(self, ctx, instance):
        yield self.server_bin(ctx, instance)

    def add_run_args(self, parser):
        super().add_run_args(parser)
        parser.add_argument('--workers', type=int, default=1,
                help='number of worker processes (default 1)')
        parser.add_argument('--server-connections', type=int, default=2048,
                help='number of concurrent connections to the server (default 2048)')

    def populate_logdir(self, runner):
        runner.ctx.log.debug('creating nginx.conf')
        a = runner.ctx.args
        max_fds = 2 * a.server_connections
        config_template = '''
        var.rundir             = "{runner.rundir}"

        server.port            = {a.port}
        server.document-root   = var.rundir + "/www"
        server.errorlog        = var.rundir + "/error.log"
        server.pid-file        = var.rundir + "/lighttpd.pid"
        server.event-handler   = "linux-sysepoll"
        server.network-backend = "sendfile"

        server.max-worker              = {a.workers}
        server.max-connections         = {a.server_connections}
        server.max-fds                 = {max_fds}
        server.max-keep-alive-requests = 500
        server.max-keep-alive-idle     = 1
        server.max-read-idle           = 1
        server.max-write-idle          = 1
        '''
        with open('lighttpd.conf', 'w') as f:
            f.write(config_template.format(**locals()))

    def start_script(self, runner):
        server_bin = self.server_bin(runner.ctx, runner.instance)
        return '''
        cp lighttpd.conf "{runner.rundir}"

        {server_bin} -f "{runner.rundir}"/lighttpd.conf
        echo -n "=== started server on port {runner.ctx.args.port}, "
        echo "pid $(cat "{runner.rundir}/lighttpd.pid")"
        '''.format(**locals())

    def stop_script(self, runner):
        return '''
        kill $(cat "{runner.rundir}/lighttpd.pid")
        '''.format(**locals())


def median_absolute_deviation(numbers):
    assert len(numbers) > 0
    med = median(numbers)
    return median(abs(x - med) for x in numbers)


def stdev_percent(numbers):
    return 100 * pstdev(numbers) / mean(numbers)


def _fetch_apache(ctx, repo, basename, dest):
    tarname = basename + '.tar.bz2'
    download(ctx, 'http://apache.cs.uu.nl/%s/%s' % (repo, tarname))
    untar(ctx, tarname, dest)
