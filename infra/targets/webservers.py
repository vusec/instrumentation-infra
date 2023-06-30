import argparse
import os
import random
import re
import shutil
import string
import time
from abc import ABCMeta, abstractmethod
from contextlib import redirect_stdout
from hashlib import md5
from multiprocessing import cpu_count
from statistics import mean, median, pstdev
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Union
from urllib.request import urlretrieve

from ..commands.report import outfile_path
from ..context import Context
from ..instance import Instance
from ..package import Package
from ..packages import Bash, Netcat, Scons, Wrk
from ..parallel import Job, Pool, ProcessPool, PrunPool, SSHJob, SSHPool
from ..target import Target
from ..util import FatalError, ResultDict, download, join_env_paths, qjoin, run, untar
from .remote_runner import RemoteRunner, RemoteRunnerError


class WebServer(Target, metaclass=ABCMeta):
    aggregation_field = "connections"

    def reportable_fields(self) -> Mapping[str, str]:
        return {
            "connections": "concurrent client connections",
            "threads": "number of client threads making connections",
            "throughput": "attained throughput (reqs/s)",
            "avg_latency": "average latency (ms)",
            "50p_latency": "50th percentile latency (ms)",
            "75p_latency": "75th percentile latency (ms)",
            "90p_latency": "90th percentile latency (ms)",
            "99p_latency": "99th percentile latency (ms)",
            "transferrate": "network traffic (KB/s)",
            "duration": "benchmark duration (s)",
            "cpu": "median server CPU load during benchmark (%%)",
        }

    def dependencies(self) -> Iterator[Package]:
        yield Bash("4.3")
        yield Wrk()
        yield Netcat("0.7.1")

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-t",
            "-type",
            dest="run_type",
            required=True,
            choices=("serve", "test", "bench", "bench-server", "bench-client"),
            help=(
                "serve: just run the web server until it is killed\n"
                "test: test a single fetch of randomized index.html\n"
                "bench: run server and wrk client on separate nodes "
                "(needs prun)"
            ),
        )

        # common options
        parser.add_argument(
            "--port",
            type=int,
            default=random.randint(10000, 30000),
            help="web server port (random by default)",
        )
        parser.add_argument(
            "--filesize",
            type=str,
            default="64",
            help=(
                "filesize for generated index.html in bytes "
                "(supports suffixes compatible with dd, default 64)"
            ),
        )

        # bench options
        parser.add_argument(
            "--duration",
            metavar="SECONDS",
            default=10,
            type=int,
            help="benchmark duration in seconds (default 10)",
        )
        parser.add_argument(
            "--threads",
            type=int,
            default=1,
            help="concurrent wrk threads (distributes client load)",
        )
        parser.add_argument(
            "--connections",
            nargs="+",
            type=int,
            help=(
                "a list of concurrent wrk connections; "
                "start low and increment until the server is saturated"
            ),
        )
        parser.add_argument(
            "--cleanup-time",
            metavar="SECONDS",
            default=0,
            type=int,
            help="time to wait between benchmarks (default 3)",
        )

        parser.add_argument(
            "--restart-server-between-runs",
            default=False,
            action="store_true",
            help=(
                "terminate and restart the server between each "
                "benchmarking run (e.g., when benchmarking multiple "
                "connection configurations or doing multiple iterations\n"
                "NOTE: only supported for --parallel=ssh!"
            ),
        )
        parser.add_argument(
            "--disable-warmup",
            default=False,
            action="store_true",
            help=(
                "disable the warmup run of the server before doing actual "
                "benchmarks. This can be useful for measuring statistics\n"
                "NOTE: only supported for --parallel=ssh!"
            ),
        )
        parser.add_argument(
            "--collect-stats",
            nargs="+",
            choices=("cpu", "cpu-proc", "rss", "vms"),
            help=(
                "Statistics to collect of server while running benchmarks "
                "(disabled if not specified)\n"
                "NOTE: only supported for --parallel=ssh!\n"
                "cpu: CPU utilization of entire server (0..100%%)\n"
                "cpu-proc: sum of CPU utilization of all server processes "
                "(0..nproc*100%%)\n"
                "rss: sum of Resident Set Size of all server processes\n"
                "vms: sum of Virtual Memory Size of all server processes\n"
            ),
        )
        parser.add_argument(
            "--collect-stats-interval",
            type=float,
            default=1,
            help=(
                "seconds between measurements of statistics provided in "
                "the --collect-stats argument. Has no effect if no "
                "statistics are specified.\n"
                "NOTE: only supported for --parallel=ssh!"
            ),
        )
        parser.add_argument(
            "--remote-client-host",
            type=str,
            default="",
            help=(
                "if specified, connect to the remote client runner via "
                "this host instead of setting up an SSH tunnel.\n"
                "NOTE: only supported for --parallel=ssh!"
            ),
        )
        parser.add_argument(
            "--remote-server-host",
            type=str,
            default="",
            help=(
                "if specified, connect to the remote server runner via "
                "this host instead of setting up an SSH tunnel.\n"
                "NOTE: only supported for --parallel=ssh!"
            ),
        )
        parser.add_argument(
            "--nofork",
            action="store_true",
            help=(
                "if specified, run the server without any forking "
                "(1 worker max)\n"
                "NOTE: only supported for --parallel=ssh and nginx!"
            ),
        )
        parser.add_argument(
            "--config",
            type=str,
            default="",
            help=(
                "Config file to be used instead of the default template\n"
                "NOTE: use absolute path"
            ),
        )

        # bench-client options
        parser.add_argument(
            "--server-ip", help="IP of machine running matching bench-server"
        )

    def run(
        self, ctx: Context, instance: Instance, pool: Optional[Pool] = None
    ) -> None:
        runner = WebServerRunner(self, ctx, instance, pool)

        if ctx.args.run_type == "serve":
            runner.run_serve()
        elif ctx.args.run_type == "test":
            runner.run_test()
        elif ctx.args.run_type == "bench":
            runner.run_bench()
        elif ctx.args.run_type == "bench-server":
            runner.run_bench_server()
        elif ctx.args.run_type == "bench-client":
            runner.run_bench_client()

    @abstractmethod
    def populate_stagedir(self, runner: "WebServerRunner") -> None:
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
    def server_bin(self, ctx: Context, instance: Instance) -> str:
        """
        Retrieve path to the server binary file.

        :param instance: the instance for which this webserver is used
        :returns: the path to the server binary
        """
        pass

    @abstractmethod
    def pid_file(self, runner: "WebServerRunner") -> str:
        """
        Retrieve path to the PID file (a file containing the process id of the
        running web server instance).

        :param runner: the web server runner instance calling this function
        :returns: the path to the pid file
        """
        pass

    @abstractmethod
    def start_cmd(self, runner: "WebServerRunner", foreground: bool = False) -> str:
        """
        Generate command to start running the webserver.

        :param runner: the web server runner instance calling this function
        :param foreground: whether to start the web server in the foreground or
                           background (i.e., daemonize, the default)
        :returns: the command that starts the server
        """
        pass

    @abstractmethod
    def stop_cmd(self, runner: "WebServerRunner") -> str:
        """
        Generate command to stop running the webserver.

        :param runner: the web server runner instance calling this function
        :returns: the command that stops the server
        """
        pass

    @staticmethod
    @abstractmethod
    def kill_cmd(runner: "WebServerRunner") -> str:
        """
        Generate command to forcefully kill the running webserver.

        :param runner: the web server runner instance calling this function
        :returns: the command that kills the server
        """
        pass

    def start_script(self, runner: "WebServerRunner") -> str:
        """
        Generate a bash script that starts the server daemon.

        :param runner: the web server runner instance calling this function
        :returns: a bash script that starts the server daemon
        """
        start_cmd = self.start_cmd(runner)
        pid_file = self.pid_file(runner)
        libs = runner.ctx.runenv.get("LD_PRELOAD")
        preload = ""
        if libs is not None:
            preload = f"LD_PRELOAD={':'.join(libs)}"
            runner.ctx.runenv["LD_PRELOAD"] = ""

        return f"""
        {preload} {start_cmd}
        echo -n "=== started server on port {runner.ctx.args.port}, "
        echo "pid $(cat "{pid_file}")"
        """

    def stop_script(self, runner: "WebServerRunner") -> str:
        """
        Generate a bash script that stops the server daemon after benchmarking.

        :param runner: the web server runner instance calling this function
        :returns: a bash script that stops the server daemon
        """
        return self.stop_cmd(runner)

    def parse_outfile(self, ctx: Context, outfile: str) -> Iterator[ResultDict]:
        dirname, filename = os.path.split(outfile)
        if not filename.startswith("bench."):
            ctx.log.debug("ignoring non-benchmark file")
            return

        with open(outfile) as f:
            outfile_contents = f.read()

        def search(regex: str) -> str:
            m = re.search(regex, outfile_contents, re.M)
            assert m, "regex not found in outfile " + outfile
            return m.group(1)

        def parse_latency(s: str) -> float:
            m = re.match(r"(\d+\.\d+)([mun]?s)", s)
            assert m, "invalid latency"
            latency = float(m.group(1))
            unit = m.group(2)
            if unit == "us":
                latency /= 1000
            elif unit == "ns":
                latency /= 1000000
            elif unit == "s":
                latency *= 1000
            return latency

        def parse_bytesize(s: str) -> float:
            m = re.match(r"(\d+\.\d+)([KMGTP]?B)", s)
            assert m, "invalid bytesize"
            size = float(m.group(1))
            unit = m.group(2)
            factors = {
                "B": 1.0 / 1024,
                "KB": 1,
                "MB": 1024,
                "GB": 1024 * 1024,
                "TB": 1024 * 1024 * 1024,
                "PB": 1024 * 1024 * 1024 * 1024,
            }
            return size * factors[unit]

        cpu_outfile = os.path.join(dirname, filename.replace("bench", "cpu"))
        with open(cpu_outfile) as f:
            try:
                cpu_usages = [float(line) for line in f]
            except ValueError:
                raise FatalError(f"{cpu_outfile} contains invalid lines")

        yield {
            "threads": int(search(r"(\d+) threads and \d+ connections")),
            "connections": int(search(r"\d+ threads and (\d+) connections")),
            "avg_latency": parse_latency(search(r"^    Latency\s+([^ ]+)")),
            "50p_latency": parse_latency(search(r"^\s+50%\s+(.+)")),
            "75p_latency": parse_latency(search(r"^\s+75%\s+(.+)")),
            "90p_latency": parse_latency(search(r"^\s+90%\s+(.+)")),
            "99p_latency": parse_latency(search(r"^\s+99%\s+(.+)")),
            "throughput": float(search(r"^Requests/sec:\s+([0-9.]+)")),
            "transferrate": parse_bytesize(search(r"^Transfer/sec:\s+(.+)")),
            "duration": float(search(r"\d+ requests in ([\d.]+)s,")),
            "cpu": median(sorted(cpu_usages)),
        }


