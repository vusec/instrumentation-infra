import sys
import os
import threading
import time
import shlex
import select
import re
import io
import fcntl
import logging
import random
from subprocess import Popen, STDOUT
from abc import ABCMeta, abstractmethod
from typing import Union, List, Optional, Iterator, Callable
from .util import Namespace, run, require_program


# TODO: rewrite this to use
# https://docs.python.org/3/library/concurrent.futures.html?


class Pool(metaclass=ABCMeta):
    """
    A pool is used to run processes in parallel as jobs when ``--parallel`` is
    specified on the command line. The pool is created automatically by
    :class:`Setup` and passed to :func:`Target.build`, :func:`Target.link` and
    :func:`Target.run`. However, the pool is only passed if the method
    implementation defines a parameter for the pool, i.e.::

        class MyTarget(Target):
            def build(self, ctx, instance, pool): # receives Pool instance
               ...
            def run(self, ctx, instance):         # does not receive it
               ...

    The maximum number of parallel jobs is controlled by ``--parallelmax``. For
    ``--parallel=proc`` this is simply the number of parallel processes on the
    current machine. For ``--parallel=prun`` it is the maximum number of
    simultaneous jobs in the job queue (pending or running).
    """
    poll_interval = 0.050  # seconds to wait for blocking actions

    @abstractmethod
    def make_jobs(self, ctx: Namespace, cmd: Union[str, List[str]], jobid: str,
                  outfile: str, nnodes: int, **kwargs) -> Iterator[Popen]:
        pass

    @abstractmethod
    def process_job_output(self, job: Popen):
        pass

    def __init__(self, logger: logging.getLoggerClass(), parallelmax: int):
        """
        :param logger: logging object for status updates (set to ``ctx.log``)
        :param parallelmax: value of ``--parallelmax``
        """
        self.log = logger
        self.parallelmax = parallelmax
        self.jobs = {}

    def __del__(self):
        if hasattr(self, 'pollthread'):
            self.done = True
            self.pollthread.join(self.poll_interval)

    def _start_poller(self):
        if not hasattr(self, 'pollthread'):
            self.poller = select.epoll()
            self.pollthread = threading.Thread(target=self._poller_thread,
                                               name='pool-poller')
            self.pollthread.daemon = True
            self.done = False
            self.pollthread.start()

    def _poller_thread(self):
        # monitor the job queue for finished jobs, remove them from the queue
        # and call success/error callbacks
        while not self.done:
            for fd, flags in self.poller.poll(timeout=self.poll_interval):
                if flags & (select.EPOLLIN | select.EPOLLPRI):
                    self.process_job_output(self.jobs[fd])

                if flags & select.EPOLLERR:
                    self.poller.unregister(fd)
                    job = self.jobs.pop(fd)
                    self.onerror(job)

                if flags & select.EPOLLHUP:
                    job = self.jobs[fd]
                    if job.poll() is None:
                        self.log.debug('job %s hung up but does not yet have a '
                                       'return code, check later' % job.jobid)
                        continue

                    self.poller.unregister(fd)
                    del self.jobs[fd]

                    if job.poll() == 0:
                        self.onsuccess(job)
                    else:
                        self.onerror(job)

    def _wait_for_queue_space(self, nodes_needed):
        if self.parallelmax is not None:
            nnodes = lambda: sum(job.nnodes for job in self.jobs.values())
            while nnodes() + nodes_needed > self.parallelmax:
                time.sleep(self.poll_interval)

    def wait_all(self):
        """
        Block (busy-wait) until all jobs in the queue have been completed.
        Called automatically by :class:`Setup` after the ``build`` and ``run``
        commands.
        """
        while len(self.jobs):
            time.sleep(self.poll_interval)

    def run(self, ctx: Namespace, cmd: Union[str, List[str]],
            jobid: str, outfile: str, nnodes: int,
            onsuccess: Optional[Callable[[Popen], None]] = None,
            onerror: Optional[Callable[[Popen], None]] = None,
            **kwargs) -> List[Popen]:
        """
        A non-blocking wrapper for :func:`util.run`, to be used when
        ``--parallel`` is specified.

        :param ctx: the configuration context
        :param cmd: the command to run
        :param jobid: a human-readable ID for status reporting
        :param outfile: full path to target file for command output
        :param nnodes: number of cores or machines to run the command on
        :param onsuccess: callback when the job finishes successfully
        :param onerror: callback when the job exits with (typically I/O) error
        :param kwargs: passed directly to :func:`util.run`
        :returns: handles to created job processes
        """
        # TODO: generate outfile from jobid
        self._start_poller()

        if isinstance(cmd, str):
            cmd = shlex.split(cmd)

        jobs = []

        for job in self.make_jobs(ctx, cmd, jobid, outfile, nnodes, **kwargs):
            assert hasattr(job, 'jobid')
            assert hasattr(job, 'nnodes')
            assert hasattr(job, 'stdout')
            job.onsuccess = onsuccess
            job.onerror = onerror
            job.output = ''
            self.jobs[job.stdout.fileno()] = job
            self.poller.register(job.stdout, select.EPOLLIN | select.EPOLLPRI |
                                             select.EPOLLERR | select.EPOLLHUP)
            jobs.append(job)

        return jobs

    def onsuccess(self, job):
        # don't log if onsuccess() returns False
        if not job.onsuccess or job.onsuccess(job) is not False:
            self.log.info('job %s finished%s' %
                        (job.jobid, self._get_elapsed(job)))
            self.log.debug('command: %s' % job.cmd_print)

    def onerror(self, job):
        # don't log if onerror() returns False
        if not job.onerror or job.onerror(job) is not False:
            self.log.error('job %s returned status %d%s' %
                        (job.jobid, job.poll(), self._get_elapsed(job)))
            self.log.error('command: %s' % job.cmd_print)
            sys.stdout.write(job.output)

    def _get_elapsed(self, job):
        if not hasattr(job, 'start_time'):
            return ''
        return ' after %d seconds' % round(time.time() - job.start_time)


