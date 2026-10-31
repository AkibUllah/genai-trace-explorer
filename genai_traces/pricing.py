"""Token prices and cost calculation."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path

import yaml

PER = 1_000_000


@dataclass(frozen=True)
class Price:
    pattern: str
    input: float
    output: float


class PriceTable:
    def __init__(self, prices: list[Price]) -> None:
        # Longest pattern first: "chat-large-2026*" beats "chat-large*".
        self.prices = sorted(prices, key=lambda p: len(p.pattern.replace("*", "")), reverse=True)

    @classmethod
    def from_mapping(cls, data: dict) -> PriceTable:
        models = (data or {}).get("models") or {}
        if not isinstance(models, dict):
            raise ValueError("pricing: 'models' must be a mapping of pattern -> {input, output}")
        prices = []
        for pattern, entry in models.items():
            try:
                price = Price(str(pattern), float(entry["input"]), float(entry["output"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"pricing: {pattern!r} needs numeric input and output") from exc
            if price.input < 0 or price.output < 0:
                raise ValueError(f"pricing: {pattern!r} has a negative price")
            prices.append(price)
        return cls(prices)

    @classmethod
    def load(cls, path: Path | None = None) -> PriceTable:
        if path is None:
            text = resources.files("genai_traces").joinpath("pricing.yaml").read_text("utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
        return cls.from_mapping(yaml.safe_load(text))

    def match(self, *models: str | None) -> Price | None:
        for model in models:
            if not model:
                continue
            for price in self.prices:
                if fnmatchcase(model, price.pattern):
                    return price
        return None

    def cost(
        self,
        response_model: str | None,
        request_model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        """USD cost, or None when the model has no price or the span reported no usage."""
        if input_tokens is None and output_tokens is None:
            return None
        price = self.match(response_model, request_model)
        if price is None:
            return None
        total = (input_tokens or 0) * price.input + (output_tokens or 0) * price.output
        return round(total / PER, 8)
