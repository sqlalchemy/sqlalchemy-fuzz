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


def render_check(summary, check):
    """Markdown lines for one failed check."""
    info = summary["failed"][check]
    lines = [f"The statement {summary['checks'][check]}.", ""]
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
    lo, hi = summary["seeds"]
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
        f"Seeds `{lo}:{hi}`: {stats['diff']} with differences, "
        f"{stats['ok']} OK, {stats['invalid']} skipped as invalid, "
        f"{stats['crash']} crashed.",
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
        lines += [f"### {check}: {info['count']} seeds", ""]
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
