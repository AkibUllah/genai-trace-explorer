"""Span tree for one trace: nesting, self time, token/cost rollups and the critical path."""

from __future__ import annotations

from typing import Any

from .store import USAGE_KINDS

PUBLIC_FIELDS = (
    "span_id",
    "parent_span_id",
    "name",
    "service",
    "span_kind",
    "kind",
    "status",
    "status_message",
    "operation",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "tool_name",
    "agent_name",
    "error_type",
    "attributes",
    "events",
)


def covered_ns(intervals: list[tuple[int, int]], lo: int, hi: int) -> int:
    """Length of the union of ``intervals`` clipped to [lo, hi]."""
    total = 0
    cur_start = cur_end = None
    for start, end in sorted((max(s, lo), min(e, hi)) for s, e in intervals):
        if end <= start:
            continue
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def build_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spans in depth-first order, each with ``depth``, timings relative to the trace start,
    ``self_ms`` (time not covered by children), subtree rollups and ``critical`` marks.

    A span whose parent was never received is shown as a root with ``orphan: true``.
    """
    if not rows:
        return []
    by_id = {r["span_id"]: r for r in rows}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for r in rows:
        parent = r["parent_span_id"] if r["parent_span_id"] in by_id else None
        children.setdefault(parent, []).append(r)
    for kids in children.values():
        kids.sort(key=lambda r: (r["start_ns"], r["span_id"]))

    t0 = min(r["start_ns"] for r in rows)
    rollup: dict[str, tuple[int, int, float]] = {}

    def roll(span: dict[str, Any]) -> tuple[int, int, float]:
        own = span["kind"] in USAGE_KINDS
        tin = (span["input_tokens"] or 0) if own else 0
        tout = (span["output_tokens"] or 0) if own else 0
        cost = (span["cost_usd"] or 0.0) if own else 0.0
        for kid in children.get(span["span_id"], []):
            k_in, k_out, k_cost = roll(kid)
            tin, tout, cost = tin + k_in, tout + k_out, cost + k_cost
        rollup[span["span_id"]] = (tin, tout, cost)
        return rollup[span["span_id"]]

    roots = children.get(None, [])
    for root in roots:
        roll(root)

    critical: set[str] = set()
    if roots:
        node = max(roots, key=lambda r: (r["end_ns"], -r["start_ns"]))
        while node is not None:
            critical.add(node["span_id"])
            kids = children.get(node["span_id"], [])
            # The child that finished last is the one the parent was waiting on.
            node = max(kids, key=lambda r: (r["end_ns"], -r["start_ns"])) if kids else None

    out: list[dict[str, Any]] = []

    def walk(span: dict[str, Any], depth: int) -> None:
        kids = children.get(span["span_id"], [])
        covered = covered_ns(
            [(k["start_ns"], k["end_ns"]) for k in kids], span["start_ns"], span["end_ns"]
        )
        tin, tout, cost = rollup[span["span_id"]]
        item = {key: span[key] for key in PUBLIC_FIELDS}
        item.update(
            depth=depth,
            offset_ms=round((span["start_ns"] - t0) / 1e6, 3),
            duration_ms=round(span["duration_ms"], 3),
            self_ms=round((span["end_ns"] - span["start_ns"] - covered) / 1e6, 3),
            subtree_input_tokens=tin,
            subtree_output_tokens=tout,
            subtree_cost_usd=round(cost, 8),
            critical=span["span_id"] in critical,
            orphan=span["parent_span_id"] is not None and span["parent_span_id"] not in by_id,
            child_count=len(kids),
        )
        out.append(item)
        for kid in kids:
            walk(kid, depth + 1)

    for root in roots:
        walk(root, 0)
    return out
