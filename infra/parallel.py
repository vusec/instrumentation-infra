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
                    self.poller.unregister(fd)
                    job = self.jobs.pop(fd)
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
