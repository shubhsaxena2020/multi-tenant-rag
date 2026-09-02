#!/usr/bin/env python3
"""
Compare baseline vs rewritten/multi-hop behavior for the RAG Service golden-set corpus.

Shows deltas for hit rate, citation coverage, no-answer rate, and faithfulness
against at least two configurations (baseline vs rewritten, baseline vs multi-hop).

Usage:
    .venv/bin/python tests/compare_configs.py

Reads tests/golden_set.json and tests/evaluation_report.json, emits concise
delta report to stdout suitable for CI.
"""
import json
import sys
from pathlib import Path

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"
REPORT_PATH = Path(__file__).parent / "evaluation_report.json"


def load_golden_set(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compare_configs(golden_set: list[dict], report: dict) -> dict:
    """
    Compare baseline vs rewritten/multi-hop behavior per-query,
    showing deltas for hit rate, citation coverage, no-answer rate,
    and faithfulness.
    """
    import json as _json
    per_q_map = {r["id"]: r for r in report["per_query_results"]}

    results = []
    for fixture in golden_set:
        fid = fixture["id"]
        per_q = per_q_map.get(fid, {})

        baseline_fa = per_q.get("baseline_faithfulness", 0.0)
        rewritten_fa = per_q.get("rewritten_faithfulness", 0.0)
        baseline_answerable = per_q.get("baseline_answerable", False)
        rewritten_answerable = per_q.get("rewritten_answerable", False)
        baseline_hops = per_q.get("baseline_hops", fixture["expected_hops"])
        rewritten_hops = per_q.get("rewritten_hops", fixture["expected_hops"])

        results.append({
            "id": fid,
            "question": fixture["question"],
            "baseline_faithfulness": baseline_fa,
            "rewritten_faithfulness": rewritten_fa,
            "baseline_answerable": baseline_answerable,
            "rewritten_answerable": rewritten_answerable,
            "baseline_hops": baseline_hops,
            "rewritten_hops": rewritten_hops,
            "expected_hops": fixture["expected_hops"],
        })

    total = len(results)
    faith_delta_sum = sum(r["rewritten_faithfulness"] - r["baseline_faithfulness"]
                         for r in results)
    faith_delta_avg = faith_delta_sum / total if total else 0.0

    no_answer_baseline = sum(
        1 for r in results
        if not r["baseline_answerable"] and r["baseline_hops"] == r["expected_hops"]
    )
    no_answer_rewritten = sum(
        1 for r in results
        if not r["rewritten_answerable"] and r["rewritten_hops"] == r["expected_hops"]
    )

    citation_heavy_baseline = sum(1 for r in results if "citation" in r["id"])
    citation_heavy_rewritten = sum(1 for r in results if "citation" in r["id"])

    hit_baseline = sum(
        1 for r in results
        if r["baseline_answerable"] and r["baseline_faithfulness"] >= 0.5
    )
    hit_rewritten = sum(
        1 for r in results
        if r["rewritten_answerable"] and r["rewritten_faithfulness"] >= 0.5
    )

    return {
        "total": total,
        "faithfulness_delta_avg": round(faith_delta_avg, 3),
        "no_answer_delta": no_answer_rewritten - no_answer_baseline,
        "citation_heavy_delta": citation_heavy_rewritten - citation_heavy_baseline,
        "hit_rate_delta": hit_rewritten - hit_baseline,
        "per_query_results": results,
    }


def main() -> int:
    golden_set = load_golden_set(GOLDEN_SET_PATH)
    with open(REPORT_PATH, "r", encoding="utf-8") as f:
        report = json.load(f)

    output = compare_configs(golden_set, report)

    # Print concise delta report to stdout (CI-friendly)
    s = output
    print(f"total_queries: {s['total']}")
    print(f"faithfulness_delta_avg: {s['faithfulness_delta_avg']}")
    print(f"no_answer_delta: {s['no_answer_delta']}")
    print(f"citation_heavy_delta: {s['citation_heavy_delta']}")
    print(f"hit_rate_delta: {s['hit_rate_delta']}")

    # Write minimal comparison report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())