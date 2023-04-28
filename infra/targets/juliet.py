import os
import re
import shutil
from itertools import chain
from pathlib import Path
from typing import List, Optional

from ..util import Namespace, download, run
from ..instance import Instance
from ..target import Target
from ..parallel import Pool


class Juliet(Target):
    """
    The `Juliet Test Suite for C/C++ <https://samate.nist.gov/SRD/around.php#juliet_documents>`_.

    This test suite contains a large amount of programs, categorized by
    vulnerability type (CWE). Most programs include both a "good" and "bad"
    version, where the good version should succeed (no bug) whereas the bad
    version should be detected by the applied mitigation. In other words, the
    good version tests for false positives, and the bad version for false
    negatives.

    The ``--cwe`` command-line argument specifies which CWEs to build and/or
    run, and can be a CWE-ID (``416`` or ``CWE416``) or an alias (e.g.,
    ``uaf``). A mix of CWE-IDs and aliases is allowed.

    The Juliet suite contains multiple *flow variants* per test case. These are
    different control-flows in the program, that in the end all arrive at the
    same bug. This is only relevant for static analysis tools, and for run-time
    mitigations these are unsuitable. In particular, some flow variants (e.g.,
    12) do not (always) trigger or reach the bug at runtime. Therefore, by
    default only flow variant 01 is used, but others can be specified with the
    ``--variants`` command-line argument.

    By default, a good test is counted as successful (true negative) if its
    returncode is 0, and a bad test is counted as successful (true positive) if
    its returncode is non-zero. The latter behavior can be fine-tuned via the
    ``mitigation_return_code`` argument to this class, which can be set to match
    the returncode of the mitigation.

    Each test receives a fixed string to stdin.
    Tests that are based on sockets are currently not supported, as this
    requires running two tests at the same time (a client and a server).

    Tests can be built in parallel (using ``--parallel=proc``), since this
    process might take a while when multiple CWEs or variants are selected.
    Running tests in parallel is not supported (yet).

    :name: juliet
    :param mitigation_return_code: Return code the mitigation exits with, to
                                   distinguish true positives for the bad
                                   version of testcases. If ``None``, any
                                   non-zero value is considered a success.

    """

    name = 'juliet'

    zip_name = 'Juliet_Test_Suite_v1.3_for_C_Cpp.zip'

    def __init__(self, mitigation_return_code: Optional[int] = None):
        self.mitigation_return_code = mitigation_return_code

    def add_build_args(self, parser):
        parser.add_argument('--cwe',
                required=True, nargs='+',
                help='which CWE to build')
        parser.add_argument('--variants',
                nargs='+', type=int, default=[1],
                help='which flow variants to build')

    def add_run_args(self, parser):
        parser.add_argument('--cwe',
                required=True, nargs='+',
                help='which CWE to run')
        parser.add_argument('--variants',
                nargs='+', type=int, default=[1],
                help='which flow variants to build')

    @staticmethod
    def parse_cwe_list(cwe_list: List[str]) -> List[str]:
        aliases = {}
        aliases['buffer-overflow'] = ['CWE121', 'CWE122', 'CWE124', 'CWE126',
                                      'CWE127', 'CWE680']
        aliases['spatial'] = aliases['buffer-overflow'] + ['CWE123']
        aliases['double-free'] = ['CWE415']
        aliases['uaf'] = ['CWE416']
        aliases['stack-uaf'] = ['CWE562']
        aliases['invalid-free'] = ['CWE590', 'CWE761']
        aliases['memory-error'] = chain(*aliases.values())

        ret = set()
        for cwe in cwe_list:
            if re.match(r'^CWE\d+$', cwe):
                ret.add(cwe)
            elif re.match(r'^\d+$', cwe):
                ret.add(f'CWE{cwe}')
            elif cwe in aliases:
                for c in aliases[cwe]:
                    ret.add(c)
            else:
                raise ValueError(f'CWE must be in format "CWE<number>" or one '
                                 f'of {",".join(aliases)}, not {cwe}')
        return list(ret)

    def is_fetched(self, ctx: Namespace):
        return os.path.exists(self.zip_name)

    def fetch(self, ctx: Namespace):
        url = f'https://zenodo.org/record/4701387/files/{self.zip_name}?download=1'
        download(ctx, url)

    def build(self, ctx: Namespace, instance: Instance,
              pool: Optional[Pool] = None):
        for cwe in self.parse_cwe_list(ctx.args.cwe):
            self.build_cwe(ctx, instance, pool, cwe)

    def build_cwe(self, ctx: Namespace, instance: Instance,
                  pool: Optional[Pool], cwe: str):
        bdir = Path(self.path(ctx))
        srcrootdir = bdir / 'src'
        os.makedirs(srcrootdir, exist_ok=True)

        testcasedir = srcrootdir / 'C' / 'testcases'
        incdir = srcrootdir / 'C' / 'testcasesupport'
        if not testcasedir.is_dir():
            run(ctx, ['unzip', self.zip_name, '-d', str(srcrootdir)])
        cwedirs = list(testcasedir.glob(f'{cwe}_*'))
        if not cwedirs:
            raise Exception(f'Could not find {cwe}')
        assert len(cwedirs) == 1
        cwedir = cwedirs[0]

        objdir = bdir / 'obj' / instance.name / cwe
        gooddir = objdir / 'good'
        baddir = objdir / 'bad'
        if objdir.exists():
            shutil.rmtree(objdir)
        os.makedirs(gooddir, exist_ok=True)
        os.makedirs(baddir, exist_ok=True)

        # Some CWEs split their tests up in subdirs
        cwesrcdirs = [cwedir]
        if (cwedir / 's01').exists():
            cwesrcdirs = list(cwedir.glob('s*'))
            assert len(cwesrcdirs) > 1

        for cwesrcdir in cwesrcdirs:
            for testpath in chain(cwesrcdir.glob('*.c'),
                                  cwesrcdir.glob('*.cpp')):
                testname = testpath.stem

                m = re.match(r'.*_(\d+)([a-z]|_[a-zA-Z0-9]+)?', testname)
                if not m:
                    continue
                variant = int(m.group(1))
                part = m.group(2)

                # Only run selected flow-variants (normally only 01)
                if variant not in ctx.args.variants:
                    continue

                # Skip windows-only tests
                if 'w32' in testname or 'wchar_t' in testname:
                    continue

                # Skip socket tests since we cannot run them (multi-program)
                if 'socket' in testname:
                    continue

                # Handle multi-file test-cases
                testfiles = [str(testpath)]
                if part:
                    if part != 'a':
                        continue
                    testname = testname[:-1]
                    pattern = f'{testname}*{testpath.suffix}'
                    testfiles = [str(f) for f in cwesrcdir.glob(pattern)]

                ctx.log.info(f'building {testname}')

                goodbin = gooddir / testname
                badbin = baddir / testname

                if testpath.suffix == '.c':
                    compiler = [ctx.cc, *ctx.cflags]
                else:
                    compiler = [ctx.cxx, *ctx.cxxflags]

                compiler += ['-DINCLUDEMAIN']
                compiler += ['-I', str(incdir)]
                testfiles += [str(incdir / 'io.c')]

                # Support parallel builds via a pool (use --parallel=proc)
                runfunc = run
                kwargs_good, kwargs_bad = {}, {}
                if pool:
                    runfunc = pool.run
                    kwargs_good['nnodes'] = kwargs_bad['nnodes'] = 1
                    kwargs_good['jobid'] = f'build-{testname}-good'
                    kwargs_bad['jobid'] = f'build-{testname}-bad'
                    resdir = Path(ctx.paths.pool_results)
                    outdir = resdir / 'build' / self.name / instance.name
                    kwargs_good['outfile'] = f'{outdir}/{testname}-good'
                    kwargs_bad['outfile'] = f'{outdir}/{testname}-bad'

                if 'bad' not in testname:
                    runfunc(ctx, [
                        *compiler,
                        *testfiles,
                        '-o', str(goodbin),
                        '-DOMITBAD',
                        *ctx.ldflags,
                    ], **kwargs_good)
                if 'good' not in testname:
                    runfunc(ctx, [
                        *compiler,
                        *testfiles,
                        '-o', str(badbin),
                        '-DOMITGOOD',
                        *ctx.ldflags
                    ], **kwargs_bad)

    def binary_paths(self, ctx: Namespace, instance: Instance):
        paths = []

        for cwe in self.parse_cwe_list(ctx.args.cwe):
            bdir = Path(self.path(ctx))
            objdir = bdir / 'obj' / instance.name / cwe

            gooddir = objdir / 'good'
            for testpath in gooddir.iterdir():
                paths.append(testpath)

            baddir = objdir / 'bad'
            for testpath in baddir.iterdir():
                paths.append(testpath)

        return paths

    def run(self, ctx: Namespace, instance: Instance):
        for cwe in self.parse_cwe_list(ctx.args.cwe):
            self.run_cwe(ctx, instance, cwe)

    def run_cwe(self, ctx: Namespace, instance: Instance, cwe: str):
        bdir = Path(self.path(ctx))
        objdir = bdir / 'obj' / instance.name / cwe
        stdin = b'A' * 8

        good_ok_cnt, good_total_cnt = 0, 0
        gooddir = objdir / 'good'
        for testpath in gooddir.iterdir():
            testname = testpath.stem
            good_total_cnt += 1
            proc = run(ctx, [str(testpath)], env=ctx.runenv, silent=True,
                       allow_error=False, input=stdin, universal_newlines=False)
            if proc.returncode:
                ctx.log.error(f'GOOD {testname} returned error')
            else:
                good_ok_cnt += 1

        bad_ok_cnt, bad_total_cnt = 0, 0
        baddir = objdir / 'bad'
        for testpath in baddir.iterdir():
            testname = testpath.stem
            if 'good' in testname:
                continue
            bad_total_cnt += 1
            proc = run(ctx, [str(testpath)], env=ctx.runenv, silent=True,
                       allow_error=True, input=stdin, universal_newlines=False)
            if self.mitigation_return_code is not None and \
                self.mitigation_return_code != proc.returncode:
                ctx.log.error(f'BAD {testname} did not return correct error: '
                              f'returned {proc.returncode}, expected '
                              f'{self.mitigation_return_code}')
            elif self.mitigation_return_code is None and \
                 not proc.returncode:
                ctx.log.error(f'BAD {testname} did not return error')
            else:
                bad_ok_cnt += 1

        ctx.log.info(f'{cwe}: Passed {good_ok_cnt}/{good_total_cnt} GOOD tests')
        ctx.log.info(f'{cwe}: Passed {bad_ok_cnt}/{bad_total_cnt} BAD tests')
