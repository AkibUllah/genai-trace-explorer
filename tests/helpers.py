"""Build OTLP payloads with the real OpenTelemetry SDK and its protobuf encoder."""

from __future__ import annotations

import json

from google.protobuf import json_format
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode, set_span_in_context

MS = 1_000_000
T0 = 1_790_000_000_000_000_000  # a fixed wall-clock origin, in ns


class Recorder:
    """A tracer provider whose finished spans can be encoded as an OTLP export request."""

    def __init__(self, service: str = "support-agent") -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider(resource=Resource.create({"service.name": service}))
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("tests")

    def span(self, name, start_ms, end_ms, parent=None, attrs=None, error=None, kind=None):
        ctx = set_span_in_context(parent) if parent is not None else None
        span = self.tracer.start_span(
            name,
            context=ctx,
            kind=kind or SpanKind.INTERNAL,
            attributes=attrs or {},
            start_time=T0 + int(start_ms * MS),
        )
        if error:
            span.set_status(Status(StatusCode.ERROR, error))
        return span

    def end(self, span, end_ms):
        span.end(end_time=T0 + int(end_ms * MS))

    def request(self):
        return encode_spans(self.exporter.get_finished_spans())

    def protobuf(self) -> bytes:
        return self.request().SerializeToString()

    def otlp_json(self) -> bytes:
        """OTLP/JSON as the spec defines it: hex ids, not the protobuf mapping's base64."""
        data = json_format.MessageToDict(self.request())
        for rs in data.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    for key in ("traceId", "spanId", "parentSpanId"):
                        if key in span:
                            span[key] = _b64_to_hex(span[key])
        return json.dumps(data).encode()


def _b64_to_hex(value: str) -> str:
    import base64

    return base64.b64decode(value).hex()


def agent_trace(rec: Recorder, conversation: str = "conv-1", fail_tool: bool = False):
    """invoke_agent -> chat -> execute_tool -> chat, like an instrumented support agent."""
    root = rec.span(
        "invoke_agent support",
        0,
        900,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "support",
            "gen_ai.conversation.id": conversation,
        },
    )
    chat1 = rec.span(
        "chat chat-large",
        10,
        400,
        parent=root,
        kind=SpanKind.CLIENT,
        attrs={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "chat-large",
            "gen_ai.response.model": "chat-large-2026-08",
            "gen_ai.usage.input_tokens": 1200,
            "gen_ai.usage.output_tokens": 80,
            "gen_ai.input.messages": (
                '[{"role":"user","content":"refund order 1042, call 01712345678"}]'
            ),
        },
    )
    rec.end(chat1, 400)
    tool = rec.span(
        "execute_tool lookup_order",
        410,
        520,
        parent=root,
        attrs={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "lookup_order",
            "gen_ai.tool.call.arguments": '{"order_id": 1042}',
            **({"error.type": "TimeoutError"} if fail_tool else {}),
        },
        error="order service timed out" if fail_tool else None,
    )
    rec.end(tool, 520)
    chat2 = rec.span(
        "chat chat-large",
        530,
        880,
        parent=root,
        kind=SpanKind.CLIENT,
        attrs={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "chat-large",
            "gen_ai.usage.input_tokens": 1400,
            "gen_ai.usage.output_tokens": 120,
        },
    )
    rec.end(chat2, 880)
    rec.end(root, 900)
    return root
