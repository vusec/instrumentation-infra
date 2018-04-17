import sys
import threading
import time
import shlex
from subprocess import PIPE
from .util import run, FatalError


class PrunScheduler:
    poll_interval = 0.050 # seconds

    def __init__(self, parallelmax=None, iterations=1, prun_opts=[]):
        self.poller = None

        if parallelmax is not None and parallelmax % iterations != 0:
            raise FatalError('%s: parallelmax should be a multiple of '
                             'iterations' % self.__class__.__name__)

        self.iterations = iterations
        self.parallelmax = parallelmax / iterations
        self.prun_opts = prun_opts
        self.jobs = set()
        self.jobctx = {}

    def __del__(self):
        self.done = True
        if self.poller:
            self.poller.join(self.poll_interval)

    def start_poller(self):
        if self.poller is None:
            self.poller = threading.Thread(target=self.poller_thread,
                                           name='prun-poller')
            self.poller.daemon = True
            self.queue_lock = threading.Lock()
            self.done = False
            self.poller.start()

    def poller_thread(self):
        # monitor the job queue for finished jobs, remove them from the queue
        # and call success/error callbacks
        while not self.done:
            finished = set()
            with self.queue_lock:
                for job in self.jobs:
                    if job.poll() is not None:
                        finished.add(job)
                self.jobs -= finished

            for job in finished:
                ctx = self.jobctx.pop(job)
                if job.returncode == 0:
                    self.onsuccess(job, ctx)
                else:
                    self.onerror(job, ctx)

            time.sleep(self.poll_interval)

    def wait_for_queue_space(self):
        if self.parallelmax is not None:
            while len(self.jobs) >= self.parallelmax:
                time.sleep(self.poll_interval)

    def run(self, ctx, cmd, outfile, **kwargs):
        self.start_poller()
        self.wait_for_queue_space()

        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        cmd = ['prun', '-v', '-np', '%d' % self.iterations, '-1',
               '-o', outfile, *self.prun_opts, *cmd]

        job = run(ctx, cmd, defer=True, **kwargs)
        self.jobctx[job] = ctx

        with self.queue_lock:
           self.jobs.add(job)

        return job

    def wait_all(self):
        while len(self.jobs):
            time.sleep(self.poll_interval)

    def onsuccess(self, job, ctx):
        ctx.log.info('prun command finished successfully')
        ctx.log.debug('command: %s' % job.cmd_print)
        stdout, stderr = job.communicate()
        sys.stdout.write(stdout)
        sys.stdout.write(stderr)

    def onerror(self, job, ctx):
        ctx.log.error('prun command returned status %d' % job.returncode)
        ctx.log.error('command: %s' % job.cmd_print)
        stdout, stderr = job.communicate()
        sys.stdout.write(stdout)
        sys.stdout.write(stderr)
