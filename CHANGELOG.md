# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-31

First public release.

### Added

- OTLP/HTTP trace receiver on `/v1/traces`: protobuf and JSON, gzip and deflate, partial success for spans with bad ids, `google.rpc.Status` errors, optional bearer token, body limit that also bounds decompressed size.
- GenAI semantic conventions: span kinds (agent, LLM, tool, embeddings, retrieval) and indexed fields, with legacy attribute names as fallbacks.
- Ingest-time privacy: prompt, completion and tool payload attributes and events dropped unless `CAPTURE_CONTENT=true`; email, phone and card number masking.
- YAML price table with glob patterns and per-span cost.
- SQLAlchemy store for SQLite (WAL) and PostgreSQL with per-trace summaries recomputed per export, idempotent re-sends and out-of-order arrival.
- Span trees with self time, subtree rollups, critical path and orphans; per-model and per-tool stats with p50/p95 and an hourly timeline; conversations.
- Query API with filters and cursor paging, optional read token, Prometheus metrics, health check.
- Viewer page (live API or static export), seeded demo traffic from three services via the OpenTelemetry SDK, `genai-traces` CLI (serve, demo, prune, export-static), ingest benchmark.
- Docker image on port 4318, docker compose with PostgreSQL, CI on Python 3.10-3.12 plus a PostgreSQL job and a Docker OTLP smoke test, CodeQL, Dependabot, Pages demo, Codespaces and Render kits.

[Unreleased]: https://github.com/AkibUllah/genai-trace-explorer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AkibUllah/genai-trace-explorer/releases/tag/v0.1.0
