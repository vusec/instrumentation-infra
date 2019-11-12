import argparse
import csv
import sys
from contextlib import redirect_stdout
from decimal import Decimal
from functools import reduce
from itertools import chain, zip_longest
from statistics import median, pstdev, pvariance, mean
from ..command import Command
from ..packages import BenchmarkUtils
from ..util import FatalError


def median_absolute_deviation(numbers):
    assert len(numbers) > 0
    med = median(numbers)
    return median(abs(x - med) for x in numbers)


def stdev_percent(numbers):
    return 100 * pstdev(numbers) / mean(numbers)


def assert_all_same(values):
    uniq = set(values)
    if len(uniq) > 1:
        raise FatalError('multiple values for "same" field: %s' % list(values))
    return uniq.pop()


def assert_one(values):
    values = list(values)
    assert len(values) > 0
    if len(values) > 1:
        raise FatalError('multiple values for "one" field: %s' % values)
    return values[0]


def first(values):
    assert len(values) > 0
    return values[0]


def geomean(values):
    assert len(values) > 0
    return reduce(lambda x, y: x * y, values) ** (1.0 / len(values))


_aggregate_fns = {'mean': mean, 'median': median,
                  'stdev': pstdev, 'stdev_percent': stdev_percent,
                  'variance': pvariance, 'mad': median_absolute_deviation,
                  'min': min, 'max': max, 'sum': sum, 'count': len,
                  'same': assert_all_same, 'one': assert_one,
                  'first': first, 'all': list, 'geomean': geomean}


