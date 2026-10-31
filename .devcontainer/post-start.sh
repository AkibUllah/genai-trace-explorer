#!/usr/bin/env bash
# Codespaces start hook: collector + viewer on :4318 (SQLite), then demo traffic over OTLP.
set -euo pipefail
cd "$(dirname "$0")/.."

nohup uvicorn genai_traces.asgi:app --host 0.0.0.0 --port 4318 > /tmp/collector.log 2>&1 &

echo "waiting for the collector on :4318 ..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:4318/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Seeded demo traffic is idempotent: re-sent spans are recognised and skipped.
if [ ! -f .demo-seeded ]; then
  genai-traces demo && touch .demo-seeded
fi

cat <<'MSG'

GenAI Trace Explorer is up on forwarded port 4318.
  Viewer:     /
  API docs:   /docs
  Your app:   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python your_app.py
MSG