class WebServerRunner:
    comm_port = 40000

    server: WebServer
    ctx: Context
    instance: Instance
    pool: Optional[Pool]

    def __init__(
        self, server: WebServer, ctx: Context, instance: Instance, pool: Optional[Pool]
    ):
        self.server = server
        self.ctx = ctx
        self.instance = instance
        self.pool = pool

        tmpdir = f"/tmp/infra-{server.name}-{instance.name}"

        # Directory where we stage our run directory, which will then be copied
        # to the (node-local) rundir.
        self.stagedir = os.path.join(
            ctx.paths.buildroot, "run-staging", f"{server.name}-{instance.name}"
        )

        if self.pool:
            if isinstance(self.pool, SSHPool):
                tmpdir = self.pool.tempdir
            self.rundir = os.path.join(tmpdir, "run")
            self.logdir = outfile_path(ctx, server, instance)
        else:
            self.rundir = os.path.join(tmpdir, "run")
            self.logdir = os.path.join(tmpdir, "log")

    def logfile(self, outfile: str) -> str:
        return os.path.join(self.logdir, outfile)

    def run_serve(self) -> None:
        if self.pool:
            if not self.ctx.args.duration:
                raise FatalError("need --duration argument")

            self.populate_stagedir()

            server_command = self.bash_command(self.standalone_server_script())
            outfile = self.logfile("server.out")
            self.ctx.log.debug("server will log to " + outfile)
            self.pool.run(
                self.ctx, server_command, jobid="server", nnodes=1, outfile=outfile
            )
        else:
            self.create_logdir()
            self.populate_stagedir()
            self.start_server()

            try:
                self.ctx.log.info("press ctrl-C to kill the server")
                while True:
                    time.sleep(100000)
            except KeyboardInterrupt:
                pass

            self.stop_server()

    def run_test(self) -> None:
        if self.pool:
            self.populate_stagedir()

            server_command = self.bash_command(self.test_server_script())
            outfile = self.logfile("server.out")
            self.ctx.log.debug("server will log to " + outfile)
            self.pool.run(
                self.ctx, server_command, jobid="server", nnodes=1, outfile=outfile
            )

            client_command = self.bash_command(self.test_client_script())
            outfile = self.logfile("client.out")
            self.ctx.log.debug("client will log to " + outfile)
            self.pool.run(
                self.ctx, client_command, jobid="client", nnodes=1, outfile=outfile
            )
        else:
            self.create_logdir()
            self.populate_stagedir()
            self.start_server()
            self.request_and_check_index()
            self.stop_server()

    def _run_bench_over_ssh(self) -> None:
        assert isinstance(self.pool, SSHPool)

        def _start_server() -> None:
            """Start the server for benchmarking, verify it is behaving
            correctly and perfom warmup run."""

            server_cmd = self.server.start_cmd(self, foreground=True)
            server.run(server_cmd, wait=False, env=self.ctx.runenv)

            # Wait for server to come up
            starttime = time.time()
            while time.time() - starttime < 5:
                test_cmd = f"curl -s {url}"
                ret = client.run(test_cmd, allow_error=True)
                if ret["rv"] == 0:
                    break
                time.sleep(0.1)
            else:
                raise RemoteRunnerError("server did not come up")

            server.poll(expect_alive=True)
            with open(os.path.join(self.stagedir, "www/index.html")) as f:
                if ret["stdout"] != f.read():
                    raise RemoteRunnerError("contents of " + url + " do not match")

            # Do a warmup run
            if not self.ctx.args.disable_warmup:
                client.run(
                    f"{wrk_path} --duration 1s --threads {wrk_threads} "
                    f'--connections 400 "{url}"'
                )

            server.poll(expect_alive=True)

        def _run_bench_client(cons: int, it: int) -> None:
            """Run workload on client, and write back the results. Optionally
            monitor statistics of the server and write back those as well."""

            self.ctx.log.info(
                f"Benchmarking {self.server.name} with {cons} connections, #{it}"
            )

            if collect_stats:
                server.start_monitoring(
                    stats=collect_stats, interval=self.ctx.args.collect_stats_interval
                )

            # Allow wrk to return non-zero values, which it does when (some) of
            # the requests are errors. We just go on benchmarking, and let the
            # report command worry about this.
            ret = client.run(
                (
                    f"{wrk_path} "
                    "--latency "
                    f"--duration {wrk_duration}s "
                    f"--connections {cons} "
                    f"--threads {wrk_threads} "
                    f'"{url}"'
                ),
                allow_error=True,
            )

            stats: Dict[str, List[Union[int, float]]] = {}
            if collect_stats:
                stats = server.stop_monitoring()

            # Write results: wrk output and all of our collected stats
            def resfile(base: str) -> str:
                return self.logfile(f"{base}.{cons}.{it}")

            with open(resfile("bench"), "w") as f:
                f.write(ret["stdout"])
            for stat in collect_stats:
                with open(resfile(stat), "w") as f:
                    vals = [str(v) for v in stats[stat]]
                    if isinstance(vals[0], float):
                        vals = ["%.3f" % v for v in vals]
                    f.write("\n".join(map(str, vals + [""])))

        def _kill_server() -> None:
            """Really really kills the running server."""
            server.kill()
            server.wait(timeout=1, allow_error=True)
            forcekillcmd = self.server.kill_cmd(self)
            server.run(forcekillcmd, allow_error=True)

        assert self.rundir.startswith(self.pool.tempdir)

        def tempfile(*p: str) -> str:
            assert isinstance(self.pool, SSHPool)
            return os.path.join(self.pool.tempdir, *p)

        client_node, server_node = self.ctx.args.ssh_nodes
        client_outfile = self.logfile("client_runner.out")
        server_outfile = self.logfile("server_runner.out")
        client_debug_file = "client_runner_debug.out"
        server_debug_file = "server_runner_debug.out"
        rrunner_port_client, rrunner_port_server = 20010, 20011
        rrunner_script = "remote_runner.py"
        rrunner_script_path = tempfile(rrunner_script)
        client_cmd = [
            "python3",
            rrunner_script_path,
            "-p",
            str(rrunner_port_client),
            "-o",
            tempfile(client_debug_file),
        ]
        server_cmd = [
            "python3",
            rrunner_script_path,
            "-p",
            str(rrunner_port_server),
            "-o",
            tempfile(server_debug_file),
        ]
        curdir = os.path.dirname(os.path.abspath(__file__))

        client_host = self.ctx.args.remote_client_host or "localhost"
        server_host = self.ctx.args.remote_server_host or "localhost"

        client_tunnel_dest, server_tunnel_dest = None, None
        if not self.ctx.args.remote_client_host:
            client_tunnel_dest = rrunner_port_client
        if not self.ctx.args.remote_server_host:
            server_tunnel_dest = rrunner_port_server

        url = f"http://{self.ctx.args.server_ip}:{self.ctx.args.port}/index.html"
        wrk_path = Wrk().get_binary_path(self.ctx)
        wrk_threads = self.ctx.args.threads
        wrk_duration = self.ctx.args.duration

        collect_stats = []
        if self.ctx.args.collect_stats:
            collect_stats = ["time"] + self.ctx.args.collect_stats

        has_started_server = False

        # Create local stagedir and transfer files to other nodes.
        self.ctx.log.info("Setting up local and remote files")
        self.populate_stagedir()
        self.pool.sync_to_nodes(self.stagedir, "run")
        self.pool.sync_to_nodes(os.path.join(curdir, rrunner_script))

        # Launch the remote runners so we can easily control each node.
        client_job: Job = list(
            self.pool.run(
                self.ctx,
                client_cmd,
                jobid="client",
                nnodes=1,
                outfile=client_outfile,
                nodes=client_node,
                tunnel_to_nodes_dest=client_tunnel_dest,
            )
        )[0]
        server_job: Job = list(
            self.pool.run(
                self.ctx,
                server_cmd,
                jobid="server",
                nnodes=1,
                outfile=server_outfile,
                nodes=server_node,
                tunnel_to_nodes_dest=server_tunnel_dest,
            )
        )[0]

        assert isinstance(client_job, SSHJob)
        assert isinstance(server_job, SSHJob)

        # Connect to the remote runners. SSH can be slow, so give generous
        # timeout (retry window) so we don't end up with a ConnectionRefused.
        # Client here means "connect to the remote runner server", not the
        # client/server of our webserver setup.
        self.ctx.log.info("Connecting to remote nodes")
        client_port = (
            client_job.tunnel_src if client_tunnel_dest else rrunner_port_client
        )
        server_port = (
            server_job.tunnel_src if server_tunnel_dest else rrunner_port_server
        )
        client = RemoteRunner(
            self.ctx.log, side="client", host=client_host, port=client_port, timeout=10
        )
        server = RemoteRunner(
            self.ctx.log, side="client", host=server_host, port=server_port, timeout=10
        )

        _err: Optional[BaseException] = None
        try:
            # Do some minor sanity checks on the remote file system of server
            server_bin = self.server.server_bin(self.ctx, self.instance)
            if not server.has_file(server_bin):
                raise RemoteRunnerError(
                    "server binary " + server_bin + " not present on server"
                )

            # Copy wrk binary only as needed
            if not client.has_file(wrk_path):
                self.ctx.log.info("wrk binary not found on client, syncing...")
                self.pool.sync_to_nodes(wrk_path)
                wrk_path = tempfile("wrk")

            # Clean up any lingering server. # XXX hacky
            for s in (Nginx, ApacheHttpd, Lighttpd):
                assert hasattr(s, "kill_cmd")
                kill_cmd = s.kill_cmd(self)
                server.run(kill_cmd, allow_error=True)

            # Start actual server and benchmarking!
            for cons in self.ctx.args.connections:
                for it in range(self.ctx.args.iterations):
                    if (
                        not has_started_server
                        or self.ctx.args.restart_server_between_runs
                    ):
                        if has_started_server:
                            _kill_server()
                        _start_server()
                        has_started_server = True

                    _run_bench_client(cons, it)

            _kill_server()

        except RemoteRunnerError as e:
            _err = e
            self.ctx.log.error(f"aborting tests due to error:\n {e}")
        except KeyboardInterrupt as e:
            self.ctx.log.error(
                "Received KeyboardInterrupt, aborting "
                "gracefully...\n"
                "Note that this will wait for the last "
                "benchmark to finish, which may take up to "
                f"{wrk_duration} seconds."
            )
            _err = e

        # Terminate the remote runners and clean up.
        client.close()
        server.close()
        self.pool.wait_all()

        self.ctx.log.info("Done, syncing results to " + self.logdir)
        self.pool.sync_from_nodes(
            client_debug_file, self.logfile(client_debug_file), client_node
        )
        self.pool.sync_from_nodes(
            server_debug_file, self.logfile(server_debug_file), server_node
        )

        self.pool.cleanup_tempdirs()

        if _err:
            raise _err

    def run_bench(self) -> None:
        if not self.pool:
            raise FatalError("need --parallel= argument to run benchmark")
        elif isinstance(self.pool, SSHPool):
            if len(self.ctx.args.ssh_nodes) != 2:
                raise FatalError("need exactly 2 nodes (via --ssh-nodes)")
            if not self.ctx.args.server_ip:
                raise FatalError("need --server-ip")
        elif isinstance(self.pool, ProcessPool):
            self.ctx.log.warn(
                "the client should not run on the same machine "
                "as the server, use prun for benchmarking"
            )

        if not self.ctx.args.duration:
            raise FatalError("need --duration")

        if not self.ctx.args.connections:
            raise FatalError("need --connections")

        for conn in self.ctx.args.connections:
            if conn < self.ctx.args.threads:
                raise FatalError(
                    "#connections must be >= #threads "
                    f"({conn} < {self.ctx.args.threads})"
                )

        # Set up directory for results
        os.makedirs(self.logdir, exist_ok=True)
        self.write_log_of_config()

        if isinstance(self.pool, SSHPool):
            self._run_bench_over_ssh()
        else:
            client_outfile = self.logfile("client.out")
            server_outfile = self.logfile("server.out")

            self.populate_stagedir()

            server_script = self.wrk_server_script()
            server_command = self.bash_command(server_script)
            self.ctx.log.debug("server will log to " + server_outfile)
            self.pool.run(
                self.ctx,
                server_command,
                outfile=server_outfile,
                jobid="server",
                nnodes=1,
            )

            client_command = self.bash_command(self.wrk_client_script())
            self.ctx.log.debug("client will log to " + client_outfile)
            self.pool.run(
                self.ctx,
                client_command,
                outfile=client_outfile,
                jobid="wrk-client",
                nnodes=1,
            )

    def run_bench_server(self) -> None:
        if self.pool:
            raise FatalError("cannot run this command with --parallel")

        self.ctx.log.warn("another machine should run a matching bench-client")
        self.ctx.log.info(f"will log to {self.logdir} (merge with client log)")

        self.populate_stagedir()
        self.write_log_of_config()
        run(self.ctx, self.bash_command(self.wrk_server_script()), teeout=True)

    def run_bench_client(self) -> None:
        if self.pool:
            raise FatalError("cannot run this command with --parallel")

        if not self.ctx.args.duration:
            raise FatalError("need --duration")

        if not self.ctx.args.connections:
            raise FatalError("need --connections")

        if not self.ctx.args.server_ip:
            raise FatalError("need --server-ip and --port")

        for conn in self.ctx.args.connections:
            if conn < self.ctx.args.threads:
                raise FatalError(
                    "#connections must be >= #threads "
                    f"({conn} < {self.ctx.args.threads})"
                )

        self.ctx.log.warn(
            f"matching bench-server should be running at {self.ctx.args.server_ip}"
        )
        self.ctx.log.info(f"will log to {self.logdir} (merge with server log)")

        self.ctx.log.debug("creating log directory")
        os.makedirs(self.logdir, exist_ok=True)
        os.chdir(self.logdir)

        with open(self.logfile("server_host"), "w") as f:
            f.write(self.ctx.args.server_ip + "\n")

        self.write_log_of_config()
        run(self.ctx, self.bash_command(self.wrk_client_script()), teeout=True)

    def write_log_of_config(self) -> None:
        with open(self.logfile("config.txt"), "w") as f:
            with redirect_stdout(f):
                print("server workers:    ", self.ctx.args.workers)
                print("client threads:    ", self.ctx.args.threads)
                print("client connections:", self.ctx.args.connections)
                print("benchmark duration:", self.ctx.args.duration, "seconds")

    def start_server(self) -> None:
        self.ctx.log.info("starting server")
        script = self.wrap_start_script()
        run(self.ctx, self.bash_command(script), teeout=True)

    def stop_server(self) -> None:
        self.ctx.log.info("stopping server")
        script = self.wrap_stop_script()
        run(self.ctx, self.bash_command(script), teeout=True)

    def bash_command(self, script: str) -> Iterable[str]:
        if isinstance(self.pool, PrunPool):
            # escape for passing as: prun ... bash -c '<script>'
            script = script.replace("$", "\\$").replace('"', '\\"')

        return ["bash", "-c", f"set -e; cd {self.logdir}; {script}"]

    def create_logdir(self) -> None:
        assert not self.pool
        if os.path.exists(self.logdir):
            self.ctx.log.debug("removing old log directory " + self.logdir)
            shutil.rmtree(self.logdir)
        self.ctx.log.debug("creating log directory " + self.logdir)
        os.makedirs(self.logdir)

    def populate_stagedir(self) -> None:
        if os.path.exists(self.stagedir):
            self.ctx.log.debug("removing old staging run directory " + self.stagedir)
            shutil.rmtree(self.stagedir)

        self.ctx.log.debug("populating local staging run directory")
        os.makedirs(self.stagedir, exist_ok=True)
        os.chdir(self.stagedir)

        os.makedirs("www", exist_ok=True)
        with open("www/index.html", "w") as f:
            chars = string.printable
            filesize = parse_filesize(self.ctx.args.filesize)
            f.write("".join(random.choice(chars) for i in range(filesize)))

        self.server.populate_stagedir(self)

    def request_and_check_index(self) -> None:
        assert not self.pool
        url = f"http://localhost:{self.ctx.args.port}/index.html"
        self.ctx.log.info("requesting " + url)
        urlretrieve(url, "requested_index.html")

        with open(os.path.join(self.rundir, "www", "index.html"), "rb") as f:
            expected = f.read()
        with open("requested_index.html", "rb") as f:
            got = f.read()

        if got != expected:
            self.stop_server()
            raise FatalError("content does not match generated index.html")
        self.ctx.log.info("contents of index.html are correct")

    def wrap_start_script(self) -> str:
        start_script = self.server.start_script(self)
        host_command = "echo localhost"
        if isinstance(self.pool, PrunPool):
            # get the infiniband network IP
            host_command = 'ifconfig ib0 2>/dev/null | grep -Po "(?<=inet )[^ ]+"'
        return f"""
        echo "=== creating local run directory"
        rm -rf "{self.rundir}"
        cp -r {self.stagedir} {self.rundir}

        echo "=== starting web server"
        {start_script}
        server_host="$({host_command})"
        echo "=== serving at $server_host:{self.ctx.args.port}"
        """

    def wrap_stop_script(self) -> str:
        stop_script = self.server.stop_script(self)
        return f"""
        echo "=== received stop signal, stopping web server"
        {stop_script}

        if [ -s "{self.rundir}/error.log" ]; then
            echo "=== there were errors, copying log to {self.logdir}/error.log"
            cp "{self.rundir}/error.log" .
        fi

        echo "=== removing local run directory"
        rm -rf "{self.rundir}"
        """

    def server_script(self, body_template: str) -> str:
        start_script = self.wrap_start_script()
        stop_script = self.wrap_stop_script()
        return f"""
        comm_recv() {{ netcat --close -l -p {self.comm_port} || true; }}

        {start_script}

        echo "=== writing hostname to file"
        echo "$server_host" > server_host
        sync

        {body_template}

        {stop_script}
        """

    def client_script(self, body_template: str) -> str:
        return f"""
        comm_send() {{
            read msg
            while ! netcat --close "$server_host" {self.comm_port} \\
                    <<< "$msg" 2>/dev/null; do :; done
        }}

        echo "=== waiting for server to write its IP to file"
        while [ ! -e server_host ]; do sleep 0.1; sync; done
        server_host="$(cat server_host)"

        {body_template}

        echo "=== sending stop signal to server"
        comm_send <<< stop
        """

    def test_server_script(self) -> str:
        return self.server_script(f"""
        echo "=== copying index.html to log directory for client"
        cp "{self.rundir}/www/index.html" .

        echo "=== waiting for stop signal from client"
        test "$(comm_recv)" = stop
        """)

    def test_client_script(self) -> str:
        return (
            self.client_script(f"""
        url="http://$server_host:{self.ctx.args.port}/index.html"
        echo "=== requesting $url"
        wget -q -O requested_index.html "$url"
        """)
            + """
        if diff -q index.html requested_index.html; then
            echo "=== contents of index.html are correct"
        else
            echo "=== ERROR: content mismatch:"
            echo "  $(pwd)/requested_index.html"
            echo "does not match:"
            echo "  $(pwd)/index.html"
            exit 1
        fi
        """
        )

    def wrk_server_script(self) -> str:
        duration = self.ctx.args.duration
        return self.server_script(f"""
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
        """)

    def wrk_client_script(self) -> str:
        conns = " ".join(str(c) for c in self.ctx.args.connections)
        a = self.ctx.args
        return self.client_script(f"""
        url="http://$server_host:{a.port}/index.html"
        echo "=== will benchmark $url for {a.duration} seconds for each work rate"

        echo "=== 3 second warmup run"
        wrk --duration 3s --threads {a.threads} --connections 400 "$url"

        for i in $(seq 1 1 {a.iterations}); do
            for connections in {conns}; do
                if [ {a.cleanup_time} -gt 0 ]; then
                    echo "=== waiting {a.cleanup_time} seconds for server to clean up"
                    sleep {a.cleanup_time}
                fi

                echo "=== sending work rate $connections.$i to server"
                comm_send <<< "$connections.$i"

                echo "=== starting benchmark"
                set -x
                wrk --duration {a.duration}s --connections $connections \\
                        --threads {a.threads} --latency "$url" \\
                        > bench.$connections.$i
                set +x
            done
        done
        """)

    def standalone_server_script(self) -> str:
        duration = self.ctx.args.duration
        return self.server_script(f"""
        echo "=== logging cpu usage to cpu for {duration} seconds"
        {{ timeout {duration} mpstat 1 {duration} || true; }} | \\
                awk '/^[0-9].+all/ {{print 100-$13; fflush()}}' \\
                > cpu
        """)


