import argparse
import csv
import io
import os
import re
import sys
from contextlib import redirect_stdout
from decimal import Decimal
from functools import reduce
from itertools import chain, zip_longest
from statistics import mean, median, pstdev, pvariance
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from ..command import Command
from ..context import Context
from ..instance import Instance
from ..target import Target
from ..util import FatalError, ResultDict, ResultsByInstance, ResultVal

T = TypeVar("T")


def median_absolute_deviation(numbers: Sequence[float]) -> float:
    assert len(numbers) > 0
    med = median(numbers)
    return median(abs(x - med) for x in numbers)


def stdev_percent(numbers: Sequence[float]) -> float:
    return 100 * pstdev(numbers) / mean(numbers)


def assert_all_same(values: Sequence[T]) -> T:
    uniq = set(values)
    if len(uniq) > 1:
        raise FatalError(f"multiple values for 'same' field: {list(values)}")
    return uniq.pop()


def assert_one(values: Sequence[T]) -> T:
    values = list(values)
    assert len(values) > 0
    if len(values) > 1:
        raise FatalError(f"multiple values for 'one' field: {values}")
    return values[0]


def first(values: Sequence[T]) -> T:
    assert len(values) > 0
    return values[0]


def geomean(values: Sequence[float]) -> float:
    assert len(values) > 0
    return reduce(lambda x, y: x * y, values) ** (1.0 / len(values))


_aggregate_fns: Dict[str, Callable[[Sequence[Any]], Any]] = {
    "mean": mean,
    "median": median,
    "stdev": pstdev,
    "stdev_percent": stdev_percent,
    "variance": pvariance,
    "mad": median_absolute_deviation,
    "min": min,
    "max": max,
    "sum": sum,
    "count": len,
    "same": assert_all_same,
    "one": assert_one,
    "first": first,
    "all": list,
    "sort": sorted,
    "geomean": geomean,
}


FieldAggregators = Iterable[Tuple[str, Tuple[str, ...]]]
result_prefix = "[setup-report]"


