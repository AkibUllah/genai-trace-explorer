"""Demo traffic: three instrumented services, emitted with the real OpenTelemetry SDK.

Spans carry the attributes GenAI instrumentation libraries set (operation, provider, models,
token usage, tool names, conversation ids), with explicit timestamps so a few days of traffic
are generated in well under a second. Everything is seeded, so the same seed gives the same
traces, ids included.

- ``wallet-support-agent``: a support agent for a fictional mobile wallet. Multi-turn
  conversations; tool calls that sometimes time out and are retried; a rate-limited model
  call that falls back to a smaller model; now and then a run that gives up.
- ``faq-rag``: an HTTP endpoint that embeds the question, searches a vector index and answers.
- ``statement-summarizer``: a nightly batch job on a self-hosted model (no per-token price).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, set_span_in_context

MS = 1_000_000

QUESTIONS = [
    ("cash_out_failed", "My cash out to agent failed but the balance was deducted"),
    ("refund", "I sent 2,500 taka to the wrong number, can I get it back?"),
    ("kyc", "Why is my account limited? I already uploaded my NID"),
    ("statement", "Please send my statement for last month"),
    ("pin_reset", "I forgot my PIN"),
    ("merchant_fee", "Why was I charged a fee at the merchant?"),
]
TOOLS_FOR = {
    "cash_out_failed": ["lookup_transaction", "check_agent_status", "create_ticket"],
    "refund": ["lookup_transaction", "initiate_reversal"],
    "kyc": ["check_kyc_status"],
    "statement": ["generate_statement"],
    "pin_reset": ["send_pin_reset_link"],
    "merchant_fee": ["lookup_transaction", "get_fee_schedule"],
}
FAQ = [
    "What are the cash out charges?",
    "How do I add money from my bank card?",
    "Is there a daily send money limit?",
    "How long does a reversal take?",
    "How do I change my registered mobile number?",
]


class SeededIds(IdGenerator):
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def generate_span_id(self) -> int:
        return self.rng.getrandbits(64) or 1

    def generate_trace_id(self) -> int:
        return self.rng.getrandbits(128) or 1


@dataclass
class Clock:
    """Hands out timestamps; ``at`` is where the next span starts, in ns."""

    at: int

    def advance(self, ms: float) -> int:
        self.at += int(ms * MS)
        return self.at


class Emitter:
    def __init__(self, processor_factory: Callable[[], SpanProcessor], seed: int) -> None:
        self.rng = random.Random(seed)
        self.providers: dict[str, TracerProvider] = {}
        self.processor_factory = processor_factory

    def tracer(self, service: str):
        if service not in self.providers:
            provider = TracerProvider(
                resource=Resource.create(
                    {"service.name": service, "deployment.environment": "demo"}
                ),
                id_generator=SeededIds(self.rng),
            )
            provider.add_span_processor(self.processor_factory())
            self.providers[service] = provider
        return self.providers[service].get_tracer("genai_traces.demo")

    def shutdown(self) -> None:
        for provider in self.providers.values():
            provider.force_flush()
            provider.shutdown()


def _start(tracer, name, clock: Clock, parent: Span | None, kind=SpanKind.INTERNAL, attrs=None):
    ctx = set_span_in_context(parent) if parent is not None else None
    return tracer.start_span(
        name, context=ctx, kind=kind, attributes=attrs or {}, start_time=clock.at
    )


def _end(span: Span, clock: Clock, error: str | None = None, error_type: str | None = None):
    if error:
        span.set_status(Status(StatusCode.ERROR, error))
        if error_type:
            span.set_attribute("error.type", error_type)
    span.end(end_time=clock.at)


def _latency(rng: random.Random, low: float, high: float, tail: float = 0.05) -> float:
    ms = rng.uniform(low, high)
    if rng.random() < tail:
        ms *= rng.uniform(2.5, 4.0)  # the slow tail p95 is about
    return ms


def chat(
    em: Emitter,
    tracer,
    clock: Clock,
    parent,
    model: str,
    provider: str,
    in_tok: int,
    error: str | None = None,
    conversation: str | None = None,
) -> None:
    rng = em.rng
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "gen_ai.request.temperature": 0.2,
        "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"..."}]}]',
    }
    if conversation:
        attrs["gen_ai.conversation.id"] = conversation
    span = _start(tracer, f"chat {model}", clock, parent, SpanKind.CLIENT, attrs)
    if error:
        clock.advance(rng.uniform(80, 200))
        _end(span, clock, error, "RateLimitError")
        return
    if model.startswith("local/"):
        clock.advance(_latency(rng, 2500, 7000))  # 8B model on one GPU, long prompts
    elif "small" in model:
        clock.advance(_latency(rng, 250, 900))
    else:
        clock.advance(_latency(rng, 700, 2400))
    out_tok = rng.randint(40, 320)
    span.set_attribute(
        "gen_ai.response.model", model if model.startswith("local/") else f"{model}-2026-08"
    )
    span.set_attribute("gen_ai.usage.input_tokens", in_tok)
    span.set_attribute("gen_ai.usage.output_tokens", out_tok)
    span.set_attribute("gen_ai.response.finish_reasons", ["stop"])
    _end(span, clock)


def tool(em: Emitter, tracer, clock: Clock, parent, name: str, fail_rate: float) -> bool:
    rng = em.rng
    span = _start(
        tracer,
        f"execute_tool {name}",
        clock,
        parent,
        attrs={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": f"call_{rng.getrandbits(40):010x}",
            "gen_ai.tool.call.arguments": '{"wallet": "01712345678", "txn_id": "TXN8KQ2P"}',
        },
    )
    if rng.random() < fail_rate:
        clock.advance(5000)
        _end(span, clock, f"{name} did not answer within 5 s", "TimeoutError")
        return False
    clock.advance(_latency(rng, 30, 380, tail=0.08))
    _end(span, clock)
    return True


def support_turn(em: Emitter, clock: Clock, conversation: str, topic: str, turn: int) -> None:
    rng = em.rng
    tracer = em.tracer("wallet-support-agent")
    root = _start(
        tracer,
        "invoke_agent wallet_support",
        clock,
        None,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "wallet_support",
            "gen_ai.conversation.id": conversation,
            "app.turn": turn,
            "app.topic": topic,
        },
    )
    clock.advance(rng.uniform(3, 12))
    context_tokens = 900 + 450 * turn
    gave_up = rng.random() < 0.04
    if rng.random() < 0.08:
        # primary model rate-limited: fall back to the small one for this step
        chat(
            em,
            tracer,
            clock,
            root,
            "chat-large",
            "openai",
            context_tokens,
            error="429 rate limited",
        )
        clock.advance(rng.uniform(5, 20))
        chat(
            em,
            tracer,
            clock,
            root,
            "chat-small",
            "openai",
            context_tokens,
            conversation=conversation,
        )
    else:
        chat(
            em,
            tracer,
            clock,
            root,
            "chat-large",
            "openai",
            context_tokens,
            conversation=conversation,
        )
    for name in TOOLS_FOR[topic]:
        clock.advance(rng.uniform(2, 8))
        fail_rate = 0.25 if name == "check_agent_status" else 0.04
        if not tool(em, tracer, clock, root, name, fail_rate):
            clock.advance(rng.uniform(200, 400))  # backoff, then one retry
            if not tool(em, tracer, clock, root, name, fail_rate) and gave_up:
                break
        context_tokens += rng.randint(120, 600)
        clock.advance(rng.uniform(2, 8))
        chat(
            em,
            tracer,
            clock,
            root,
            "chat-large",
            "openai",
            context_tokens,
            conversation=conversation,
        )
    clock.advance(rng.uniform(3, 10))
    if gave_up:
        _end(root, clock, "agent stopped after reaching the step limit", "MaxStepsExceeded")
    else:
        _end(root, clock)


def faq_request(em: Emitter, clock: Clock) -> None:
    rng = em.rng
    tracer = em.tracer("faq-rag")
    question = rng.choice(FAQ)
    root = _start(
        tracer,
        "POST /ask",
        clock,
        None,
        SpanKind.SERVER,
        {"http.request.method": "POST", "http.route": "/ask", "app.question": question},
    )
    clock.advance(rng.uniform(1, 4))
    emb = _start(
        tracer,
        "embeddings embed-small",
        clock,
        root,
        SpanKind.CLIENT,
        {
            "gen_ai.operation.name": "embeddings",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "embed-small",
            "gen_ai.usage.input_tokens": len(question.split()) * 2,
        },
    )
    clock.advance(_latency(rng, 40, 120))
    _end(emb, clock)
    search = _start(
        tracer,
        "retrieval faq_chunks",
        clock,
        root,
        attrs={
            "gen_ai.operation.name": "retrieval",
            "db.system.name": "postgresql",
            "app.top_k": 5,
        },
    )
    clock.advance(_latency(rng, 8, 35))
    _end(search, clock)
    chat(em, tracer, clock, root, "chat-small", "openai", rng.randint(700, 1600))
    clock.advance(rng.uniform(1, 3))
    root.set_attribute("http.response.status_code", 200)
    _end(root, clock)


def nightly_summary(em: Emitter, clock: Clock) -> None:
    rng = em.rng
    tracer = em.tracer("statement-summarizer")
    root = _start(tracer, "summarize_statements", clock, None, attrs={"app.batch_size": 3})
    for _ in range(3):
        clock.advance(rng.uniform(5, 20))
        chat(em, tracer, clock, root, "local/llama3.1:8b", "ollama", rng.randint(2500, 6000))
    clock.advance(rng.uniform(5, 20))
    _end(root, clock)


def generate(
    processor_factory: Callable[[], SpanProcessor],
    end_ns: int,
    hours: int = 48,
    conversations: int = 40,
    faq_requests: int = 60,
    seed: int = 7,
) -> int:
    """Emit demo traffic spread over ``hours`` ending at ``end_ns``; returns the trace count."""
    em = Emitter(processor_factory, seed)
    rng = em.rng
    span_ns = hours * 3600 * 1_000 * MS
    jobs: list[tuple[int, str, tuple]] = []
    for i in range(conversations):
        topic = rng.choice(QUESTIONS)[0]
        start = end_ns - rng.randint(span_ns // 20, span_ns)
        for turn in range(1, rng.choice([1, 1, 2, 2, 3, 4]) + 1):
            jobs.append((start, "support", (f"conv-{i + 1:04d}", topic, turn)))
            start += rng.randint(40, 300) * 1_000 * MS  # the customer replies a bit later
    for _ in range(faq_requests):
        jobs.append((end_ns - rng.randint(0, span_ns), "faq", ()))
    for night in range(max(1, hours // 24)):
        jobs.append(
            (end_ns - (night + 1) * 24 * 3600 * 1_000 * MS + 2 * 3600 * 1_000 * MS, "batch", ())
        )
    jobs.sort(key=lambda j: j[0])
    for start, kind, args in jobs:
        clock = Clock(min(start, end_ns - 60_000 * MS))
        if kind == "support":
            support_turn(em, clock, *args)
        elif kind == "faq":
            faq_request(em, clock)
        else:
            nightly_summary(em, clock)
    em.shutdown()
    return len(jobs)


class _KeepOpen(SpanExporter):
    """Shares one exporter between several providers: their shutdown must not close it."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans):
        return self.inner.export(spans)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)

    def shutdown(self) -> None:
        pass


def processor_for(exporter: SpanExporter) -> Callable[[], SpanProcessor]:
    """One batch processor per service, all sending through the same exporter."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    shared = _KeepOpen(exporter)
    return lambda: BatchSpanProcessor(shared, max_export_batch_size=256, schedule_delay_millis=200)
