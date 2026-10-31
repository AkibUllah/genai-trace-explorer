"""Ingest throughput: demo traffic encoded as OTLP protobuf batches, decoded and stored.

    python bench/bench.py                                   # SQLite file in a temp dir
    python bench/bench.py --url postgresql+psycopg://traces:traces@localhost:5432/traces

Measures the collector's own work per export (decode, classify, scrub, price, insert,
recompute the summaries of the touched traces), not HTTP.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genai_traces.demo import generate, processor_for  # noqa: E402
from genai_traces.ingest import Ingestor  # noqa: E402
from genai_traces.otlp import flatten, parse_request  # noqa: E402
from genai_traces.pricing import PriceTable  # noqa: E402
from genai_traces.store import Store, make_engine, metadata  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="database URL (default: a fresh SQLite file)")
    parser.add_argument("--conversations", type=int, default=400)
    parser.add_argument("--faq", type=int, default=600)
    parser.add_argument("--batch", type=int, default=512, help="spans per export, the SDK default")
    args = parser.parse_args()

    tmp = tempfile.TemporaryDirectory()
    url = args.url or f"sqlite:///{tmp.name}/bench.db"
    memory = InMemorySpanExporter()
    generate(processor_for(memory), time.time_ns(), 72, args.conversations, args.faq, seed=11)
    spans = list(memory.get_finished_spans())
    bodies = [
        encode_spans(spans[i : i + args.batch]).SerializeToString()
        for i in range(0, len(spans), args.batch)
    ]

    engine = make_engine(url)
    metadata.drop_all(engine)
    store = Store(engine)
    ingestor = Ingestor(store, PriceTable.load())
    started = time.perf_counter()
    for body in bodies:
        raw, _ = flatten(parse_request(body, "application/x-protobuf"))
        ingestor.ingest(raw)
    elapsed = time.perf_counter() - started
    counts = store.counts()
    print(
        f"{store.dialect}: {len(spans)} spans / {counts['traces']} traces in {len(bodies)} "
        f"exports of {args.batch}: {elapsed:.2f} s, {len(spans) / elapsed:,.0f} spans/s, "
        f"{elapsed / len(bodies) * 1000:.0f} ms per export"
    )
    metadata.drop_all(engine)
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
