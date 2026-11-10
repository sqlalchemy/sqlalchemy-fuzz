"""Command line interface: ``sqla-fuzz <fuzzer> [options]``."""

from __future__ import annotations

import argparse
import json

from . import report
from . import runner
from .fuzzers import FUZZERS


def _seed_range(text):
    try:
        lo, hi = (int(x) for x in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a range like 0:20000, got {text!r}"
        )
    if hi <= lo:
        raise argparse.ArgumentTypeError(f"empty range {text!r}")
    return lo, hi


def make_parser():
    parser = argparse.ArgumentParser(
        prog="sqla-fuzz",
        description="Differential fuzzers for SQLAlchemy.  Tests whichever "
        "SQLAlchemy is importable, normally a checkout installed into the "
        "same environment.",
    )
    subparsers = parser.add_subparsers(
        dest="fuzzer", metavar="FUZZER", required=True
    )
    for name, fuzzer in FUZZERS.items():
        sub = subparsers.add_parser(
            name,
            help=fuzzer.DESCRIPTION,
            description=fuzzer.__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        group = sub.add_mutually_exclusive_group()
        group.add_argument(
            "--seeds",
            type=_seed_range,
            default=(0, 5000),
            metavar="LO:HI",
            help="range of seeds to run (default 0:5000)",
        )
        group.add_argument(
            "--replay",
            type=int,
            metavar="SEED",
            help="run one seed, printing the scenario and differences",
        )
        sub.add_argument(
            "--procs",
            type=int,
            default=None,
            help="worker processes (default: number of CPUs)",
        )
        sub.add_argument(
            "--show",
            type=int,
            default=5,
            help="seeds to show per check in the report (default 5)",
        )
        sub.add_argument(
            "--markdown",
            metavar="FILE",
            help="also append the report to FILE, e.g. $GITHUB_STEP_SUMMARY",
        )
        sub.add_argument(
            "--json",
            metavar="FILE",
            help="also write the results as JSON to FILE, for "
            "sqla-fuzz-issues",
        )
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    fuzzer = FUZZERS[args.fuzzer]

    if args.replay is not None:
        print(f"Testing {report.target_text(runner.describe_target())}.")
        fuzzer.replay(args.replay)
        return 0

    lo, hi = args.seeds
    stats, findings, invalid, crashes = runner.run_seeds(
        fuzzer, lo, hi, args.procs
    )
    summary = runner.summarize(
        fuzzer, lo, hi, stats, findings, invalid, crashes, args.show
    )
    text = report.render_markdown(summary)
    print(text)
    if args.markdown:
        with open(args.markdown, "a") as file_:
            file_.write(text + "\n")
    if args.json:
        with open(args.json, "w") as file_:
            json.dump(summary, file_, indent=2)

    # exit status 1 if anything was found, so that CI runs show as failed
    return 1 if findings or crashes else 0
