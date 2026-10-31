"""Response bodies shared by the HTTP API and the static export, so both produce the same JSON."""

from __future__ import annotations

from typing import Any

from .analytics import HOUR_NS, stats
from .store import Store, TraceFilter
from .tree import build_tree


def trace_detail(store: Store, trace_id: str) -> dict[str, Any] | None:
    found = store.get_trace(trace_id.lower())
    if found is None:
        return None
    summary, rows = found
    return {"trace": summary, "spans": build_tree(rows)}


def conversation(store: Store, conversation_id: str) -> dict[str, Any] | None:
    items = store.list_traces(TraceFilter(conversation_id=conversation_id, limit=500))
    if not items:
        return None
    items.reverse()
    return {
        "conversation_id": conversation_id,
        "turns": len(items),
        "input_tokens": sum(t["input_tokens"] for t in items),
        "output_tokens": sum(t["output_tokens"] for t in items),
        "cost_usd": round(sum(t["cost_usd"] for t in items), 6),
        "duration_ms": round(sum(t["duration_ms"] for t in items), 3),
        "traces": items,
    }


def window_stats(store: Store, hours: int, until_ns: int) -> dict[str, Any]:
    since = until_ns - hours * HOUR_NS
    trace_rows = [
        t
        for t in store.list_traces(TraceFilter(since_ns=since, limit=100_000))
        if t["start_ns"] <= until_ns
    ]
    span_rows = [s for s in store.spans_since(since) if s["start_ns"] <= until_ns]
    return stats(span_rows, trace_rows, since, until_ns)
