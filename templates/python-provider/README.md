# Cutie BYO Backtest Provider — Python Template

A minimal, production-shaped **FastAPI** template for wrapping your own backtest
tool as a Cutie backtest provider. It implements the v1 provider HTTP contract
(IMPL W3.9 §4–§9) so the Cutie Connector running on the same OpenClaw / Hermes
machine can discover and run your tool.

You normally only edit **one file**: [`cutie_byo_provider/adapter.py`](./cutie_byo_provider/adapter.py).
Everything else (FastAPI app, Bearer auth, schema models, secret scrub, report
retention, Decimal/JSON helpers) is provided.

## Endpoints

```
GET  /health          # no auth; must not leak secrets / local paths
GET  /catalog         # Bearer auth; cutie.backtest_provider_catalog.v1
POST /cutie/backtest  # Bearer auth; cutie.external_backtest.request/response.v1
GET  /reports/{name}  # serves a local report file (local_machine_only)
```

## Quick start

```bash
pip install -e .            # or: pip install -e '.[test]'
CUTIE_BACKTEST_PROVIDER_TOKEN="local-dev-token" \
  python -m cutie_byo_provider.app
# Provider now runs on http://127.0.0.1:8767

# Health (no auth):
curl http://127.0.0.1:8767/health

# Catalog (Bearer):
curl -H "Authorization: Bearer local-dev-token" http://127.0.0.1:8767/catalog
```

Then validate and register it on the same machine:

```bash
cutie-backtest-provider-validator --base-url http://127.0.0.1:8767 \
  --token local-dev-token --tool-id my.backtester.default \
  --smoke-symbol BTCUSDT --smoke-timeframe 1h \
  --start-at 1771996800 --end-at 1772601600

cutie-connector backtest-tool add --id my.backtester.default \
  --base-url http://127.0.0.1:8767 --api-key local-dev-token --default
cutie-connector backtest-tool refresh
```

## What you fill in (`adapter.py`)

Three functions:

| Function | Returns | Purpose |
|---|---|---|
| `list_tools()` | `list[CatalogTool]` | Describe your tool(s) for `/catalog`. |
| `run_backtest(request)` | `BacktestResult` or `business_failure(...)` dict | Run the backtest, map output to the v1 response. |
| `build_report(result)` | relative `report_url` or `None` | Write a local report file. |

The template ships a runnable "echo" adapter so you can start the server and
pass a smoke request immediately, then replace the marked `TODO` bodies.

### Contract rules the template enforces for you

- **Money / quantity fields are decimal strings** (capital, equity, price, qty,
  cost, fee, pnl, bps) — use `contract.decimal_str`. (IMPL §6.2)
- **Ratio / percentage metrics (`*_pct`) are JSON numbers**, never NaN/Infinity.
- **`report_url` is a relative path/ref only** — never host/port or local path. (IMPL §7)
- **Standard `error_type` codes are UPPERCASE** at the HTTP layer (`NO_DATA`,
  `INVALID_PARAMS`, …). The connector lowercases them before persisting. (IMPL §6.3)
- **Catalog uses `is_default`** (not `default`), has **no `health` field**, and
  declares `cutie.backtest_provider_catalog.v1`. (IMPL §5.1)
- **Secret scrub** runs on every response: API keys, tokens, exchange secrets,
  and absolute local paths are redacted. (IMPL §8.4)

## Three ways to wrap a tool

### 1. Local Python library (`wrapper_type: python_inprocess`)

For a backtest library installed on the OpenClaw / Hermes machine
(e.g. backtesting.py, Backtrader, your own engine).

```python
# adapter.py
def run_backtest(request):
    import my_engine                       # the local library
    bt = request.backtest
    stats = my_engine.run(
        symbol=bt.symbol, timeframe=bt.timeframe,
        start=bt.start_at, end=bt.end_at,
        cash=float(parse_decimal(bt.initial_capital, default=Decimal("10000"))),
        **bt.provider_params,
    )
    return BacktestResult(
        provider_run_id=f"my_{bt.run_id}",
        metrics={
            "total_return_pct": round(stats.return_pct, 2),   # number
            "win_rate_pct": round(stats.win_rate, 2),
            "max_drawdown_pct": round(abs(stats.max_dd), 2),
            "trade_count": stats.n_trades,
        },
        equity_curve=[
            {"t": int(p.ts), "equity": decimal_str(p.equity, places=2)}  # decimal str
            for p in stats.equity
        ],
        trades=[
            {"side": t.side, "entry_at": t.entry, "exit_at": t.exit,
             "pnl": decimal_str(t.pnl, places=2)}
            for t in stats.trades
        ],
        assumptions={"data_source": "provider_reported", "no_live_trading": True},
        # IMPL §9.4: say whether you actually implemented the Cutie draft.
        limitations={"verification": "external_unverified", "verified_by_cutie": False,
                     "strategy_match": "provider_strategy_class_not_verified"},
        raw_report={"provider_summary": stats.summary},
    )
```