class ReportCommand(Command):
    name = "report"
    description = "report results after a (parallel) run"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        subparsers = parser.add_subparsers(
            title="target",
            metavar="TARGET",
            dest="target",
            help=" | ".join(self.targets),
        )
        subparsers.required = True

        for name, target in self.targets.items():
            tparser = subparsers.add_parser(
                name=name,
                help=f"report configuration options for target {name} ({target.name})",
                formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            )

            rundirsarg = tparser.add_argument(
                "rundirs",
                nargs="+",
                metavar="RUNDIR",
                default=[],
                help="run directories to parse (results/run.XXX)",
            )

            tparser.add_argument(
                "-i",
                "--instances",
                nargs="+",
                metavar="INSTANCE",
                default=[],
                choices=self.instances,
                help=" | ".join(self.instances),
            )
            tparser.add_argument(
                "--no-cache",
                action="store_false",
                dest="cache",
                help="cached results in the bottom of log files",
            )
            tparser.add_argument("--refresh", action="store_true", help="refresh cached results in logs")

            add_table_report_args(tparser)

            report_modes = tparser.add_mutually_exclusive_group()
            report_modes.add_argument(
                "--raw",
                action="store_true",
                help="output all data points instead of aggregates",
            )
            report_modes.add_argument(
                "--overhead",
                metavar="INSTANCE",
                choices=self.instances,
                help="report each field as overhead relative to this baseline",
            )

            tparser.add_argument(
                "--groupby",
                metavar="FIELD",
                choices=_reportable_fields(target),
                default=target.aggregation_field,
                help=("field to group by when aggregating results " f"(default {target.aggregation_field})"),
            )
            tparser.add_argument(
                "--filter",
                nargs="+",
                default=[],
                help="only report these values of the --groupby field",
            )
            fieldarg = tparser.add_argument(
                "-f",
                "--field",
                nargs="+",
                action="append",
                metavar="FIELD [AGGR...]",
                default=[],
                help="""
                add reported field, followed by aggregation methods
                (unless --raw is true) separated by colons.
                Valid aggregations are
                mean|median|stdev|stdev_percent|mad|min|max|
                sum|count|first|same|one|all|geomean
                (stdev_percent = 100*stdev/mean,
                mad = median absolute deviation,
                same = asserts each value is the same,
                all = join values by space,
                sort = join sorted values by space)""",
            )
            tparser.add_argument(
                "--help-fields",
                action="store_true",
                help="print valid values for --field",
            )
            tparser.add_argument(
                "--aggregate",
                choices=_aggregate_fns,
                help="aggregation method for entire columns",
            )

            try:
                from argcomplete.completers import DirectoriesCompleter

                setattr(rundirsarg, "completer", DirectoriesCompleter())
                setattr(fieldarg, "completer", _FieldCompleter(target))
            except ImportError:
                pass

    def run(self, ctx: Context) -> None:
        a = ctx.args
        target: Target = self.targets[a.target]

        if a.help_fields:
            reportable_fields = _reportable_fields(target)
            colwidth = max(len(f) for f in reportable_fields)
            for f, desc in reportable_fields.items():
                print("%%-%ds" % colwidth % f, ":", desc)
            return

        fields = list(self._parse_fields(ctx, target))

        uniq_instances = set(a.instances)
        if len(a.instances) and a.overhead:
            uniq_instances.add(a.overhead)
        instances = self.instances.select(uniq_instances)

        # collect results: {instance: [{field:value}]}
        results = parse_logs(
            ctx,
            target,
            instances,
            a.rundirs,
            write_cache=a.cache,
            read_cache=a.cache and not a.refresh,
        )

        fn = self.report_raw if a.raw else self.report_aggregate
        fn(ctx, target, results, fields)

    def report_raw(
        self,
        ctx: Context,
        target: Target,
        results: ResultsByInstance,
        field_aggrs: FieldAggregators,
    ) -> None:
        fields = [f for f, aggr in field_aggrs]
        instances = sorted(results)

        header: List[str] = []
        human_header: List[str] = []
        for instance in instances:
            prefix = instance + "\n"
            for f in fields:
                header.append(f"{instance}_{f}")
                human_header.append(prefix + f)
                prefix = "\n"

        rows: Dict[str, List[Tuple[ResultVal, ...]]] = {}
        for instance in instances:
            rows[instance] = sorted(tuple(r[f] for f in fields) for r in results[instance])

        instance_rows = [rows[i] for i in instances]
        joined_rows: List[List[ResultVal]] = []
        for parts in zip_longest(*instance_rows, fillvalue=["", ""]):
            joined_rows.append(list(chain.from_iterable(parts)))

        title = f"{target.name} raw data"
        report_table(ctx, header, human_header, joined_rows, title)

    def report_aggregate(
        self,
        ctx: Context,
        target: Target,
        results: ResultsByInstance,
        fields: FieldAggregators,
    ) -> None:
        def keep(result: ResultDict) -> bool:
            return not ctx.args.filter or str(result[ctx.args.groupby]) in ctx.args.filter

        baseline_instance = ctx.args.overhead

        instances = sorted(results)
        groupby_values = sorted(
            set(
                result[ctx.args.groupby] for instance_results in results.values() for result in instance_results if keep(result)
            )
        )
        grouped: Dict[Tuple[Tuple[ResultVal, str], str], List[ResultVal]] = {}
        for instance, instance_results in results.items():
            for result in instance_results:
                if not keep(result):
                    continue
                key = result[ctx.args.groupby], instance
                for f, aggr in fields:
                    if f in result:
                        grouped.setdefault((key, f), []).append(result[f])

        header = [ctx.args.groupby]
        human_header = ["\n\n" + ctx.args.groupby]
        for instance in instances:
            if instance == baseline_instance:
                continue

            for i, (f, aggr) in enumerate(fields):
                for ag in aggr:
                    prefix = f"{'' if i else instance}\n{f}\n"
                    header.append(f"{instance}_{f}_{ag}")
                    human_header.append(prefix + ag)
                    prefix = "\n\n"

        data: List[List[Optional[ResultVal]]] = []
        for groupby_value in groupby_values:
            baseline_results = {}
            if baseline_instance:
                key = groupby_value, baseline_instance
                for f, aggr in fields:
                    for ag in aggr:
                        series = grouped.get((key, f), [-1])
                        value = _aggregate_fns[ag](series)
                        baseline_results[(groupby_value, f)] = value

            row: List[Optional[ResultVal]] = [groupby_value]
            for instance in instances:
                if instance == baseline_instance:
                    continue

                key = groupby_value, instance
                for f, aggr in fields:
                    for ag in aggr:
                        if (key, f) not in grouped:
                            value = None
                        else:
                            series = grouped[(key, f)]
                            value = _aggregate_fns[ag](series)
                            if baseline_results and isinstance(value, (int, float)):
                                value /= baseline_results[(groupby_value, f)]
                        row.append(value)
            data.append(row)

        if baseline_instance:
            title = f"{target.name} overhead vs {baseline_instance}"
        else:
            title = f"{target.name} aggregated data"

        table_options: Dict[str, bool] = {}

        if ctx.args.aggregate:
            aggrfn = _aggregate_fns[ctx.args.aggregate]

            def try_aggr(values: Sequence[Any]) -> Any:
                try:
                    return aggrfn(values)
                except Exception:
                    pass

            aggregate_row = [try_aggr(c) for c in zip(*data)]
            aggregate_row[0] = ctx.args.aggregate
            data.append(aggregate_row)
            table_options["inner_footing_row_border"] = True

        report_table(ctx, header, human_header, data, title, **table_options)

    def _parse_fields(self, ctx: Context, target: Target) -> FieldAggregators:
        for arg in chain.from_iterable(ctx.args.field):
            parts = arg.split(":")
            field = parts[0]

            if not ctx.args.raw and len(parts) == 1:
                raise FatalError(f"need aggregation methods for '{field}'")
            elif ctx.args.raw and len(parts) > 1:
                raise FatalError("cannot aggregate when reporting raw results")

            if field not in _reportable_fields(target):
                raise FatalError(f"unknown field '{field}'")

            for aggr in parts[1:]:
                if aggr not in _aggregate_fns:
                    raise FatalError(f"unknown aggregator '{aggr}' for {field}")

            yield field, tuple(parts[1:])