class Nginx(WebServer):
    """
    The Nginx web server.

    :name: nginx
    :param version: which (open source) version to download
    """

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in Nginx.
    custom_allocs_flags = [
        "-allocs-custom-funcs="
        + ".".join(
            (
                "ngx_alloc:malloc:0",
                "ngx_palloc:malloc:1",
                "ngx_palloc_small:malloc:1",
                "ngx_palloc_large:malloc:1",
            )
        )
    ]

    version: str

    def __init__(self, version: str, build_flags: List[str] = []):
        super().__init__()
        self.build_flags = build_flags
        self.version = version
        self.name = "nginx-" + version

    def fetch(self, ctx: Context) -> None:
        download(ctx, "https://nginx.org/download/" + self.tar_name())

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists(self.tar_name())

    def tar_name(self) -> str:
        return "nginx-" + self.version + ".tar.gz"

    def build(
        self, ctx: Context, instance: Instance, pool: Optional[Pool] = None
    ) -> None:
        if not os.path.exists(instance.name):
            ctx.log.debug("unpacking nginx-" + self.version)
            shutil.rmtree("nginx-" + self.version, ignore_errors=True)
            untar(ctx, self.tar_name(), instance.name, remove=False)

        # Configure if there is no Makefile or if flags changed
        os.chdir(instance.name)
        if self.should_configure(ctx):
            ctx.log.debug("no Makefile or flags changed, reconfiguring")
            run(
                ctx,
                [
                    "./configure",
                    "--with-cc=" + ctx.cc,
                    "--with-cc-opt=" + qjoin(ctx.cflags),
                    "--with-ld-opt=" + qjoin(ctx.ldflags),
                    *self.build_flags,
                ],
            )
        else:
            ctx.log.debug("same flags as before, skip reconfigure")

        run(ctx, ["make", f"-j{ctx.jobs}", "--always-make"])

    def should_configure(self, ctx: Context) -> bool:
        if not os.path.exists("Makefile"):
            return True

        try:
            with open("flags_hash") as f:
                old_hash = f.read()
        except FileNotFoundError:
            old_hash = None

        new_hash = self.hash_flags(ctx)
        if new_hash == old_hash:
            return False

        with open("flags_hash", "w") as f:
            f.write(new_hash)
        return True

    def hash_flags(self, ctx: Context) -> str:
        h = md5()
        h.update(b"CC=" + ctx.cc.encode("ascii"))
        h.update(b"\nCFLAGS=" + qjoin(ctx.cflags).encode("ascii"))
        h.update(b"\nLDFLAGS=" + qjoin(ctx.ldflags).encode("ascii"))
        return h.hexdigest()

    def server_bin(self, ctx: Context, instance: Instance) -> str:
        return self.path(ctx, instance.name, "objs", "nginx")

    def binary_paths(self, ctx: Context, instance: Instance) -> Iterator[str]:
        yield self.server_bin(ctx, instance)

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        super().add_run_args(parser)
        parser.add_argument(
            "--workers",
            type=int,
            default=1,
            help="number of worker processes (default 1)",
        )
        parser.add_argument(
            "--worker-connections",
            type=int,
            default=1024,
            help="number of connections per worker process (default 1024)",
        )

    def populate_stagedir(self, runner: WebServerRunner) -> None:
        # Nginx needs the logs/ dir to create the default error log before
        # processing the error_logs directive
        os.makedirs("logs", exist_ok=True)

        runner.ctx.log.debug("creating nginx.conf")
        a = runner.ctx.args

        if os.path.exists(a.config):
            runner.ctx.log.debug(f"Found configuration file: {a.config}")
            shutil.copyfile(a.config, "nginx.conf")
            return

        config_template = f"""
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
        """
        with open("nginx.conf", "w") as f:
            f.write(config_template)

    def pid_file(self, runner: WebServerRunner) -> str:
        return f"{runner.rundir}/nginx.pid"

    def start_cmd(self, runner: WebServerRunner, foreground: bool = False) -> str:
        nginx = self.server_bin(runner.ctx, runner.instance)
        runopt = '-g "daemon off;"' if foreground else ""
        if runner.ctx.args.nofork:
            runopt = '-g "daemon off; master_process off;"'
        return f'{nginx} -p "{runner.rundir}" -c nginx.conf {runopt}'

    def stop_cmd(self, runner: WebServerRunner) -> str:
        nginx = self.server_bin(runner.ctx, runner.instance)
        return f'{nginx} -p "{runner.rundir}" -c nginx.conf -s quit'

    @staticmethod
    def kill_cmd(runner: WebServerRunner) -> str:
        return "pkill -9 nginx"


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

    #: :class:`list` Command line arguments for the built-in ``-allocs`` pass;
    #: Registers custom allocation function wrappers in Apache.
    custom_allocs_flags = [
        "-allocs-custom-funcs="
        + ".".join(
            (
                "apr_palloc:malloc:1",
                "apr_palloc_debug:malloc:1",
                "apr_pcalloc:calloc:1",
                "apr_pcalloc_debug:calloc:1",
            )
        )
    ]

    def __init__(
        self,
        version: str,
        apr_version: str,
        apr_util_version: str,
        modules: Iterable[str] = ["few"],
        build_flags: List[str] = [],
    ):
        self.version = version
        self.apr_version = apr_version
        self.apr_util_version = apr_util_version
        self.modules = modules
        self.name = "apache-" + version
        self.build_flags = build_flags
        self.modules = modules
        super().__init__()

    def fetch(self, ctx: Context) -> None:
        _fetch_apache(ctx, "httpd", "httpd-" + self.version, "src")
        _fetch_apache(ctx, "apr", "apr-" + self.apr_version, "src/srclib/apr")
        _fetch_apache(
            ctx, "apr", "apr-util-" + self.apr_util_version, "src/srclib/apr-util"
        )

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists("src")

    def build(
        self, ctx: Context, instance: Instance, pool: Optional[Pool] = None
    ) -> None:
        # create build directory
        objdir = os.path.join(instance.name, "obj")
        if os.path.exists(objdir):
            ctx.log.debug("removing old object directory " + objdir)
            shutil.rmtree(objdir)
        ctx.log.debug("creating object directory " + objdir)
        os.makedirs(objdir)
        os.chdir(objdir)

        # set environment for configure scripts
        prefix = self.path(ctx, instance.name, "install")
        env = {
            "CC": ctx.cc,
            "CFLAGS": qjoin(ctx.cflags),
            "LDFLAGS": qjoin(ctx.lib_ldflags),
            "HTTPD_LDFLAGS": qjoin(ctx.ldflags),
            "AR": ctx.ar,
            "RANLIB": ctx.ranlib,
        }

        # build APR
        ctx.log.info(f"building {self.name}-{instance.name}-apr")
        os.mkdir("apr")
        os.chdir("apr")
        run(
            ctx,
            [
                "../../../src/srclib/apr/configure",
                "--prefix=" + prefix,
                "--enable-static",
                "--enable-shared=no",
            ],
            env=env,
        )
        run(ctx, f"make -j{ctx.jobs}")
        run(ctx, "make install")
        os.chdir("..")

        # build APR-Util
        ctx.log.info(f"building {self.name}-{instance.name}-apr-util")
        os.mkdir("apr-util")
        os.chdir("apr-util")
        run(
            ctx,
            [
                "../../../src/srclib/apr-util/configure",
                "--prefix=" + prefix,
                "--with-apr=" + prefix,
            ],
            env=env,
        )
        run(ctx, f"make -j{ctx.jobs}")
        run(ctx, "make install")
        os.chdir("..")

        # build httpd web server
        ctx.log.info(f"building {self.name}-{instance.name}-httpd")
        os.mkdir("httpd")
        os.chdir("httpd")
        run(
            ctx,
            [
                "../../../src/configure",
                "--prefix=" + prefix,
                "--with-apr=" + prefix,
                "--with-apr-util=" + prefix,
                "--enable-modules=none",  # only build static
                "--enable-mods-static=" + qjoin(self.modules),
                *self.build_flags,
            ],
            env=env,
        )

        run(ctx, f"make -j{ctx.jobs}")
        run(ctx, "make install")
        os.chdir("..")

    def server_bin(self, ctx: Context, instance: Instance) -> str:
        return self.path(ctx, instance.name, "install", "bin", "httpd")

    def binary_paths(self, ctx: Context, instance: Instance) -> Iterator[str]:
        yield self.server_bin(ctx, instance)

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        super().add_run_args(parser)
        nproc = cpu_count()
        parser.add_argument(
            "--workers",
            type=int,
            default=nproc,
            help=f"number of worker processes (ServerLimit, default {nproc})",
        )
        parser.add_argument(
            "--worker-threads",
            type=int,
            default=25,
            help=(
                "number of connection threads per worker process "
                "(ThreadsPerChild, default 25)"
            ),
        )

    def populate_stagedir(self, runner: WebServerRunner) -> None:
        runner.ctx.log.debug("copying base config")
        rootdir = self.path(runner.ctx, runner.instance.name, "install")
        copytree(rootdir, runner.stagedir)

        a = runner.ctx.args

        if os.path.exists(a.config):
            runner.ctx.log.debug(f"Found configuration file: {a.config}")
            shutil.copyfile(a.config, "conf/httpd.conf")
            return

        runner.ctx.log.debug("creating httpd.conf from template")
        total_threads = a.workers * a.worker_threads
        config_template = f"""
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
        """
        with open("conf/httpd.conf", "w") as f:
            f.write(config_template)

    def pid_file(self, runner: WebServerRunner) -> str:
        return f"{runner.rundir}/apache.pid"

    def start_cmd(self, runner: WebServerRunner, foreground: bool = False) -> str:
        httpd = self.path(runner.ctx, runner.instance.name, "install", "bin", "httpd")
        runopt = "-D FOREGROUND" if foreground else "-k start"
        return f'{httpd} -d "{runner.rundir}" {runopt}'

    def stop_cmd(self, runner: WebServerRunner) -> str:
        httpd = self.path(runner.ctx, runner.instance.name, "install", "bin", "httpd")
        return f'{httpd} -d "{runner.rundir}" -k stop'

    @staticmethod
    def kill_cmd(runner: WebServerRunner) -> str:
        return "pkill -9 httpd"


