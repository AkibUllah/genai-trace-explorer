import gzip
import json

import pytest

from genai_traces.otlp import OTLPDecodeError, decompress, encode_response, flatten, parse_request
from tests.helpers import T0, Recorder, agent_trace


def test_protobuf_and_json_decode_to_the_same_spans():
    rec = Recorder()
    agent_trace(rec)
    from_pb, _ = flatten(parse_request(rec.protobuf(), "application/x-protobuf"))
    from_json, _ = flatten(parse_request(rec.otlp_json(), "application/json; charset=utf-8"))
    assert len(from_pb) == 4
    assert [(s.span_id, s.parent_span_id, s.attributes) for s in from_pb] == [
        (s.span_id, s.parent_span_id, s.attributes) for s in from_json
    ]


def test_span_fields():
    rec = Recorder(service="billing-bot")
    agent_trace(rec, fail_tool=True)
    spans, rejected = flatten(parse_request(rec.protobuf(), None))
    assert rejected == 0
    by_name = {s.name: s for s in spans}
    root = by_name["invoke_agent support"]
    tool = by_name["execute_tool lookup_order"]
    assert root.parent_span_id is None
    assert tool.parent_span_id == root.span_id
    assert tool.trace_id == root.trace_id and len(root.trace_id) == 32
    assert tool.status == "error" and tool.status_message == "order service timed out"
    assert tool.start_ns == T0 + 410_000_000
    assert root.service == "billing-bot"
    assert by_name["chat chat-large"].span_kind == "client"
    tokens = sorted(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in spans)
    assert tokens == [0, 0, 1200, 1400]


def test_json_any_values():
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "x"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "name": "chat",
                                "startTimeUnixNano": "1544712660000000000",
                                "endTimeUnixNano": "1544712661000000000",
                                "kind": 3,
                                "attributes": [
                                    {"key": "n", "value": {"intValue": "42"}},
                                    {"key": "f", "value": {"doubleValue": 0.5}},
                                    {"key": "b", "value": {"boolValue": True}},
                                    {
                                        "key": "reasons",
                                        "value": {
                                            "arrayValue": {"values": [{"stringValue": "stop"}]}
                                        },
                                    },
                                    {
                                        "key": "kv",
                                        "value": {
                                            "kvlistValue": {
                                                "values": [{"key": "a", "value": {"intValue": "1"}}]
                                            }
                                        },
                                    },
                                ],
                                "status": {"code": 1},
                            }
                        ]
                    }
                ],
            }
        ]
    }
    (span,), _ = flatten(parse_request(json.dumps(body).encode(), "application/json"))
    assert span.trace_id == "5b8efff798038103d269b633813fc60c"
    assert span.attributes == {"n": 42, "f": 0.5, "b": True, "reasons": ["stop"], "kv": {"a": 1}}
    assert span.status == "ok" and span.span_kind == "client"
    assert span.end_ns - span.start_ns == 1_000_000_000


def test_spans_with_bad_ids_are_rejected_not_fatal():
    rec = Recorder()
    agent_trace(rec)
    request = rec.request()
    request.resource_spans[0].scope_spans[0].spans[0].trace_id = b"\x00" * 16
    spans, rejected = flatten(request)
    assert rejected == 1 and len(spans) == 3


def _one_span(span: dict) -> bytes:
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode()


@pytest.mark.parametrize(
    "body,ctype",
    [
        (b"not json", "application/json"),
        (b"[1, 2]", "application/json"),
        (
            b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz"}]}]}]}',
            "application/json",
        ),
        (
            b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"spanId": "abcd"}]}]}]}',
            "application/json",
        ),
        (b"\xff\xff\xff\xff", "application/x-protobuf"),
    ],
)
def test_malformed_bodies(body, ctype):
    with pytest.raises(OTLPDecodeError):
        parse_request(body, ctype)


def test_decompress():
    assert decompress(gzip.compress(b"abc"), "gzip") == b"abc"
    assert decompress(b"abc", None) == b"abc"
    with pytest.raises(OTLPDecodeError):
        decompress(b"abc", "gzip")
    with pytest.raises(OTLPDecodeError):
        decompress(b"abc", "br")


def test_partial_success_response():
    assert encode_response(0, "", as_json=True) == b"{}"
    body = json.loads(encode_response(2, "missing ids", as_json=True))
    assert body == {"partialSuccess": {"rejectedSpans": "2", "errorMessage": "missing ids"}}
    assert encode_response(0, "", as_json=False) == b""
