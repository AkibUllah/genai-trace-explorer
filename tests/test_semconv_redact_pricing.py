import pytest

from genai_traces.pricing import PriceTable
from genai_traces.redact import NOT_CAPTURED, mask_text, scrub_attributes, scrub_events
from genai_traces.semconv import extract


def test_current_attribute_names():
    f = extract(
        "chat chat-large",
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "chat-large",
            "gen_ai.response.model": "chat-large-2026-08",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
            "gen_ai.conversation.id": "c1",
        },
    )
    assert (f.kind, f.provider, f.model, f.input_tokens, f.output_tokens) == (
        "llm",
        "anthropic",
        "chat-large-2026-08",
        10,
        5,
    )
    assert f.conversation_id == "c1"


def test_legacy_attribute_names_are_read():
    f = extract(
        "openai.chat",
        {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "chat-small",
            "gen_ai.usage.prompt_tokens": 7,
            "gen_ai.usage.completion_tokens": 3,
            "session.id": "s9",
        },
    )
    assert (f.kind, f.provider, f.input_tokens, f.output_tokens) == ("llm", "openai", 7, 3)
    assert f.conversation_id == "s9"


@pytest.mark.parametrize(
    "attrs,kind",
    [
        ({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "t"}, "tool"),
        ({"gen_ai.tool.name": "t"}, "tool"),
        ({"gen_ai.operation.name": "embeddings"}, "embeddings"),
        ({"gen_ai.operation.name": "invoke_agent"}, "agent"),
        ({"gen_ai.agent.name": "planner"}, "agent"),
        ({"gen_ai.operation.name": "retrieval"}, "retrieval"),
        ({"http.method": "GET"}, "other"),
    ],
)
def test_kind_classification(attrs, kind):
    assert extract("x", attrs).kind == kind


def test_bad_token_counts_are_ignored():
    f = extract("x", {"gen_ai.usage.input_tokens": "lots", "gen_ai.usage.output_tokens": -3})
    assert f.input_tokens is None and f.output_tokens is None


def test_mask_text():
    text = (
        "mail rahim@example.com.bd or call +880 1712-345678 / 01812345678, card 4111 1111 1111 1111"
    )
    assert mask_text(text) == "mail [email] or call [phone] / [phone], card [card ****1111]"
    assert mask_text("order 1234567890123 total 4500") == "order 1234567890123 total 4500"


def test_content_dropped_unless_captured():
    attrs = {
        "gen_ai.input.messages": "call 01712345678",
        "gen_ai.prompt.0.content": "hello",
        "gen_ai.request.model": "chat-small",
        "user.email": "a@b.co",
    }
    dropped = scrub_attributes(attrs, capture_content=False, redact_pii=True)
    assert dropped["gen_ai.input.messages"] == NOT_CAPTURED
    assert dropped["gen_ai.prompt.0.content"] == NOT_CAPTURED
    assert dropped["gen_ai.request.model"] == "chat-small"
    assert dropped["user.email"] == "[email]"

    kept = scrub_attributes(attrs, capture_content=True, redact_pii=True)
    assert kept["gen_ai.input.messages"] == "call [phone]"
    raw = scrub_attributes(attrs, capture_content=True, redact_pii=False)
    assert raw == attrs


def test_content_events():
    events = [
        {"name": "gen_ai.user.message", "time_ns": 1, "attributes": {"content": "hi"}},
        {"name": "exception", "time_ns": 2, "attributes": {"exception.message": "x@y.org failed"}},
    ]
    out = scrub_events(events, capture_content=False, redact_pii=True)
    assert out[0]["attributes"] == {"content": NOT_CAPTURED}
    assert out[1]["attributes"] == {"exception.message": "[email] failed"}


def test_bundled_prices_and_most_specific_pattern():
    table = PriceTable.load()
    assert table.cost("chat-large-2026-08", "chat-large", 1_000_000, 100_000) == 3.5
    custom = PriceTable.from_mapping(
        {"models": {"chat-*": {"input": 1, "output": 1}, "chat-large*": {"input": 5, "output": 5}}}
    )
    assert custom.match("chat-large-x").pattern == "chat-large*"
    assert custom.match("chat-tiny").pattern == "chat-*"


def test_cost_falls_back_to_request_model_and_handles_unknowns():
    table = PriceTable.load()
    assert table.cost("vendor-renamed-model", "chat-small", 2_000_000, 0) == 0.3
    assert table.cost("mystery", None, 100, 100) is None
    assert table.cost("chat-small", None, None, None) is None
    assert table.cost("local/llama3.1:8b", None, 5000, 500) == 0.0


@pytest.mark.parametrize(
    "data",
    [
        {"models": ["a"]},
        {"models": {"a": {"input": 1}}},
        {"models": {"a": {"input": "x", "output": 1}}},
        {"models": {"a": {"input": -1, "output": 1}}},
    ],
)
def test_invalid_price_tables(data):
    with pytest.raises(ValueError):
        PriceTable.from_mapping(data)
