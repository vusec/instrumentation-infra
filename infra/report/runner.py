import os.path
import re
from contextlib import redirect_stdout
from subprocess import Popen
from typing import Iterable, List, Dict, Union, Optional, Callable, Any
from ..target import Target
from ..package import Package
from ..instance import Instance
from ..parallel import Pool
from ..util import Namespace, run
#from ..packages import BenchmarkUtils


prefix = '[setup-report]'


class BenchmarkRunner:
    """
    :param ctx: the configuration context
    :param target:
    :param instance:
    :param filename:
    """

    def __init__(self, ctx: Namespace, target: Target,
                 instance: Instance, filename: str):
        self.ctx = ctx
        self.target = target
        self.instance = instance
        self.outfile = os.path.join(self._make_rundir(), filename)

    @staticmethod
    def dependencies() -> Iterable[Package]:
        """
        """
        yield from []
        #yield BenchmarkUtils()

    @staticmethod
    def configure(ctx: Namespace):
        """
        :param ctx: the configuration context
        """
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
        """
        :param job:
        """
        env = {'SETUP_REPORT': '1'}

        if pool:
            def callback(job):
                self._print_footer(job)
                if onsuccess:
                    return onsuccess(job)

            for job in pool.run(self.ctx, cmd, onsuccess=callback, env=env,
                                outfile=self.outfile, **kwargs):
                job.runner = self
        else:
            job = run(self.ctx, cmd, env=env, **kwargs)
            job.runner = self
            self._print_footer(job)

    def _print_footer(self, job):
        if hasattr(job, 'outfile'):
            # print to results logfile
            self.ctx.log.debug('appending metadata to ' + job.outfile)
            with open(job.outfile) as f:
                output = f.read()
            with open(job.outfile, 'a') as outfile:
                with redirect_stdout(outfile):
                    self.target.log_results(self.ctx, output, self.instance, self)
        elif job.teeout:
            # print to stdout
            self.target.log_results(self.ctx, job.stdout, self.instance, self)
        else:
            # print to command output log
            with open(self.ctx.paths.runlog, 'a') as outfile:
                with redirect_stdout(outfile):
                    self.target.log_results(self.ctx, job.stdout, self.instance, self)

    def log_result(self, data: Dict[str, Any]):
        """
        :param job:
        :param data:
        """
        print(prefix, 'begin')
        #print(prefix, 'target:', _box_value(self.target.name))
        #print(prefix, 'instance:', _box_value(self.instance.name))

        for key, value in data.items():
            print(prefix, key + ':', _box_value(value))

        print(prefix, 'end')

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


def parse_rundirs(ctx, target, instances, rundirs):
    instance_names = [instance.name for instance in instances]
    instance_dirs = []
    results = dict((iname, []) for iname in instance_names)

    for rundir in rundirs:
        targetdir = os.path.join(rundir, target.name)
        if os.path.exists(targetdir):
            for instance in os.listdir(targetdir):
                instancedir = os.path.join(targetdir, instance)
                if os.path.isdir(instancedir):
                    if not instance_names or instance in instance_names:
                        instance_dirs.append((instance, instancedir))
        else:
            ctx.log.warning('rundir %s contains no results for target %s' %
                            (rundir, target.name))

    for iname, idir in instance_dirs:
        instance_results = results.setdefault(iname, [])

        for filename in sorted(os.listdir(idir)):
            path = os.path.join(idir, filename)

            for result in _parse_metadata(ctx, path):
                result['outfile'] = path
                instance_results.append(result)

    return results


def _parse_metadata(ctx, path):
    with open(path) as f:
        meta = None

        for line in f:
            line = line.rstrip()
            if line.startswith(prefix):
                statement = line[len(prefix) + 1:]
                if statement == 'begin':
                    meta = Namespace()
                elif statement == 'end':
                    yield meta
                    meta = None
                elif meta is None:
                    ctx.log.error('ignoring %s statement outside of begin-end '
                                  'in %s' % (prefix, path))
                else:
                    name, value = statement.split(': ', 1)

                    if name in meta:
                        ctx.log.warning('duplicate metadata entry for "%s" in '
                                        '%s, using the last one' % (name, path))

                    meta[name] = _unbox_value(value)

    if meta is not None:
        ctx.log.error('%s begin statement without end in %s' % (prefix, path))


def _box_value(value):
    return str(value)


def _unbox_value(value):
    # bool
    if value == 'True':
        return True
    if value == 'False':
        return False

    # int
    if value.isdigit():
        return int(value)

    # float
    try:
        return float(value)
    except ValueError:
        pass

    # string
    return value


def _unindent(cmd):
    stripped = re.sub(r'^\n|\n *$', '', cmd)
    indent = re.search('^ +', stripped, re.M)
    if indent:
        return re.sub(r'^' + indent.group(0), '', stripped, 0, re.M)
    return stripped
