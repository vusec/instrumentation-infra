#!/usr/bin/env python3
#
# Remote running of commands over the network with a subprocess-like interface.
# The RemoteRunner provides an RPC interface for the client, which allows
# running arbitrary commands and other measurements on the server. On the remote
# side this file should be executed standalone to start the server-side of this
# interface.
#
# The infra uses this by scp'ing this file to each remote, executing it using
# ssh, and then connects to it by instantiating RemoteRunner class itself.
#
# As such, it is important that currently this file can run standalone (i.e.,
# does not import parts of the infra. This mechanism can in the future be
# refactored to actually be part of the framework; where the server side of this
# file is entered via a (hidden) setup.py command. This would allow for tighter
# integration of the infra functionality in this script and vice versa. The
# problem with this is that it would require syncing the entire infra to each
# remote filesystem.
#

import argparse
import json
import logging
import os
import psutil
import shlex
import socket
import subprocess
import sys
import threading
import time
import traceback


class RemoteRunnerError(Exception):
    pass


class MonitorThread(threading.Thread):
    """Asynchronous thread that monitors statistics of the whole system or
    a given set of processes.

    Instantiate this class with the interval, PIDs and statistics you can to
    monitor, and then start it using `start`. The thread can be stopped through
    `stop`, which will destroy the thread (i.e., it cannot be restarted
    afterwards). See the `data` field to read back the collected data."""

    supported_stats = ('time', 'cpu', 'cpu-proc', 'rss', 'vms')

    # Statistics that require looping over all monitored processes
    aggregated_stats = ('cpu-proc', 'rss', 'vms')


    def __init__(self, interval, pids=[], stats=('cpu', 'rss')):
        threading.Thread.__init__(self)
        self._event = threading.Event()
        self._exception = None

        self.interval = interval
        self.stats = tuple(set(list(stats) + ['time']))

        unsupported_stats = set(self.stats) - set(self.supported_stats)
        if unsupported_stats:
            raise ValueError('Unsupported stats requested: '
                    + str(unsupported_stats))

        self.data = {stat: [] for stat in self.stats}
        self.procs = [psutil.Process(pid) for pid in pids]

    def stop(self):
        self._event.set()
        self.join()

        if self._exception is not None:
            raise self._exception

    def sample_data(self):
        if 'time' in self.stats:
            time_elapsed = time.time() - self.start_time
            self.data['time'].append(time_elapsed)

        if 'cpu' in self.stats:
            self.data['cpu'].append(psutil.cpu_percent())

        aggr_stats = tuple(set(self.stats) & set(self.aggregated_stats))
        if aggr_stats:
            aggr = {s: 0 for s in aggr_stats}
            for proc in self.procs:
                with proc.oneshot():
                    if 'cpu-proc' in aggr:
                        aggr['cpu-proc'] += proc.cpu_percent()
                    if 'rss' in aggr:
                        aggr['rss'] += proc.memory_info().rss
                    if 'vms' in aggr:
                        aggr['vms'] += proc.memory_info().vms
            for stat, value in aggr.items():
                self.data[stat].append(value)

    def run(self):
        """Do not call directly! Called by `threading.Thread` to start executing
        our code of this thread. To start the thread, using `thread.start()`.

        Samples data every `interval` seconds until the monitoring thread is
        stopped. Catches any exception so it can be transfered out of the thread
        when `stop` is called."""
        try:
            self.start_time = time.time()
            self.sample_data()
            while not self._event.wait(self.interval):
                self.sample_data()
            self.sample_data()
        except Exception as e:
            self._exception = e


class RemoteRunnerComms:
    """Internal communication protocol used by the runner.

    Messages are line-separated, with `recv` blocking until a whole message is
    received. Data is json encoded, with the payload simply being the `args` and
    `kwargs` arguments."""

    def __init__(self, log, sock):
        assert isinstance(sock, socket.socket), sock
        self.log = log
        self.sock = sock
        self.rsock = sock.makefile('r')
        self.wsock = sock.makefile('w')
        self.last_pkg = ''

    def close(self):
        if self.sock is None:
            return
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.sock, self.rsock, self.wsock = None, None, None

    def send(self, func, *args, **kwargs):
        self.log.debug(' > {func} {args} {kwargs}'.format(**locals()))
        if self.sock is None:
            self.log.warning('Could not send message {func} because there is '
                             'no connection'.format(**locals()))
            return
        pkg = json.dumps((func, args, kwargs))
        self.sock.sendall(pkg.encode('utf-8') + b'\n')

    def recv(self):
        if self.sock is None:
            self.log.warning('Could not receive data because there is no '
                             'connection')
            return

        pkg = self.rsock.readline()
        if not pkg:
            raise RemoteRunnerError('connection closed')
        self.log.debug(' < %s', pkg.rstrip())
        self.last_pkg = pkg.rstrip()
        return json.loads(pkg)