class _FieldCompleter:
    def __init__(self, target: Target):
        self.fields = _reportable_fields(target)

    def __call__(self, prefix: str, parsed_args: argparse.Namespace, **kwargs: Any) -> Iterator[str]:
        parts = prefix.split(":")
        if len(parts) == 1:
            field_prefix = parts[0]
            colon = "" if parsed_args.raw else ":"
            for field in self.fields:
                if field.startswith(field_prefix):
                    yield field + colon
        else:
            aggr_prefix = parts[-1]
            for aggr in _aggregate_fns:
                if aggr.startswith(aggr_prefix):
                    yield ":".join(parts[:-1] + [aggr])


def add_table_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o",
        "--outfile",
        type=argparse.FileType("w"),
        default=sys.stdout,
        help="outfile (default: stdout)",
    )

    can_fancy = sys.stdout.encoding == "UTF-8" and sys.stdout.name == "<stdout>"
    parser.add_argument(
        "--table",
        choices=("fancy", "ascii", "csv", "tsv", "ssv"),
        default="fancy" if can_fancy else "ascii",
        help=("output mode for tables: UTF-8 formatted (default) / " "ASCII tables / {comma,tab,space}-separated"),
    )

    parser.add_argument(
        "--precision",
        type=int,
        default=3,
        help="least significant digits to round numbers to (default 3)",
    )

    quickset_group = parser.add_mutually_exclusive_group()
    for mode in ("ascii", "csv", "tsv", "ssv"):
        quickset_group.add_argument(
            "--" + mode,
            action="store_const",
            const=mode,
            dest="table",
            help="short for --table=" + mode,
        )


