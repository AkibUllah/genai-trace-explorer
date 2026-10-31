import json

from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from genai_traces.cli import export_static, main
from genai_traces.config import Settings
from genai_traces.demo import generate, processor_for
from genai_traces.otlp import flatten
from genai_traces.store import Store, TraceFilter
from tests.helpers import T0


def demo_spans(seed=7, conversations=6, faq=5, hours=24):
    memory = InMemorySpanExporter()
    count = generate(processor_for(memory), T0, hours, conversations, faq, seed)
    return count, memory.get_finished_spans()


def test_demo_is_deterministic_per_seed():
    _, first = demo_spans(seed=3)
    _, again = demo_spans(seed=3)
    _, other = demo_spans(seed=4)
    ids = lambda spans: [(s.context.trace_id, s.context.span_id, s.start_time) for s in spans]  # noqa: E731
    assert ids(first) == ids(again)
    assert ids(first) != ids(other)


def test_demo_traffic_is_valid_genai_telemetry(ingestor, store):
    count, spans = demo_spans(conversations=10, faq=8)
    raw, rejected = flatten(encode_spans(spans))
    assert rejected == 0
    ingestor.ingest(raw)
    traces = store.list_traces(TraceFilter(limit=1000))
    assert len(traces) == count
    services = {t["service"] for t in traces}
    assert services == {"wallet-support-agent", "faq-rag", "statement-summarizer"}
    assert all(t["complete"] for t in traces)
    assert all(t["end_ns"] <= T0 for t in traces)
    support = [t for t in traces if t["service"] == "wallet-support-agent"]
    assert all(t["conversation_id"].startswith("conv-") for t in support)
    assert any(t["cost_usd"] > 0 for t in support)
    batch = [t for t in traces if t["service"] == "statement-summarizer"]
    assert batch and all(t["cost_usd"] == 0 and t["input_tokens"] > 0 for t in batch)
    rag = next(t for t in traces if t["service"] == "faq-rag")
    detail = store.get_trace(rag["trace_id"])[1]
    assert {s["kind"] for s in detail} == {"other", "embeddings", "retrieval", "llm"}


def test_demo_tool_arguments_are_not_stored(ingestor, store):
    _, spans = demo_spans()
    ingestor.ingest(flatten(encode_spans(spans))[0])
    trace = next(t for t in store.list_traces(TraceFilter(limit=1000)) if t["tool_calls"])
    rows = store.get_trace(trace["trace_id"])[1]
    assert "01712345678" not in json.dumps(rows)


def test_cli_demo_direct_export_static_and_prune(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    assert main(["demo", "--direct", "--conversations", "4", "--faq", "3", "--hours", "24"]) == 0
    assert "spans written to sqlite" in capsys.readouterr().out

    out = tmp_path / "site"
    counts = export_static(Settings(_env_file=None, database_url=f"sqlite:///{db}"), out)
    page = (out / "index.html").read_text()
    assert '<meta name="gtx-mode" content="static">' in page
    listing = json.loads((out / "data" / "traces.json").read_text())
    assert len(listing["items"]) == counts["traces"]
    first = listing["items"][0]["trace_id"]
    detail = json.loads((out / "data" / "traces" / f"{first}.json").read_text())
    assert detail["trace"]["trace_id"] == first and detail["spans"]
    stats = json.loads((out / "data" / "stats.json").read_text())
    assert stats["totals"]["traces"] == counts["traces"]
    assert counts["conversations"] == 4
    assert (out / "data" / "conversations" / "conv-0001.json").exists()
    assert json.loads((out / "data" / "services.json").read_text())

    assert main(["prune", "--days", "1"]) == 0
    assert "removed 0 traces" in capsys.readouterr().out  # all within the last day
    assert Store.from_url(f"sqlite:///{db}").counts()["traces"] == counts["traces"]
