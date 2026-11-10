"""File fuzzer findings as GitHub issues: ``sqla-fuzz-issues``.

Run by the weekly workflow after the fuzzers, with the JSON results they
wrote.  To keep a misbehaving fuzzer from flooding the tracker:

* there is at most one open issue per fuzzer and check, found by its
  label, e.g. ``orm-loading:repeat``; a check that fails again gets a
  comment on that issue, not a new one
* at most ``--max-new`` issues are opened per run
* issues are never closed automatically; when a check passes again, its
  issue gets one comment saying so and the ``fuzz-passing`` label

Uses the ``gh`` command line tool, authenticated through ``GH_TOKEN``.

"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys

from . import report

FINDING_LABEL = "fuzz-finding"
PASSING_LABEL = "fuzz-passing"

INTRO = """\
Filed by the weekly fuzz run.  This issue tracks the **{check}** check of \
the **{fuzzer}** fuzzer, which fails when the statement {description}.

The findings below are not validated: they may be a SQLAlchemy bug, \
several bugs, or a bug in the fuzzer.  To validate one, replay a seed \
(`sqla-fuzz {fuzzer} --replay SEED`, see README.rst) and reduce it to a \
reproducer that doesn't use the fuzzer.  If it's a SQLAlchemy bug, open \
an issue in sqlalchemy/sqlalchemy with that reproducer and link it here.

Leave this issue open until the check passes.  Later runs comment here \
rather than opening new issues, and say when the check passes again.
"""


class GitHub:
    def __init__(self, repo, dry_run):
        self.repo = repo
        self.dry_run = dry_run

    def _gh(self, *args, body=None, write=False):
        cmd = ["gh", *args, "--repo", self.repo]
        if body is not None:
            cmd += ["--body-file", "-"]
        if write and self.dry_run:
            print(f"DRY RUN: {' '.join(cmd)}")
            if body is not None:
                print("  " + body.replace("\n", "\n  "))
            return ""
        result = subprocess.run(
            cmd, input=body, capture_output=True, text=True, check=True
        )
        return result.stdout

    def ensure_label(self, name, description, color):
        self._gh(
            "label",
            "create",
            name,
            "--force",
            "--color",
            color,
            "--description",
            description,
            write=True,
        )

    def open_issue_for(self, label):
        out = self._gh(
            "issue",
            "list",
            "--label",
            label,
            "--state",
            "open",
            "--json",
            "number,labels",
        )
        issues = json.loads(out or "[]")
        if not issues:
            return None, set()
        issue = min(issues, key=lambda i: i["number"])
        return issue["number"], {lbl["name"] for lbl in issue["labels"]}

    def create(self, title, body, labels):
        args = ["issue", "create", "--title", title]
        for label in labels:
            args += ["--label", label]
        return self._gh(*args, body=body, write=True).strip()

    def comment(self, number, body):
        self._gh("issue", "comment", str(number), body=body, write=True)

    def edit_labels(self, number, add=(), remove=()):
        args = ["issue", "edit", str(number)]
        for label in add:
            args += ["--add-label", label]
        for label in remove:
            args += ["--remove-label", label]
        self._gh(*args, write=True)


def render_findings(summaries, check, run_url):
    lines = [f"Run: {run_url}", ""]
    for summary in summaries:
        lo, hi = summary["seeds"]
        target = report.target_text(summary["target"])
        if check == "crash":
            lines += [
                f"#### {target}, seeds `{lo}:{hi}`: "
                f"{summary['stats']['crash']} seeds crashed",
                "",
            ]
            for entry in summary["crashes"]:
                lines += [
                    f"seed {entry['seed']}:",
                    "```",
                    entry["traceback"],
                    "```",
                    "",
                ]
            continue
        info = summary["failed"][check]
        lines += [
            f"#### {target}, seeds `{lo}:{hi}`: {info['count']} seeds",
            "",
        ]
        if summary["fuzz_sha"]:
            lines += [f"sqlalchemy-fuzz commit `{summary['fuzz_sha']}`.", ""]
        lines += report.render_check(summary, check)
    return "\n".join(lines)


def file_issues(summaries, gh, run_url, max_new):
    failing = collections.defaultdict(list)
    ran = set()
    descriptions = {}
    for summary in summaries:
        fuzzer = summary["fuzzer"]
        for check, description in summary["checks"].items():
            ran.add((fuzzer, check))
            descriptions[(fuzzer, check)] = description
        ran.add((fuzzer, "crash"))
        descriptions[(fuzzer, "crash")] = "crashes the fuzzer itself"
        for check in summary["failed"]:
            failing[(fuzzer, check)].append(summary)
        if summary["stats"]["crash"]:
            failing[(fuzzer, "crash")].append(summary)

    gh.ensure_label(
        FINDING_LABEL, "unvalidated finding from a fuzz run", "d93f0b"
    )
    gh.ensure_label(
        PASSING_LABEL, "the check has passed since this was filed", "0e8a16"
    )

    new = 0
    status = 0
    for key in sorted(ran):
        fuzzer, check = key
        label = f"{fuzzer}:{check}"
        number, labels = gh.open_issue_for(label)

        if key not in failing:
            if number is not None and PASSING_LABEL not in labels:
                print(f"{label}: passing, noting on #{number}")
                gh.comment(
                    number,
                    f"No failures for this check in {run_url}.  Close "
                    "this issue if the bugs it found are fixed.",
                )
                gh.edit_labels(number, add=[PASSING_LABEL])
            continue

        body = render_findings(failing[key], check, run_url)
        if number is not None:
            print(f"{label}: still failing, commenting on #{number}")
            gh.comment(number, "Still failing.\n\n" + body)
            if PASSING_LABEL in labels:
                gh.edit_labels(number, remove=[PASSING_LABEL])
        elif new >= max_new:
            print(
                f"{label}: failing, but --max-new {max_new} issues were "
                "already opened in this run; not opening another",
                file=sys.stderr,
            )
            status = 1
        else:
            gh.ensure_label(
                label, f"fuzz findings for {fuzzer} / {check}", "fbca04"
            )
            intro = INTRO.format(
                check=check, fuzzer=fuzzer, description=descriptions[key]
            )
            url = gh.create(
                f"{fuzzer}: {check} check failing",
                intro + "\n" + body,
                [FINDING_LABEL, label],
            )
            new += 1
            print(f"{label}: failing, opened {url}")
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sqla-fuzz-issues",
        description="File fuzzer findings from JSON results as GitHub "
        "issues, at most one open issue per fuzzer and check.",
    )
    parser.add_argument(
        "results", nargs="+", help="JSON files written by sqla-fuzz --json"
    )
    parser.add_argument(
        "--repo", default="sqlalchemy/sqlalchemy-fuzz", help="owner/name"
    )
    parser.add_argument(
        "--run-url", required=True, help="link to the run, for the issues"
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=4,
        help="most issues to open in one run (default 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be done; still reads existing issues",
    )
    args = parser.parse_args(argv)

    summaries = []
    for path in args.results:
        with open(path) as file_:
            summaries.append(json.load(file_))
    summaries.sort(key=lambda s: (s["fuzzer"], s["target"]["ref"] or ""))

    gh = GitHub(args.repo, args.dry_run)
    return file_issues(summaries, gh, args.run_url, args.max_new)


if __name__ == "__main__":
    sys.exit(main())