def report_table(
    ctx: Context,
    nonhuman_header: List[str],
    human_header: List[str],
    data_rows: Iterable[Iterable[Optional[ResultVal]]],
    title: str,
    **table_options: bool,
) -> None:
    # don't align numbers for non-human reporting
    if ctx.args.table in ("csv", "tsv", "ssv"):
        data_rows = [[_to_string(ctx, v) for v in row] for row in data_rows]

        delim = {"csv": ",", "tsv": "\t", "ssv": " "}[ctx.args.table]
        writer = csv.writer(sys.stdout, quoting=csv.QUOTE_MINIMAL, delimiter=delim)
        with redirect_stdout(ctx.args.outfile):
            writer.writerow(nonhuman_header)
            for row in data_rows:
                writer.writerow(row)
        return

    # align numbers on the decimal point
    def get_whole_digits(n: Optional[ResultVal]) -> int:
        if isinstance(n, int):
            return len(str(n))
        if isinstance(n, float):
            return get_whole_digits(int(n + 0.5))
        return 0

    whole_digits = [max(map(get_whole_digits, values)) for values in zip(*data_rows)]

    def pad(n_string: str, n: Optional[ResultVal], col: int) -> str:
        if isinstance(n, int):
            return " " * (whole_digits[col] - len(n_string)) + n_string
        if isinstance(n, float):
            digits = n_string.find(".")
            if digits == -1:
                digits = len(n_string)
            return " " * (whole_digits[col] - digits) + n_string
        return n_string

    # stringify data
    data_rows = [[pad(_to_string(ctx, v), v, col) for col, v in enumerate(row)] for row in data_rows]

    # print human-readable table
    if ctx.args.table == "fancy":
        from terminaltables import SingleTable as Table
    else:
        assert ctx.args.table == "ascii"
        from terminaltables import AsciiTable as Table

    table = Table([human_header] + data_rows, f" {title} ")
    table.inner_column_border = False
    table.padding_left = 0

    for kw, val in table_options.items():
        setattr(table, kw, val)

    with redirect_stdout(ctx.args.outfile):
        print(table.table)


def _reportable_fields(target: Target) -> Mapping[str, str]:
    return {
        **target.reportable_fields(),
        "outfile": "log file containing the result",
    }


def _to_string(ctx: Context, n: Any) -> str:
    if n is None:
        return "-"
    if isinstance(n, float):
        return _precise_float(n, ctx.args.precision)
    if isinstance(n, list):
        s = " ".join(_to_string(ctx, v) for v in n)
        return f"[{s}]"
    if isinstance(n, bool):
        return "yes" if n else "no"
    return str(n)


def _precise_float(n: float, precision: int) -> str:
    """
    Ignore insignificant decimal numbers but avoid scientific notation.

    :param n: float to stringify
    :param precision: minimum nuber of significant digits
    :returns: non-scientific string notation of `n`

    >>> _precise_float(1234., 3)
    '1234'
    >>> _precise_float(123.4, 3)
    '123'
    >>> _precise_float(123.5, 3)
    '124'
    >>> _precise_float(12.34, 3)
    '12.3'
    >>> _precise_float(1.234, 3)
    '1.23'
    >>> _precise_float(.1234, 3)
    '0.123'
    >>> _precise_float(.01234, 3)
    '0.0123'
    >>> _precise_float(.00001234, 3)
    '0.0000123'
    >>> _precise_float(.1, 3)
    '0.100'
    >>> _precise_float(99.9, 3)
    '99.9'
    """
    tup = Decimal(n).as_tuple()
    assert isinstance(tup.exponent, int)
    zero_decimals = -tup.exponent - len(tup.digits)
    total_decimals = max(zero_decimals + precision, 0)
    return "%%.%df" % total_decimals % n


