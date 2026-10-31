"""OTLP/HTTP trace decoding: protobuf and JSON bodies into flat span records.

Both encodings go through the generated ``ExportTraceServiceRequest`` message, so there is one
conversion path. OTLP/JSON differs from the canonical protobuf JSON mapping in one way that matters
here: trace and span ids are hex strings, not base64, so they are rewritten before parsing.
"""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

SPAN_KINDS = {
    0: "unspecified",
    1: "internal",
    2: "server",
    3: "client",
    4: "producer",
    5: "consumer",
}
STATUS_CODES = {0: "unset", 1: "ok", 2: "error"}
_ID_FIELDS = {"traceId": 16, "spanId": 8, "parentSpanId": 8}


class OTLPDecodeError(ValueError):
    """The request body is not a valid OTLP trace export."""


@dataclass
class RawSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    span_kind: str
    start_ns: int
    end_ns: int
    status: str
    status_message: str
    attributes: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    resource: dict[str, Any] = field(default_factory=dict)
    scope: str = ""

    @property
    def service(self) -> str:
        return str(self.resource.get("service.name") or "unknown_service")


class BodyTooLarge(OTLPDecodeError):
    """The body, or what it decompresses to, is over the configured limit."""


def decompress(body: bytes, encoding: str | None, max_bytes: int = 50_000_000) -> bytes:
    """Undo Content-Encoding without ever holding more than ``max_bytes`` of output."""
    encoding = (encoding or "identity").lower().strip()
    if encoding in ("identity", ""):
        if len(body) > max_bytes:
            raise BodyTooLarge(f"body is larger than {max_bytes} bytes")
        return body
    if encoding not in ("gzip", "deflate"):
        raise OTLPDecodeError(f"unsupported Content-Encoding {encoding!r}")
    wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
    inflater = zlib.decompressobj(wbits)
    try:
        out = inflater.decompress(body, max_bytes + 1)
    except zlib.error as exc:
        raise OTLPDecodeError(f"invalid {encoding} body") from exc
    if len(out) > max_bytes:
        raise BodyTooLarge(f"decompressed body is larger than {max_bytes} bytes")
    if not inflater.eof:
        raise OTLPDecodeError(f"truncated {encoding} body")
    return out


def parse_request(body: bytes, content_type: str | None) -> ExportTraceServiceRequest:
    """Parse an OTLP/HTTP body. ``application/json`` is JSON, anything else is protobuf."""
    media = (content_type or "application/x-protobuf").split(";")[0].strip().lower()
    message = ExportTraceServiceRequest()
    if media == "application/json":
        try:
            data = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OTLPDecodeError("body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise OTLPDecodeError("body must be a JSON object")
        try:
            json_format.ParseDict(_hex_ids_to_base64(data), message, ignore_unknown_fields=True)
        except json_format.ParseError as exc:
            raise OTLPDecodeError(f"not an OTLP trace export: {exc}") from exc
        return message
    try:
        message.ParseFromString(body)
    except DecodeError as exc:
        raise OTLPDecodeError("body is not a valid OTLP protobuf message") from exc
    return message


def _hex_ids_to_base64(data: Any) -> Any:
    if isinstance(data, list):
        return [_hex_ids_to_base64(item) for item in data]
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _ID_FIELDS and isinstance(value, str):
            out[key] = _hex_to_base64(key, value)
        else:
            out[key] = _hex_ids_to_base64(value)
    return out


def _hex_to_base64(key: str, value: str) -> str:
    if value == "":
        return ""
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise OTLPDecodeError(f"{key} must be a hex string") from exc
    if len(raw) != _ID_FIELDS[key]:
        raise OTLPDecodeError(f"{key} must be {_ID_FIELDS[key] * 2} hex characters")
    return base64.b64encode(raw).decode()


def any_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return attributes(value.kvlist_value.values)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, kind)


def attributes(items: list[KeyValue] | Any) -> dict[str, Any]:
    return {item.key: any_value(item.value) for item in items}


def flatten(request: ExportTraceServiceRequest) -> tuple[list[RawSpan], int]:
    """Spans in the request, plus how many were rejected for missing or malformed ids."""
    spans: list[RawSpan] = []
    rejected = 0
    for resource_spans in request.resource_spans:
        resource = attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            scope = scope_spans.scope.name
            for span in scope_spans.spans:
                if len(span.trace_id) != 16 or len(span.span_id) != 8 or not any(span.trace_id):
                    rejected += 1
                    continue
                parent = span.parent_span_id.hex() if len(span.parent_span_id) == 8 else None
                end = max(span.end_time_unix_nano, span.start_time_unix_nano)
                spans.append(
                    RawSpan(
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_span_id=parent,
                        name=span.name or "(unnamed)",
                        span_kind=SPAN_KINDS.get(span.kind, "unspecified"),
                        start_ns=span.start_time_unix_nano,
                        end_ns=end,
                        status=STATUS_CODES.get(span.status.code, "unset"),
                        status_message=span.status.message,
                        attributes=attributes(span.attributes),
                        events=[
                            {
                                "name": event.name,
                                "time_ns": event.time_unix_nano,
                                "attributes": attributes(event.attributes),
                            }
                            for event in span.events
                        ],
                        resource=resource,
                        scope=scope,
                    )
                )
    return spans, rejected


def encode_response(rejected: int, message: str, as_json: bool) -> bytes:
    response = ExportTraceServiceResponse()
    if rejected:
        response.partial_success.rejected_spans = rejected
        response.partial_success.error_message = message
    if as_json:
        return json.dumps(json_format.MessageToDict(response)).encode()
    return response.SerializeToString()