### 2. Local CLI (`wrapper_type: local_cli`)

For a command-line tool on the machine (e.g. Freqtrade, LEAN CLI, your own CLI).

```python
# adapter.py
import json, subprocess, tempfile
from pathlib import Path

def run_backtest(request):
    bt = request.backtest
    with tempfile.TemporaryDirectory() as workdir:          # isolated run dir
        cfg = Path(workdir) / "config.json"
        cfg.write_text(json.dumps({                          # never write Cutie token here
            "symbol": bt.symbol, "timeframe": bt.timeframe,
            "start": bt.start_at, "end": bt.end_at,
            **bt.provider_params,
        }))
        proc = subprocess.run(
            ["my-cli", "backtest", "--config", str(cfg), "--out", workdir],
            capture_output=True, text=True, timeout=300,     # timeout + truncate stdout/stderr
        )
        result_files = list(Path(workdir).glob("backtest-result-*.json"))
        if not result_files:
            # No parseable business result -> let connector treat exit as runner failure
            return business_failure(error_type="ENGINE_ERROR",
                                    error_message=(proc.stderr or "no result file")[:500],
                                    provider_name=PROVIDER_NAME)
        raw = json.loads(result_files[0].read_text())
    # ... map raw -> BacktestResult (decimal strings for money, numbers for *_pct) ...
    return _map_cli_output(raw, bt)
```

CLI notes: create an isolated working dir per run, set a timeout, truncate
stdout/stderr, and keep only **relative** artifact names in `raw_report` — never
absolute local paths. LEAN-style Docker backtests are slow (image pull, compile,
data download) and should target P1 async; P0 stays synchronous.

### 3. Internal HTTP service (`wrapper_type: local_http`)

For an existing self-hosted backtest API on localhost or your private network.

```python
# adapter.py
import os, httpx

UPSTREAM = os.environ["MY_ENGINE_URL"]                       # localhost/private only
UPSTREAM_KEY = os.environ.get("MY_ENGINE_API_KEY", "")       # stays in provider env

def run_backtest(request):
    bt = request.backtest
    resp = httpx.post(
        f"{UPSTREAM}/run",
        headers={"Authorization": f"Bearer {UPSTREAM_KEY}"},  # never forwarded to Cutie
        json={"symbol": bt.symbol, "timeframe": bt.timeframe,
              "start": bt.start_at, "end": bt.end_at, **bt.provider_params},
        timeout=120,
    )
    if resp.status_code == 429:
        return business_failure(error_type="RATE_LIMITED",
                                error_message="upstream rate limited",
                                provider_name=PROVIDER_NAME, reason="rate_limited")
    raw = resp.json()
    # ... map raw -> BacktestResult ...
    return _map_http_output(raw, bt)
```

## Do NOT (security boundary — IMPL §3.x, §9.3, §12)

- **Do not host or upload a commercial tool's API key to Cutie.** Upstream keys
  stay in the provider's local env/config only; they are never forwarded to the
  Cutie Server or returned in any response.
- **Do not build a natural-language / prompt wrapper.** The Cutie backtest path
  is a deterministic HTTP contract, not an LLM prompt. Do not call OpenClaw /
  Hermes prompts to "do a backtest".
- **Do not enable live trading or order placement.** `security.live_trading`
  must stay `False`. A provider that can place orders must not register.
- **Do not expose the provider on the public internet.** Bind to `127.0.0.1` or
  a private-network address; the connector rejects public provider URLs.
- **Do not return absolute local filesystem paths or host/port URLs** in
  `report_url`, catalog fields, or `raw_report`.
- **Do not claim Cutie verification.** Results are always `external_unverified`;
  any `cutie_verified` you return is ignored.

## Tests

```bash
pip install -e '.[test]'
pytest tests/
```

- `tests/test_contract.py` — catalog & response schema compliance (IMPL §5.1, §6).
- `tests/test_security.py` — secret / path scrub and relative `report_url`.
- `tests/test_smoke.py` — drives `/health` `/catalog` `/cutie/backtest` via the
  FastAPI TestClient, including a fake adapter business-failure path.

## License

MIT