class ProcessPool(Pool):
    def make_jobs(self, ctx, cmd, jobid_base, outfile_base, nnodes, **kwargs):
        for i in range(nnodes):
            jobid = jobid_base
            outfile = outfile_base
            if nnodes > 1:
                jobid += '-%d' % i
                outfile += '-%d' % i

            self._wait_for_queue_space(1)
            ctx.log.info('running ' + jobid)

            job = run(ctx, cmd, defer=True, stderr=STDOUT,
                      bufsize=io.DEFAULT_BUFFER_SIZE,
                      universal_newlines=False, **kwargs)
            _set_non_blocking(job.stdout)
            job.start_time = time.time()
            job.jobid = jobid
            job.nnodes = 1

            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            job.outfiles = [outfile]
            job.outfile_handle = open(outfile, 'wb')

            yield job

    def process_job_output(self, job):
        buf = job.stdout.read(io.DEFAULT_BUFFER_SIZE)
        if buf is not None:
            job.output += buf.decode('ascii', errors='replace')
            job.outfile_handle.write(buf)

    def onsuccess(self, job):
        job.outfile_handle.close()
        super().onsuccess(job)

    def onerror(self, job):
        job.outfile_handle.close()
        super().onerror(job)


class SSHPool(Pool):
    """
    An SSHPool runs jobs on remote nodes via ssh.

    The --ssh-nodes argument specified a list of ssh hosts to distribute the
    work over. These hosts are passed as-is to the ssh command; the best way for
    specifying alternative ssh ports, user, and other options is to add your
    hosts to the ~/.ssh/config file. Additionally, make sure the hosts can be
    reached without password prompts (e.g., by using passphrase-less keys or
    using an ssh agent).

    For targets that are being run via an SSHPool additional functionality is
    available, such as distributing files to/from nodes.
    """

    ssh_opts = [
            # Block stdin and background ssh before executing command.
            '-f',
            # Eliminate some of the yes/no questions ssh may ask.
            '-oStrictHostKeyChecking=accept-new',
        ]
    scp_opts = [
            # Quiet mode to disable progress meter
            '-q',
            # Batch mode to prevent asking for password
            '-B',
            # Copy directories
            '-r',
        ]

    def __init__(self, ctx, logger, parallelmax, nodes):
        if parallelmax > len(nodes):
            raise FatalError('parallelmax cannot be greater than number of '
                             'available nodes')
        super().__init__(logger, parallelmax)
        self._ctx = ctx
        self.nodes = nodes[:]
        self.available_nodes = nodes[:]
        self.has_tested_nodes = False
        self.has_created_tempdirs = False

    @property
    def tempdir(self):
        if not self.has_created_tempdirs:
            self.create_tempdirs()
        return self._tempdir

    def _ssh_cmd(self, node, cmd, extra_opts=None):
        if not isinstance(cmd, str):
            cmd = ' '.join(shlex.quote(str(c)) for c in cmd)
        extra_opts = extra_opts or []
        return ['ssh', *self.ssh_opts, *extra_opts, node, cmd]

    def test_nodes(self):
        if self.has_tested_nodes:
            return
        for node in self.nodes:
            cmd = ['ssh', *self.ssh_opts, node, 'echo -n hi']
            p = run(self._ctx, cmd, stderr=STDOUT, silent=True)
            if p.returncode or p.stdout != 'hi':
                self._ctx.log.error('Testing SSH node ' + node + ' failed:\n'
                        + p.stdout)
                sys.exit(-1)

        self.has_tested_nodes = True

    def create_tempdirs(self):
        if self.has_created_tempdirs:
            return

        self.test_nodes()

        starttime = self._ctx.starttime.strftime('%Y-%m-%d.%H-%M-%S')
        self._tempdir = os.path.join('/tmp', 'infra-' + starttime)

        self._ctx.log.debug('creating SSHPool temp dir {self._tempdir} on '
                'nodes {self.nodes}'.format(**locals()))

        for node in self.nodes:
            run(self._ctx, self._ssh_cmd(node, ['mkdir', '-p', self._tempdir]))

        self.has_created_tempdirs = True

    def cleanup_tempdirs(self):
        if not self.has_created_tempdirs:
            return
        self._ctx.log.debug('cleaning up SSHPool temp directory '
                '{self._tempdir} on nodes {self.nodes}'.format(**locals()))
        for node in self.nodes:
            run(self._ctx, self._ssh_cmd(node, ['rm', '-rf', self._tempdir]))
        self.has_created_tempdirs = False
        self._tempdir = None

    def sync_to_nodes(self, sources, destination='', target_nodes=None):
        if isinstance(sources, str): sources = [sources]
        if isinstance(target_nodes, str): target_nodes = [target_nodes]
        nodes = target_nodes or self.nodes
        self._ctx.log.debug('syncing file to SSHPool nodes, sources={sources},'
                'destination={destination}, nodes={nodes}'.format(**locals()))
        for node in nodes:
            dest = '%s:%s' % (node, os.path.join(self.tempdir, destination))
            cmd = ['scp', *self.scp_opts, *sources, dest]
            run(self._ctx, cmd)

    def sync_from_nodes(self, source, destination='', source_nodes=None):
        if isinstance(source_nodes, str): source_nodes = [source_nodes]
        nodes = source_nodes or self.nodes

        self._ctx.log.debug('syncing file from SSHPool nodes, source={source},'
                'destination={destination}, nodes={nodes}'.format(**locals()))

        for i, node in enumerate(nodes):
            dest = destination or os.path.basename(source)
            if len(nodes) > 1:
                dest += '.' + node
                if len(nodes) != len(set(nodes)):
                    dest += i
            src = '%s:%s' % (node, os.path.join(self.tempdir, source))
            cmd = ['scp', *self.scp_opts, src, dest]
            run(self._ctx, cmd)

    def get_free_node(self, override_node=None):
        if override_node:
            assert override_node in self.nodes
            assert override_node in self.available_nodes
            self.available_nodes.remove(override_node)
            return override_node
        else:
            return self.available_nodes.pop()

    def make_jobs(self, ctx, cmd, jobid_base, outfile_base, nnodes, nodes=None,
            tunnel_to_nodes_dest=None, **kwargs):

        if isinstance(nodes, str): nodes = [nodes]

        self.test_nodes()

        for i in range(nnodes):
            jobid = jobid_base
            outfile = outfile_base
            if nnodes > 1:
                jobid += '-%d' % i
                outfile += '-%d' % i

            self._wait_for_queue_space(1)
            override_node = nodes[i] if nodes else None
            node = self.get_free_node(override_node)
            ctx.log.info('running ' + jobid + ' on ' + node)

            ssh_node_opts = []
            if tunnel_to_nodes_dest:
                tunnel_src = random.randint(10000, 30000)
                ssh_node_opts += ['-Llocalhost:%d:0.0.0.0:%d' %
                        (tunnel_src, tunnel_to_nodes_dest)]

            ssh_cmd = self._ssh_cmd(node, cmd, ssh_node_opts)
            job = run(ctx, ssh_cmd, defer=True, stderr=STDOUT,
                        bufsize=io.DEFAULT_BUFFER_SIZE,
                        universal_newlines=False, **kwargs)
            _set_non_blocking(job.stdout)
            job.start_time = time.time()
            job.jobid = jobid
            job.nnodes = 1
            job.node = node

            if tunnel_to_nodes_dest:
                job.tunnel_src = tunnel_src
                job.tunnel_dest = tunnel_to_nodes_dest

            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            job.outfiles = [outfile]
            job.outfile_handle = open(outfile, 'wb')

            yield job

    def process_job_output(self, job):
        buf = job.stdout.read(io.DEFAULT_BUFFER_SIZE)
        if buf is not None:
            job.output += buf.decode('ascii', errors='replace')
            job.outfile_handle.write(buf)

    def onsuccess(self, job):
        job.outfile_handle.close()
        self.available_nodes.append(job.node)
        super().onsuccess(job)

    def onerror(self, job):
        self.available_nodes.append(job.node)
        job.outfile_handle.close()
        super().onerror(job)


