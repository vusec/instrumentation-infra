import argparse
import re
import statistics
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple, Union, cast

from ...command import Command
from ...commands.report import add_table_report_args, parse_logs, report_table
from ...context import Context
from ...util import FatalError, ResultDict, ResultVal


class SpecFindBadPrunNodesCommand(Command):
    name = "spec-find-bad-prun-nodes"
    description = "identify DAS-5 nodes with consistently high runtimes"

    # highlight runtimes whose deviation from the mean exceeds 3 times the
    # variance, but only if the percentage deviation is at least 2%
    highlight_variance_deviation = 3
    highlight_percent_threshold = 0.02

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        targetarg = parser.add_argument(
            "target",
            metavar="TARGET",
            choices=self.targets,
            help=" | ".join(self.targets),
        )
        rundirsarg = parser.add_argument(
            "rundirs",
            nargs="+",
            metavar="RUNDIR",
            default=[],
            help="run directories to parse (results/run.XXX)",
        )

        parser.add_argument(
            "-i",
            "--instances",
            nargs="+",
            metavar="INSTANCE",
            default=[],
            choices=self.instances,
            help=" | ".join(self.instances),
        )
        parser.add_argument(
            "--no-cache",
            action="store_false",
            dest="cache",
            help="cached results in the bottom of log files",
        )
        parser.add_argument(
            "--refresh", action="store_true", help="refresh cached results in logs"
        )

        add_table_report_args(parser)

        try:
            from argcomplete.completers import DirectoriesCompleter

            setattr(targetarg, "completer", self.complete_package)
            setattr(rundirsarg, "completer", DirectoriesCompleter())
        except ImportError:
            pass

    def run(self, ctx: Context) -> None:
        target = self.targets[ctx.args.target]
        instances = self.instances.select(ctx.args.instances)
        fancy = ctx.args.table == "fancy"

        # optional support for colored text
        try:
            if not fancy:
                raise ImportError
            from termcolor import colored
        except ImportError:

            def colored(
                text: str,
                color: Optional[str] = None,
                on_color: Optional[str] = None,
                attrs: Optional[Iterable[str]] = None,
                *,
                no_color: Optional[bool] = None,
                force_color: Optional[bool] = None,
            ) -> str:
                return text

        # parse result logs
        results = parse_logs(ctx, target, instances, ctx.args.rundirs)

        # compute aggregates
        benchdata: Dict[str, Dict] = defaultdict(lambda: defaultdict(dict))
        node_zscores: Dict[str, Dict] = defaultdict(lambda: defaultdict(list))
        node_runtimes: Dict[Tuple[str, str, str], List[Tuple[float, float, str]]] = (
            defaultdict(list)
        )
        workload = None

        for iname, iresults in results.items():
            grouped: Dict[str, List[ResultDict]] = defaultdict(list)

            for result in iresults:
                grouped[cast(str, result["benchmark"])].append(result)
                if workload is None:
                    workload = result.get("workload", None)
                elif result.get("workload", workload) != workload:
                    raise FatalError(
                        f"{result['benchmark']} uses {result['workload']} "
                        "workload whereas previous benchmarks use "
                        f"{workload} (logfile {result['outfile']})"
                    )

            for bench, bresults in grouped.items():
                if len(bresults) <= 1:
                    continue

                if any(r["status"] != "ok" for r in bresults):
                    continue

                # z-score per node
                entry: Dict[str, float] = benchdata[bench][iname]
                runtimes = cast(
                    List[Union[int, float]], [r["runtime"] for r in bresults]
                )
                entry["rt_mean"] = rt_mean = statistics.mean(runtimes)
                entry["rt_stdev"] = rt_stdev = statistics.pstdev(runtimes)
                entry["rt_variance"] = statistics.pvariance(runtimes)
                entry["rt_median"] = statistics.median(runtimes)
                for r in bresults:
                    node = cast(str, r["hostname"])
                    runtime = cast(float, r["runtime"])
                    zscore: float = (runtime - rt_mean) / rt_stdev
                    node_zscores[node][bench].append(zscore)
                    node_rt = runtime, zscore, cast(str, r["outfile"])
                    node_runtimes[(node, bench, iname)].append(node_rt)

        # order nodes such that the one with the highest z-scores (the most
        # deviating) come first
        zmeans: Dict[str, float] = {}
        for hostname, benchscores in node_zscores.items():
            allscores = []
            for bscores in benchscores.values():
                for score in bscores:
                    allscores.append(score)
            zmeans[hostname] = statistics.mean(allscores)
        nodes = sorted(zmeans, key=lambda n: zmeans[n], reverse=True)

        # show table with runtimes per node
        header = [" node:\n mean z-score:", ""]
        for node in nodes:
            nodename = node.replace("node", "")
            zscore_str: str = ("%.1f" % zmeans[node]).replace("0.", ".")
            header.append(nodename + "\n" + zscore_str)

        data: List[List[ResultVal]] = []
        high_devs: List[Tuple[str, str, str, float, str]] = []

        for bench, index in sorted(benchdata.items()):
            for iname, entry in index.items():
                row: List[ResultVal] = [" " + bench, iname]
                for node in nodes:
                    nruntimes = node_runtimes[(node, bench, iname)]
                    nruntimes.sort(reverse=True)

                    # highlight outliers to easily identify bad nodes
                    highlighted = []
                    for runtime, zscore, ofile in nruntimes:
                        rt = str(round(runtime))
                        deviation = runtime - entry["rt_mean"]
                        deviation_ratio = abs(deviation) / entry["rt_mean"]

                        if (
                            deviation**2
                            > entry["rt_variance"] * self.highlight_variance_deviation
                            and deviation_ratio > self.highlight_percent_threshold
                        ):
                            rt = colored(rt, "red")
                            high_devs.append((bench, node, iname, runtime, ofile))
                        elif runtime == entry["rt_median"]:
                            rt = colored(rt, "blue", attrs=["bold"])

                        highlighted.append(rt)

                    row.append(",".join(highlighted))

                data.append(row)

        title = "node runtimes"
        if fancy:
            title += " (red = high deviation, blue = median)"
        report_table(ctx, header, header, data, title)

        # show measurements with high deviations in separate table with log file
        # paths for easy access
        if high_devs:
            header = ["benchmark", "node", "instance", "runtime", "log file"]
            hd_data: List[List[ResultVal]] = []
            for bench, node, iname, runtime, ofile in high_devs:
                nodename = node.replace("node", "")
                opath = re.sub(f"^{ctx.paths.workdir}/", "", ofile)
                hd_data.append([bench, nodename, iname, runtime, opath])

            print(file=ctx.args.outfile)
            report_table(ctx, header, header, hd_data, "high deviations")
