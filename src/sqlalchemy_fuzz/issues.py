"""File fuzzer findings as GitHub issues: ``sqla-fuzz-issues``.

Run by the weekly workflow after the fuzzers, with the JSON results they
wrote.  To keep a misbehaving fuzzer from flooding the tracker:

* there is at most one open issue per fuzzer and check, found by its
  label, e.g. ``orm-loading:repeat``; a check that fails again gets a
  comment on that issue, not a new one
* at most ``--max-new`` issues are opened per run

An issue tracks the SQLAlchemy issues it links to, as
``sqlalchemy/sqlalchemy#NNNN`` in its body or comments, or by being
mentioned from them.  Once its check passes on every SQLAlchemy branch in
the run and every linked SQLAlchemy issue is closed, the issue is closed.
While a linked issue is still open, it gets the ``fuzz-passing`` label and
waits.  A check that fails again later gets a new issue, which links to
the closed one.

Uses the ``gh`` command line tool, authenticated through ``GH_TOKEN``.

"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys

from . import report

FINDING_LABEL = "fuzz-finding"
PASSING_LABEL = "fuzz-passing"

SQLALCHEMY_REPO = "sqlalchemy/sqlalchemy"

# sqlalchemy/sqlalchemy#123, .../sqlalchemy/sqlalchemy/issues/123 or
# /pull/123; not sqlalchemy/sqlalchemy-fuzz#123
SQLALCHEMY_LINK = re.compile(
    r"sqlalchemy/sqlalchemy(?:#|/issues/|/pull/)(\d+)"
)

INTRO = """\
Filed by the weekly fuzz run.  This issue tracks the **{check}** check of \
the **{fuzzer}** fuzzer, which fails when the {subject} {description}.

The findings below are not validated: they may be a SQLAlchemy bug, \
several bugs, or a bug in the fuzzer.  To validate one, replay a seed \
(`sqla-fuzz {fuzzer} --replay SEED`, see README.rst) and reduce it to a \
reproducer that doesn't use the fuzzer.  If it's a SQLAlchemy bug, open \
an issue in sqlalchemy/sqlalchemy with that reproducer and link it here.

Link the SQLAlchemy issue in a comment here as `sqlalchemy/sqlalchemy#NNNN`, \
or mention this issue from it.  Later runs comment here rather than \
opening new issues, and close this issue once the check passes and every \
linked SQLAlchemy issue is closed.
"""


class GitHub:
    def __init__(self, repo, dry_run):
        self.repo = repo
        self.dry_run = dry_run

    def _gh(self, *args, body=None, write=False, repo=True):
        cmd = ["gh", *args]
        if repo:
            cmd += ["--repo", self.repo]
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

    def last_closed_for(self, label):
        out = self._gh(
            "issue",
            "list",
            "--label",
            label,
            "--state",
            "closed",
            "--json",
            "number",
        )
        issues = json.loads(out or "[]")
        return max((i["number"] for i in issues), default=None)

    def linked_issues(self, number):
        """{number: state} of the SQLAlchemy issues an issue links to,
        in its body or comments, or that mention it."""
        out = self._gh("issue", "view", str(number), "--json", "body,comments")
        issue = json.loads(out)
        text = "\n".join(
            [issue["body"]] + [c["body"] for c in issue["comments"]]
        )
        linked = {int(n) for n in SQLALCHEMY_LINK.findall(text)}
        out = self._gh(
            "api",
            "--paginate",
            f"repos/{self.repo}/issues/{number}/timeline",
            "--jq",
            '.[] | select(.event == "cross-referenced") '
            "| .source.issue | select(.repository.full_name == "
            f'"{SQLALCHEMY_REPO}") | .number',
            repo=False,
        )
        linked.update(int(n) for n in out.split())
        return {
            n: self._gh(
                "api",
                f"repos/{SQLALCHEMY_REPO}/issues/{n}",
                "--jq",
                ".state",
                repo=False,
            ).strip()
            for n in sorted(linked)
        }

    def close(self, number):
        self._gh(
            "issue", "close", str(number), "--reason", "completed", write=True
        )

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
        target = report.target_text(summary["target"])
        seeds = report.seeds_text(summary)
        if check == "crash":
            lines += [
                f"#### {target}, {seeds}: "
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
            f"#### {target}, {seeds}: {report.count_text(summary, info)}",
            "",
        ]
        if summary["fuzz_sha"]:
            lines += [f"sqlalchemy-fuzz commit `{summary['fuzz_sha']}`.", ""]
        lines += [
            f"`{report.run_command(summary)}` runs the same seeds again.",
            "",
        ]
        lines += report.render_check(summary, check)
    return "\n".join(lines)


def _issue_list(numbers):
    return ", ".join(f"{SQLALCHEMY_REPO}#{n}" for n in numbers)


def file_issues(summaries, gh, run_url, max_new, refs_tested):
    failing = collections.defaultdict(list)
    ran = set()
    refs = collections.defaultdict(set)
    descriptions = {}
    subjects = {}
    for summary in summaries:
        fuzzer = summary["fuzzer"]
        refs[fuzzer].add(summary["target"]["ref"])
        subjects[fuzzer] = summary.get("subject", "statement")
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

    # a check only counts as passing if its fuzzer reported on every
    # branch tested, not if the job for one branch failed
    refs_tested = set(refs_tested)

    new = 0
    status = 0
    for key in sorted(ran):
        fuzzer, check = key
        label = f"{fuzzer}:{check}"
        number, labels = gh.open_issue_for(label)

        if key not in failing:
            if number is None:
                continue
            if not refs_tested <= refs[fuzzer]:
                print(
                    f"{label}: passing, but not run on every branch; "
                    f"leaving #{number}"
                )
                continue
            branches = " and ".join(f"`{ref}`" for ref in sorted(refs_tested))
            linked = gh.linked_issues(number)
            still_open = [n for n, state in linked.items() if state == "open"]
            if not still_open:
                print(f"{label}: passing, closing #{number}")
                text = f"No failures for this check on {branches} in {run_url}"
                if linked:
                    text += (
                        ", and "
                        + _issue_list(linked)
                        + (" is closed" if len(linked) == 1 else " are closed")
                    )
                gh.comment(
                    number,
                    text + ".  Closing; if the check fails again, a new "
                    "issue will link back here.",
                )
                gh.close(number)
            elif PASSING_LABEL not in labels:
                print(f"{label}: passing, waiting on {still_open}")
                gh.comment(
                    number,
                    f"No failures for this check on {branches} in "
                    f"{run_url}.  Leaving this open until "
                    + _issue_list(still_open)
                    + " is closed.",
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
                check=check,
                fuzzer=fuzzer,
                subject=subjects[fuzzer],
                description=descriptions[key],
            )
            previous = gh.last_closed_for(label)
            if previous is not None:
                intro += (
                    f"\nThis check failed before in #{previous}, which was "
                    "closed once it passed; this may be a regression.\n"
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
        "--refs",
        type=json.loads,
        default=["main", "rel_2_0"],
        help="SQLAlchemy refs the run tested, as a JSON list; an issue is "
        'only closed if its check passed on all of them (default \'["main", '
        '"rel_2_0"]\')',
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
    return file_issues(summaries, gh, args.run_url, args.max_new, args.refs)


if __name__ == "__main__":
    sys.exit(main())
