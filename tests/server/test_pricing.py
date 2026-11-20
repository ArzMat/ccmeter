from __future__ import annotations

import pytest

from ccmeter.server.pricing import (
    BUNDLED_PRICES_PATH,
    UsageCounts,
    cost_usd,
    is_per_request_priced,
    load_price_book,
)


def test_cost_usd_prices_fable_with_cache_split() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    usage = UsageCounts(
        input_tokens=12_864,
        output_tokens=6_079,
        cache_read_tokens=23_938,
        cache_write_5m=0,
        cache_write_1h=7_355,
        cache_write_total=7_355,
    )

    cost = cost_usd("claude-fable-5", usage, prices)

    assert cost is not None
    assert cost.input_usd == pytest.approx(0.12864)
    assert cost.output_usd == pytest.approx(0.30395)
    assert cost.cache_read_usd == pytest.approx(0.023938)
    assert cost.cache_write_usd == pytest.approx(0.1471)
    assert cost.total_usd == pytest.approx(0.603628)


def test_cost_usd_falls_back_to_1h_cache_when_split_is_missing() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    usage = UsageCounts(
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_5m=0,
        cache_write_1h=0,
        cache_write_total=1_000_000,
    )

    cost = cost_usd("claude-fable-5", usage, prices)

    assert cost is not None
    assert cost.cache_write_usd == pytest.approx(20.0)
    assert cost.used_cache_write_fallback is True


def test_cost_usd_returns_none_for_unknown_and_synthetic_models() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    usage = UsageCounts(
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=1,
        cache_write_5m=1,
        cache_write_1h=1,
        cache_write_total=2,
    )

    assert cost_usd("unknown-model", usage, prices) is None
    assert cost_usd("<synthetic>", usage, prices) is None


def _codex_usage(*, non_cached: int, cached: int, output: int, cache_write: int = 0) -> UsageCounts:
    return UsageCounts(
        input_tokens=non_cached,
        output_tokens=output,
        cache_read_tokens=cached,
        cache_write_5m=0,
        cache_write_1h=0,
        cache_write_total=cache_write,
    )


def test_codex_request_at_threshold_uses_standard_rates() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    # 200k non-cached + 72k cached = 272000: does not exceed the threshold
    usage = _codex_usage(non_cached=200_000, cached=72_000, output=10_000)

    cost = cost_usd("gpt-6-astra", usage, prices)

    assert cost is not None
    assert cost.input_usd == pytest.approx(200_000 * 10 / 1e6)
    assert cost.cache_read_usd == pytest.approx(72_000 * 1 / 1e6)
    assert cost.output_usd == pytest.approx(10_000 * 50 / 1e6)
    assert cost.total_usd == pytest.approx(2.0 + 0.072 + 0.5)


def test_codex_request_over_threshold_uses_long_rates_for_whole_request() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    usage = _codex_usage(non_cached=200_001, cached=72_000, output=10_000)

    cost = cost_usd("gpt-6-astra", usage, prices)

    assert cost is not None
    assert cost.input_usd == pytest.approx(200_001 * 20 / 1e6)
    assert cost.cache_read_usd == pytest.approx(72_000 * 2 / 1e6)
    assert cost.output_usd == pytest.approx(10_000 * 75 / 1e6)


def test_codex_threshold_counts_cache_write_as_input() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    usage = _codex_usage(non_cached=100_000, cached=172_000, output=0, cache_write=1)

    cost = cost_usd("gpt-5.6-terra", usage, prices)

    assert cost is not None
    assert cost.input_usd == pytest.approx(100_000 * 4 / 1e6)
    assert cost.cache_write_usd == pytest.approx(1 * 5 / 1e6)


def test_codex_models_match_exactly_not_by_substring() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    usage = _codex_usage(non_cached=0, cached=100_000, output=0)

    sol_61 = cost_usd("gpt-6.1-sol", usage, prices)
    sol_6 = cost_usd("gpt-6-sol", usage, prices)

    assert sol_61 is not None and sol_6 is not None
    assert sol_61.cache_read_usd == pytest.approx(0.01)
    assert sol_6.cache_read_usd == pytest.approx(0.02)
    assert cost_usd("gpt-5.5-pro", usage, prices) is None
    assert cost_usd("gpt-5.5", usage, prices) is not None
    assert cost_usd("codex-auto-review", usage, prices) is None
    assert cost_usd("codex-unknown", usage, prices) is None