class ReportCommand(Command):
    name = 'report'
    description = 'report results after a (parallel) run'

    def add_args(self, parser):
        subparsers = parser.add_subparsers(
                title='target', metavar='TARGET', dest='target',
                help=' | '.join(self.targets))
        subparsers.required = True

        for name, target in self.targets.items():
            tparser = subparsers.add_parser(name)
            rundirsarg = tparser.add_argument('rundirs', nargs='+',
                    help='run directories containing result logs')

            tparser.add_argument('-o', '--outfile',
                    type=argparse.FileType('w'), default=sys.stdout,
                    help='outfile (default: stdout)')
            tparser.add_argument('-i', '--instances', nargs='+',
                    metavar='INSTANCE', default=[], choices=self.instances,
                    help=' | '.join(self.instances))
            tparser.add_argument('--no-cache', action='store_false',
                    dest='cache',
                    help='cached results in the bottom of log files')
            tparser.add_argument('--refresh', action='store_true',
                    help='refresh cached results in logs')
            tparser.add_argument('--precision', type=int, default=3,
                    help='least significant digits to round numbers to '
                         '(default 3)')

            add_table_report_args(tparser)

            report_modes = tparser.add_mutually_exclusive_group()
            report_modes.add_argument('--raw', action='store_true',
                    help='output all data points instead of aggregates')
            report_modes.add_argument('--overhead', metavar='INSTANCE',
                    choices=self.instances,
                    help='report each field as overhead relative to this baseline')

            tparser.add_argument('--groupby', metavar='FIELD',
                    choices=_reportable_fields(target),
                    default=target.aggregation_field,
                    help='field to group by when aggregating results '
                         '(default %s)' % target.aggregation_field)
            tparser.add_argument('--filter', nargs='+', default=[],
                    help='only report these values of the --groupby field')
            fieldarg = tparser.add_argument('-f', '--field', nargs='+',
                    action='append', metavar='FIELD [AGGR...]', default=[],
                    help='''
                add reported field, followed by aggregation methods
                (unless --raw is true) separated by colons.
                Valid aggregations are
                mean|median|stdev|stdev_percent|mad|min|max|
                sum|count|first|same|one|all|geomean
                (stdev_percent = 100*stdev/mean,
                mad = median absolute deviation,
                same = asserts each value is the same,
                all = join values by space)''')
            tparser.add_argument('--help-fields', action='store_true',
                    help='print valid values for --field')
            tparser.add_argument('--aggregate', choices=_aggregate_fns,
                    help='aggregation method for entire columns')

            try:
                from argcomplete.completers import DirectoriesCompleter
                rundirsarg.completer = DirectoriesCompleter()
                fieldarg.completer = _FieldCompleter(target)
            except ImportError:
                pass

    def run(self, ctx):
        a = ctx.args
        target = self.targets[a.target]

        if a.help_fields:
            reportable_fields = _reportable_fields(target)
            colwidth = max(len(f) for f in reportable_fields)
            for f, desc in reportable_fields.items():
                print('%%-%ds' % colwidth % f, ':', desc)
            return

        fields = list(self._parse_fields(ctx, target))

        uniq_instances = set(a.instances)
        if len(a.instances) and a.overhead:
            uniq_instances.add(a.overhead)
        instances = self.instances.select(uniq_instances)

        # collect results: {instance: [{field:value}]}
        butils = BenchmarkUtils(target)
        results = butils.parse_logs(ctx, instances, a.rundirs,
                                    write_cache=a.cache,
                                    read_cache=a.cache and not a.refresh)

        with redirect_stdout(a.outfile):
            if a.raw:
                self.report_raw(ctx, target, results, fields)
            else:
                self.report_aggregate(ctx, target, results, fields)

    def report_raw(self, ctx, target, results, fields):
        fields = [f for f, aggr in fields]
        instances = sorted(results)

        header = []
        human_header = []
        for instance in instances:
            prefix = instance + '\n'
            for f in fields:
                header.append('%s_%s' % (instance, f))
                human_header.append(prefix + f)
                prefix = '\n'

        rows = {}
        for instance in instances:
            rows[instance] = sorted(tuple(r[f] for f in fields)
                                    for r in results[instance])

        instance_rows = [rows[i] for i in instances]
        joined_rows = []
        for parts in zip_longest(*instance_rows, fillvalue=['', '']):
            joined_rows.append(list(chain.from_iterable(parts)))

        title = ' %s raw data ' % target.name
        report_table(ctx, header, human_header, joined_rows, title)

    def report_aggregate(self, ctx, target, results, fields):
        def keep(result):
            return not ctx.args.filter or \
                str(result[ctx.args.groupby]) in ctx.args.filter

        baseline_instance = ctx.args.overhead

        instances = sorted(results)
        groupby_values = sorted(set(
            result[ctx.args.groupby]
            for instance_results in results.values()
            for result in instance_results
            if keep(result)
        ))
        grouped = {}
        for instance, instance_results in results.items():
            for result in instance_results:
                if not keep(result):
                    continue
                key = result[ctx.args.groupby], instance
                for f, aggr in fields:
                    if f in result:
                        grouped.setdefault((key, f), []).append(result[f])

        header = [ctx.args.groupby]
        human_header = ['\n\n' + ctx.args.groupby]
        for instance in instances:
            if instance == baseline_instance:
                continue

            for i, (f, aggr) in enumerate(fields):
                for ag in aggr:
                    prefix = '%s\n%s\n' % ('' if i else instance, f)
                    header.append('%s_%s_%s' % (instance, f, ag))
                    human_header.append(prefix + ag)
                    prefix = '\n\n'

        data = []
        for groupby_value in groupby_values:
            baseline_results = {}
            if baseline_instance:
                key = groupby_value, baseline_instance
                for f, aggr in fields:
                    for ag in aggr:
                        series = grouped.get((key, f), [-1])
                        value = _aggregate_fns[ag](series)
                        baseline_results[(groupby_value, f)] = value

            row = [groupby_value]
            for instance in instances:
                if instance == baseline_instance:
                    continue

                key = groupby_value, instance
                for f, aggr in fields:
                    for ag in aggr:
                        series = grouped.get((key, f), None)
                        if series is None:
                            value = None
                        else:
                            value = _aggregate_fns[ag](series)
                            if baseline_results and isinstance(value, (int, float)):
                                value /= baseline_results[(groupby_value, f)]
                        row.append(value)
            data.append(row)

        if baseline_instance:
            title = ' %s overhead on %s ' % (target.name, baseline_instance)
        else:
            title = ' %s aggregated data ' % target.name

        table_options = {}

        if ctx.args.aggregate:
            aggr = _aggregate_fns[ctx.args.aggregate]
            def try_aggr(values):
                try:
                    return aggr(values)
                except:
                    pass
            aggregate_row = [try_aggr(c) for c in zip(*data)]
            aggregate_row[0] = ctx.args.aggregate
            data.append(aggregate_row)
            table_options['inner_footing_row_border'] = True

        report_table(ctx, header, human_header, data, title, **table_options)

    def _parse_fields(self, ctx, target):
        for arg in chain.from_iterable(ctx.args.field):
            parts = arg.split(':')
            field = parts[0]

            if not ctx.args.raw and len(parts) == 1:
                raise FatalError('need aggregation methods for "%s"' % field)
            elif ctx.args.raw and len(parts) > 1:
                raise FatalError('cannot aggregate when reporting raw results')

            if field not in _reportable_fields(target):
                raise FatalError('unknown field "%s"' % field)

            for aggr in parts[1:]:
                if aggr not in _aggregate_fns:
                    raise FatalError('unknown aggregator "%s" for %s' %
                                     (aggr, field))

            yield field, tuple(parts[1:])


