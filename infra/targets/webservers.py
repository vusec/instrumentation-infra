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
from ..packages import Bash, BenchmarkUtils, ApacheBench
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
        yield ApacheBench.default()

    def add_run_args(self, parser):
        parser.add_argument('-t', '-type',
                dest='run_type', required=True, choices=('serve', 'test', 'ab'),
                help='serve: just run the web server until it is killed\n'
                     'test: test a single fetch of randomized index.html\n'
                     'ab: run server and ApacheBench client on separate nodes '
                     '(needs prun)')

        # common options
        parser.add_argument('--port', type=int,
                default=random.randint(10000, 30000),
                help='web server port (random by default)')
        parser.add_argument('--filesize', type=str, default='64',
                help='filesize for generated index.html in bytes '
                     '(supports suffixes compatible with dd, default 64)')

        # ApacheBench options
        parser.add_argument('--ab-duration',
                metavar='SECONDS', default=10, type=int,
                help='ab test duration in seconds (default 10)')
        parser.add_argument('--ab-concurrencies',
                nargs='+', type=int, metavar='N',
                help='a list of concurrency levels to run ab with (ab -c); '
                     'start low and increment until the server is saturated')

    def add_report_args(self, parser):
        self.butils.add_report_args(parser)
        add_table_report_args(parser)

        parser.add_argument('--columns', nargs='+',
                choices=['nthreads', 'throughput', 'latency', 'duration'],
                default=['throughput', 'latency'],
                help='columns to show (default throughput,latency)')

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

    def report(self, ctx, instances, outfile, args):
        # collect results: {instance: [{nthreads:x, throughput:x, latency:x}]}
        results = self.butils.parse_logs(ctx, instances, args.rundirs)

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
        columns = [col for col in ctx.args.columns if col != 'nthreads']

        # group results by nthreads
        instances = sorted(results.keys())
        all_nthreads = sorted(set(result['nthreads']
                                  for instance_results in results.values()
                                  for result in instance_results))
        grouped = {}
        for instance, instance_results in results.items():
            for result in instance_results:
                key = result['nthreads'], instance
                for col in columns:
                    grouped.setdefault((key, col), []).append(result[col])

        # print a table with selected aggregate columns for each instance,
        # grouped by the number of threads
        header = ['threads']
        human_header = ['\n\nthreads']
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
        for nthreads in all_nthreads:
            row = [nthreads]
            for instance in instances:
                key = nthreads, instance
                for col in columns:
                    series = grouped.get((key, col), [-1])
                    for aggr_mode in ctx.args.aggregate:
                        row.append('%.3f' % aggregate_fns[aggr_mode](series))
            data.append(row)

        title = ' %s aggregated data ' % self.name
        report_table(ctx, header, human_header, data, title)


    def parse_outfile(self, ctx, instance_name, outfile):
        if not os.path.basename(outfile).startswith('ab'):
            ctx.log.debug('ignoring non-benchmark file')
            return

        with open(outfile) as f:
            outfile_contents = f.read()

        def search(regex):
            m = re.search(regex, outfile_contents, re.M)
            assert m, 'regex not found in outfile ' + outfile
            return m.group(1)

        yield {
            'duration': float(search(r'^Time taken for tests:\s+([^ ]+)')),
            'nthreads': int(search(r'^Concurrency Level:\s+(\d+)')),
            'throughput': float(search(r'^Requests per second:\s+([^ ]+)')),
            'latency': float(search(r'^Time per request:\s+([^ ]+)')),
        }


class WebServerRunner:
    @param_attrs
    def __init__(self, server, ctx, instance, pool):
        self.rundir = server.path(ctx, instance.name, 'run')
        self.rootdir = os.path.join(self.rundir, 'www')

    def outfile_path(self, outfile):
        assert self.pool
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

        if not self.ctx.args.ab_concurrencies:
            raise FatalError('need --ab-concurrencies')

        self.create_rundir()

        for i in range(self.ctx.args.iterations):
            id_suffix = '%d/%d' % (i + 1, self.ctx.args.iterations)

            server_script = self.server_script(i)
            server_command = self.bash_command(server_script)
            outfile = self.outfile_path('server.out.%d' % i)
            self.ctx.log.debug('server will log to ' + outfile)
            self.pool.run(self.ctx, server_command, outfile=outfile,
                          jobid='server-' + id_suffix, nnodes=1)

            client_command = self.bash_command(self.ab_client_script(i))
            outfile = self.outfile_path('client.out.%d' % i)
            self.ctx.log.debug('client will log to ' + outfile)
            self.pool.run(self.ctx, client_command, outfile=outfile,
                          jobid='ab-client-' + id_suffix, nnodes=1)

            self.pool.wait_all()

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

    def server_script(self, iteration=None):
        start_script = self.server.start_script(self.ctx, self.instance)
        stop_script = self.server.stop_script(self.ctx, self.instance)
        if isinstance(self.pool, PrunPool):
            # get the infiniband network IP
            host_command = 'ifconfig ib0 2>/dev/null | grep -Po "(?<=inet )[^ ]+"'
        else:
            host_command = 'echo localhost'
        suffix = '' if iteration is None else '.%d' % iteration
        port = self.ctx.args.port
        return '''
        {start_script}

        {host_command} > host{suffix}
        sync
        echo "=== serving at $(cat host{suffix}):{port}"

        echo "=== waiting for another process to create stop_server{suffix} file"
        while [ ! -e stop_server{suffix} ]; do sleep 0.1; sync; done

        echo "=== stopping server"
        {stop_script}
        '''.format(**locals())

    def test_client_script(self):
        port = self.ctx.args.port
        return '''
        while [ ! -e nginx.pid ]; do sleep 0.1; sync; done
        url="http://$(cat host):{port}/index.html"
        echo "=== requesting $url"
        wget -q -O requested_index.html "$url"

        echo "=== creating stop_server to stop web server"
        touch stop_server
        sync

        if diff -q requested_index.html {self.rootdir}/index.html; then
            echo "=== contents of index.html are correct"
        else
            echo "=== ERROR: fetched content does not match generated index.html"
            exit 1
        fi
        '''.format(**locals())

    def ab_client_script(self, iteration):
        port = self.ctx.args.port
        duration = self.ctx.args.ab_duration
        concurrencies = ' '.join(map(str, self.ctx.args.ab_concurrencies))
        num_reqs = 100000000  # something high so that ab always times out
        outfile = self.outfile_path('ab')
        suffix = '.%d' % iteration
        return '''
        while [ ! -e host{suffix} ]; do sleep 0.1; sync; done
        url="http://$(cat host{suffix}):{port}/index.html"

        echo "=== 1 second warmup run with 32 threads"
        ab -k -t 1 -c 32 "$url"

        echo "=== Benchmarking $url for {duration} seconds for threads {concurrencies}"
        echo ""
        for n in {concurrencies}; do
            echo "=== $n threads, writing to {outfile}.$n{suffix}"
            ab -k -t {duration} -n {num_reqs} -c $n "$url" > "{outfile}.$n{suffix}"
        done

        echo "=== creating stop_server{suffix} to stop web server"
        touch stop_server{suffix}
        sync
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