def outfile_path(ctx: Context, target: Target, instance: Instance, *args: str) -> str:
    """
    Returns the path to a log file for the benchmark of a particular
    instance, after creating the instance directory if it did not exist
    yet.

    :param ctx: the configuration context
    :param target: benchmark target
    :param instance: benchmark instance
    :param args: log file name, optionally preceded by nested directory names
    :returns: ``results/run.YY-MM-DD.HH-MM-SS/<target>/<instance>[/<arg>...]``
    """
    rundir = ctx.starttime.strftime("run.%Y-%m-%d.%H-%M-%S")
    path = os.path.join(ctx.paths.pool_results, rundir, target.name, instance.name, *args)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Maintain a 'last' symlink to latest results
    symname = os.path.join(ctx.paths.pool_results, "last")
    if os.path.exists(symname):
        os.unlink(symname)
    os.symlink(os.path.join(ctx.paths.pool_results, rundir), symname, target_is_directory=True)

    return path


def parse_logs(
    ctx: Context,
    target: Target,
    instances: Iterable[Instance],
    rundirs: List[str],
    write_cache: bool = True,
    read_cache: bool = True,
) -> Dict[str, List[ResultDict]]:
    """
    Parse logs from specified run directories.

    Traverse the directories to find log files, parse each logfile, and
    optionally append the parsed results to the log file. To get results
    for a log file, :func:`Target.parse_outfile` is called on the target.
    This should parse the log file and yield a number of result
    dictionaries of results found in the log file (e.g., an entry for each
    benchmark runtime). The parsed results are grouped per instance and
    returned as an ``{<instance_name>: [<result>, ...]}`` dictionary.

    If ``write_cache`` is true (which is the default), the results returned
    by each invocation of :func:`Target.parse_outfile` are logged into the
    log file itself and, is ``read_cache is true`` (also default), read
    from there in the next invocation of the report command. This means
    that expensive log file parsing is only done on the first invocation,
    and also that the log files become portable across systems without
    having to also copy any files referenced by the logs.

    :param ctx: the configuration context
    :param instances: list of instances to filter from, leave empty to get
                        results for all instances in the logs
    :param rundirs: run directories to traverse
    :param write_cache: whether to append log file results to the log file
                        itself
    :param read_cache: whether to read existing results from log files
                        instead of calling ``Target.parse_outfile``
    """
    abs_rundirs = []
    for d in rundirs:
        if not os.path.exists(d):
            raise FatalError(f"rundir {d} does not exist")
        abs_rundirs.append(os.path.abspath(d))

    instance_names = [instance.name for instance in instances]
    instance_dirs = []
    results: Dict[str, List[ResultDict]] = dict((iname, []) for iname in instance_names)

    for rundir in abs_rundirs:
        targetdir = os.path.join(rundir, target.name)
        if os.path.exists(targetdir):
            for instance in os.listdir(targetdir):
                instancedir = os.path.join(targetdir, instance)
                if os.path.isdir(instancedir):
                    if not instance_names or instance in instance_names:
                        instance_dirs.append((instance, instancedir))
        else:
            ctx.log.warning(f"rundir {rundir} contains no results for target {target.name}")

    for iname, idir in instance_dirs:
        instance_results: List[ResultDict] = results.setdefault(iname, [])

        for filename in sorted(os.listdir(idir)):
            path = os.path.join(idir, filename)
            if not os.path.isfile(path):
                continue

            fresults = process_log(ctx, path, target, write_cache=write_cache, read_cache=read_cache)
            instance_results += fresults

    return results


