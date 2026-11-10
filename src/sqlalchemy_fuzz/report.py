"""Markdown rendering of run summaries; doesn't import SQLAlchemy."""

from __future__ import annotations


def target_text(target):
    text = f"SQLAlchemy {target['version']}"
    if target["ref"]:
        text += f" ({target['ref']})"
    if target["sha"]:
        text += f" at commit `{target['sha']}`"
    else:
        text += f" from `{target['path']}`"
    return text


def replay_command(summary, seed):
    return f"sqla-fuzz {summary['fuzzer']} --replay {seed}"


def run_command(summary):
    """Command that runs the same seeds again."""
    lo, hi = summary["seeds"]
    cmd = f"sqla-fuzz {summary['fuzzer']} --seeds {lo}:{hi}"
    sample = summary.get("sample")
    if sample:
        cmd += f" --sample {sample['count']} --sample-seed {sample['seed']}"
    return cmd


def seeds_text(summary):
    """e.g. 'seeds `0:20000` and 20000 sampled seeds'"""
    lo, hi = summary["seeds"]
    parts = [f"seeds `{lo}:{hi}`"] if hi > lo else []
    sample = summary.get("sample")
    if sample:
        parts.append(f"{sample['count']} sampled seeds")
    return " and ".join(parts) or "no seeds"


def count_text(summary, info):
    """e.g. '12 seeds, 3 of them in `0:20000`'"""
    text = f"{info['count']} seeds"
    lo, hi = summary["seeds"]
    if summary.get("sample") and hi > lo:
        text += f", {info['fixed']} of them in `{lo}:{hi}`"
    return text


def render_check(summary, check):
    """Markdown lines for one failed check."""
    info = summary["failed"][check]
    subject = summary.get("subject", "statement")
    lines = [f"The {subject} {summary['checks'][check]}.", ""]
    if info["by_mapping"]:
        lines += [
            "By mapping: "
            + ", ".join(f"{m}: {n}" for m, n in info["by_mapping"].items()),
            "",
        ]
    lines += [
        "Features in every seed: "
        + (", ".join(f"`{f}`" for f in info["common"]) or "(none)"),
        "",
        "Most frequent features: "
        + ", ".join(f"`{f}` ({n})" for f, n in info["frequent"]),
        "",
        "Smallest seeds:",
        "",
    ]
    for entry in info["seeds"]:
        also = (
            f"; also fails {', '.join(entry['also'])}" if entry["also"] else ""
        )
        lines.append(
            f"- `{replay_command(summary, entry['seed'])}` "
            f"({', '.join(entry['features'])}{also})"
        )
    lines.append("")
    return lines


def render_markdown(summary):
    stats = summary["stats"]
    lines = [
        f"## {summary['fuzzer']}: {summary['description']}",
        "",
        f"Testing {target_text(summary['target'])}.",
        "",
    ]
    if summary["fuzz_sha"]:
        lines += [
            f"sqlalchemy-fuzz commit `{summary['fuzz_sha']}`; seeds replay "
            "the same scenarios against this version of the fuzzer.",
            "",
        ]
    lines += [
        f"{seeds_text(summary).capitalize()}: {stats['diff']} with "
        f"differences, {stats['ok']} OK, {stats['invalid']} skipped as "
        f"invalid, {stats['crash']} crashed.  `{run_command(summary)}` "
        "runs the same seeds again.",
        "",
        "Each seed is one generated scenario, run through every check.  "
        "Seeds are listed under each check that they fail, so the "
        "sections can overlap.  "
        f"`{replay_command(summary, 'SEED')}` prints the scenario and the "
        "exact differences.",
        "",
    ]
    if not summary["failed"] and not stats["crash"]:
        lines += ["No differences found.", ""]

    for check, info in summary["failed"].items():
        lines += [f"### {check}: {count_text(summary, info)}", ""]
        lines += render_check(summary, check)

    if stats["crash"]:
        lines += [f"### crashes: {stats['crash']} seeds", ""]
        for entry in summary["crashes"]:
            lines += [
                f"seed {entry['seed']}:",
                "```",
                entry["traceback"],
                "```",
                "",
            ]

    if summary["invalid"]:
        lines += [
            "<details><summary>Most common errors from scenarios skipped "
            "as invalid</summary>",
            "",
        ]
        for msg, n in summary["invalid"]:
            lines.append(f"- {n}: `{msg}`")
        lines += ["", "</details>", ""]
    return "\n".join(lines)
