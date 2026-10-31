import gzip
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from google.rpc.status_pb2 import Status
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.trace.export import SpanExportResult

from genai_traces.api import create_app
from genai_traces.config import Settings
from tests.helpers import T0, Recorder, agent_trace

PB = {"content-type": "application/x-protobuf"}


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{"database_url": "sqlite://", **overrides})


@pytest.fixture
def client(store):
    with TestClient(create_app(settings(), store=store)) as c:
        yield c


def post(client, rec, headers=PB, gz=False):
    body = rec.protobuf() if headers["content-type"].endswith("protobuf") else rec.otlp_json()
    headers = dict(headers)
    if gz:
        body = gzip.compress(body)
        headers["content-encoding"] = "gzip"
    return client.post("/v1/traces", content=body, headers=headers)


def test_protobuf_export_then_query(client):
    rec = Recorder()
    agent_trace(rec, conversation="conv-9", fail_tool=True)
    response = post(client, rec, gz=True)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(response.content)
    assert parsed.partial_success.rejected_spans == 0

    page = client.get("/api/traces").json()
    assert page["next_before_ns"] is None
    (trace,) = page["items"]
    detail = client.get(f"/api/traces/{trace['trace_id'].upper()}").json()
    assert detail["trace"]["trace_id"] == trace["trace_id"]
    assert [s["name"] for s in detail["spans"]][:2] == ["invoke_agent support", "chat chat-large"]
    assert detail["spans"][2]["status"] == "error"

    conv = client.get("/api/conversations/conv-9").json()
    assert conv["turns"] == 1 and conv["input_tokens"] == 2600
    assert client.get("/api/conversations/nope").status_code == 404
    assert client.get("/api/traces/" + "0" * 32).status_code == 404
    assert client.get("/api/services").json() == ["support-agent"]


def test_json_export_and_partial_success(client):
    rec = Recorder()
    agent_trace(rec)
    data = json.loads(rec.otlp_json())
    data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = ""
    response = client.post(
        "/v1/traces", content=json.dumps(data), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["partialSuccess"]["rejectedSpans"] == "1"
    assert client.get("/health").json()["spans"] == 3


def test_filters_and_paging(client):
    rec = Recorder()
    for i in range(5):
        span = rec.span(f"job {i}", i * 1000, i * 1000 + 10)
        rec.end(span, i * 1000 + 10)
    post(client, rec)
    first = client.get("/api/traces", params={"limit": 2}).json()
    assert [t["name"] for t in first["items"]] == ["job 4", "job 3"]
    second = client.get(
        "/api/traces", params={"limit": 2, "before_ns": first["next_before_ns"]}
    ).json()
    assert [t["name"] for t in second["items"]] == ["job 2", "job 1"]
    assert client.get("/api/traces", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/traces", params={"limit": 0}).status_code == 422


@pytest.mark.parametrize(
    "body,headers,status",
    [
        (b"garbage", {"content-type": "application/json"}, 400),
        (b"\xff\xff", PB, 400),
        (b"{}", {"content-type": "text/plain"}, 415),
        (b"abc", {**PB, "content-encoding": "gzip"}, 400),
    ],
)
def test_bad_exports(client, body, headers, status):
    response = client.post("/v1/traces", content=body, headers=headers)
    assert response.status_code == status
    if headers["content-type"] == "application/x-protobuf":
        rpc = Status()
        rpc.ParseFromString(response.content)
        assert rpc.message and rpc.code == 3
    elif status == 400:
        assert response.json()["code"] == 3


def test_body_limits(store):
    app = create_app(settings(max_body_bytes=2048), store=store)
    with TestClient(app) as client:
        bomb = gzip.compress(b"\0" * 100_000)
        response = client.post(
            "/v1/traces", content=bomb, headers={**PB, "content-encoding": "gzip"}
        )
        assert response.status_code == 413
        assert client.post("/v1/traces", content=b"x" * 5000, headers=PB).status_code == 413


def test_tokens(store):
    app = create_app(settings(ingest_token="in-secret", read_token="read-secret"), store=store)
    rec = Recorder()
    agent_trace(rec)
    with TestClient(app) as client:
        assert post(client, rec).status_code == 401
        ok = post(client, rec, headers={**PB, "authorization": "Bearer in-secret"})
        assert ok.status_code == 200
        assert client.get("/api/traces").status_code == 401
        wrong = {"authorization": "Bearer in-secret"}
        assert client.get("/api/traces", headers=wrong).status_code == 401
        good = {"authorization": "Bearer read-secret"}
        assert len(client.get("/api/traces", headers=good).json()["items"]) == 1
        health = client.get("/health").json()  # health stays open for probes
        assert health["ingest_auth"] is True and health["traces"] == 1


def test_capture_content_setting(store):
    app = create_app(settings(capture_content=True), store=store)
    rec = Recorder()
    agent_trace(rec)
    with TestClient(app) as client:
        post(client, rec)
        trace_id = client.get("/api/traces").json()["items"][0]["trace_id"]
        spans = client.get(f"/api/traces/{trace_id}").json()["spans"]
    messages = [
        s["attributes"]["gen_ai.input.messages"]
        for s in spans
        if "gen_ai.input.messages" in s["attributes"]
    ]
    assert "refund order 1042, call [phone]" in messages[0]  # kept, PII still masked


def test_stats_endpoint(client):
    rec = Recorder()
    agent_trace(rec)
    post(client, rec)
    stats = client.get("/api/stats", params={"hours": 2, "until_ns": T0 + 3_600_000_000_000})
    body = stats.json()
    assert body["totals"]["traces"] == 1 and body["totals"]["llm_calls"] == 2
    assert body["models"][0]["model"] in ("chat-large", "chat-large-2026-08")
    assert client.get("/api/stats").json()["totals"]["traces"] == 0  # now: long after T0


def test_metrics(client):
    rec = Recorder()
    agent_trace(rec)
    post(client, rec)
    post(client, rec)
    text = client.get("/metrics").text
    assert 'otlp_spans_total{result="accepted"} 4.0' in text
    assert 'otlp_spans_total{result="duplicate"} 4.0' in text
    assert 'otlp_export_requests_total{encoding="protobuf",outcome="ok"} 2.0' in text
    assert 'genai_tokens_total{direction="input",model="chat-large-2026-08"} 1200.0' in text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_otlp_exporter_end_to_end(tmp_path):
    """The OpenTelemetry SDK's own OTLP/HTTP exporter (protobuf + gzip) against a live server."""
    app = create_app(settings(database_url=f"sqlite:///{tmp_path}/e2e.db", ingest_token="tok"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        rec = Recorder(service="checkout-assistant")
        agent_trace(rec)
        exporter = OTLPSpanExporter(
            endpoint=f"http://127.0.0.1:{port}/v1/traces",
            headers={"authorization": "Bearer tok"},
            compression=Compression.Gzip,
        )
        result = exporter.export(rec.exporter.get_finished_spans())
        exporter.shutdown()
        assert result == SpanExportResult.SUCCESS
        with TestClient(app) as client:
            (trace,) = client.get("/api/traces").json()["items"]
        assert trace["service"] == "checkout-assistant" and trace["span_count"] == 4
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_viewer_page_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert '<meta name="gtx-mode" content="api">' in page.text
    assert "function esc(" in page.text  # every attribute value is escaped before rendering