class PrunPool(Pool):
    default_job_time = 900 # if prun reserves this amount, it is not logged

    def __init__(self, logger, parallelmax, prun_opts):
        super().__init__(logger, parallelmax)
        self.prun_opts = prun_opts

    def make_jobs(self, ctx, cmd, jobid, outfile, nnodes, **kwargs):
        require_program(ctx, 'prun')
        self._wait_for_queue_space(nnodes)
        ctx.log.info('scheduling ' + jobid)
        cmd = ['prun', '-v', '-np', '%d' % nnodes, '-1',
               '-o', outfile, *self.prun_opts, *cmd]
        job = run(ctx, cmd, defer=True, stderr=STDOUT, bufsize=0,
                  universal_newlines=False, **kwargs)
        _set_non_blocking(job.stdout)
        job.jobid = jobid
        job.nnodes = nnodes
        job.outfiles = ['%s.%d' % (outfile, i) for i in range(nnodes)]
        yield job

    def process_job_output(self, job):
        def find_ranges(numbers):
            ranges = [(i, i) for i in numbers]
            ranges.sort()
            for i in range(len(ranges) - 1, 0, -1):
                lstart, lend = ranges[i - 1]
                rstart, rend = ranges[i]
                if lend + 1 == rstart:
                    ranges[i - 1] = lstart, rend
                    del ranges[i]
            return ranges

        def group_cores(nodes):
            nodes = sorted(nodes)
            while len(nodes):
                machine, core = nodes.pop(0)
                cores = [core]
                while len(nodes) and nodes[0][0] == machine:
                    cores.append(nodes.pop(0)[1])
                yield machine, cores

        def group_nodes(nodes):
            groups = [([m], [c]) for m, c in sorted(nodes)]
            for i in range(len(groups) - 1, 0, -1):
                lmachines, lcores = groups[i - 1]
                rmachines, rcores = groups[i]
                if lmachines == rmachines and lcores[-1] + 1 == rcores[0]:
                    groups[i - 1] = lmachines, lcores + rcores
                    del groups[i]
                elif len(lcores) == 1 and lmachines[-1] + 1 == rmachines[0] and \
                        lcores == rcores:
                    groups[i - 1] = lmachines + rmachines, lcores
                    del groups[i]
            return groups

        def stringify_groups(groups):
            samecore = set(c for m, cores in groups for c in cores) == set([0])

            def join(n, fmt):
                if len(n) == 1:
                    return fmt % n[0]
                else:
                    return fmt % n[0] + '-' + fmt % n[-1]

            if samecore:
                # all on core 0, omit it
                groupstrings = (join(m, '%03d') for m, c in groups)
            else:
                # different cores, add /N suffix
                groupstrings = ('%s/%s' % (join(m, '%03d'), join(c, '%d')) for m, c in groups)

            if len(groups) == 1:
                m, c = groups[0]
                if len(m) == 1 and len(c) == 1:
                    return 'node' + next(groupstrings)

            return 'node[%s]' % ','.join(groupstrings)

        buf = job.stdout.read(1024)
        if buf is None:
            return

        job.output += buf.decode('ascii')

        if getattr(job, 'logged', False):
            return

        numseconds = None
        nodes = []

        for line in job.output.splitlines():
            if line.startswith(':'):
                for m in re.finditer(r'node(\d+)/(\d+)', line):
                    nodes.append((int(m.group(1)), int(m.group(2))))
            elif numseconds is None:
                m = re.search('for (\d+) seconds', line)
                if m:
                    numseconds = int(m.group(1))

        if len(nodes) == job.nnodes:
            assert numseconds is not None
            nodestr = stringify_groups(group_nodes(nodes))
            self.log.info('running %s on %s' % (job.jobid, nodestr))
            job.start_time = time.time()
            job.logged = True


def _set_non_blocking(f):
    flags = fcntl.fcntl(f, fcntl.F_GETFL)
    fcntl.fcntl(f, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _find_ranges(numbers):
    ranges = [(i, i) for i in numbers]
    ranges.sort()
    for i in range(len(ranges) - 1, 0, -1):
        lstart, lend = ranges[i - 1]
        rstart, rend = ranges[i]
        if lend + 1 == rstart:
            ranges[i - 1] = lstart, rend
            del ranges[i]
    return ranges
