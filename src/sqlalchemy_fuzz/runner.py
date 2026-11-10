"""Runs a fuzzer over a set of seeds and summarizes the results."""

from __future__ import annotations

import collections
import multiprocessing
import os
import random

import sqlalchemy

# seeds sampled with --sample are drawn from range(SEED_SPACE), unless the
# fuzzer enumerates fewer cases (its CASES)
SEED_SPACE = 10**9


def sample_seeds(fuzzer, count, sample_seed, fixed):
    """Return ``count`` distinct seeds outside the ``fixed`` range.

    The seeds depend only on ``sample_seed``, so a run can be repeated.

    """
    lo, hi = fixed
    space = getattr(fuzzer, "CASES", SEED_SPACE)
    count = min(count, space - max(0, min(hi, space) - max(lo, 0)))
    rng = random.Random(sample_seed)
    seeds = set()
    while len(seeds) < count:
        seeds.update(
            seed
            for seed in rng.sample(range(space), count - len(seeds))
            if not lo <= seed < hi
        )
    return sorted(seeds)


def run_seeds(fuzzer, seeds, procs=None):
    """Run ``fuzzer.trial`` for each seed in ``seeds``.

    Returns ``(stats, findings, invalid, crashes)``.

    """
    stats = collections.Counter()
    findings = []
    crashes = []
    invalid = collections.Counter()
    with multiprocessing.Pool(procs, initializer=fuzzer.init_worker) as pool:
        for seed, status, info in pool.imap_unordered(
            fuzzer.trial, seeds, chunksize=20
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


def summarize(fuzzer, fixed, sample, stats, findings, invalid, crashes, show):
    """Return a JSON-serializable summary of a run.

    ``fixed`` is the ``(lo, hi)`` range of fixed seeds, ``sample`` is
    ``{"count": n, "seed": sample_seed}`` or None.

    """
    lo, hi = fixed

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
            # failures in the fixed range point at a recent change, rather
            # than at an older bug that was sampled for the first time
            "fixed": sum(1 for seed, _, _, _ in items if lo <= seed < hi),
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
        "subject": getattr(fuzzer, "SUBJECT", "statement"),
        "checks": dict(fuzzer.CHECKS),
        "target": describe_target(),
        "fuzz_sha": os.environ.get("SQLALCHEMY_FUZZ_SHA"),
        "seeds": [lo, hi],
        "sample": sample,
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
