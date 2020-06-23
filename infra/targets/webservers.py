import os
import shutil
import string
import random
import time
import re
from contextlib import redirect_stdout
from abc import ABCMeta, abstractmethod
from hashlib import md5
from urllib.request import urlretrieve
from statistics import median, pstdev, mean
from multiprocessing import cpu_count
from ..commands.report import outfile_path
from ..packages import Bash, Wrk, Netcat, Scons
from ..parallel import ProcessPool, SSHPool, PrunPool
from ..target import Target
from ..util import run, download, qjoin, param_attrs, FatalError, untar
from .remote_runner import RemoteRunner, RemoteRunnerError


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

    def dependencies(self):
        yield Bash('4.3')
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

        parser.add_argument('--restart-server-between-runs',
                default=False, action='store_true',
                help='terminate and restart the server between each '
                     'benchmarking run (e.g., when benchmarking multiple '
                     'connection configurations or doing multiple iterations\n'
                     'NOTE: only supported for --parallel=ssh!')
        parser.add_argument('--disable-warmup',
                default=False, action='store_true',
                help='disable the warmup run of the server before doing actual '
                     'benchmarks. This can be useful for measuring statistics\n'
                     'NOTE: only supported for --parallel=ssh!')
        parser.add_argument('--collect-stats',
                nargs='+',
                choices=('cpu', 'cpu-proc', 'rss', 'vms'),
                help='Statistics to collect of server while running benchmarks '
                     '(disabled if not specified)\n'
                     'NOTE: only supported for --parallel=ssh!\n'
                     'cpu: CPU utilization of entire server (0..100%)\n'
                     'cpu-proc: sum of CPU utilization of all server processes '
                     '(0..nproc*100%)\n'
                     'rss: sum of Resident Set Size of all server processes\n'
                     'vms: sum of Virtual Memory Size of all server processes\n')
        parser.add_argument('--collect-stats-interval',
                type=float, default=1,
                help='seconds between measurements of statistics provided in '
                     'the --collect-stats argument. Has no effect if no '
                     'statistics are specified.\n'
                     'NOTE: only supported for --parallel=ssh!')


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
    def populate_stagedir(self, runner: 'WebServerRunner'):
        """
        Populate the staging directory (`runner.stagedir`), which will be copied
        to (or mounted on) both server and the client as their run directory
        (`runner.rundir`) later. E.g., write the server configuration file here.
        The configuration should store temporary files, such as access logs, in
        the rundir (`runner.rundir`) which will be private to each host in the
        run pool.

        :param runner: the web server runner instance calling this function
        """
        pass

    @abstractmethod
    def server_bin(self, runner: 'WebServerRunner') -> str:
        """
        Retrieve path to the server binary file.

        :param runner: the web server runner instance calling this function
        :returns: the path to the server binary
        """
        pass

    @abstractmethod
    def pid_file(self, runner: 'WebServerRunner') -> str:
        """
        Retrieve path to the PID file (a file containing the process id of the
        running web server instance).

        :param runner: the web server runner instance calling this function
        :returns: the path to the pid file
        """
        pass

    @abstractmethod
    def start_cmd(self, runner: 'WebServerRunner', foreground=False) -> str:
        """
        Generate command to start running the webserver.

        :param runner: the web server runner instance calling this function
        :param foreground: whether to start the web server in the foreground or
                           background (i.e., daemonize, the default)
        :returns: the command that starts the server
        """
        pass

    @abstractmethod
    def stop_cmd(self, runner: 'WebServerRunner') -> str:
        """
        Generate command to stop running the webserver.

        :param runner: the web server runner instance calling this function
        :returns: the command that stops the server
        """
        pass

    @abstractmethod
    def kill_cmd(self, runner: 'WebServerRunner') -> str:
        """
        Generate command to forcefully kill the running webserver.

        :param runner: the web server runner instance calling this function
        :returns: the command that kills the server
        """
        pass

    def start_script(self, runner: 'WebServerRunner'):
        """
        Generate a bash script that starts the server daemon.

        :param runner: the web server runner instance calling this function
        :returns: a bash script that starts the server daemon
        """
        start_cmd = self.start_cmd(runner)
        pid_file = self.pid_file(runner)
        return '''
        {start_cmd}
        echo -n "=== started server on port {runner.ctx.args.port}, "
        echo "pid $(cat "{pid_file}")"
        '''.format(**locals())

    def stop_script(self, runner: 'WebServerRunner'):
        """
        Generate a bash script that stops the server daemon after benchmarking.

        :param runner: the web server runner instance calling this function
        :returns: a bash script that stops the server daemon
        """
        return self.stop_cmd(runner)

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
            m = re.match(r'(\d+\.\d+)([KMGTP]?B)', s)
            assert m, 'invalid bytesize'
            size = float(m.group(1))
            unit = m.group(2)
            factors = {
                 'B': 1./1024,
                'KB': 1,
                'MB': 1024,
                'GB': 1024 * 1024,
                'TB': 1024 * 1024 * 1024,
                'PB': 1024 * 1024 * 1024 * 1024,
            }
            return size * factors[unit]

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
        tmpdir = '/tmp/infra-%s-%s' % (server.name, instance.name)

        # Directory where we stage our run directory, which will then be copied
        # to the (node-local) rundir.
        self.stagedir = os.path.join(ctx.paths.buildroot, 'run-staging',
                '%s-%s' % (server.name, instance.name))

        if self.pool:
            if isinstance(self.pool, SSHPool):
                tmpdir = self.pool.tempdir
            self.rundir = os.path.join(tmpdir, 'run')
            self.logdir = outfile_path(ctx, server, instance)
        else:
            self.rundir = os.path.join(tmpdir, 'run')
            self.logdir = os.path.join(tmpdir, 'log')

    def logfile(self, outfile):
        return os.path.join(self.logdir, outfile)

    def run_serve(self):
        if self.pool:
            if not self.ctx.args.duration:
                raise FatalError('need --duration argument')

            self.populate_stagedir()

            server_command = self.bash_command(self.standalone_server_script())
            outfile = self.logfile('server.out')
            self.ctx.log.debug('server will log to ' + outfile)
            self.pool.run(self.ctx, server_command, jobid='server', nnodes=1,
                          outfile=outfile)
        else:
            self.create_logdir()
            self.populate_stagedir()
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
            self.populate_stagedir()

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
            self.populate_stagedir()
            self.start_server()
            self.request_and_check_index()
            self.stop_server()

    def _run_bench_over_ssh(self):

        def _start_server():
            """Start the server for benchmarking, verify it is behaving
            correctly and perfom warmup run."""

            server_cmd = self.server.start_cmd(self, foreground=True)
            server.run(server_cmd, wait=False)

            # Wait for server to come up
            starttime = time.time()
            while time.time() - starttime < 5:
                test_cmd = 'curl -s {url}'.format(url=url)
                ret = client.run(test_cmd, allow_error=True)
                if ret['rv'] == 0:
                    break
                time.sleep(0.1)
            else:
                raise RemoteRunnerError('server did not come up')

            server.poll(expect_alive=True)
            with open(os.path.join(self.stagedir, 'www/index.html')) as f:
                if ret['stdout'] != f.read():
                    raise RemoteRunnerError('contents of ' + url +
                            ' do not match')

            # Do a warmup run
            if not self.ctx.args.disable_warmup:
                client.run('{wrk_path} --duration 1s --threads {wrk_threads} '
                            '--connections 400 "{url}"'
                                .format(wrk_path=wrk_path,
                                        wrk_threads=wrk_threads,
                                        url=url))

            server.poll(expect_alive=True)


        def _run_bench_client(cons, it):
            """Run workload on client, and write back the results. Optionally
            monitor statistics of the server and write back those as well."""

            self.ctx.log.info('Benchmarking {server} with {cons} connections, '
                    '#{it}'.format(cons=cons, it=it, server=self.server.name))

            if collect_stats:
                server.start_monitoring(stats=collect_stats,
                        interval=self.ctx.args.collect_stats_interval)

            ret = client.run('{wrk_path} '
                    '--latency '
                    '--duration {wrk_duration}s '
                    '--connections {cons} '
                    '--threads {wrk_threads} '
                    '"{url}"'.format(wrk_path=wrk_path,
                                     wrk_duration=wrk_duration,
                                     wrk_threads=wrk_threads,
                                     cons=cons,
                                     url=url))

            if collect_stats:
                stats = server.stop_monitoring()

            # Write results: wrk output and all of our collected stats
            resfile = lambda base: self.logfile('{base}.{cons}.{it}'
                    .format(base=base, cons=cons, it=it))
            with open(resfile('bench'), 'w') as f:
                f.write(ret['stdout'])
            for stat in collect_stats:
                with open(resfile(stat), 'w') as f:
                    vals = stats[stat]
                    if isinstance(vals[0], float):
                        vals = ['%.3f' % v for v in vals]
                    f.write('\n'.join(map(str, vals + [''])))


        assert self.rundir.startswith(self.pool.tempdir)

        tempfile = lambda *p: os.path.join(self.pool.tempdir, *p)

        client_node, server_node = self.ctx.args.ssh_nodes
        client_outfile = self.logfile('client_runner.out')
        server_outfile = self.logfile('server_runner.out')
        client_debug_file = 'client_runner_debug.out'
        server_debug_file = 'server_runner_debug.out'
        rrunner_port_client, rrunner_port_server = 20010, 20011
        rrunner_script = 'remote_runner.py'
        rrunner_script_path = tempfile(rrunner_script)
        client_cmd = ['python3', rrunner_script_path,
                '-p', rrunner_port_client,
                '-o', tempfile(client_debug_file)]
        server_cmd = ['python3', rrunner_script_path,
                '-p', rrunner_port_server,
                '-o', tempfile(server_debug_file)]
        curdir = os.path.dirname(os.path.abspath(__file__))

        url = 'http://{a.server_ip}:{a.port}/index.html'.format(a=self.ctx.args)
        wrk_path = Wrk().get_binary_path(self.ctx)
        wrk_threads = self.ctx.args.threads
        wrk_duration = self.ctx.args.duration

        collect_stats = []
        if self.ctx.args.collect_stats:
            collect_stats = ['time'] + self.ctx.args.collect_stats

        has_started_server = False

        # Create local stagedir and transfer files to other nodes.
        self.ctx.log.info('Setting up local and remote files')
        self.populate_stagedir()
        self.pool.sync_to_nodes(self.stagedir, 'run')
        self.pool.sync_to_nodes(os.path.join(curdir, rrunner_script))

        # Launch the remote runners so we can easily control each node.
        client_job = self.pool.run(self.ctx, client_cmd, jobid='client',
                nnodes=1, outfile=client_outfile, nodes=client_node,
                tunnel_to_nodes_dest=rrunner_port_client)[0]
        server_job = self.pool.run(self.ctx, server_cmd, jobid='server',
                nnodes=1, outfile=server_outfile, nodes=server_node,
                tunnel_to_nodes_dest=rrunner_port_server)[0]

        # Connect to the remote runners. SSH can be slow, so give generous
        # timeout (retry window) so we don't end up with a ConnectionRefused.
        # Client here means "connect to the remote runner server", not the
        # client/server of our webserver setup.
        self.ctx.log.info('Connecting to remote nodes')
        client = RemoteRunner(self.ctx.log, side='client',
                port=client_job.tunnel_src, timeout=10)
        server = RemoteRunner(self.ctx.log, side='client',
                port=server_job.tunnel_src, timeout=10)

        _err = None
        try:
            # Do some minor sanity checks on the remote file system of server
            server_bin = self.server.server_bin(self.ctx, self.instance)
            if not server.has_file(server_bin):
                raise RemoteRunnerError('server binary ' + server_bin +
                                        ' not present on server')

            # Copy wrk binary only as needed
            if not client.has_file(wrk_path):
                self.ctx.log.info('wrk binary not found on client, syncing...')
                self.pool.sync_to_nodes(wrk_path)
                wrk_path = tempfile('wrk')

            # Clean up any lingering server. # XXX hacky
            for s in (Nginx, ApacheHttpd, Lighttpd):
                kill_cmd = s.kill_cmd(None, self)
                server.run(kill_cmd, allow_error=True)

            # Start actual server and benchmarking!
            for cons in self.ctx.args.connections:
                for it in range(self.ctx.args.iterations):

                    if not has_started_server or \
                            self.ctx.args.restart_server_between_runs:
                        if has_started_server:
                            server.kill()
                            server.wait()
                        _start_server()
                        has_started_server = True

                    _run_bench_client(cons, it)

            server.kill()
            server.wait()

        except RemoteRunnerError as e:
            _err = e
            self.ctx.log.error('aborting tests due to error:\n' + str(e))
        except KeyboardInterrupt as e:
            self.ctx.log.error('Received KeyboardInterrupt, aborting '
                               'gracefully...\n'
                               'Note that this will wait for the last '
                               'benchmark to finish, which may take up to '
                               '{wrk_duration} seconds.'.format(**locals()))
            _err = e

        # Terminate the remote runners and clean up.
        client.close()
        server.close()
        self.pool.wait_all()

        self.ctx.log.info('Done, syncing results to ' + self.logdir)
        self.pool.sync_from_nodes(client_debug_file,
                self.logfile(client_debug_file), client_node)
        self.pool.sync_from_nodes(server_debug_file,
                self.logfile(server_debug_file), server_node)

        self.pool.cleanup_tempdirs()

        if _err:
            raise _err


    def run_bench(self):
        if not self.pool:
            raise FatalError('need --parallel= argument to run benchmark')
        elif isinstance(self.pool, SSHPool):
            if len(self.ctx.args.ssh_nodes) != 2:
                raise FatalError('need exactly 2 nodes (via --ssh-nodes)')
            if not self.ctx.args.server_ip:
                raise FatalError('need --server-ip')
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

        # Set up directory for results
        os.makedirs(self.logdir, exist_ok=True)
        self.write_log_of_config()

        if isinstance(self.pool, SSHPool):
            self._run_bench_over_ssh()
        else:
            client_outfile = self.logfile('client.out')
            server_outfile = self.logfile('server.out')

            self.populate_stagedir()

            server_script = self.wrk_server_script()
            server_command = self.bash_command(server_script)
            self.ctx.log.debug('server will log to ' + server_outfile)
            self.pool.run(self.ctx, server_command, outfile=server_outfile,
                            jobid='server', nnodes=1)

            client_command = self.bash_command(self.wrk_client_script())
            self.ctx.log.debug('client will log to ' + client_outfile)
            self.pool.run(self.ctx, client_command, outfile=client_outfile,
                            jobid='wrk-client', nnodes=1)

    def run_bench_server(self):
        if self.pool:
            raise FatalError('cannot run this command with --parallel')

        self.ctx.log.warn('another machine should run a matching bench-client')
        self.ctx.log.info('will log to %s (merge with client log)'
                          % self.logdir)

        self.populate_stagedir()
        self.write_log_of_config()
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

        self.write_log_of_config()
        run(self.ctx, self.bash_command(self.wrk_client_script()), teeout=True)

    def write_log_of_config(self):
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

    def populate_stagedir(self):
        if os.path.exists(self.stagedir):
            self.ctx.log.debug('removing old staging run directory ' +
                    self.stagedir)
            shutil.rmtree(self.stagedir)

        self.ctx.log.debug('populating local staging run directory')
        os.makedirs(self.stagedir, exist_ok=True)
        os.chdir(self.stagedir)

        os.makedirs('www', exist_ok=True)
        with open('www/index.html', 'w') as f:
            chars = string.printable
            filesize = parse_filesize(self.ctx.args.filesize)
            f.write(''.join(random.choice(chars) for i in range(filesize)))

        self.server.populate_stagedir(self)

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
        echo "=== creating local run directory"
        rm -rf "{self.rundir}"
        cp -r {self.stagedir} {self.rundir}

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
                    awk 'BEGIN {{idle=13}}
                         /%idle/ {{for(i=1;i<=NF;i++) if($i == "%idle") idle=i}}
                         /^[0-9].+all/ {{print 100-$idle; fflush()}}' \\
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

    def populate_stagedir(self, runner):
        # Nginx needs the logs/ dir to create the default error log before
        # processing the error_logs directive
        os.makedirs('logs', exist_ok=True)

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

    def pid_file(self, runner):
        return '{runner.rundir}/nginx.pid'.format(**locals())

    def start_cmd(self, runner, foreground=False):
        nginx = self.server_bin(runner.ctx, runner.instance)
        runopt = '-g "daemon off;"' if foreground else ''
        return '{nginx} -p "{runner.rundir}" -c nginx.conf {runopt}'\
                .format(**locals())

    def stop_cmd(self, runner):
        nginx = self.server_bin(runner.ctx, runner.instance)
        return '{nginx} -p "{runner.rundir}" -c nginx.conf -s quit'\
                .format(**locals())

    def kill_cmd(self, runner):
        return 'pkill -9 nginx'


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

    def populate_stagedir(self, runner):
        runner.ctx.log.debug('copying base config')
        rootdir = self.path(runner.ctx, runner.instance.name, 'install')
        copytree(rootdir, runner.stagedir)

        runner.ctx.log.debug('creating httpd.conf')
        a = runner.ctx.args
        total_threads = a.workers * a.worker_threads
        config_template = '''
        Listen {a.port}
        ErrorLog error.log
        PidFile apache.pid
        ServerName localhost
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
        with open('conf/httpd.conf', 'w') as f:
            f.write(config_template.format(**locals()))

    def pid_file(self, runner):
        return '{runner.rundir}/apache.pid'.format(**locals())

    def start_cmd(self, runner, foreground=False):
        httpd = self.path(runner.ctx, runner.instance.name,
                          'install', 'bin', 'httpd')
        runopt = '-D FOREGROUND' if foreground else '-k start'
        return '{httpd} -d "{runner.rundir}" {runopt}'.format(**locals())

    def stop_cmd(self, runner):
        httpd = self.path(runner.ctx, runner.instance.name,
                          'install', 'bin', 'httpd')
        return '{httpd} -d "{runner.rundir}" -k stop'.format(**locals())

    def kill_cmd(self, runner):
        return 'pkill -9 httpd'


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

    def populate_stagedir(self, runner):
        runner.ctx.log.debug('creating lighttpd.conf')
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

    def stop_script(self, runner):
        return '''
        kill $(cat "{runner.rundir}/lighttpd.pid")
        '''.format(**locals())

    def pid_file(self, runner):
        return '{runner.rundir}/lighttpd.pid'.format(**locals())

    def start_cmd(self, runner, foreground=False):
        lighttpd = self.server_bin(runner.ctx, runner.instance)
        runopt = '-D' if foreground else ''
        return '{lighttpd} -f "{runner.rundir}/lighttpd.conf" {runopt}'\
                .format(**locals())

    def stop_cmd(self, runner):
        # TODO better to read pidfile
        return 'pkill lighttpd'

    def kill_cmd(self, runner):
        return 'pkill -9 lighttpd'


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


def copytree(src, dst):
    """Wrapper for shutil.copytree, which does not have dirs_exist_ok until
    python 3.8."""
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def parse_filesize(filesize):
    """Convert a size in human-readable form to bytes (e.g., 4K, 2G)."""
    if isinstance(filesize, int):
        return filesize
    if not isinstance(filesize, str):
        raise FatalError('unsupported filesize type ' + repr(filesize))
    factors = { '': 1,
               'K': 1024,
               'M': 1024 * 1024,
               'G': 1024 * 1024 * 1024}
    filesize = filesize.upper()
    factor = ''
    if filesize[-1] not in string.digits:
        filesize, factor = filesize[:-1], filesize[-1]
    return int(filesize) * factors[factor]
