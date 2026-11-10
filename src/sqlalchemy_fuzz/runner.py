"""Runs a fuzzer over a range of seeds and summarizes the results."""

from __future__ import annotations

import collections
import multiprocessing
import os

import sqlalchemy


def run_seeds(fuzzer, lo, hi, procs=None):
    """Run ``fuzzer.trial`` for each seed in ``lo:hi``.

    Returns ``(stats, findings, invalid, crashes)``.

    """
    stats = collections.Counter()
    findings = []
    crashes = []
    invalid = collections.Counter()
    with multiprocessing.Pool(procs, initializer=fuzzer.init_worker) as pool:
        for seed, status, info in pool.imap_unordered(
            fuzzer.trial, range(lo, hi), chunksize=20
        ):
            stats[status] += 1
            if status == "diff":
                findings.append((seed, info))
            elif status == "crash":
                crashes.append((seed, info))
            elif status == "invalid":
                invalid[f"{info[0]}: {info[1][:100]}"] += 1
    return stats, sorted(findings), invalid, sorted(crashes)


def _parse_finding(finding):
    """'repeat[default]' -> ('repeat', 'default')"""
    check, _, mapping = finding.partition("[")
    return check, mapping.rstrip("]") or None


def describe_target():
    """The SQLAlchemy being tested."""
    return {
        "version": sqlalchemy.__version__,
        "path": os.path.dirname(sqlalchemy.__file__),
        # set by the workflow
        "ref": os.environ.get("SQLALCHEMY_REF"),
        "sha": os.environ.get("SQLALCHEMY_SHA"),
    }


def summarize(fuzzer, lo, hi, stats, findings, invalid, crashes, show):
    """Return a JSON-serializable summary of a run."""

    # a seed that fails more than one check is listed under each of them
    by_check = collections.defaultdict(list)
    for seed, kinds in findings:
        checks = collections.defaultdict(list)
        for kind in kinds:
            check, mapping = _parse_finding(kind)
            checks[check].append(mapping)
        feats = fuzzer.seed_features(seed)
        for check, mappings in checks.items():
            others = sorted(set(checks) - {check})
            by_check[check].append((seed, feats, mappings, others))

    mappings_order = getattr(fuzzer, "MAPPINGS", ())
    failed = {}
    for check in fuzzer.CHECKS:
        items = by_check.get(check)
        if not items:
            continue
        mapping_counts = collections.Counter(
            m for _, _, mappings, _ in items for m in mappings if m
        )
        counts = collections.Counter(x for _, f, _, _ in items for x in f)
        failed[check] = {
            "count": len(items),
            "by_mapping": {
                m: mapping_counts[m]
                for m in mappings_order
                if m in mapping_counts
            },
            "common": sorted(set.intersection(*(f for _, f, _, _ in items))),
            "frequent": counts.most_common(8),
            "seeds": [
                {"seed": seed, "features": sorted(feats), "also": others}
                for seed, feats, _, others in items[:show]
            ],
        }

    return {
        "fuzzer": fuzzer.NAME,
        "description": fuzzer.DESCRIPTION,
        "checks": dict(fuzzer.CHECKS),
        "target": describe_target(),
        "fuzz_sha": os.environ.get("SQLALCHEMY_FUZZ_SHA"),
        "seeds": [lo, hi],
        "stats": {
            key: stats[key] for key in ("diff", "ok", "invalid", "crash")
        },
        "failed": failed,
        "crashes": [
            {"seed": seed, "traceback": tb.strip()}
            for seed, tb in crashes[:show]
        ],
        "invalid": invalid.most_common(10),
    }
