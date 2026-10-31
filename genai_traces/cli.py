"""``genai-traces`` command line: serve, demo traffic, retention and the static export."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from importlib import resources
from pathlib import Path

from .config import Settings

DAY_NS = 86_400 * 1_000_000_000


def _store(settings: Settings):
    from .store import Store

    return Store.from_url(settings.database_url)


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    uvicorn.run("genai_traces.asgi:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    from .demo import generate, processor_for

    end_ns = time.time_ns()
    if args.direct:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from .ingest import Ingestor
        from .otlp import flatten
        from .pricing import PriceTable

        memory = InMemorySpanExporter()
        count = generate(
            processor_for(memory), end_ns, args.hours, args.conversations, args.faq, args.seed
        )
        spans, _ = flatten(encode_spans(memory.get_finished_spans()))
        store = _store(settings)
        ingestor = Ingestor(
            store,
            PriceTable.load(settings.pricing_file),
            settings.capture_content,
            settings.redact_pii,
        )
        result = ingestor.ingest(spans)
        print(f"{count} traces, {result.accepted} spans written to {store.dialect}")
        return 0

    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    headers = {"authorization": f"Bearer {args.token}"} if args.token else None
    exporter = OTLPSpanExporter(
        endpoint=args.endpoint, headers=headers, compression=Compression.Gzip
    )
    count = generate(
        processor_for(exporter), end_ns, args.hours, args.conversations, args.faq, args.seed
    )
    exporter.shutdown()
    print(f"{count} traces sent to {args.endpoint}")
    return 0


def cmd_prune(args: argparse.Namespace, settings: Settings) -> int:
    days = args.days or settings.retention_days
    removed = _store(settings).prune(time.time_ns() - days * DAY_NS)
    print(f"removed {removed} traces older than {days} days")
    return 0


SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def export_static(settings: Settings, out: Path, hours: int | None = None) -> dict[str, int]:
    """Write the viewer and every API response it needs as files, for static hosting.

    The page reads ``data/<path>.json`` instead of ``/api/<path>`` when its mode meta says
    ``static``; filtering then happens in the browser.
    """
    from . import views
    from .analytics import HOUR_NS
    from .store import TraceFilter

    store = _store(settings)
    items = store.list_traces(TraceFilter(limit=100_000))
    data = out / "data"
    (data / "traces").mkdir(parents=True, exist_ok=True)
    (data / "conversations").mkdir(parents=True, exist_ok=True)

    def write(path: Path, body) -> None:
        path.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")

    write(data / "traces.json", {"items": items, "next_before_ns": None})
    for trace in items:
        write(
            data / "traces" / f"{trace['trace_id']}.json",
            views.trace_detail(store, trace["trace_id"]),
        )
    conversations = sorted({t["conversation_id"] for t in items if t["conversation_id"]})
    exported = 0
    for conversation_id in conversations:
        if SAFE_NAME.match(conversation_id):  # becomes a file name
            write(
                data / "conversations" / f"{conversation_id}.json",
                views.conversation(store, conversation_id),
            )
            exported += 1
    until = max((t["end_ns"] for t in items), default=time.time_ns())
    if hours is None:  # just wide enough for the oldest trace
        oldest = min((t["start_ns"] for t in items), default=until)
        hours = max(1, -(-(until - oldest) // HOUR_NS))
    write(data / "stats.json", views.window_stats(store, hours, until))
    write(data / "services.json", store.services())

    page = resources.files("genai_traces").joinpath("static/index.html").read_text("utf-8")
    page = page.replace(
        '<meta name="gtx-mode" content="api">', '<meta name="gtx-mode" content="static">'
    )
    (out / "index.html").write_text(page, encoding="utf-8")
    return {"traces": len(items), "conversations": exported}


def cmd_export_static(args: argparse.Namespace, settings: Settings) -> int:
    counts = export_static(settings, Path(args.out), args.hours)
    print(
        f"wrote {counts['traces']} traces and {counts['conversations']} conversations to {args.out}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genai-traces", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the collector, API and viewer")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=4318, help="4318 is the standard OTLP/HTTP port")
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser("demo", help="emit demo traffic with the OpenTelemetry SDK")
    target = demo.add_mutually_exclusive_group()
    target.add_argument("--endpoint", default="http://localhost:4318/v1/traces")
    target.add_argument("--direct", action="store_true", help="write into DATABASE_URL, no server")
    demo.add_argument("--token", help="ingest token, if the server requires one")
    demo.add_argument("--hours", type=int, default=48)
    demo.add_argument("--conversations", type=int, default=40)
    demo.add_argument("--faq", type=int, default=60, help="FAQ (RAG) requests")
    demo.add_argument("--seed", type=int, default=7)
    demo.set_defaults(func=cmd_demo)

    prune = sub.add_parser("prune", help="delete traces older than the retention period")
    prune.add_argument("--days", type=int, help="default: RETENTION_DAYS")
    prune.set_defaults(func=cmd_prune)

    export = sub.add_parser("export-static", help="write the viewer and data for static hosting")
    export.add_argument("out")
    export.add_argument(
        "--hours", type=int, default=72, help="stats window, ending at the last trace"
    )
    export.set_defaults(func=cmd_export_static)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, Settings())


if __name__ == "__main__":
    sys.exit(main())
