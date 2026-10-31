<!-- topics: opentelemetry,llm-observability,genai,ai-agents,tracing,otlp,fastapi,python,postgresql,llmops -->
# genai-trace-explorer

An OpenTelemetry collector and trace viewer for LLM and agent applications. It receives OTLP/HTTP traces from any OpenTelemetry SDK and reads the GenAI semantic conventions to show span trees with self time and the critical path, token and cost rollups per trace and conversation, per-model and per-tool latency and error rates, and drops prompt content at ingest unless you opt in.

[![CI](https://github.com/AkibUllah/genai-trace-explorer/actions/workflows/ci.yml/badge.svg)](https://github.com/AkibUllah/genai-trace-explorer/actions/workflows/ci.yml)
[![CodeQL](https://github.com/AkibUllah/genai-trace-explorer/actions/workflows/codeql.yml/badge.svg)](https://github.com/AkibUllah/genai-trace-explorer/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/AkibUllah/genai-trace-explorer)](https://github.com/AkibUllah/genai-trace-explorer/releases)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-OTLP%2FHTTP-425cc7)
![License](https://img.shields.io/badge/license-MIT-green)

## Try it live

| | |
|---|---|
| Live demo | https://akibullah.github.io/genai-trace-explorer/: the real viewer on about 150 traces that the demo services emit with the OpenTelemetry SDK. CI regenerates it on every push and weekly. It is a static export, so filters run in the browser and nothing is ingested. |
| Run in your browser | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/AkibUllah/genai-trace-explorer?quickstart=1). It starts the collector on port 4318 and sends two days of demo traffic over OTLP. Open forwarded port 4318 for the viewer, or `/docs` for the API. |
| One-click deploy | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/AkibUllah/genai-trace-explorer). `render.yaml` provisions the collector and a free PostgreSQL, and generates `INGEST_TOKEN` and `READ_TOKEN`. Enter the read token in the viewer, and give the ingest token to your exporter. |
| Local | `pip install -e ".[demo]" && genai-traces serve`, then `genai-traces demo` in a second terminal. Or run `docker compose up` |

## Screenshots

![A support-agent trace: waterfall, critical path and span attributes](docs/images/trace.png)

One turn of the demo wallet-support agent:
- The first `chat-large` call was rate-limited (red outline), so the agent fell back to `chat-small`.
- Two tool calls followed, then the final answer.
- The diamonds mark the critical path. The span table shows `gen_ai.input.messages` as `[content not captured]`, because content capture is off by default.

![Models and tools: latency percentiles, error rates, cost](docs/images/models-tools.png)

The models and tools view:
- `p95` of 5.00 s on three tools means timeouts.
- The failed `chat-large` row is the rate-limited calls, which never report usage.
- The self-hosted model is priced at $0.

## Why this exists

Every LLM feature I have shipped eventually brought the same questions:
- Why was this answer slow?
- Which tool keeps timing out?
- How much did that one customer conversation cost?
- Did the fallback model kick in yesterday?

Request logs can't answer these, because one user turn is a tree of model calls, tool calls and retrieval steps. The answer depends on which step the others were waiting for.

OpenTelemetry now has semantic conventions for exactly this (`gen_ai.*` attributes on spans), and most LLM SDK instrumentations emit them. This repo is the backend that reads them, built from scratch so every step is visible and tested:
- OTLP decoding
- out-of-order span assembly
- self-time and critical-path math
- pricing
- privacy at ingest

## Features

- **OTLP/HTTP receiver** on `/v1/traces`, the default path of every OpenTelemetry SDK exporter:
  - protobuf and JSON encodings, gzip or deflate
  - partial-success responses for spans with bad ids
  - `google.rpc.Status` error bodies, as the OTLP spec asks
- **GenAI semantic conventions:** classifies spans as agent, LLM, tool, embeddings, retrieval or other, from `gen_ai.operation.name`, and extracts:
  - provider and request/response model
  - input and output tokens
  - tool and agent names
  - conversation id and `error.type`

  Older attribute names (`gen_ai.system`, `gen_ai.usage.prompt_tokens`) are read as fallbacks.
- **Privacy at ingest:** prompt, completion, tool-argument and tool-result attributes (and the matching span events) are replaced with `[content not captured]` before anything is written. Emails, phone numbers (Bangladeshi and international) and Luhn-valid card numbers are masked in everything that is kept. Opt in to content with `CAPTURE_CONTENT=true`; masking still applies.
- **Costs** from a YAML price table: USD per 1M tokens, glob patterns, the most specific match wins, checked against the response model and then the request model. Self-hosted models can be priced at zero.
- **Trace summaries** kept current as spans arrive in any order and across batches:
  - a trace without its root shows as partial
  - re-sent spans are skipped, so exporter retries are idempotent
- **Span trees:**
  - depth-first waterfall
  - self time (time not covered by children)
  - subtree token and cost rollups
  - the critical path (the chain of last-finishing children)
  - orphaned spans shown as roots
- **Analytics** over any window:
  - per-model calls, errors, tokens, cost, p50 and p95
  - per-tool error rate with error types, p50 and p95
  - trace-level totals and an hourly timeline
- **Conversations:** every turn that shares `gen_ai.conversation.id`, with total tokens, cost and time spent in the agent.
- **Viewer:** one HTML file with no build step. It has filters, a waterfall, span attributes and events, conversations, and model and tool tables, in light and dark mode. The same page works against the live API or as a static export (the Pages demo).
- **Operations:**
  - optional bearer tokens for ingest and for reads
  - a body limit that also bounds decompressed size (a gzip bomb stops at the limit)
  - Prometheus `/metrics`: exports, spans, tokens and cost by model, ingest latency
  - `/health`
  - retention pruning
  - SQLite or PostgreSQL

## Architecture

```mermaid
flowchart LR
    subgraph Apps["Your services (any OpenTelemetry SDK)"]
        A1[agent] --- A2[RAG API] --- A3[batch job]
    end
    Apps -- "OTLP/HTTP\nprotobuf or JSON, gzip" --> R[POST /v1/traces]
    R --> D[decode\notlp.py]
    D --> C[classify\nsemconv.py]
    C --> S[scrub content, mask PII\nredact.py]
    S --> P[price\npricing.py]
    P --> DB[(SQLite / PostgreSQL\nspans · traces)]
    DB --> Q[/api: traces, trees,\nconversations, stats/]
    Q --> UI[viewer /]
    Q --> X[export-static\n→ GitHub Pages]
    R --> M[/metrics/]
```

Key decisions:

- **Summaries are recomputed, not patched.** Children usually finish and get exported before their parent, and one trace often spans several export batches. Each export does one read of the stored summary columns for the traces it touches. That read answers both "was this span sent before?" and "what does the trace hold now?". The touched summaries are then rewritten in the same transaction.
- **Tokens are counted only on LLM and embedding spans.** Some instrumentations also put the total usage on the agent span. Summing every span would double-count it.
- **The critical path follows the last-finishing child.** A parent can only finish after its slowest dependent work. So from the root, the child that ended last is what the parent waited on. Repeating that gives the chain worth optimising.
- **Privacy is enforced at the door.** Content is dropped before the insert, not hidden in the UI. With capture on, PII masking still runs unless `REDACT_PII=false`. With the defaults, a database dump never holds a raw phone number from a prompt.
- **Decompression is bounded.** The body limit is applied to the inflated output with `zlib.decompressobj(max_length)`. A 10 KB gzip body that expands to 10 MB is rejected at the limit instead of being inflated in memory.
- **SQLite runs in WAL with `synchronous=NORMAL`.** The viewer can read during writes, and an export costs no fsync. A crash of the process loses nothing, and a power cut can lose the last few exports, which is the right trade for telemetry.

## Sending traces from your app

Any OpenTelemetry SDK works. With Python:

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=support-agent
# export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <INGEST_TOKEN>"   # if set
```

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("support-agent")

with tracer.start_as_current_span("invoke_agent support") as agent:
    agent.set_attribute("gen_ai.operation.name", "invoke_agent")
    agent.set_attribute("gen_ai.conversation.id", conversation_id)
    with tracer.start_as_current_span("chat chat-large") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "chat-large")
        reply = client.chat(...)
        span.set_attribute("gen_ai.usage.input_tokens", reply.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", reply.usage.output_tokens)
```

Instrumentation libraries that emit the GenAI conventions set these attributes for you. [`genai_traces/demo.py`](genai_traces/demo.py) shows the full set the viewer uses: tool calls, retrieval, embeddings, errors with `error.type`.

## Benchmark

`bench/bench.py` measures the collector's work per export. It encodes 7,041 demo spans (1,444 traces) as 14 protobuf exports of 512 spans, the SDK's default batch size. Each export is then decoded, classified, scrubbed, priced and stored, and its trace summaries are recomputed. The run excludes HTTP.

Real numbers from this machine: Intel i5-6200U (2 cores / 4 threads), laptop SSD, Python 3.10.12. PostgreSQL 16 ran in Docker with default settings.

| database | spans/s | ms per export of 512 spans |
| --- | ---: | ---: |
| SQLite (WAL, `synchronous=NORMAL`) | 2,089 | 241 |
| PostgreSQL 16 | 2,701 | 186 |

With SQLite's default `synchronous=FULL`, the same run did 485 spans/s, because each export's commit waited about 0.8 s for fsync on this disk.

```bash
python bench/bench.py
python bench/bench.py --url postgresql+psycopg://traces:traces@localhost:5432/traces
```

## Tech Stack

| Layer | Choice |
| --- | --- |
| Ingest | FastAPI, `opentelemetry-proto` generated messages, protobuf `json_format` for OTLP/JSON |
| Storage | SQLAlchemy 2 Core; SQLite (WAL) or PostgreSQL 16 via psycopg 3 |
| Viewer | One HTML file, vanilla JS, CSS variables for light and dark |
| Ops | prometheus-client, pydantic-settings, uvicorn |
| Demo | OpenTelemetry Python SDK with seeded ids and explicit timestamps |
| Tooling | pytest (+ coverage), ruff, GitHub Actions (3.10-3.12 matrix, PostgreSQL job, Docker OTLP smoke test, Pages), CodeQL, Dependabot |
| Runtime | Docker (python:3.12-slim, non-root, port 4318), docker compose with PostgreSQL |

## Quick Start

### Local, SQLite

```bash
git clone git@github.com:AkibUllah/genai-trace-explorer.git
cd genai-trace-explorer
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
genai-traces serve                 # viewer and OTLP/HTTP on http://localhost:4318
genai-traces demo                  # in another terminal: two days of demo traffic over OTLP
```

### docker compose, PostgreSQL

```bash
docker compose up -d --build       # collector on :4318 + PostgreSQL
docker compose run --rm demo       # demo traffic over OTLP from a second container
```

### CLI

```bash
genai-traces serve --port 4318
genai-traces demo --endpoint http://localhost:4318/v1/traces --token "$INGEST_TOKEN"
genai-traces demo --direct --hours 72 --conversations 100   # straight into DATABASE_URL
genai-traces prune --days 14
genai-traces export-static _site   # the viewer + JSON for static hosting
```

## API

| Method | Path | Description |
| --- | --- | --- |
| POST | `/v1/traces` | OTLP/HTTP export: `application/x-protobuf` or `application/json`, optional `Content-Encoding: gzip`. Returns `ExportTraceServiceResponse`; `400` bad body, `401` token, `413` too large (before or after decompression), `415` content type |
| GET | `/api/traces` | Filters: `service`, `model` (exact), `status` (`ok`, `error`, `has_errors`), `conversation`, `min_duration_ms`, `q`. Paged by `limit` and `before_ns` (the response has `next_before_ns`) |
| GET | `/api/traces/{trace_id}` | Summary plus the span tree: depth, offsets, self time, rollups, critical path, attributes and events |
| GET | `/api/conversations/{id}` | Every turn of a conversation with totals |
| GET | `/api/stats?hours=24` | Totals, per-model and per-tool stats, hourly timeline |
| GET | `/api/services` | Known service names |
| GET | `/health` | Status, database, trace and span counts (no token needed) |
| GET | `/metrics` | Prometheus metrics |
| GET | `/` | Viewer |

The `/api` endpoints require `Authorization: Bearer <READ_TOKEN>` when `READ_TOKEN` is set. Configuration (see [`.env.example`](.env.example)):

- `DATABASE_URL`: `postgres://` URLs from hosting providers are accepted
- `INGEST_TOKEN` and `READ_TOKEN`
- `CAPTURE_CONTENT` and `REDACT_PII`
- `PRICING_FILE`
- `MAX_BODY_BYTES` and `RETENTION_DAYS`

## Project Structure

```
genai_traces/
├── otlp.py         # OTLP/HTTP decoding (protobuf, JSON with hex ids), bounded decompression
├── semconv.py      # GenAI semantic conventions: span kind and indexed fields
├── redact.py       # content dropping and PII masking at ingest
├── pricing.py      # price table with glob patterns; pricing.yaml is the bundled example
├── ingest.py       # decoded span -> stored row
├── store.py        # SQLAlchemy tables, dedupe + summary recompute per export, queries, prune
├── tree.py         # span tree: self time, rollups, critical path, orphans
├── analytics.py    # per-model / per-tool percentiles, error rates, timeline
├── views.py        # response bodies shared by the API and the static export
├── api.py, asgi.py # FastAPI app factory + entry point
├── cli.py          # `genai-traces` serve / demo / prune / export-static
├── demo.py         # three instrumented demo services on the OpenTelemetry SDK
└── static/index.html
bench/bench.py      # ingest throughput (table above)
tests/              # 62 tests; the whole suite also runs on PostgreSQL in CI
```

## Testing

```bash
make test        # SQLite
TEST_DATABASE_URL=postgresql+psycopg://traces:traces@localhost:5432/traces pytest   # PostgreSQL
make lint
```

There are 62 tests at 96% line coverage, and all of them run against both SQLite and PostgreSQL. Test payloads are built with the real OpenTelemetry SDK and its OTLP encoder, not hand-written fixtures.

**OTLP:**
- protobuf and JSON decode to identical spans
- every `AnyValue` type
- bad ids give a partial success
- malformed bodies are rejected
- gzip and deflate
- truncated streams
- a decompression bomb stops at the limit

**Conventions and privacy:**
- current and legacy attribute names
- kind classification
- bad token counts
- content dropping for attributes and events
- email, Bangladeshi and international phone, and Luhn-checked card masking

**Pricing:**
- the most specific pattern wins
- fallback to the request model
- unknown models and invalid tables

**Store and trees:**
- summaries
- idempotent re-sends
- children arriving before their root
- every filter and cursor paging
- pruning
- self time with overlapping children
- rollups
- the critical path
- orphans

**API:**
- the SDK's own `OTLPSpanExporter` (protobuf + gzip) against a live uvicorn server
- `google.rpc.Status` error bodies
- ingest and read tokens
- body limits
- content capture
- stats
- Prometheus counters
- the viewer page

**Demo and CLI:**
- seeded determinism
- valid GenAI telemetry from all three services
- tool arguments never stored
- `demo --direct`, `export-static` and `prune`

## Roadmap

- [ ] OTLP logs and metrics (`/v1/logs`, `/v1/metrics`) so GenAI content events and token histograms from SDKs that emit them land next to the spans
- [ ] Evaluation scores as span annotations (`POST /api/traces/{id}/scores`) and a "worst-scored traces" view
- [ ] Tail-based sampling rules: keep every failed or slow trace, sample the rest
- [ ] Cost budgets per service with an alert webhook, reusing the hourly timeline
- [ ] ClickHouse store for high-volume deployments; the store interface is already isolated

## License

MIT © 2026 Akib Jafor
