#!/usr/bin/env python3
import sys
import os.path
import glob
import re
from pprint import pprint


def parse_setfile(path):
    with open(path) as f:
        contents = f.read()
    pat = r"^\$name\s*=\s*'([^']*)'.*^@benchmarks\s*=\s*qw\((.*)\)"
    match = re.search(pat, contents, re.MULTILINE | re.DOTALL)
    assert match
    name = match.group(1)
    benchmarks = match.group(2).split()
    return name, benchmarks


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: %s SPECDIR' % sys.argv[0])
        sys.exit(1)

    specdir = sys.argv[1]
    assert os.path.exists(specdir)

    sets = {}
    allbench = set()

    for path in glob.glob(specdir + '/benchspec/CPU2006/*.bset'):
        name, benchmarks = parse_setfile(path)
        sets[name] = benchmarks

        for bench in benchmarks:
            allbench.add(bench)

    sets['all'] = sorted(allbench)
    for bench in allbench:
        sets[bench] = [bench]

    print('benchmark_sets = \\')
    pprint(sets)
