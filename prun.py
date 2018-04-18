import sys
import threading
import time
import shlex
import shutil
import select
import re
import subprocess
import math
from .util import run


class PrunScheduler:
    poll_interval = 0.050  # seconds to wait for blocking actions
    default_job_time = 900 # if prun reserves this amount, it is not logged

    def __init__(self, logger, parallelmax=None, prun_opts=[]):
        self.log = logger
        self.parallelmax = parallelmax
        self.prun_opts = prun_opts
        self.jobs = {}

    def __del__(self):
        if hasattr(self, 'pollthread'):
            self.done = True
            self.pollthread.join(self.poll_interval)

    def start_poller(self):
        if not hasattr(self, 'pollthread'):
            self.poller = select.epoll()
            self.pollthread = threading.Thread(target=self.poller_thread,
                                               name='prun-poller')
            self.pollthread.daemon = True
            self.done = False
            self.pollthread.start()

    def poller_thread(self):
        # monitor the job queue for finished jobs, remove them from the queue
        # and call success/error callbacks
        while not self.done:
            for fd, flags in self.poller.poll(timeout=self.poll_interval):
                if flags & (select.EPOLLIN | select.EPOLLPRI):
                    self.append_and_parse_output(self.jobs[fd])

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

    def append_and_parse_output(self, job):
        job.output += job.stdout.read(1024).decode('utf-8')

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

    def wait_for_queue_space(self, nodes_needed):
        if self.parallelmax is not None:
            nnodes = sum(job.nnodes for job in self.jobs.values())
            while nnodes + nodes_needed > self.parallelmax:
                time.sleep(self.poll_interval)

    def run(self, ctx, cmd, jobid, outfile, nnodes, **kwargs):
        self.start_poller()
        self.wait_for_queue_space(nnodes)

        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        cmd = ['prun', '-v', '-np', '%d' % nnodes, '-1',
               '-o', outfile, *self.prun_opts, *cmd]

        ctx.log.info('scheduling ' + jobid)
        job = run(ctx, cmd, defer=True, stderr=subprocess.STDOUT, bufsize=0,
                  universal_newlines=False, **kwargs)
        job.nnodes = nnodes
        job.jobid = jobid
        job.output = ''
        self.jobs[job.stdout.fileno()] = job
        self.poller.register(job.stdout, select.EPOLLIN | select.EPOLLPRI |
                                         select.EPOLLERR | select.EPOLLHUP)
        return job

    def wait_all(self):
        while len(self.jobs):
            time.sleep(self.poll_interval)

    def onsuccess(self, job):
        self.log.info('job %s finished%s' %
                      (job.jobid, self.get_elapsed(job)))
        self.log.debug('command: %s' % job.cmd_print)

    def onerror(self, job):
        self.log.error('job %s returned status %d%s' %
                      (job.jobid, job.poll(), self.get_elapsed(job)))
        self.log.error('command: %s' % job.cmd_print)
        sys.stdout.write(job.output)

    def get_elapsed(self, job):
        if not hasattr(job, 'start_time'):
            return ''
        return ' after %d seconds' % (math.ceil(time.time() - job.start_time))


def prun_supported():
    return shutil.which('prun') is not None
