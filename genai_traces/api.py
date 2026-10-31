"""FastAPI app: OTLP/HTTP receiver on /v1/traces, query API on /api, viewer on /."""

from __future__ import annotations

import hmac
import logging
import time
from importlib import resources
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from prometheus_client import Histogram as PromHistogram

from . import __version__
from .analytics import HOUR_NS, stats
from .config import Settings
from .ingest import Ingestor
from .otlp import BodyTooLarge, OTLPDecodeError, decompress, encode_response, flatten
from .otlp import parse_request as parse_otlp
from .pricing import PriceTable
from .store import Store, TraceFilter
from .tree import build_tree

log = logging.getLogger("genai_traces")


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "otlp_export_requests",
            "OTLP trace export requests",
            ["encoding", "outcome"],
            registry=self.registry,
        )
        self.spans = Counter("otlp_spans", "Spans received", ["result"], registry=self.registry)
        self.tokens = Counter(
            "genai_tokens",
            "Tokens reported by LLM spans",
            ["model", "direction"],
            registry=self.registry,
        )
        self.cost = Counter(
            "genai_cost_usd", "Priced cost of LLM spans", ["model"], registry=self.registry
        )
        self.ingest_seconds = PromHistogram(
            "otlp_ingest_seconds", "Time to decode and store one export", registry=self.registry
        )


