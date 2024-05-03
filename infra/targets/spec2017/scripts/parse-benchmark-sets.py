#!/usr/bin/env python3
import argparse
import glob
import os.path
import re
import sys
from pprint import pprint


def parse_setfile(path: str) -> tuple[str, list[str]]:
    with open(path) as f:
        contents = f.read()
    pat = r"^\$name\s*=\s*'([^']*)'.*^@benchmarks\s*=\s*qw\((.*)\)"
    match = re.search(pat, contents, re.MULTILINE | re.DOTALL)
    assert match
    name = match.group(1)
    benchmarks = match.group(2).split()
    return name, benchmarks


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} SPECDIR")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-x",
        "--exclude",
        nargs="+",
        default=[
            "996.specrand_fs",
            "997.specrand_fr",
            "998.specrand_is",
            "999.specrand_ir",
        ],
        help=(
            "names of benchmarks to exclude from all sets "
            "(default 996.specrand_fs,997.specrand_fr,998.specrand_is,999.specrand_ir)"
        ),
    )
    parser.add_argument("specdir", help="location of SPEC directory")
    args = parser.parse_args()

    assert os.path.exists(args.specdir)

    sets = {}
    allbench = set()

    for path in glob.glob(args.specdir + "/benchspec/CPU/*.bset"):
        name, benchmarks = parse_setfile(path)
        print(benchmarks)
        benchmarks = [b for b in benchmarks if b not in args.exclude]
        sets[name] = benchmarks

        for bench in benchmarks:
            allbench.add(bench)

    sets["all"] = sorted(allbench)
    for bench in allbench:
        sets[bench] = [bench]

    print(f"# this file has been generated by {os.path.basename(__file__)}")
    print("benchmark_sets = \\")
    pprint(sets)
