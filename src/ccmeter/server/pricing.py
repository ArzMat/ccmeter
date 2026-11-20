from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

TOKENS_PER_MILLION = 1_000_000
# List prices shipped with the package; a different file can be passed to the server.
BUNDLED_PRICES_PATH = Path(__file__).with_name("prices.json")
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class UsageCounts:
    """Token counters required to calculate model cost."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_total: int


@dataclass(frozen=True)
class LongContextPrice:
    """Rates for a request whose prompt (input plus cache) is above the threshold.

    OpenAI entries give one `cache_write` rate; Anthropic entries priced by prompt length
    (Claude Haiku 5.5) give `cache_write_5m` and `cache_write_1h` instead.
    """

    threshold_input_tokens: int
    input: float
    output: float
    cache_read: float
    cache_write: float | None
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None

    @property
    def split_cache_writes(self) -> bool:
        return self.cache_write_5m is not None and self.cache_write_1h is not None


@dataclass(frozen=True)
class PriceEntry:
    """Pricing entry for one logical model family.

    OpenAI (Codex) entries have `match_exact` and a single `cache_write` rate. Entries with
    `long_context` (OpenAI, Claude Haiku 5.5) must be priced per single request, because the
    threshold applies to each request's prompt (see `cost_usd`).
    """

    match: list[str]
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    verified: str
    match_exact: bool = False
    cache_write: float | None = None
    long_context: LongContextPrice | None = None

    @property
    def per_request(self) -> bool:
        """True if the price depends on the single request (long context threshold)."""
        return self.long_context is not None


@dataclass(frozen=True)
class PriceBook:
    """Loaded model price book.

    `match_cache` memoizes model name -> matched entry: the dashboard prices hundreds of
    thousands of rows and the substring matching would otherwise be redone for each one.
    """

    models: dict[str, PriceEntry]
    match_cache: dict[str, PriceEntry | None] = field(
        default_factory=dict[str, PriceEntry | None], compare=False, repr=False
    )


@dataclass(frozen=True)
class CostBreakdown:
    """Detailed USD cost breakdown for one usage aggregate."""

    input_usd: float
    output_usd: float
    cache_read_usd: float
    cache_write_usd: float
    total_usd: float
    used_cache_write_fallback: bool


def load_price_book(path: Path) -> PriceBook:
    """Load a price book from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    data = cast(dict[str, Any], raw)
    raw_models = cast(dict[str, dict[str, Any]], data["models"])
    models: dict[str, PriceEntry] = {}
    for name, entry in raw_models.items():
        cache_write = _optional_float(entry.get("cache_write"))
        models[name] = PriceEntry(
            match=[str(value) for value in cast(list[object], entry["match"])],
            input=float(entry["input"]),
            output=float(entry["output"]),
            cache_read=float(entry["cache_read"]),
            # OpenAI entries have no 5m/1h split: these values are never used (per-request price)
            cache_write_5m=float(entry.get("cache_write_5m", cache_write or 0.0)),
            cache_write_1h=float(entry.get("cache_write_1h", cache_write or 0.0)),
            verified=str(entry["verified"]),
            match_exact=bool(entry.get("match_exact", False)),
            cache_write=cache_write,
            long_context=_long_context(entry.get("long_context")),
        )
    return PriceBook(models=models)


def cost_usd(model: str, usage: UsageCounts, prices: PriceBook) -> CostBreakdown | None:
    """Calculate USD cost for a usage aggregate, if the model is priced.

    For entries with `long_context` (OpenAI/Codex, Claude Haiku 5.5) `usage` must be ONE
    request: the input token threshold is evaluated on the single request, so aggregates
    must be priced row by row and then summed.
    """
    if is_synthetic_model(model):
        return None

    price = _match_price(model, prices)
    if price is None:
        return None
    long_context = price.long_context
    if long_context is not None and long_context.split_cache_writes:
        # Anthropic rates by prompt length: the request's tier, then the usual 5m/1h pricing
        if _prompt_tokens(usage) > long_context.threshold_input_tokens:
            price = replace(
                price,
                input=long_context.input,
                output=long_context.output,
                cache_read=long_context.cache_read,
                cache_write_5m=cast(float, long_context.cache_write_5m),
                cache_write_1h=cast(float, long_context.cache_write_1h),
            )
    elif long_context is not None:
        return _per_request_cost(usage, price, long_context)

    input_usd = _priced(usage.input_tokens, price.input)
    output_usd = _priced(usage.output_tokens, price.output)
    cache_read_usd = _priced(usage.cache_read_tokens, price.cache_read)
    cache_write_usd, used_fallback = _cache_write_cost(usage, price)
    total = input_usd + output_usd + cache_read_usd + cache_write_usd
    return CostBreakdown(
        input_usd=input_usd,
        output_usd=output_usd,
        cache_read_usd=cache_read_usd,
        cache_write_usd=cache_write_usd,
        total_usd=total,
        used_cache_write_fallback=used_fallback,
    )


