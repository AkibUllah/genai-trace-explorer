"""Turn decoded OTLP spans into stored rows: classify, scrub, price."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .otlp import RawSpan
from .pricing import PriceTable
from .redact import scrub_attributes, scrub_events
from .semconv import extract
from .store import Store


@dataclass
class IngestResult:
    received: int
    accepted: int
    duplicates: int
    traces: set[str] = field(default_factory=set)


class Ingestor:
    def __init__(
        self,
        store: Store,
        prices: PriceTable,
        capture_content: bool = False,
        redact_pii: bool = True,
    ) -> None:
        self.store = store
        self.prices = prices
        self.capture_content = capture_content
        self.redact_pii = redact_pii

    def row(self, span: RawSpan) -> dict[str, Any]:
        g = extract(span.name, span.attributes)
        cost = None
        if g.kind in ("llm", "embeddings"):
            cost = self.prices.cost(
                g.response_model, g.request_model, g.input_tokens, g.output_tokens
            )
        return {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name[:512],
            "service": span.service[:255],
            "span_kind": span.span_kind,
            "kind": g.kind,
            "start_ns": span.start_ns,
            "end_ns": span.end_ns,
            "duration_ms": (span.end_ns - span.start_ns) / 1e6,
            "status": span.status,
            "status_message": span.status_message,
            "operation": g.operation,
            "provider": g.provider,
            "model": g.model,
            "input_tokens": g.input_tokens,
            "output_tokens": g.output_tokens,
            "cost_usd": cost,
            "tool_name": g.tool_name,
            "agent_name": g.agent_name,
            "conversation_id": g.conversation_id,
            "error_type": g.error_type,
            "attributes": scrub_attributes(span.attributes, self.capture_content, self.redact_pii),
            "events": scrub_events(span.events, self.capture_content, self.redact_pii),
            "resource": scrub_attributes(span.resource, True, self.redact_pii),
        }

    def ingest(self, raw: list[RawSpan]) -> IngestResult:
        rows = [self.row(span) for span in raw]
        accepted, touched = self.store.add_spans(rows)
        return IngestResult(
            received=len(raw), accepted=accepted, duplicates=len(raw) - accepted, traces=touched
        )
