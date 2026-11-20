# Prices

ccmeter estimates what your usage would cost through the API, at list prices. With a Claude or
ChatGPT subscription you do not pay per token: the dashboard answers "what would this cost
through the API", which is useful to compare plans, spot expensive sessions and see where the
tokens go.

## The price book

`src/ccmeter/server/prices.json` has one entry per model family, in US dollars per million
tokens:

```json
"sonnet-4-5": {
  "match": ["sonnet-4-5"],
  "input": 3.0,
  "output": 15.0,
  "cache_read": 0.3,
  "cache_write_5m": 3.75,
  "cache_write_1h": 6.0,
  "verified": "2026-10-05"
}
```

- **Matching.** Entries are tried in order and the first whose `match` text appears in the
  lowercase model name wins, so specific names (`opus-4-8`) must come before generic ones
  (`opus`). `"match_exact": true` requires the whole name instead.
- **Cache writes.** Claude reports 5-minute and 1-hour cache writes separately and each has its
  price. When a transcript gives only the total, the 1-hour price is used (the higher one).
- **Per-request prices.** An entry can have a `long_context` block: above
  `threshold_input_tokens` (input plus cached input and cache writes of that single request)
  the whole request is priced at the long-context rates. OpenAI entries use it with one
  `cache_write` rate (threshold 272,000); Claude Haiku 5.5 uses it with `cache_write_5m` and
  `cache_write_1h` (threshold 100,000). These models are priced request by request.
- **`<synthetic>`** messages (written by Claude Code itself, not by the API) cost nothing.
- **Unknown models** have no price: the dashboard lists them and leaves them out of the totals.
  Add an entry and the next refresh prices the whole history.

`verified` is the date the entry was checked against the provider's public price list. Prices
change: check the providers' pricing pages before trusting an old entry.

## Your own prices

```bash
cp "$(python -c 'import ccmeter.server.pricing as p; print(p.BUNDLED_PRICES_PATH)')" my-prices.json
ccmeter server run --prices my-prices.json        # or CCMETER_PRICES_PATH=my-prices.json
```

Pull requests that add or update entries are welcome: include the source page and the date.