class Lighttpd(WebServer):
    """
    TODO: docs
    """

    def __init__(self, version: str):
        self.version = version
        self.name += "lighttpd-" + version
        super().__init__()

    def dependencies(self) -> Iterator[Package]:
        yield from super().dependencies()
        yield Scons.default()

    def fetch(self, ctx: Context) -> None:
        m = re.match(r"(\d+\.\d+)\.\d+", self.version)
        assert m
        minor_version = m.group(1)
        download(
            ctx,
            (
                "https://download.lighttpd.net/lighttpd/"
                f"releases-{minor_version}.x/{self.tar_name()}"
            ),
        )

    def is_fetched(self, ctx: Context) -> bool:
        return os.path.exists(self.tar_name())

    def tar_name(self) -> str:
        return "lighttpd-" + self.version + ".tar.gz"

    def build(
        self, ctx: Context, instance: Instance, pool: Optional[Pool] = None
    ) -> None:
        if not os.path.exists(instance.name):
            ctx.log.debug("unpacking lighttpd-" + self.version)
            shutil.rmtree("lighttpd-" + self.version, ignore_errors=True)
            untar(ctx, self.tar_name(), instance.name, remove=False)

        os.chdir(instance.name)

        # remove old build directory to force a rebuild
        if os.path.exists("sconsbuild"):
            ctx.log.debug("removing old sconsbuild directory")
            shutil.rmtree("sconsbuild")

        path = join_env_paths(ctx.runenv).get("PATH", "")
        cc = shutil.which(ctx.cc, path=path)
        assert cc
        env: Dict[str, Union[str, List[str]]] = {
            "CFLAGS": qjoin(ctx.cflags),
            "LDFLAGS": qjoin(ctx.ldflags),
            "ASAN_OPTIONS": "detect_leaks=0",  # Lighttphd suffers from memory leaks
        }
        run(
            ctx,
            [
                "scons",
                "-j",
                ctx.jobs,
                "CC=" + cc,
                "with_pcre=no",
                "build_static=yes",
                "build_dynamic=no",
            ],
            env=env,
        )

    def server_bin(self, ctx: Context, instance: Instance) -> str:
        return self.path(
            ctx, instance.name, "sconsbuild", "static", "build", "lighttpd"
        )

    def binary_paths(self, ctx: Context, instance: Instance) -> Iterator[str]:
        yield self.server_bin(ctx, instance)

    def add_run_args(self, parser: argparse.ArgumentParser) -> None:
        super().add_run_args(parser)
        parser.add_argument(
            "--workers",
            type=int,
            default=1,
            help="number of worker processes (default 1)",
        )
        parser.add_argument(
            "--server-connections",
            type=int,
            default=2048,
            help="number of concurrent connections to the server (default 2048)",
        )

    def populate_stagedir(self, runner: WebServerRunner) -> None:
        a = runner.ctx.args

        if os.path.exists(a.config):
            runner.ctx.log.debug(f"Found configuration file: {a.config}")
            runner.ctx.log.debug(f"Port: {a.port}")
            shutil.copyfile(a.config, "lighttpd.conf")
            return

        runner.ctx.log.debug("creating lighttpd.conf from template")
        max_fds = 2 * a.server_connections
        config_template = f"""
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
        """
        with open("lighttpd.conf", "w") as f:
            f.write(config_template)

    def stop_script(self, runner: WebServerRunner) -> str:
        return f"""
        kill $(cat "{runner.rundir}/lighttpd.pid")
        """

    def pid_file(self, runner: WebServerRunner) -> str:
        return f"{runner.rundir}/lighttpd.pid"

    def start_cmd(self, runner: WebServerRunner, foreground: bool = False) -> str:
        lighttpd = self.server_bin(runner.ctx, runner.instance)
        runopt = "-D" if foreground else ""
        return f'{lighttpd} -f "{runner.rundir}/lighttpd.conf" {runopt}'

    def stop_cmd(self, runner: WebServerRunner) -> str:
        # TODO better to read pidfile
        return "pkill lighttpd"

    @staticmethod
    def kill_cmd(runner: WebServerRunner) -> str:
        return "pkill -9 lighttpd"


def median_absolute_deviation(numbers: Sequence[float]) -> float:
    assert len(numbers) > 0
    med = median(numbers)
    return median(abs(x - med) for x in numbers)


def stdev_percent(numbers: Sequence[float]) -> float:
    return 100 * pstdev(numbers) / mean(numbers)


def _fetch_apache(ctx: Context, repo: str, basename: str, dest: str) -> None:
    tarname = basename + ".tar.bz2"
    download(ctx, f"https://archive.apache.org/dist/{repo}/{tarname}")
    untar(ctx, tarname, dest)


def copytree(src: str, dst: str) -> None:
    """Wrapper for shutil.copytree, which does not have dirs_exist_ok until
    python 3.8."""
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def parse_filesize(filesize: str) -> int:
    """Convert a size in human-readable form to bytes (e.g., 4K, 2G)."""
    if isinstance(filesize, int):
        return filesize
    if not isinstance(filesize, str):
        raise FatalError("unsupported filesize type " + repr(filesize))
    factors = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
    filesize = filesize.upper()
    factor = ""
    if filesize[-1] not in string.digits:
        filesize, factor = filesize[:-1], filesize[-1]
    return int(filesize) * factors[factor]
