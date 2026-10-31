"""Aggregates over a time window: per-model and per-tool latency, errors, tokens and cost."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

HOUR_NS = 3_600 * 1_000_000_000


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _latency(durations: list[float]) -> dict[str, float | None]:
    p50, p95 = percentile(durations, 50), percentile(durations, 95)
    return {
        "p50_ms": round(p50, 3) if p50 is not None else None,
        "p95_ms": round(p95, 3) if p95 is not None else None,
    }


def stats(
    span_rows: list[dict[str, Any]], trace_rows: list[dict[str, Any]], since_ns: int, now_ns: int
) -> dict[str, Any]:
    models: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    tools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in span_rows:
        if row["kind"] in ("llm", "embeddings"):
            models[(row["model"] or "(unknown)", row["provider"] or "")].append(row)
        elif row["kind"] == "tool":
            tools[row["tool_name"] or "(unnamed)"].append(row)

    model_stats = []
    for (model, provider), rows in models.items():
        priced = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        model_stats.append(
            {
                "model": model,
                "provider": provider or None,
                "calls": len(rows),
                "errors": sum(1 for r in rows if r["status"] == "error"),
                "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
                "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
                "cost_usd": round(sum(priced), 6) if priced else None,
                **_latency([r["duration_ms"] for r in rows]),
            }
        )
    model_stats.sort(key=lambda m: (-(m["cost_usd"] or 0), -m["calls"], m["model"]))

    tool_stats = []
    for name, rows in tools.items():
        errors = [r for r in rows if r["status"] == "error"]
        error_types: dict[str, int] = defaultdict(int)
        for r in errors:
            error_types[r["error_type"] or "error"] += 1
        tool_stats.append(
            {
                "tool": name,
                "calls": len(rows),
                "errors": len(errors),
                "error_rate": round(len(errors) / len(rows), 4),
                "error_types": dict(sorted(error_types.items())),
                **_latency([r["duration_ms"] for r in rows]),
            }
        )
    tool_stats.sort(key=lambda t: (-t["errors"], -t["calls"], t["tool"]))

    buckets: dict[int, dict[str, float]] = {}
    first = since_ns - since_ns % HOUR_NS
    for start in range(first, now_ns + 1, HOUR_NS):
        buckets[start] = {"traces": 0, "llm_calls": 0, "tokens": 0, "cost_usd": 0.0, "errors": 0}
    for row in trace_rows:
        bucket = buckets.get(row["start_ns"] - row["start_ns"] % HOUR_NS)
        if bucket is None:
            continue
        bucket["traces"] += 1
        bucket["llm_calls"] += row["llm_calls"]
        bucket["tokens"] += row["input_tokens"] + row["output_tokens"]
        bucket["cost_usd"] += row["cost_usd"]
        bucket["errors"] += 1 if row["error_count"] else 0
    timeline = [
        {"start_ns": start, **values, "cost_usd": round(values["cost_usd"], 6)}
        for start, values in sorted(buckets.items())
    ]

    llm_rows = [r for r in span_rows if r["kind"] == "llm"]
    return {
        "since_ns": since_ns,
        "until_ns": now_ns,
        "totals": {
            "traces": len(trace_rows),
            "traces_with_errors": sum(1 for t in trace_rows if t["error_count"]),
            "failed_traces": sum(1 for t in trace_rows if t["status"] == "error"),
            "llm_calls": len(llm_rows),
            "tool_calls": sum(len(rows) for rows in tools.values()),
            "input_tokens": sum(t["input_tokens"] for t in trace_rows),
            "output_tokens": sum(t["output_tokens"] for t in trace_rows),
            "cost_usd": round(sum(t["cost_usd"] for t in trace_rows), 6),
            **{f"llm_{k}": v for k, v in _latency([r["duration_ms"] for r in llm_rows]).items()},
            **{
                f"trace_{k}": v for k, v in _latency([t["duration_ms"] for t in trace_rows]).items()
            },
        },
        "models": model_stats,
        "tools": tool_stats,
        "timeline": timeline,
    }