def is_synthetic_model(model: str) -> bool:
    """Return whether a model is local synthetic usage, not billable API usage."""
    return model == SYNTHETIC_MODEL


def is_per_request_priced(model: str, prices: PriceBook) -> bool:
    """Return whether the model must be priced request by request (long-context threshold)."""
    price = _match_price(model, prices)
    return price is not None and price.per_request


def _match_price(model: str, prices: PriceBook) -> PriceEntry | None:
    if model in prices.match_cache:
        return prices.match_cache[model]
    entry = _scan_price(model, prices)
    prices.match_cache[model] = entry
    return entry


def _scan_price(model: str, prices: PriceBook) -> PriceEntry | None:
    normalized = model.lower()
    for entry in prices.models.values():
        if entry.match_exact:
            if any(alias.lower() == normalized for alias in entry.match):
                return entry
        elif any(alias.lower() in normalized for alias in entry.match):
            return entry
    return None


def _per_request_cost(
    usage: UsageCounts,
    price: PriceEntry,
    long_context: LongContextPrice,
) -> CostBreakdown:
    if _prompt_tokens(usage) > long_context.threshold_input_tokens:
        input_rate = long_context.input
        output_rate = long_context.output
        cache_read_rate = long_context.cache_read
        cache_write_rate = long_context.cache_write
    else:
        input_rate = price.input
        output_rate = price.output
        cache_read_rate = price.cache_read
        cache_write_rate = price.cache_write
    used_fallback = cache_write_rate is None and usage.cache_write_total > 0
    if cache_write_rate is None:
        # no published cache write rate (gpt-5.5): the input rate is used
        cache_write_rate = input_rate
    input_usd = _priced(usage.input_tokens, input_rate)
    output_usd = _priced(usage.output_tokens, output_rate)
    cache_read_usd = _priced(usage.cache_read_tokens, cache_read_rate)
    cache_write_usd = _priced(usage.cache_write_total, cache_write_rate)
    return CostBreakdown(
        input_usd=input_usd,
        output_usd=output_usd,
        cache_read_usd=cache_read_usd,
        cache_write_usd=cache_write_usd,
        total_usd=input_usd + output_usd + cache_read_usd + cache_write_usd,
        used_cache_write_fallback=used_fallback,
    )


def _prompt_tokens(usage: UsageCounts) -> int:
    """A request's prompt length: every input token, cache reads and writes included."""
    return usage.input_tokens + usage.cache_read_tokens + usage.cache_write_total


def _long_context(raw: object) -> LongContextPrice | None:
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    return LongContextPrice(
        threshold_input_tokens=int(data["threshold_input_tokens"]),
        input=float(data["input"]),
        output=float(data["output"]),
        cache_read=float(data["cache_read"]),
        cache_write=_optional_float(data.get("cache_write")),
        cache_write_5m=_optional_float(data.get("cache_write_5m")),
        cache_write_1h=_optional_float(data.get("cache_write_1h")),
    )


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _cache_write_cost(usage: UsageCounts, price: PriceEntry) -> tuple[float, bool]:
    if usage.cache_write_5m == 0 and usage.cache_write_1h == 0 and usage.cache_write_total > 0:
        return _priced(usage.cache_write_total, price.cache_write_1h), True
    cost = _priced(usage.cache_write_5m, price.cache_write_5m)
    cost += _priced(usage.cache_write_1h, price.cache_write_1h)
    return cost, False


def _priced(tokens: int, usd_per_million: float) -> float:
    return tokens * usd_per_million / TOKENS_PER_MILLION