def _bearer_ok(request: Request, expected: str | None) -> bool:
    if expected is None:
        return True
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), expected)


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    store = store or Store.from_url(settings.database_url)
    prices = PriceTable.load(settings.pricing_file)
    ingestor = Ingestor(store, prices, settings.capture_content, settings.redact_pii)
    metrics = Metrics()

    app = FastAPI(
        title="GenAI Trace Explorer",
        version=__version__,
        description=(
            "OTLP/HTTP receiver for OpenTelemetry traces from LLM and agent applications, and a "
            "query API over them: span trees, token and cost rollups, per-model and per-tool "
            "latency and error stats. Point any OpenTelemetry SDK's OTLP/HTTP exporter at "
            "`/v1/traces` (the default endpoint path)."
        ),
    )
    app.state.store = store
    app.state.settings = settings
    app.state.metrics = metrics

    def require_reader(request: Request) -> None:
        if not _bearer_ok(request, settings.read_token):
            raise HTTPException(401, "missing or invalid bearer token")

    reader = [Depends(require_reader)]

    @app.post(
        "/v1/traces",
        tags=["ingest"],
        summary="OTLP/HTTP trace export (protobuf or JSON, optionally gzip)",
        response_class=Response,
        responses={
            200: {"description": "ExportTraceServiceResponse, partial success when spans are bad"},
            400: {"description": "Body is not an OTLP trace export"},
            401: {"description": "INGEST_TOKEN is set and the bearer token is wrong"},
            413: {"description": "Body over MAX_BODY_BYTES (before or after decompression)"},
            415: {"description": "Unsupported content type"},
        },
    )
    async def export_traces(request: Request) -> Response:
        media = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        as_json = media == "application/json"
        encoding = "json" if as_json else "protobuf"
        if media not in ("application/json", "application/x-protobuf"):
            metrics.requests.labels(encoding, "unsupported").inc()
            return _otlp_error(415, f"unsupported content type {media!r}", as_json)
        if not _bearer_ok(request, settings.ingest_token):
            metrics.requests.labels(encoding, "unauthorized").inc()
            return _otlp_error(401, "missing or invalid bearer token", as_json)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            metrics.requests.labels(encoding, "too_large").inc()
            return _otlp_error(413, "body too large", as_json)

        started = time.perf_counter()
        body = await request.body()
        try:
            if len(body) > settings.max_body_bytes:
                raise BodyTooLarge("body too large")
            raw = decompress(body, request.headers.get("content-encoding"), settings.max_body_bytes)
            spans, rejected = flatten(parse_otlp(raw, media))
        except BodyTooLarge as exc:
            metrics.requests.labels(encoding, "too_large").inc()
            return _otlp_error(413, str(exc), as_json)
        except OTLPDecodeError as exc:
            metrics.requests.labels(encoding, "bad_request").inc()
            return _otlp_error(400, str(exc), as_json)

        result = ingestor.ingest(spans)
        metrics.ingest_seconds.observe(time.perf_counter() - started)
        metrics.requests.labels(encoding, "ok").inc()
        metrics.spans.labels("accepted").inc(result.accepted)
        metrics.spans.labels("duplicate").inc(result.duplicates)
        metrics.spans.labels("rejected").inc(rejected)
        for row in result.new_rows:
            if row["kind"] not in ("llm", "embeddings"):
                continue
            model = row["model"] or "unknown"
            if row["input_tokens"]:
                metrics.tokens.labels(model, "input").inc(row["input_tokens"])
            if row["output_tokens"]:
                metrics.tokens.labels(model, "output").inc(row["output_tokens"])
            if row["cost_usd"]:
                metrics.cost.labels(model).inc(row["cost_usd"])
        log.info(
            "export: %d spans, %d new, %d duplicate, %d rejected, %d traces",
            len(spans) + rejected,
            result.accepted,
            result.duplicates,
            rejected,
            len(result.traces),
        )
        message = "spans need a 16-byte trace id and an 8-byte span id" if rejected else ""
        return Response(
            encode_response(rejected, message, as_json),
            media_type="application/json" if as_json else "application/x-protobuf",
        )

    @app.get("/api/traces", tags=["query"], dependencies=reader, summary="Recent traces")
    def list_traces(
        service: str | None = None,
        model: str | None = Query(None, description="Exact model name (request or response)"),
        status: Literal["ok", "error", "has_errors"] | None = Query(
            None, description="error: the root span failed; has_errors: any span failed"
        ),
        conversation: str | None = None,
        min_duration_ms: float | None = Query(None, ge=0),
        q: str | None = Query(None, max_length=200, description="Substring of the root name"),
        before_ns: int | None = Query(None, description="Paging cursor: start_ns of the last row"),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        items = store.list_traces(
            TraceFilter(
                service=service,
                model=model,
                status=status,
                conversation_id=conversation,
                min_duration_ms=min_duration_ms,
                q=q,
                before_ns=before_ns,
                limit=limit,
            )
        )
        next_cursor = items[-1]["start_ns"] if len(items) == limit else None
        return {"items": items, "next_before_ns": next_cursor}

    @app.get(
        "/api/traces/{trace_id}",
        tags=["query"],
        dependencies=reader,
        summary="One trace: summary and span tree",
    )
    def get_trace(trace_id: str) -> dict[str, Any]:
        found = store.get_trace(trace_id.lower())
        if found is None:
            raise HTTPException(404, "trace not found")
        summary, rows = found
        return {"trace": summary, "spans": build_tree(rows)}

    @app.get(
        "/api/conversations/{conversation_id}",
        tags=["query"],
        dependencies=reader,
        summary="Every trace of one conversation (gen_ai.conversation.id), oldest first",
    )
    def get_conversation(conversation_id: str) -> dict[str, Any]:
        items = store.list_traces(TraceFilter(conversation_id=conversation_id, limit=500))
        if not items:
            raise HTTPException(404, "conversation not found")
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

    @app.get(
        "/api/stats",
        tags=["query"],
        dependencies=reader,
        summary="Totals, per-model and per-tool stats and an hourly timeline",
    )
    def get_stats(
        hours: int = Query(24, ge=1, le=24 * 31),
        until_ns: int | None = Query(None, description="End of the window; default now"),
    ) -> dict[str, Any]:
        now = until_ns or time.time_ns()
        since = now - hours * HOUR_NS
        trace_rows = [
            t
            for t in store.list_traces(TraceFilter(since_ns=since, limit=100_000))
            if t["start_ns"] <= now
        ]
        span_rows = [s for s in store.spans_since(since) if s["start_ns"] <= now]
        return stats(span_rows, trace_rows, since, now)

    @app.get("/api/services", tags=["query"], dependencies=reader, summary="Known services")
    def get_services() -> list[str]:
        return store.services()

    @app.get("/health", tags=["ops"], summary="Liveness and database check")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "database": store.dialect,
            **store.counts(),
            "capture_content": settings.capture_content,
            "ingest_auth": settings.ingest_token is not None,
        }

    @app.get("/metrics", tags=["ops"], summary="Prometheus metrics", response_class=Response)
    def prometheus() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def viewer() -> HTMLResponse:
        page = resources.files("genai_traces").joinpath("static/index.html").read_text("utf-8")
        return HTMLResponse(page)

    return app


GRPC_CODES = {400: 3, 401: 16, 413: 8, 415: 3}  # INVALID_ARGUMENT, UNAUTHENTICATED, ...


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def rpc_status(code: int, message: str) -> bytes:
    """A serialized google.rpc.Status {code = 1, message = 2}, without pulling in its package."""
    text = message.encode()
    return b"\x08" + _varint(code) + b"\x12" + _varint(len(text)) + text


def _otlp_error(status: int, message: str, as_json: bool) -> Response:
    """OTLP/HTTP failures carry a google.rpc.Status in the request's encoding."""
    code = GRPC_CODES.get(status, 2)
    if as_json:
        return JSONResponse({"code": code, "message": message}, status_code=status)
    return Response(
        rpc_status(code, message), status_code=status, media_type="application/x-protobuf"
    )
