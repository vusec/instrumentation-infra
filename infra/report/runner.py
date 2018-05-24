import sys
import os.path
import re
from contextlib import redirect_stdout
from subprocess import Popen
from typing import Iterable, List, Dict, Union, Optional, Callable, Any, Iterator
from ..target import Target
from ..package import Package
from ..instance import Instance
from ..parallel import Pool
from ..util import Namespace, run
from ..packages import BenchmarkUtils


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
        yield BenchmarkUtils()

    @staticmethod
    def configure(ctx: Namespace):
        """
        :param ctx: the configuration context
        """
        BenchmarkUtils().configure(ctx)

    def _make_rundir(self):
        dirname = self.ctx.timestamp.strftime('run.%Y-%m-%d.%H-%M-%S')
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
        if hasattr(job, 'outfiles'):
            # print to results logfiles
            outfiles = []
            for outfile in job.outfiles:
                self.ctx.log.debug('appending metadata to ' + outfile)
                with open(outfile) as f:
                    output = f.read()
                outfiles.append((open(outfile, 'a'), output))
            opened = True
        elif job.teeout:
            # print to stdout
            outfiles = [(sys.stdout, job.stdout)]
            opened = False
        else:
            # print to command output log
            outfile = [(open(self.ctx.paths.runlog, 'a'), job.stdout)]
            opened = True

        for outfile, output in outfiles:
            self.target.log_results(self.ctx, output, self.instance, self, outfile)
            if opened:
                outfile.close()

    def log_result(self, data: Dict[str, Any], outfile):
        """
        :param job:
        :param data:
        """
        with redirect_stdout(outfile):
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


def log_result(result: dict, outfile=sys.stdout):
    """
    """
    with redirect_stdout(outfile):
        print(prefix, 'begin')

        for key, value in result.items():
            print(prefix, key + ':', _box_value(value))

        print(prefix, 'end')


def parse_rundirs(ctx: Namespace,
                  target: Target,
                  instances: Iterable[Instance],
                  rundirs: List[str]
                  ) -> Dict[str, List[Namespace]]:
    """
    """
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

            for result in parse_results(ctx, path):
                result['outfile'] = path
                instance_results.append(result)

    return results


def parse_results(ctx: Namespace, path: str) -> Iterator[Namespace]:
    """
    """
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
