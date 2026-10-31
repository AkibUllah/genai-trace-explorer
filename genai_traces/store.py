"""SQLAlchemy Core storage: one row per span, plus a per-trace summary kept up to date on ingest.

Spans of one trace arrive in several export batches and in any order (children usually end, and
are exported, before their parent), so the summary is recomputed from all stored spans of every
trace a batch touched rather than incrementally patched.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    event,
    func,
    insert,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

spans = Table(
    "spans",
    metadata,
    Column("trace_id", String(32), primary_key=True),
    Column("span_id", String(16), primary_key=True),
    Column("parent_span_id", String(16), nullable=True),
    Column("name", String(512), nullable=False),
    Column("service", String(255), nullable=False),
    Column("span_kind", String(16), nullable=False),
    Column("kind", String(16), nullable=False, index=True),
    Column("start_ns", BigInteger, nullable=False, index=True),
    Column("end_ns", BigInteger, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("status", String(8), nullable=False),
    Column("status_message", Text, nullable=False, default=""),
    Column("operation", String(64)),
    Column("provider", String(64)),
    Column("model", String(255), index=True),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", Float),
    Column("tool_name", String(255), index=True),
    Column("agent_name", String(255)),
    Column("conversation_id", String(255)),
    Column("error_type", String(255)),
    Column("attributes", JSON, nullable=False),
    Column("events", JSON, nullable=False),
    Column("resource", JSON, nullable=False),
)

traces = Table(
    "traces",
    metadata,
    Column("trace_id", String(32), primary_key=True),
    Column("name", String(512), nullable=False),
    Column("service", String(255), nullable=False, index=True),
    Column("start_ns", BigInteger, nullable=False, index=True),
    Column("end_ns", BigInteger, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("status", String(8), nullable=False),
    Column("span_count", Integer, nullable=False),
    Column("llm_calls", Integer, nullable=False),
    Column("tool_calls", Integer, nullable=False),
    Column("error_count", Integer, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("models", Text, nullable=False, default=""),  # ",model-a,model-b," for exact LIKE
    Column("agent_name", String(255)),
    Column("conversation_id", String(255), index=True),
    Column("complete", Integer, nullable=False),  # 1 when the root span has arrived
)

SUMMARY_COLUMNS = [
    spans.c[name]
    for name in (
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "service",
        "kind",
        "start_ns",
        "end_ns",
        "status",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "agent_name",
        "conversation_id",
    )
]
USAGE_KINDS = ("llm", "embeddings")  # where token usage is counted; agent spans may repeat it


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    in_memory = url in ("sqlite://", "sqlite:///:memory:")
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if in_memory:
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if not in_memory:

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record) -> None:
            # WAL lets the viewer read while an export is being written. synchronous=NORMAL
            # survives a crash of this process; a power cut can lose the last commits, a fair
            # trade for telemetry, and it removes an fsync from every export.
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


@dataclass
class TraceFilter:
    service: str | None = None
    model: str | None = None
    status: str | None = None  # "ok", "error" (root failed) or "has_errors" (any span failed)
    conversation_id: str | None = None
    min_duration_ms: float | None = None
    q: str | None = None
    since_ns: int | None = None
    before_ns: int | None = None
    limit: int = 50


class Store:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata.create_all(engine)

    @classmethod
    def from_url(cls, url: str) -> Store:
        return cls(make_engine(url))

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    # -- writes ---------------------------------------------------------------------------------

    def add_spans(self, rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
        """Insert new spans (re-sent ones are skipped) and refresh the touched trace summaries.

        Returns the rows that were new and the ids of the traces that changed.
        """
        if not rows:
            return [], set()
        unique = {(r["trace_id"], r["span_id"]): r for r in rows}
        trace_ids = sorted({trace_id for trace_id, _ in unique})
        by_trace: dict[str, list[dict[str, Any]]] = {}
        with self.engine.begin() as conn:
            # One read of the summary columns of every span already stored for these traces:
            # it answers both "was this span sent before?" and "what does the trace hold now?".
            for i in range(0, len(trace_ids), 500):
                query = select(*SUMMARY_COLUMNS).where(spans.c.trace_id.in_(trace_ids[i : i + 500]))
                for r in conn.execute(query):
                    by_trace.setdefault(r.trace_id, []).append(dict(r._mapping))
            existing = {(r["trace_id"], r["span_id"]) for rs in by_trace.values() for r in rs}
            new = [row for key, row in unique.items() if key not in existing]
            if not new:
                return [], set()
            conn.execute(insert(spans), new)
            touched = {row["trace_id"] for row in new}
            for row in new:
                by_trace.setdefault(row["trace_id"], []).append(row)
            ordered = sorted(touched)
            for i in range(0, len(ordered), 500):
                conn.execute(delete(traces).where(traces.c.trace_id.in_(ordered[i : i + 500])))
            conn.execute(insert(traces), [summarize(by_trace[t]) for t in ordered])
        return new, touched

    def prune(self, older_than_ns: int) -> int:
        """Delete traces that started before the cut-off, with their spans."""
        with self.engine.begin() as conn:
            old = select(traces.c.trace_id).where(traces.c.start_ns < older_than_ns)
            ids = [row[0] for row in conn.execute(old)]
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                conn.execute(delete(spans).where(spans.c.trace_id.in_(chunk)))
                conn.execute(delete(traces).where(traces.c.trace_id.in_(chunk)))
        return len(ids)

    # -- reads ----------------------------------------------------------------------------------

    def list_traces(self, f: TraceFilter) -> list[dict[str, Any]]:
        conditions = []
        if f.service:
            conditions.append(traces.c.service == f.service)
        if f.model:
            conditions.append(traces.c.models.like(f"%,{_escape_like(f.model)},%", escape="\\"))
        if f.status == "ok":
            conditions.append(traces.c.status != "error")
        elif f.status == "error":
            conditions.append(traces.c.status == "error")
        elif f.status == "has_errors":
            conditions.append(traces.c.error_count > 0)
        if f.conversation_id:
            conditions.append(traces.c.conversation_id == f.conversation_id)
        if f.min_duration_ms is not None:
            conditions.append(traces.c.duration_ms >= f.min_duration_ms)
        if f.q:
            pattern = f"%{_escape_like(f.q.lower())}%"
            conditions.append(func.lower(traces.c.name).like(pattern, escape="\\"))
        if f.since_ns is not None:
            conditions.append(traces.c.start_ns >= f.since_ns)
        if f.before_ns is not None:
            conditions.append(traces.c.start_ns < f.before_ns)
        query = (
            select(traces)
            .where(and_(True, *conditions))
            .order_by(traces.c.start_ns.desc(), traces.c.trace_id)
            .limit(f.limit)
        )
        with self.engine.connect() as conn:
            return [trace_out(dict(r._mapping)) for r in conn.execute(query)]

    def get_trace(self, trace_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(traces).where(traces.c.trace_id == trace_id)).first()
            if row is None:
                return None
            rows = conn.execute(
                select(spans).where(spans.c.trace_id == trace_id).order_by(spans.c.start_ns)
            )
            return trace_out(dict(row._mapping)), [dict(r._mapping) for r in rows]

    def spans_since(self, since_ns: int, kinds: Iterable[str] | None = None) -> list[dict]:
        query = select(
            spans.c.trace_id,
            spans.c.kind,
            spans.c.start_ns,
            spans.c.duration_ms,
            spans.c.status,
            spans.c.model,
            spans.c.provider,
            spans.c.tool_name,
            spans.c.input_tokens,
            spans.c.output_tokens,
            spans.c.cost_usd,
            spans.c.error_type,
        ).where(spans.c.start_ns >= since_ns)
        if kinds is not None:
            query = query.where(spans.c.kind.in_(list(kinds)))
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(query)]

    def services(self) -> list[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(traces.c.service).distinct().order_by(traces.c.service))
            return [r[0] for r in rows]

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            return {
                "traces": conn.execute(select(func.count()).select_from(traces)).scalar_one(),
                "spans": conn.execute(select(func.count()).select_from(spans)).scalar_one(),
            }


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_root(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    """The trace's root span and whether it is a true root (False: earliest orphan so far)."""
    ids = {r["span_id"] for r in rows}
    true_roots = [r for r in rows if r["parent_span_id"] is None]
    if true_roots:
        return min(true_roots, key=lambda r: r["start_ns"]), True
    orphans = [r for r in rows if r["parent_span_id"] not in ids]
    return min(orphans or rows, key=lambda r: r["start_ns"]), False


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    root, complete = find_root(rows)
    start = min(r["start_ns"] for r in rows)
    end = max(r["end_ns"] for r in rows)
    usage = [r for r in rows if r["kind"] in USAGE_KINDS]
    models = sorted({r["model"] for r in usage if r["model"]})
    agent = next((r["agent_name"] for r in rows if r["agent_name"]), None)
    conversation = next((r["conversation_id"] for r in rows if r["conversation_id"]), None)
    return {
        "trace_id": root["trace_id"],
        "name": root["name"],
        "service": root["service"],
        "start_ns": start,
        "end_ns": end,
        "duration_ms": (end - start) / 1e6,
        "status": "error" if root["status"] == "error" else "ok",
        "span_count": len(rows),
        "llm_calls": sum(1 for r in rows if r["kind"] == "llm"),
        "tool_calls": sum(1 for r in rows if r["kind"] == "tool"),
        "error_count": sum(1 for r in rows if r["status"] == "error"),
        "input_tokens": sum(r["input_tokens"] or 0 for r in usage),
        "output_tokens": sum(r["output_tokens"] or 0 for r in usage),
        "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in usage), 8),
        "models": f",{','.join(models)}," if models else "",
        "agent_name": agent,
        "conversation_id": conversation,
        "complete": 1 if complete else 0,
    }


def trace_out(row: dict[str, Any]) -> dict[str, Any]:
    """A summary row as the API returns it."""
    row = dict(row)
    row["models"] = [m for m in row["models"].split(",") if m]
    row["complete"] = bool(row["complete"])
    row["duration_ms"] = round(row["duration_ms"], 3)
    return row