def test_codex_table_rates_for_every_model() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)
    expected = {
        "gpt-6.2-astra": (8, 0.4, 10, 40, 16, 0.8, 20, 60),
        "gpt-6.2-sol": (2, 0.1, 2.5, 10, 4, 0.2, 5, 15),
        "gpt-6.1-terra": (1, 0.05, 1.25, 5, 2, 0.1, 2.5, 7.5),
        "gpt-6-astra": (10, 1, 12.5, 50, 20, 2, 25, 75),
        "gpt-6.1-sol": (2, 0.1, 2.5, 10, 4, 0.2, 5, 15),
        "gpt-6-sol": (2, 0.2, 2.5, 10, 4, 0.4, 5, 15),
        "gpt-6-luna": (0.1, 0.01, 0.125, 0.5, 0.2, 0.02, 0.25, 0.75),
        "gpt-5.6-sol": (4, 0.4, 5, 20, 8, 0.8, 10, 30),
        "gpt-5.6-terra": (2, 0.2, 2.5, 12, 4, 0.4, 5, 18),
        "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2, 0.4, 0.04, 0.5, 1.8),
        "gpt-5.5": (5, 0.5, 5, 30, 10, 1, 10, 45),
    }
    million = 1_000_000
    for model, rates in expected.items():
        small = cost_usd(model, _codex_usage(non_cached=million, cached=0, output=0), prices)
        cached = _codex_usage(non_cached=0, cached=million, output=0)
        small_cached = cost_usd(model, cached, prices)
        write = cost_usd(
            model, _codex_usage(non_cached=0, cached=0, output=0, cache_write=1), prices
        )
        out = cost_usd(model, _codex_usage(non_cached=0, cached=0, output=million), prices)
        assert small is not None and small_cached is not None
        assert write is not None and out is not None
        # 1M input tokens exceeds the threshold: long context rate
        assert small.input_usd == pytest.approx(rates[4]), model
        assert small_cached.cache_read_usd == pytest.approx(rates[5]), model
        assert write.cache_write_usd == pytest.approx(rates[2] / million), model
        assert out.output_usd == pytest.approx(rates[3]), model
        short = cost_usd(model, _codex_usage(non_cached=100_000, cached=0, output=0), prices)
        assert short is not None
        assert short.input_usd == pytest.approx(rates[0] / 10), model


def test_gpt_5_5_cache_write_falls_back_to_input_rate() -> None:
    prices = load_price_book(BUNDLED_PRICES_PATH)

    cost = cost_usd(
        "gpt-5.5", _codex_usage(non_cached=0, cached=0, output=0, cache_write=1_000_000), prices
    )

    assert cost is not None
    # 1M of cache write exceeds the threshold: long context input rate (10)
    assert cost.cache_write_usd == pytest.approx(10.0)
    assert cost.used_cache_write_fallback is True


def _usage(*, inp: int = 0, out: int = 0, read: int = 0, w5m: int = 0, w1h: int = 0) -> UsageCounts:
    return UsageCounts(
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=read,
        cache_write_5m=w5m,
        cache_write_1h=w1h,
        cache_write_total=w5m + w1h,
    )


@pytest.mark.parametrize(
    ("model", "rates"),
    [
        # input, output, cache read, cache write 5m, cache write 1h (USD per million)
        ("claude-fable-5-2", (8, 40, 0.2, 10, 16)),
        ("claude-fable-5-1", (10, 50, 0.25, 12.5, 20)),
        ("claude-fable-5", (10, 50, 1, 12.5, 20)),
        ("claude-opus-5-5", (4, 20, 0.2, 5, 8)),
        ("claude-sonnet-5-5", (2, 10, 0.1, 2.5, 4)),
        ("claude-sonnet-5", (2, 10, 0.2, 2.5, 4)),
        ("claude-haiku-5-5", (0.1, 0.5, 0.01, 0.125, 0.2)),
        ("claude-haiku-4-5-20251001", (1, 5, 0.1, 1.25, 2)),
    ],
)
def test_claude_rates_match_the_official_table(
    model: str, rates: tuple[float, float, float, float, float]
) -> None:
    """Each model gets its own entry, never a generic one (fable-5-2 is not fable-5)."""
    prices = load_price_book(BUNDLED_PRICES_PATH)
    tokens = 50_000  # below the Haiku 5.5 prompt-length threshold

    parts = [
        cost_usd(model, _usage(inp=tokens), prices),
        cost_usd(model, _usage(out=tokens), prices),
        cost_usd(model, _usage(read=tokens), prices),
        cost_usd(model, _usage(w5m=tokens), prices),
        cost_usd(model, _usage(w1h=tokens), prices),
    ]

    assert all(part is not None for part in parts)
    got = [part.total_usd * 1_000_000 / tokens for part in parts if part is not None]
    assert got == pytest.approx(list(rates)), model


def test_haiku_5_5_is_priced_by_prompt_length() -> None:
    """Over 100,000 prompt tokens (input plus cache reads and writes) the whole request pays
    the higher rates, cache writes keeping their 5m/1h split."""
    prices = load_price_book(BUNDLED_PRICES_PATH)
    model = "claude-haiku-5-5"
    at_threshold = _usage(inp=10_000, read=80_000, w5m=6_000, w1h=4_000, out=1_000)
    over = _usage(inp=10_001, read=80_000, w5m=6_000, w1h=4_000, out=1_000)

    short = cost_usd(model, at_threshold, prices)
    long = cost_usd(model, over, prices)

    assert is_per_request_priced(model, prices)
    assert short is not None and long is not None
    assert short.total_usd == pytest.approx(
        (10_000 * 0.1 + 1_000 * 0.5 + 80_000 * 0.01 + 6_000 * 0.125 + 4_000 * 0.2) / 1e6
    )
    assert long.total_usd == pytest.approx(
        (10_001 * 0.5 + 1_000 * 2.5 + 80_000 * 0.05 + 6_000 * 0.625 + 4_000 * 1.0) / 1e6
    )
    assert long.used_cache_write_fallback is False
