from genai_traces.analytics import HOUR_NS, percentile, stats
from genai_traces.otlp import flatten, parse_request
from genai_traces.redact import NOT_CAPTURED
from genai_traces.store import TraceFilter
from genai_traces.tree import build_tree, covered_ns
from tests.conftest import load
from tests.helpers import T0, Recorder, agent_trace


def test_ingest_builds_trace_summary(ingestor, store):
    rec = Recorder()
    agent_trace(rec, conversation="c-77", fail_tool=True)
    result = load(ingestor, rec)
    assert (result.received, result.accepted, result.duplicates) == (4, 4, 0)

    (trace,) = store.list_traces(TraceFilter())
    assert trace["name"] == "invoke_agent support"
    assert trace["service"] == "support-agent"
    assert trace["span_count"] == 4 and trace["llm_calls"] == 2 and trace["tool_calls"] == 1
    assert trace["error_count"] == 1 and trace["status"] == "ok"  # the agent recovered
    assert (trace["input_tokens"], trace["output_tokens"]) == (2600, 200)
    # chat-large: $2.50 / $10 per 1M tokens
    assert trace["cost_usd"] == round((2600 * 2.5 + 200 * 10) / 1e6, 8)
    assert trace["models"] == ["chat-large", "chat-large-2026-08"]
    assert trace["conversation_id"] == "c-77" and trace["agent_name"] == "support"
    assert trace["duration_ms"] == 900 and trace["complete"] is True


def test_resent_batches_are_idempotent(ingestor, store):
    rec = Recorder()
    agent_trace(rec)
    load(ingestor, rec)
    again = load(ingestor, rec)
    assert again.accepted == 0 and again.duplicates == 4
    assert store.counts() == {"traces": 1, "spans": 4}


def test_children_before_parent_then_summary_completes(ingestor, store):
    rec = Recorder()
    agent_trace(rec)
    spans, _ = flatten(parse_request(rec.protobuf(), None))
    root = next(s for s in spans if s.parent_span_id is None)
    ingestor.ingest([s for s in spans if s is not root])
    (partial,) = store.list_traces(TraceFilter())
    assert partial["complete"] is False and partial["span_count"] == 3
    ingestor.ingest([root])
    (full,) = store.list_traces(TraceFilter())
    assert full["complete"] is True and full["name"] == "invoke_agent support"
    assert full["trace_id"] == partial["trace_id"]


def test_content_is_not_stored_by_default(ingestor, store):
    rec = Recorder()
    agent_trace(rec)
    load(ingestor, rec)
    trace = store.list_traces(TraceFilter())[0]
    _, rows = store.get_trace(trace["trace_id"])
    chat = next(r for r in rows if r["attributes"].get("gen_ai.input.messages"))
    assert chat["attributes"]["gen_ai.input.messages"] == NOT_CAPTURED
    assert "01712345678" not in str(rows)


def test_filters(ingestor, store):
    rec = Recorder()
    agent_trace(rec, conversation="a")
    agent_trace(rec, conversation="b", fail_tool=True)
    failing = rec.span(
        "chat small",
        5000,
        5200,
        attrs={"gen_ai.operation.name": "chat", "gen_ai.request.model": "chat-small"},
        error="rate limited",
    )
    rec.end(failing, 5200)
    load(ingestor, rec)

    assert len(store.list_traces(TraceFilter())) == 3
    assert len(store.list_traces(TraceFilter(model="chat-large"))) == 2
    assert len(store.list_traces(TraceFilter(model="chat"))) == 0  # exact model match
    assert len(store.list_traces(TraceFilter(status="error"))) == 1
    assert len(store.list_traces(TraceFilter(status="has_errors"))) == 2
    assert len(store.list_traces(TraceFilter(status="ok"))) == 2
    assert [t["conversation_id"] for t in store.list_traces(TraceFilter(conversation_id="b"))] == [
        "b"
    ]
    assert len(store.list_traces(TraceFilter(min_duration_ms=500))) == 2
    assert len(store.list_traces(TraceFilter(q="SMALL"))) == 1
    assert len(store.list_traces(TraceFilter(q="100%"))) == 0
    newest_first = store.list_traces(TraceFilter())
    assert newest_first[0]["name"] == "chat small"
    page2 = store.list_traces(TraceFilter(before_ns=newest_first[0]["start_ns"], limit=1))
    assert len(page2) == 1 and page2[0]["start_ns"] < newest_first[0]["start_ns"]
    assert store.services() == ["support-agent"]


