import sys
import os.path
import re
from contextlib import redirect_stdout
from subprocess import Popen
from typing import Iterable, List, Union, Optional, Callable, Any
from pprint import pprint
from .target import Target
from .package import Package
from .instance import Instance
from .parallel import Pool
from .util import Namespace, FatalError, run
#from .packages import BenchmarkUtils


prefix = '[setup-report]'
special_prefix = '[setup-report-%s]'


class BenchmarkRunner:
    def __init__(self, ctx: Namespace, target: Target,
                 instance: Instance, filename: str):
        self.ctx = ctx
        self.target = target
        self.instance = instance
        self.outfile = os.path.join(self._make_rundir(), filename)

    @staticmethod
    def dependencies() -> Iterable[Package]:
        yield from []
        #yield BenchmarkUtils()

    @staticmethod
    def configure(ctx: Namespace):
        pass
        #BenchmarkUtils().configure(ctx)

    def _make_rundir(self):
        dirname = self.ctx.timestamp.strftime('run-%Y-%m-%d.%H:%M:%S')
        path = os.path.join(self.ctx.paths.pool_results, dirname,
                            self.target.name, self.instance.name)
        os.makedirs(path, exist_ok=True)
        return path

    def run(self, cmd: Union[str, List[str]],
            pool: Optional[Pool] = None,
            onsuccess: Optional[Callable[[Popen], None]] = None,
            **kwargs):
        env = {'SETUP_REPORT': '1'}

        if pool:
            def callback(job):
                self._print_footer(job)
                if onsuccess:
                    onsuccess(job)

            for job in pool.run(self.ctx, cmd, onsuccess=callback, env=env,
                                outfile=self.outfile, **kwargs):
                job.runner = self
        else:
            # TODO: make this work with stdout as outfile
            assert False
            job = run(self.ctx, cmd, env=env, **kwargs)
            job.runner = self
            self._print_footer(job)

    def _report_default_output(self, job):
        self.report('target', self.target.name)
        self.report('instance', self.instance.name)

    def _print_footer(self, job):
        self.ctx.log.debug('appending metadata to ' + job.outfile)
        with open(job.outfile, 'a') as outfile:
            with redirect_stdout(outfile):
                print(special_prefix % 'begin')
                self._report_default_output(job)
                self.target.report_output(self.ctx, job, self.instance, self)
                print(special_prefix % 'end')

    def report(self, name: str, value: Any):
        assert ' ' not in name
        print(prefix, '%s=%s' % (name, value))

    def report_next(self):
        print(special_prefix % 'next')

    #def wrap_command(self, ctx: Namespace, cmd: Union[str, List[str]]) -> List[str]:
    #    if isinstance(cmd, list):
    #        cmd = qjoin(cmd)
    #    config_root = os.path.dirname(os.path.abspath(__file__))
    #    return [
    #        'bash', '-c',
    #        '\n' + _unindent('''
    #        echo "[setup-report] target={self.target.name}"
    #        echo "[setup-report] instance={self.instance.name}"
    #        echo "[setup-report] cmd={cmd}"
    #        echo "[setup-report] cwd=`pwd`"
    #        echo "[setup-report] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
    #        echo "[setup-report] PATH=$PATH"
    #        echo "[setup-report] commit=`cd '{ctx.paths.root}' && git log -n1 --oneline`"
    #        echo "[setup-report] kernel=`uname -s`"
    #        echo "[setup-report] kernel-release=`uname -r`"
    #        echo "[setup-report] kernel-version=`uname -v`"
    #        echo "[setup-report] machine=`uname -m`"
    #        echo "[setup-report] node=`uname -n`"
    #        echo "[setup-report] date-start=`date +%Y-%m-%dT%H:%M:%S`"
    #        {cmd}
    #        echo "[setup-report] date-end=`date +%Y-%m-%dT%H:%M:%S`"
    #        '''.format(self=self, cmd=cmd, ctx=ctx))
    #    ]


class BenchmarkReporter:
    def __init__(self, ctx, target, instances, outfile):
        self.ctx = ctx
        self.target = target
        self.instances = instances
        self.outfile = outfile
        self.results = None

    def parse_rundirs(self, rundirs):
        instance_names = [instance.name for instance in self.instances]
        instance_dirs = []

        for rundir in rundirs:
            targetdir = os.path.join(rundir, self.target.name)
            if os.path.exists(targetdir):
                for subdir in os.listdir(targetdir):
                    instancedir = os.path.join(targetdir, subdir)
                    if os.path.isdir(instancedir):
                        if not instance_names or subdir in instance_names:
                            instance_dirs.append(instancedir)
            else:
                self.ctx.log.warning('rundir %s contains no results for '
                                     'target %s' % (rundir, self.target.name))

        for idir in instance_dirs:
            for filename in os.listdir(idir):
                path = os.path.join(idir, filename)
                print('file:', path)
                pprint(self._parse_metadata(path))

    def _parse_metadata(self, path):
        meta = {}

        with open(path) as f:
            for line in f:
                if line.startswith(prefix):
                    #ty, name, value = line.split(' ', 3)[1:]

                    #if ty == 'i':
                    #    value = int(value)
                    #elif ty == 'f':
                    #    value = float(value)
                    #else:
                    #    assert ty == 's'

                    name, value = line[len(prefix) + 1:].rstrip().split('=', 1)

                    if name in meta:
                        self.ctx.log.warning('duplicate metadata entry for '
                                             '"%s" in %s, using the last one' %
                                             (name, path))
                    meta[name] = value

        return meta

    def report(self, mode):
        with redirect_stdout(self.outfile):
            try:
                getattr(self, 'report_' + mode)()
            except AttributeError:
                raise FatalError('unknown reporting mode "%s"' % mode)

    def report_brief(self):
        pass

    def report_full(self):
        raise NotImplementedError

    def report_csv(self):
        raise NotImplementedError


def _unindent(cmd):
    stripped = re.sub(r'^\n|\n *$', '', cmd)
    indent = re.search('^ +', stripped, re.M)
    if indent:
        return re.sub(r'^' + indent.group(0), '', stripped, 0, re.M)
    return stripped