def process_log(
    ctx: Context,
    log_path: str,
    target: Target,
    write_cache: bool = True,
    read_cache: bool = True,
) -> Iterable[ResultDict]:
    """Parse a log file and cache any results in the logfile.
    If the log file was previously processed, cached results will be used.

    While the log file itself, maintained by the infra, should always yield
    the same results, it may contain references to additional log files
    inside the target. These files may be overwritten for any subsequent runs.
    By caching these results between runs, we preserve this data."""

    results = []
    if read_cache:
        results = list(parse_results(ctx, log_path, "cached"))

    if results:
        ctx.log.debug("using cached results from " + log_path)
    else:
        ctx.log.debug("parsing outfile " + log_path)
        results = list(target.parse_outfile(ctx, log_path))

        if write_cache:
            f = None
            try:
                if read_cache:
                    # there were no previous results, just append
                    f = open(log_path, "a")
                else:
                    # there may be previous results, strip them
                    f = open(log_path, "r+")
                    line = f.readline()
                    while line:
                        if line.startswith(result_prefix + " begin cached"):
                            ctx.log.debug("removing cached results")
                            f.seek(f.tell() - len(line))
                            f.truncate()
                            break
                        line = f.readline()

                ctx.log.debug(f"caching {len(results)} results")
                for result in results:
                    log_result("cached", result, f)
            finally:
                if f:
                    f.close()

    for result in results:
        result["outfile"] = _strip_cwd(log_path)

    return results


def log_result(name: str, result: ResultDict, ofile: io.TextIOWrapper) -> None:
    """
    :param name:
    :param result:
    :param ofile:
    """
    with redirect_stdout(ofile):
        print(result_prefix, "begin", name)

        for key, value in result.items():
            print(result_prefix, key + ":", _box_value(value))

        print(result_prefix, "end", name)


def parse_results(ctx: Context, path: str, name: str) -> Iterator[ResultDict]:
    """
    Parse results from a file and filter them by name. Can be used by a
    :func:`Target.parse_outfile` implementation to parse results written by the
    static library in the stderr log of a target.

    :param ctx: the configuration context
    :param path: path to file to parse
    :param name: name of results to retrieve
    :returns: results with the specified name
    """
    for rname, result in parse_all_results(ctx, path):
        if rname == name:
            yield result


def parse_all_results(ctx: Context, path: str) -> Iterator[Tuple[str, ResultDict]]:
    """
    Parse all results in a file.

    :param ctx: the configuration context
    :param path: path to file to parse
    :returns: (name, result) tuples
    """
    with open(path) as f:
        result: Optional[ResultDict] = None
        bname = None

        for lineno, line in enumerate(f):
            lineno = lineno + 1
            line = line.rstrip()
            if line.startswith(result_prefix):
                statement = line[len(result_prefix) + 1 :]
                if re.match(r"begin \w+", statement):
                    bname = statement[6:]
                    result = {}
                elif re.match(r"end \w+", statement):
                    if result is None:
                        ctx.log.error(f"missing start for '{bname}' end statement at" f" {path}:{lineno}")
                    else:
                        assert bname is not None
                        ename = statement[4:]
                        if ename != bname:
                            ctx.log.error("begin/end name mismatch at " f"{path}:{lineno}: {ename} != {bname}")

                        yield bname, result
                        result = bname = None
                elif result is None:
                    ctx.log.error(f"ignoring {result_prefix} statement outside of " f"begin-end at {path}:{lineno}")
                else:
                    name, value = statement.split(": ", 1)

                    if name in result:
                        ctx.log.warning(f"duplicate metadata entry for '{name}' at {path}:{lineno}," " using the last one")

                    result[name] = _unbox_value(value)

    if result is not None:
        ctx.log.error(f"{result_prefix} begin statement without end in {path}")


def _box_value(value: ResultVal) -> str:
    return str(value)


def _unbox_value(value: str) -> ResultVal:
    # bool
    if value == "True":
        return True
    if value == "False":
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


def _strip_cwd(path: str) -> str:
    cwd = os.path.join(os.getcwd(), "")
    return path[len(cwd) :] if path.startswith(cwd) else path