def test_prune(ingestor, store):
    old, new = Recorder(), Recorder()
    agent_trace(old)
    s = new.span("recent", 10_000, 10_100)
    new.end(s, 10_100)
    load(ingestor, old)
    load(ingestor, new)
    assert store.prune(T0 + 5_000 * 1_000_000) == 1
    assert store.counts() == {"traces": 1, "spans": 1}


def test_tree_order_self_time_rollups_and_critical_path(ingestor, store):
    rec = Recorder()
    agent_trace(rec)
    load(ingestor, rec)
    trace = store.list_traces(TraceFilter())[0]
    _, rows = store.get_trace(trace["trace_id"])
    tree = build_tree(rows)
    assert [(n["depth"], n["name"]) for n in tree] == [
        (0, "invoke_agent support"),
        (1, "chat chat-large"),
        (1, "execute_tool lookup_order"),
        (1, "chat chat-large"),
    ]
    root = tree[0]
    # children cover 10-400, 410-520, 530-880 = 850 ms of the root's 900 ms
    assert root["self_ms"] == 50
    assert root["subtree_input_tokens"] == 2600 and root["subtree_output_tokens"] == 200
    assert root["child_count"] == 3
    assert [n["critical"] for n in tree] == [True, False, False, True]
    assert tree[3]["offset_ms"] == 530


def test_orphans_are_shown_as_roots():
    rows = [
        _row("a", None, 0, 100),
        _row("b", "missing", 20, 50),
    ]
    tree = build_tree(rows)
    assert [(n["span_id"], n["depth"], n["orphan"]) for n in tree] == [
        ("a", 0, False),
        ("b", 0, True),
    ]
    assert build_tree([]) == []


def test_covered_ns_merges_overlaps_and_clips():
    assert covered_ns([(0, 10), (5, 20), (30, 40)], 0, 100) == 30
    assert covered_ns([(-5, 10), (90, 150)], 0, 100) == 20
    assert covered_ns([], 0, 100) == 0


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([3, 1, 2], 50) == 2
    assert percentile(list(range(1, 101)), 95) == 95


def test_stats(ingestor, store):
    rec = Recorder()
    agent_trace(rec)
    agent_trace(rec, fail_tool=True)
    load(ingestor, rec)
    since = T0 - HOUR_NS
    result = stats(
        store.spans_since(since),
        store.list_traces(TraceFilter(since_ns=since, limit=1000)),
        since,
        T0 + HOUR_NS,
    )
    totals = result["totals"]
    assert totals["traces"] == 2 and totals["traces_with_errors"] == 1
    assert totals["llm_calls"] == 4 and totals["tool_calls"] == 2
    assert totals["input_tokens"] == 5200
    (model,) = [m for m in result["models"] if m["model"] == "chat-large"]
    assert model["calls"] == 2 and model["provider"] == "openai"
    (tool,) = result["tools"]
    assert tool == {
        "tool": "lookup_order",
        "calls": 2,
        "errors": 1,
        "error_rate": 0.5,
        "error_types": {"TimeoutError": 1},
        "p50_ms": 110.0,
        "p95_ms": 110.0,
    }
    assert sum(b["traces"] for b in result["timeline"]) == 2
    assert len(result["timeline"]) == 3


def _row(span_id, parent, start, end):
    return {
        "trace_id": "t",
        "span_id": span_id,
        "parent_span_id": parent,
        "name": span_id,
        "service": "s",
        "span_kind": "internal",
        "kind": "other",
        "start_ns": start,
        "end_ns": end,
        "duration_ms": (end - start) / 1e6,
        "status": "unset",
        "status_message": "",
        "operation": None,
        "provider": None,
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "tool_name": None,
        "agent_name": None,
        "error_type": None,
        "attributes": {},
        "events": [],
    }