class RemoteRunner:
    """Client and server of the remote runner RPC interface.

    Each instance has either a client or server side, which can be set up
    through the constructor or later by running `runner_connect` or
    `runner_serve`.  Most code in this is class is wrapped in `@remotecall`
    decorators, which transparently transforms the methods into an RPC. When
    called on the client it will send the function name and all arguments to the
    server, which will execute the body of the function, and transfer the return
    value back to the client. Calling such a function directly on the server
    will execute it directly."""

    def __init__(self, log, side=None, host=None, port=None, timeout=None):
        assert side in (None, 'client', 'server'), side
        self.log = log
        self.comms = None
        self.side = side

        if side == 'client':
            self.runner_connect(host or 'localhost', port, timeout)
        elif side == 'server':
            self.runner_serve(host or '0.0.0.0', port)

    def _error(self, msg, **kwargs):
        msg = str(msg) + '\nduring handling of message:\n' + self.comms.last_pkg
        if self.side == 'server':
            self.comms.send('error', msg, **kwargs)
        raise RemoteRunnerError(msg)

    def runner_connect(self, host, port, timeout=None):
        self.side = 'client'
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        starttime = time.time()
        while True:
            try:
                s.connect((host, port))
                break
            except ConnectionRefusedError as e:
                if timeout is None or time.time() - starttime > timeout:
                    raise e
                time.sleep(0.5)

        self.comms = RemoteRunnerComms(self.log, s)

    def runner_serve(self, host, port):
        self.side = 'server'
        self.proc = None
        self.in_server_remotecall = False

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(1)

            self.log.info('Listening on %s:%s' % (host, port))

            conn, addr = s.accept()
            self.log.info('Connection from %s', addr)

            self.running = True
            self.comms = RemoteRunnerComms(self.log, conn)
            try:
                while self.running:
                    func, args, kwargs = self.comms.recv()
                    handler = getattr(self, func, None)
                    if handler is None:
                        self._error('unknown message type')
                    handler(*args, **kwargs)
            except Exception as e:
                self._error('exception occurred:\n' + str(e))
                raise e

            self.comms.close()

            if self.proc:
                self.kill()

    def remotecall(func):
        def remotecallwrapper(runner, *args, **kwargs):
            assert runner.side in ('client', 'server'), runner.side
            if runner.side == 'client':
                runner.comms.send(func.__name__, *args, **kwargs)
                status, msg, payload = runner.comms.recv()
                if status != 'ok':
                    raise RemoteRunnerError('Got unexpected ' + status + ': ' +
                            ' '.join(str(m) for m in msg) + '\n' +
                            'returned payload: ' + str(payload))
                return payload['rv']
            else:
                if runner.in_server_remotecall:
                    return func(runner, *args, **kwargs)
                else:
                    runner.in_server_remotecall = True
                    rv = func(runner, *args, **kwargs)
                    runner.comms.send('ok', rv=rv)
                    runner.in_server_remotecall = False
        return remotecallwrapper

    def clientonly(func):
        def clientonlywrapper(runner, *args, **kwargs):
            if runner.side != 'client':
                self._error('running client-only function on '
                        + str(runner.side))
            return func(runner, *args, **kwargs)
        return clientonlywrapper

    def serveronly(func):
        def serveronlywrapper(runner, *args, **kwargs):
            if runner.side != 'server':
                self._error('running server-only function on '
                        + str(runner.side))
            return func(runner, *args, **kwargs)
        return serveronlywrapper

    @clientonly
    def close(self):
        try:
            self.runner_exit()
        except (KeyboardInterrupt, RemoteRunnerError):
            pass
        self.comms.close()
        self.comms = None
        self.side = None

    @remotecall
    def get_pids(self):
        if self.proc is None:
            return []
        try:
            ppid = self.proc.pid
            psproc = psutil.Process(ppid)
            child_procs = psproc.children(recursive=True)
            pids = [c.pid for c in child_procs]
            return [ppid] + pids
        except psutil.NoSuchProcess:
            return []

    @remotecall
    def runner_exit(self):
        self.running = False

    @remotecall
    def run(self, cmd, env={}, wait=True, allow_error=False):
        if self.proc is not None and self.proc.poll() is None:
            self._error('already running a process')

        cmd = shlex.split(cmd) if isinstance(cmd, str) \
                               else [str(c) for c in cmd]

        renv = os.environ.copy()
        join = lambda v: ':'.join(v) if isinstance(v, (tuple, list)) else v
        renv.update({k: join(v) for k, v in env.items()})

        self.proc = subprocess.Popen(cmd, env=renv, encoding='utf-8',
                preexec_fn=os.setsid,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        psutil.cpu_percent() # Start measurement

        if wait:
            return self.wait(allow_error=allow_error)

    @remotecall
    def poll(self, expect_alive=False):
        if self.proc is None:
            self._error('no process was running')
        rv = self.proc.poll()
        if expect_alive:
            if rv is not None:
                stdout, stderr = self.proc_communicate()
                self._error('process has exited already ({rv})\n'
                            'stdout: {stdout}\n'
                            'stderr: {stderr}'.format(**locals()))
        return self.proc.poll()

    @remotecall
    def proc_communicate(self, stdin=None, timeout=None):
        if self.proc is None:
            self._error('no process was running')

        # Bug in communicate() if stdout/stderr is closed (fixed in py3.9)
        if self.proc.stdout.closed and self.proc.stderr.closed:
            return '', ''
        elif self.proc.stdout.closed:
            return '', self.proc.stderr.read()
        elif self.proc.stderr.closed:
            return '', self.proc.stdout.read()
        else:
            try:
                return self.proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None, None

    @remotecall
    def wait(self, timeout=None, output=True, stats=True, allow_error=False):
        if self.proc is None:
            self._error('no process was running')

        ret = {}
        try:
            ret['rv'] = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            pass

        if output and self.proc.poll() is not None:
            ret['stdout'], ret['stderr'] = \
                    self.proc_communicate(timeout=timeout)

        if stats:
            ret['cpu_percentage'] = psutil.cpu_percent()

        if not allow_error and self.proc.poll() not in (None, 0):
            self._error('process exited with error', **ret)

        return ret

    @remotecall
    def kill(self):
        if self.proc is None:
            self._error('no process was running')

        if self.proc.poll() is None:
            self.log.info('terminating ' + str(self.proc.pid))
            self.proc.terminate()
            self.log.info('terminated ' + str(self.proc.pid))

        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.log.info('killing ' + str(self.proc.pid))
            self.proc.kill()
            self.proc.wait()

        self.log.info('OK!')

    @remotecall
    def read_output_line(self):
        if self.proc is None:
            self._error('no process was running')

        line = self.proc.stdout.readline().rstrip()
        return line

    @remotecall
    def get_cpu_percentage(self):
        return psutil.cpu_percent()

    @remotecall
    def start_monitoring(self, interval=1.0, stats=('cpu', 'rss')):
        pids = self.get_pids()
        self.monitor_thread = MonitorThread(interval, pids, stats)
        self.monitor_thread.start()

    @remotecall
    def stop_monitoring(self):
        if self.monitor_thread is None:
            self._error('no monitoring thread')
        self.monitor_thread.stop()
        return self.monitor_thread.data

    @remotecall
    def has_file(self, path):
        return os.path.isfile(path)


def server_main():
    parser = argparse.ArgumentParser(
            description='Remote runner script for benchmarking.')
    parser.add_argument('--host', default='',
                        help='host to bind socket on')
    parser.add_argument('-p', '--port', default=20010, type=int,
                        help='port to bind socket on')
    parser.add_argument('-v', '--verbosity', default='info',
            choices=['critical', 'error', 'warning', 'info', 'debug'],
            help='set terminal logging verbosity (default info)')
    parser.add_argument('-o', '--debug-log-out',
            metavar='out_file', default='runner.log',
            help='path where to write the debug log (default "runner.log"')

    args = parser.parse_args()

    fmt = '%(asctime)s [%(levelname)s] %(message)s'
    timefmt = '%H:%M:%S'
    datetimefmt = '%Y-%m-%d ' + timefmt

    log = logging.getLogger('runner-server')
    log.setLevel(logging.DEBUG)
    log.propagate = False

    termlog = logging.StreamHandler(sys.stdout)
    termlog.setLevel(getattr(logging, args.verbosity.upper()))
    termlog.setFormatter(logging.Formatter(fmt, timefmt))
    log.addHandler(termlog)

    debuglog = logging.FileHandler(args.debug_log_out, mode='w')
    debuglog.setLevel(logging.DEBUG)
    debuglog.setFormatter(logging.Formatter(fmt, datetimefmt))
    log.addHandler(debuglog)

    log.info('Started runner, pid=%d', os.getpid())

    try:
        RemoteRunner(log, side='server', host=args.host, port=args.port)
    except Exception as e:
        log.critical(traceback.format_exc().rstrip())
        sys.exit(-1)

    # Really make sure we exit so the ssh doesn't linger on. This bypasses some
    # cleanup code though...
    os._exit(0)


if __name__ == '__main__':
    server_main()
