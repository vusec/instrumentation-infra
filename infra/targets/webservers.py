import os
import shutil
import random
import time
import re
from itertools import chain, zip_longest
from contextlib import redirect_stdout
from abc import ABCMeta, abstractmethod
from hashlib import md5
from urllib.request import urlretrieve
from statistics import median, pstdev, mean
from ..packages import Bash, BenchmarkUtils, Wrk2
from ..parallel import ProcessPool, PrunPool
from ..target import Target
from ..util import run, require_program, download, qjoin, param_attrs, \
                   FatalError, add_table_report_args, report_table


class WebServer(Target, metaclass=ABCMeta):
    def __init__(self):
        self.butils = BenchmarkUtils(self)

    def dependencies(self):
        yield Bash('4.3')
        yield self.butils
        yield Wrk2()

    def add_run_args(self, parser):
        parser.add_argument('-t', '-type',
                dest='run_type', required=True, choices=('serve', 'test', 'bench'),
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

        # wrk options
        parser.add_argument('--duration',
                metavar='SECONDS', default=10, type=int,
                help='ab test duration in seconds (default 10)')
        parser.add_argument('--threads',
                type=int, default=1,
                help='concurrent wrk threads (distributes client load)')
        parser.add_argument('--connections',
                type=int, default=1,
                help='concurrent wrk connections (simulates concurrent clients)')
        parser.add_argument('--rates',
                nargs='+', type=int, metavar='N', required=True,
                help='a list of throughputs to generate in 1000 reqs/sec; '
                     'start low and increment until the server is saturated')

    def add_report_args(self, parser):
        self.butils.add_report_args(parser)
        add_table_report_args(parser)

        parser.add_argument('-c', '--columns', nargs='+',
                choices=['rate', 'throughput', 'avg_latency', '99p_latency',
                         'transferrate', 'duration', 'cpu'],
                default=['throughput', '99p_latency'],
                help='''columns to show:
                    rate:         Desired throughput (reqs/s),
                    throughput:   Attained throughput (reqs/s),
                    avg_latency:  Average latency (ms),
                    99p_latency:  99th percentile latency (ms),
                    transferrate: Network traffic (KB/s),
                    duration:     Benchmark duration (s),
                    cpu:          Maximum server CPU load during benchmark (%%)''')

        report_modes = parser.add_mutually_exclusive_group()
        report_modes.add_argument('--aggregate', nargs='+',
                choices=['mean', 'median', 'stdev', 'mad', 'min', 'max', 'sum'],
                default=['median'],
                help='aggregation methods for columns')
        report_modes.add_argument('--raw', action='store_true',
                help='output all data points instead of aggregates')

    def run(self, ctx, instance, pool=None):
        runner = WebServerRunner(self, ctx, instance, pool)

        if ctx.args.run_type == 'serve':
            runner.run_serve()
        elif ctx.args.run_type == 'test':
            runner.run_test()
        elif ctx.args.run_type == 'bench':
            runner.run_bench()

    @abstractmethod
    def populate_rundir(self, ctx, instance):
        pass

    @abstractmethod
    def start_script(self, ctx, instance):
        pass

    @abstractmethod
    def stop_script(self, ctx, instance):
        pass

    def report(self, ctx, instances, outfile, args):
        # collect results: {instance: [{connections:x, throughput:x, latency:x}]}
        results = self.butils.parse_logs(ctx, instances, args.rundirs,
                read_cache=False)

        with redirect_stdout(outfile):
            if ctx.args.raw:
                self.report_raw(ctx, results)
            else:
                self.report_aggregate(ctx, results)

    def report_raw(self, ctx, results):
        columns = ctx.args.columns
        instances = sorted(results.keys())

        header = []
        human_header = []
        for instance in instances:
            prefix = instance + '\n'
            for col in columns:
                header.append('%s_%s' % (instance, col))
                human_header.append(prefix + col)
                prefix = '\n'

        rows = {}
        for instance in instances:
            rows[instance] = sorted(tuple(r[col] for col in columns)
                                    for r in results[instance])

        instance_rows = [rows[i] for i in instances]
        joined_rows = []
        for parts in zip_longest(*instance_rows, fillvalue=['', '']):
            joined_rows.append(list(chain.from_iterable(parts)))

        title = ' %s raw data ' % self.name
        report_table(ctx, header, human_header, joined_rows, title)

    def report_aggregate(self, ctx, results):
        columns = [col for col in ctx.args.columns if col != 'rate']

        # group results by connections
        instances = sorted(results.keys())
        all_rates = sorted(set(result['rate']
                               for instance_results in results.values()
                               for result in instance_results))
        grouped = {}
        for instance, instance_results in results.items():
            for result in instance_results:
                key = result['rate'], instance
                for col in columns:
                    grouped.setdefault((key, col), []).append(result[col])

        # print a table with selected aggregate columns for each instance,
        # grouped by the rate
        header = ['rate']
        human_header = ['\n\nrate']
        for instance in instances:
            for i, col in enumerate(columns):
                prefix = '%s\n%s\n' % ('' if i else instance, col)
                for aggr_mode in ctx.args.aggregate:
                    header += ['%s_%s_%s' % (instance, col, aggr_mode)]
                    human_header += [prefix + aggr_mode]
                    prefix = '\n\n'

        aggregate_fns = {'mean': mean, 'median': median,
                         'stdev': pstdev, 'mad': median_absolute_deviation,
                         'min': min, 'max': max, 'sum': sum}
        data = []
        for connections in all_rates:
            row = [connections]
            for instance in instances:
                key = connections, instance
                for col in columns:
                    series = grouped.get((key, col), [-1])
                    for aggr_mode in ctx.args.aggregate:
                        row.append('%.3f' % aggregate_fns[aggr_mode](series))
            data.append(row)

        title = ' %s aggregated data ' % self.name
        report_table(ctx, header, human_header, data, title)


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

        rate = int(filename.split('.')[1])
        cpu_outfile = os.path.join(dirname, filename.replace('bench', 'cpu'))
        max_cpu_usage = 0.0
        for line in open(cpu_outfile):
            try:
                max_cpu_usage = max(max_cpu_usage, float(line))
            except ValueError:
                raise FatalError('%s countains invalid lines' % cpu_outfile)
        yield {
            'rate': rate,
            'threads': int(search(r'(\d+) threads and \d+ connections')),
            'connections': int(search(r'\d+ threads and (\d+) connections')),
            'avg_latency': parse_latency(search(r'^    Latency\s+([^ ]+)')),
            '99p_latency': parse_latency(search(r'^ *99\.000%\s+(.+)')),
            'throughput': float(search(r'^Requests/sec:\s+([0-9.]+)')),
            'transferrate': parse_bytesize(search(r'^Transfer/sec:\s+(.+)')),
            'duration': float(search(r'\d+ requests in ([\d.]+)s,')),
            'cpu': max_cpu_usage
        }


class WebServerRunner:
    comm_port = 40000

    @param_attrs
    def __init__(self, server, ctx, instance, pool):
        self.rundir = server.path(ctx, instance.name, 'run')
        self.rootdir = os.path.join(self.rundir, 'www')
        if self.pool:
            self.logdir = os.path.dirname(server.butils.outfile_path(ctx, instance, 'x'))
        else:
            self.logdir = self.rundir

    def outfile_path(self, outfile):
        assert self.pool
        return os.path.join(self.logdir, outfile)

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
            server_command = self.bash_command(self.test_server_script())
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

    def run_bench(self):
        if not self.pool:
            raise FatalError('need prun to run benchmark')
        elif isinstance(self.pool, ProcessPool):
            self.ctx.log.warn('the client should not run on the same machine '
                              'as the server, use prun for benchmarking')

        if self.ctx.args.connections < self.ctx.args.threads:
            raise FatalError('#connections must be >= #threads')


        self.create_rundir()

        server_script = self.wrk_server_script()
        server_command = self.bash_command(server_script)
        outfile = self.outfile_path('server.out')
        self.ctx.log.debug('server will log to ' + outfile)
        self.pool.run(self.ctx, server_command, outfile=outfile,
                        jobid='server', nnodes=1)

        client_command = self.bash_command(self.wrk_client_script())
        outfile = self.outfile_path('client.out')
        self.ctx.log.debug('client will log to ' + outfile)
        self.pool.run(self.ctx, client_command, outfile=outfile,
                        jobid='wrk-client', nnodes=1)

    def start_server(self):
        self.ctx.log.info('starting server')
        script = self.server.start_script(self.ctx, self.instance)
        run(self.ctx, self.bash_command(script), teeout=True)

    def stop_server(self):
        self.ctx.log.info('stopping server')
        script = self.server.stop_script(self.ctx, self.instance)
        run(self.ctx, self.bash_command(script), teeout=True)

    def bash_command(self, script):
        if isinstance(self.pool, PrunPool):
            # escape for passing as: prun ... bash -c '<script>'
            script = script.replace('$', '\$').replace('"', '\\"')

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

    def test_server_script(self):
        start_script = self.server.start_script(self.ctx, self.instance)
        stop_script = self.server.stop_script(self.ctx, self.instance)
        if isinstance(self.pool, PrunPool):
            # get the infiniband network IP
            host_command = 'ifconfig ib0 2>/dev/null | grep -Po "(?<=inet )[^ ]+"'
        else:
            host_command = 'echo localhost'
        serve_port = self.ctx.args.port
        hostfile = self.outfile_path('server_host')
        return '''
        comm_recv() {{ nc -l {self.comm_port}; }}

        echo "=== starting web server"
        {start_script}
        server_host="$({host_command})"
        echo "=== serving at $server_host:{serve_port}"

        echo "=== writing hostname to file"
        echo $server_host > {hostfile}
        sync

        echo "=== waiting for stop signal from client"
        comm_recv

        echo "=== received stop signal, stopping web server"
        {stop_script}
        '''.format(**locals())

    def test_client_script(self):
        serve_port = self.ctx.args.port
        hostfile = self.outfile_path('server_host')
        return '''
        comm_send() {{ nc $server_host {self.comm_port}; }}

        echo "=== waiting for server to write its IP to file"
        while [ ! -e {hostfile} ]; do sleep 0.1; sync; done
        server_host=$(cat {hostfile})

        url="http://$server_host:{serve_port}/index.html"
        echo "=== requesting $url"
        wget -q -O requested_index.html "$url"

        echo "=== sending stop signal to server"
        comm_send <<< stop

        if diff -q requested_index.html {self.rootdir}/index.html; then
            echo "=== contents of index.html are correct"
        else
            echo "=== ERROR: fetched content does not match generated index.html"
            exit 1
        fi
        '''.format(**locals())

    def wrk_server_script(self):
        start_script = self.server.start_script(self.ctx, self.instance)
        stop_script = self.server.stop_script(self.ctx, self.instance)
        if isinstance(self.pool, PrunPool):
            # get the infiniband network IP
            host_command = 'ifconfig ib0 2>/dev/null | grep -Po "(?<=inet )[^ ]+"'
        else:
            host_command = 'echo localhost'
        serve_port = self.ctx.args.port
        hostfile = self.outfile_path('server_host')
        return '''
        comm_recv() {{ nc -l {self.comm_port}; }}

        echo "=== starting web server"
        {start_script}
        server_host="$({host_command})"
        echo "=== serving at $server_host:{serve_port}"

        echo "=== writing hostname to file"
        echo $server_host > "{hostfile}"
        sync

        echo "=== waiting for first work rate"
        rate=$(comm_recv)
        while [ x$rate != xstop ]; do
            mpstat 1 | awk '/^[0-9].+all/ {{print 100-$13; fflush()}}' \\
                    > {self.logdir}/cpu.$rate &

            echo "=== waiting for next work rate"
            rate=$(comm_recv)

            jobs -l | awk '/^\\[/{{print $2}} /^ /{{print $1}}' | xargs kill
            jobs
        done

        echo "=== received stop signal, stopping web server"
        {stop_script}
        '''.format(**locals())

    def wrk_client_script(self):
        serve_port = self.ctx.args.port
        iterations = self.ctx.args.iterations
        threads = self.ctx.args.threads
        duration = self.ctx.args.duration
        connections = self.ctx.args.connections
        rates = ' '.join(str(c) for c in self.ctx.args.rates)
        hostfile = self.outfile_path('server_host')
        return '''
        comm_send() {{ nc "$server_host" {self.comm_port}; }}

        echo "=== waiting for server to write its IP to file"
        while [ ! -e "{hostfile}" ]; do sleep 0.1; sync; done
        server_host="$(cat {hostfile})"

        url="http://$server_host:{serve_port}/index.html"
        echo "=== will benchmark $url for {duration} seconds for each work rate"

        echo "=== 2 second warmup run"
        wrk -d 2 -c 8 -R 1000k "$url"

        for i in $(seq 1 1 {iterations}); do
            for rate in {rates}; do
                echo "=== sending work rate $rate.$i to server"
                comm_send <<< "$rate.$i"

                sleep 0.1  # wait for server to start logging cpu usage

                echo "=== starting benchmark"
                set -x
                wrk -d {duration}s -c {connections} -R "$rate"k "$url" --latency \\
                        > {self.logdir}/bench.$rate.$i
                set +x
            done
        done

        echo "=== sending stop signal to server"
        comm_send <<< stop
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
        events {{
            worker_connections 1024;
            use epoll;
        }}
        http {{
            server {{
                listen {port};
                server_name localhost;
                sendfile on;
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


def median_absolute_deviation(numbers):
    assert len(numbers) > 0
    med = median(numbers)
    return median(abs(x - med) for x in numbers)