class _FieldCompleter:
    def __init__(self, target):
        self.fields = _reportable_fields(target)

    def __call__(self, prefix, parsed_args, **kwargs):
        parts = prefix.split(':')
        if len(parts) == 1:
            field_prefix = parts[0]
            colon = '' if parsed_args.raw else ':'
            for field in self.fields:
                if field.startswith(field_prefix):
                    yield field + colon
        else:
            aggr_prefix = parts[-1]
            for aggr in _aggregate_fns:
                if aggr.startswith(aggr_prefix):
                    yield ':'.join(parts[:-1] + [aggr])


def add_table_report_args(parser):
    can_fancy = sys.stdout.encoding == 'UTF-8' and sys.stdout.name == '<stdout>'
    parser.add_argument('--table',
            choices=('fancy', 'ascii', 'csv', 'tsv', 'ssv'),
            default='fancy' if can_fancy else 'ascii-table',
            help='output mode for tables: UTF-8 formatted (default) / '
                 'ASCII tables / {comma,tab,space}-separated')

    quickset_group = parser.add_mutually_exclusive_group()
    for mode in ('ascii', 'csv', 'tsv', 'ssv'):
        quickset_group.add_argument('--' + mode,
                action='store_const', const=mode, dest='table',
                help='short for --table=' + mode)


def report_table(ctx, nonhuman_header, human_header, data_rows, title,
                 **table_options):
    # don't align numbers for non-human reporting
    if ctx.args.table in ('csv', 'tsv', 'ssv'):
        data_rows = [[_to_string(ctx, v) for v in row] for row in data_rows]

        delim = {'csv': ',', 'tsv': '\t', 'ssv': ' '}[ctx.args.table]
        writer = csv.writer(sys.stdout, quoting=csv.QUOTE_MINIMAL,
                            delimiter=delim)
        writer.writerow(nonhuman_header)
        for row in data_rows:
            writer.writerow(row)
        return

    # align numbers on the decimal point
    def get_whole_digits(n):
        if isinstance(n, int):
            return len(str(n))
        if isinstance(n, float):
            return get_whole_digits(int(n + 0.5))
        return 0

    whole_digits = [max(map(get_whole_digits, values))
                    for values in zip(*data_rows)]
    print(whole_digits)

    def pad(n_string, n, col):
        if isinstance(n, int):
            return ' ' * (whole_digits[col] - len(n_string)) + n_string
        if isinstance(n, float):
            digits = n_string.find('.')
            if digits == -1:
                digits = len(n_string)
            return ' ' * (whole_digits[col] - digits) + n_string
        return n_string

    # stringify data
    data_rows = [[pad(_to_string(ctx, v), v, col) for col, v in enumerate(row)]
                 for row in data_rows]

    # print human-readable table
    if ctx.args.table == 'fancy':
        from terminaltables import SingleTable as Table
    else:
        assert ctx.args.table == 'ascii'
        from terminaltables import AsciiTable as Table

    table = Table([human_header] + data_rows, title)
    table.inner_column_border = False
    table.padding_left = 0

    for kw, val in table_options.items():
        if isinstance(val, dict):
            attr = getattr(table, kw)
            if isinstance(val, dict):
                attr.update(val)
            else:
                assert isinstance(attr, list)
                for index, elem in val.items():
                    attr[index] = elem
        else:
            setattr(table, kw, val)

    print(table.table)
    return table


def _reportable_fields(target):
    return {
        **target.reportable_fields,
        'outfile': 'log file containing the result',
    }


def _to_string(ctx, n):
    if n is None:
        return '-'
    if isinstance(n, float):
        return _precise_float(n, ctx.args.precision)
    if isinstance(n, list):
        return '[%s]' % ' '.join(_to_string(ctx, v) for v in n)
    if isinstance(n, bool):
        return 'yes' if n else 'no'
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
    zero_decimals = -tup.exponent - len(tup.digits)
    total_decimals = max(zero_decimals + precision, 0)
    return '%%.%df' % total_decimals % n
