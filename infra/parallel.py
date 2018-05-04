import sys
import os
import threading
import time
import shlex
import select
import re
import subprocess
import io
import fcntl
from abc import ABCMeta, abstractmethod
from .util import run


# TODO: rewrite this to use
# https://docs.python.org/3/library/concurrent.futures.html?


class Pool(metaclass=ABCMeta):
    poll_interval = 0.050  # seconds to wait for blocking actions

    @abstractmethod
    def make_jobs(self, ctx, cmd, jobid, outfile, nnodes, **kwargs):
        pass

    @abstractmethod
    def process_job_output(self, job):
        pass

    def __init__(self, logger, parallelmax):
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
        while len(self.jobs):
            time.sleep(self.poll_interval)

    def run(self, ctx, cmd, jobid, outfile, nnodes,
            onsuccess=None, onerror=None, **kwargs):
        # TODO: generate outfile from jobid
        self._start_poller()

        if isinstance(cmd, str):
            cmd = shlex.split(cmd)

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

    def onsuccess(self, job):
        # don't log if onsuccess() returns False
        if not job.onsuccess or job.onsuccess(job) is False:
            self.log.info('job %s finished%s' %
                        (job.jobid, self._get_elapsed(job)))
            self.log.debug('command: %s' % job.cmd_print)

    def onerror(self, job):
        # don't log if onerror() returns False
        if not job.onerror or job.onerror(job) is False:
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

            job = run(ctx, cmd, defer=True, stderr=subprocess.STDOUT,
                      bufsize=io.DEFAULT_BUFFER_SIZE,
                      universal_newlines=False, **kwargs)
            _set_non_blocking(job.stdout)
            job.start_time = time.time()
            job.jobid = jobid
            job.nnodes = 1

            os.makedirs(os.path.dirname(outfile), exist_ok=True)
            job.outfile = open(outfile, 'wb')

            yield job

    def process_job_output(self, job):
        buf = job.stdout.read(io.DEFAULT_BUFFER_SIZE)
        if buf is not None:
            job.output += buf.decode('ascii')
            job.outfile.write(buf)

    def onsuccess(self, job):
        super().onsuccess(job)
        job.outfile.close()

    def onerror(self, job):
        super().onerror(job)
        job.outfile.close()


class PrunPool(Pool):
    default_job_time = 900 # if prun reserves this amount, it is not logged

    def __init__(self, logger, parallelmax, prun_opts):
        super().__init__(logger, parallelmax)
        self.prun_opts = prun_opts

    def make_jobs(self, ctx, cmd, jobid, outfile, nnodes, **kwargs):
        self._wait_for_queue_space(nnodes)
        ctx.log.info('scheduling ' + jobid)
        cmd = ['prun', '-v', '-np', '%d' % nnodes, '-1',
               '-o', outfile, *self.prun_opts, *cmd]
        job = run(ctx, cmd, defer=True, stderr=subprocess.STDOUT, bufsize=0,
                  universal_newlines=False, **kwargs)
        _set_non_blocking(job.stdout)
        job.jobid = jobid
        job.nnodes = nnodes
        yield job

    def process_job_output(self, job):
        buf = job.stdout.read(1024)
        if buf is None:
            return

        job.output += buf.decode('ascii')

        numseconds = -1
        nodes = []
        for line in job.output.splitlines():
            if line.startswith(':'):
                for nodeid in line[1:].split():
                    m = re.fullmatch('node(\d+)/(\d+)', nodeid)
                    if not m:
                        return
                    machine = int(m.group(1))
                    core = int(m.group(2))
                    nodes.append((machine, core))
            elif numseconds == -1:
                m = re.search('for (\d+) seconds', line)
                if m:
                    numseconds = int(m.group(1))

        if nodes:
            assert numseconds > 0
            desc = 'node' if len(nodes) == 1 else 'nodes'
            nodelist = ', '.join('%d/%d' % ids for ids in nodes)
            suffix = '' if numseconds == self.default_job_time \
                     else ' for %d seconds' % numseconds
            self.log.info('running %s on %s %s%s' %
                          (job.jobid, desc, nodelist, suffix))
            job.start_time = time.time()


def _set_non_blocking(f):
    flags = fcntl.fcntl(f, fcntl.F_GETFL)
    fcntl.fcntl(f, fcntl.F_SETFL, flags | os.O_NONBLOCK)
