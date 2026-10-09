# Cutie Backtest Providers

Reference backtest provider implementations for [Cutie Connector](https://github.com/cutie-crypto/zylos-cutie).

Each provider is a standalone FastAPI HTTP service that runs locally on the KOL's machine. The Cutie Connector communicates with providers via `http://127.0.0.1:<port>` — no public network exposure required.

## Providers

| Provider | Engine | Default Port | Data Source |
|---|---|---:|---|
| [backtesting-py](./backtesting-py/) | [backtesting.py](https://kernc.github.io/backtesting.py/) + [ccxt](https://github.com/ccxt/ccxt) | 8765 | Public OHLCV via ccxt |
| [freqtrade](./freqtrade/) | [Freqtrade](https://www.freqtrade.io/) | 8766 | Local Freqtrade data directory |

## Provider HTTP Contract

Any service implementing these three endpoints can be used as a Cutie backtest provider:

```
GET  /health              # No auth. Returns { ok, provider_id, engine_name, ... }
GET  /catalog             # Bearer auth. Returns { schema, tools[] }
POST /cutie/backtest      # Bearer auth. JSON body. Returns backtest result.
```

See the Cutie Feature 37 W3.8 Provider Bridge IMPL in `cutie-docs` for the full schema specification. The provider source in this repository is a reference implementation of that contract.

### Single-position template leverage

The single-position `backtesting.py` templates that use the fixed-risk overlay
advertise `leverage` as an integer from 1 to 20, defaulting to 1. Booleans,
floats (including `2.0`), strings, and non-finite values are invalid. Explicit
`leverage=1` preserves the omitted parameter's trades, equity, and result.v2;
it adds no leverage-related report fields and passes no `margin` engine option.
Spot requests above 1 are rejected before market data access. Futures requests
above 1 enable the simplified MMR=0 isolated-liquidation model, independently of
`risk_layer_enabled`. This provider gate does not change server or UI enablement.

The internal sizing preparation treats `position_size_pct` as a margin budget:
the engine's `margin=1/L` applies leverage once. `position_size_notional` retains
its nominal amount by dividing its engine allocation fraction by L. Unconfigured
sizing continues to use the library's full-equity sentinel.
The `rsi_scale_in_out`, `grid`, and `dca` ledger templates do not advertise or
accept `leverage`, even 1. Basket `kernel_v3` leverage remains in [1, 3].

Liquidation uses `E*(L-1)/L` for longs and `E*(L+1)/L` for shorts, with fees
excluded from the trigger. Open beyond the liquidation price wins immediately;
otherwise a closer effective intrabar stop wins, with equality going to liquidation.
Liquidation precedes holding expiry, profit targets, and template signals. Legacy
close-only stops occur after intrabar liquidation. Reentry is blocked on that bar.
The broker still closes at the next open; a per-request close callback reconciles
cash to cumulative result.v2 PnL before subsequent sizing and insolvency checks.
If close-price marking would exhaust the account before Strategy.next, the broker
order hook invokes the same arbitration point and bridges a pending liquidation
with positive settled equity until its next-open close. Genuine exhaustion still
uses the library check. Earlier partial exits retain their own PnL; an internal
remaining-unit sidecar selects only the liquidated remainder for settlement and
reporting, without changing liquidation records or result.v2 trade keys.
Result.v2 rewrites the exit bucket and price to the liquidation bar and preserves
all frozen keys. Gap fills use that bar's open; their PnL can exceed margin.
`raw_report.isolated_risk` discloses margin lost and excess loss, while
`assumptions.isolated_margin` describes the model. Fixed stop distances use the
request's original percentage. For ATR/trailing/breakeven, the beyond-liquidation
flag and trade count use each entry's frozen initial stop, including equality;
subsequent stop ratchets do not increase this count. These nodes appear only for
futures L>1. Funding, MMR>0, and exchange-specific liquidation rules are excluded.

### Closed higher-timeframe entry filters

Single-position templates support opt-in EMA, MACD zero-axis and Supertrend
long-entry filters. Set `filter_layer_enabled=true` and select at least one
`filter_*_enabled` predicate; enabled predicates combine with AND.
`filter_timeframe` defaults to `""` (the primary timeframe). An explicit value
must be a supported fixed timeframe strictly larger than the primary timeframe;
monthly timeframes are rejected. A higher candle is visible when its close is
at or before the primary signal candle's close. Entries still fill at the next
primary open; exit rules are unchanged.

Higher-timeframe history is fetched once for the complete run plus indicator
warmup. Missing candles or unavailable history return `INSUFFICIENT_DATA`.
Disabled filters preserve existing result bytes, and a non-default
`filter_timeframe` while disabled is rejected before fetching market data.
Short/both directions, ledger templates and `signal_execution` remain unsupported
with filters. See the [parameter and verification evidence](backtesting-py/tests/entry_filters_7p2_evidence.md).

### Additional long-only patterns (9-T2)

Four registered ids share the existing candle engine and next-open market fills:

| Tool id (`local.backtesting_py.` prefix) | Confirmation | Frozen stop |
|---|---|---|
| `morning_star` | Large bearish first body; second body <= 30% of first and closes below first close; bullish third closes above first midpoint | Second candle low × 0.999 |
| `three_white_soldiers` | Three rising bullish closes, every open in the preceding body (including first soldier), each upper wick <= 30% of its body | First soldier low × 0.999 |
| `bullish_doji_reversal` | Body <= 10% of span; span >= prior-20 mean span × 0.8; immediately next close strictly above doji high | Doji low × 0.999 |
| `inside_bar_breakout` | Strict inside bar, then a close strictly above mother high in the next N bars | Mother low × 0.999 |

All four accept `direction=long` only, `reward_r=2` (0.1–20), `exchange`
and the existing sizing/time keys. Short/both fail before fetching in both spot
and futures. `position_filter=true` is available for soldiers (first soldier open
below the close 20 bars earlier) and doji (RSI14 < 30 at the doji close); it can be
disabled. Morning star has no additional position filter in the source requirement.
Its middle body has no extra prior-average small-body restriction.

Inside bar accepts strict integer `breakout_window=3` (1–20) and
`trend_filter=false`; enabling it requires breakout close > EMA20 (adjust=False,
20 closes required). A new inside bar replaces an unbroken setup. A breakout
consumes its setup even when the trend/session filter blocks the entry; expiry
never produces a delayed order. Warmup and main bars share causal confirmation
facts. Main minimum bars: star 23, soldiers 23 (4 with filter off), doji 22,
inside 3 (21 with trend filter on).

Stops, actual-entry R targets, gap skips, exit priority and mutually exclusive
external stop/target keys follow the candle engine below. New report information
stays in `raw_report.candle_pattern`; result.v2 keys remain unchanged. These are
provider tools; service registration and deployment are separate work.

### Long-only candle patterns

`local.backtesting_py.bullish_engulfing` and `local.backtesting_py.hammer_pin_bar`
confirm at the last pattern close and enter at the next open. Position filtering
is on by default: engulfing requires its two-bar low within 0.5% of the preceding
20-bar low or a touch of Bollinger(20, 2) lower; hammer requires a new low against
the preceding 20 bars or a touch of EMA20/60. Touch means the indicator lies in
the confirmation bar's Low–High range. Indicators include only the current and
previous closes; an EMA needs its full period of history. Filtered requests need
21 main bars; disabling the filter reduces that to 2.

| Parameter | Default | Accepted values |
|---|---|---|
| `direction` | `long` | `long` only; `short`/`both` rejected before fetching |
| `position_filter` | `true` | Boolean |
| `reward_r` | `2` | Number, 0.1–20 |
| `exchange` | `okx` | Existing exchange selection (environment may override the default) |

The stop is frozen at the pattern low times 0.999 (the hammer's lower-shadow tip).
The target is the actual engine entry plus `reward_r` times entry-to-stop distance.
An entry open at or below its frozen stop is canceled before a position exists;
`raw_report.candle_pattern` contains the skip reason, prices, bar indexes and count.
Stop and target use bar Low/High triggers, followed by next-open market exits;
they do not guarantee a fill at the trigger price, as disclosed in `assumptions`.
Stop wins over holding expiry, which wins over target on the same bar.

Existing sizing, leverage=1 and time gates apply. Fixed `stop_loss_pct` or
`take_profit_pct` keys, and active ATR/R/trailing/breakeven/multi-target exit keys,
are incompatible with these template-owned exits. Disabled risk/time defaults
remain valid. All new reporting stays outside the frozen result.v2 key sets.

### Basket ratio MA option

`local.backtesting_py.basket_ratio_sma_cross` accepts optional `ma_type=sma|ema`.
Omitted or explicit `sma` retains the frozen SMA spec, trades and equity bytes.
EMA is v3-only, uses `adjust=False`, seeds with the first N aligned valid values' SMA,
and emits its first value after N bars. Later values use `alpha=2/(N+1)` and
`alpha*x + (1-alpha)*previous` in decimal128. Crosses need `slow_window+1`
aligned bars; the provider fetches `slow_window` bars before the requested start.
EMA starts at the fetched history's beginning, so changing that history can
change signals. EMA-only assumptions disclose this seed/history boundary.
ROC, zscore, leg directions and the basket runner remain as declared.

### Single-position risk sizing and compounding (provider 10B)

The `_FixedRiskMixin` templates accept three mutually exclusive sizing fields:
`position_size_notional` (fixed nominal amount), `position_size_pct` (percentage
of the capital base allocated to margin), or `position_size_risk_pct` (percentage
of capital put at risk at the frozen initial stop). Risk percentages are expressed
as 1 for 1%, must be positive, and may not exceed 100.

New fields are opt-in. If `position_size_risk_pct`, `compound`, and
`position_size_qty_step` are all omitted, legacy sizing, compounding, trades,
equity and result.v2 bytes follow the existing path. If any is supplied, exactly
one of the three sizing fields is required. In this new path, `compound` defaults
to false: the capital base is initial principal. When true, it is net equity
immediately before the fill, after prior settlements, fees and slippage.
Fixed nominal sizing remains nominal in either setting.

`position_size_qty_step` is a positive quantity increment (default `0.00000001`,
minimum `0.00000001`, matching result.v2 quantity precision). It is a caller
parameter, not a verified exchange rule. At the next-open market fill, risk
quantity is `floor_to_step(capital_base * risk_pct / abs(fill_price - initial_stop))`.
The initial stop and R targets use the same frozen fill-time state; ATR comes
only from closed bars through the signal candle. Leverage affects margin,
never the risk quantity. Margin plus entry fee and slippage must fit available
funds; the provider cancels unaffordable entries rather than reducing quantity.
The existing next-open exit model remains applicable: the risk budget is an
initial-stop sizing target, not a guaranteed bound on gap losses.

A missing stop is rejected before fetching data. Invalid/unavailable fill-time
stops, non-positive distances, sub-step quantities and insufficient funds are
recorded in `raw_report.position_sizing.rejections`; valid fills disclose the
capital base, actual fill price, quantity, initial stop and margin. A final-bar
signal has no next open and is rejected. Turtle, RSI scale-in/out, grid, DCA and
all kernel_v3 baskets reject every new field, including `compound=false`.
This change enables the provider only; server parsing and user interfaces are
separate work.

### StrategySpec v2 artifact execution

The backtesting.py service also advertises
`local.strategy_spec_v2.compiler` when `CUTIE_PROVIDER_REVISION` is a locked
7–64 character lowercase Git revision. Unlike the seven legacy fixed-strategy
tools, this tool consumes the complete
`cutie.strategy_execution_request.v1` envelope and executes only the immutable
`cutie.strategy_spec.v2` artifact it contains.

The artifact path is fail-closed: hashes, capability revision, static types,
operators, declared data sources, instrument rules, and result schemas must all
match before data access or execution. It reads only declared central platform
streams and never falls back to ccxt, a fixed strategy, or a default tool. The
same deterministic `StrategyKernel.evaluate` method advances historical replay
and future paper frames; the HTTP API currently accepts historical replay only.
Legacy request bodies and their fixed tools remain unchanged.

## One-command install + self-check (recommended)

Each provider ships a copy-and-run installer (`scripts/install-*-provider.sh`).
A non-developer KOL can paste it into an OpenClaw / Hermes terminal, hand it to
ops, or attach it to a ticket. The script:

1. installs the provider's Python dependencies into an isolated `.venv`,
2. starts the provider on `127.0.0.1:<port>` (a user `systemd` unit when
   available, otherwise a detached background process),
3. runs the `cutie-backtest-provider-validator` self-check,
4. runs `cutie-connector backtest-tool add --default` + `refresh` when
   `cutie-connector` is installed,
5. prints exactly **one** outcome:

   - `READY` (已可用) — provider healthy, self-check passed, registered.
   - `AWAITING_CONNECTOR` (等待 connector 上报) — provider healthy and
     validated, but `cutie-connector` is not installed yet; the script prints
     the copy-paste registration command.
   - `FAILED` (安装/检测失败) — prints a short, copy-paste diagnostic block
     (failure category, port, run log, key log lines) to send to
     OpenClaw / Hermes / ops / a ticket. No raw stack traces.

Re-running is safe (idempotent): a healthy provider is reused.

Configurable via environment (all optional):

| Env | Default (backtesting.py / Freqtrade) | Purpose |
|---|---|---|
| `CUTIE_BACKTEST_PROVIDER_PORT` | `8765` / `8766` | Provider port (127.0.0.1) |
| `CUTIE_BACKTEST_PROVIDER_TOKEN` | `local-dev-token` | Bearer token |
| `CUTIE_BACKTEST_SOURCE_ID` | `local-backtesting-py` / `local-freqtrade` | Connector source id |
| `CUTIE_BACKTEST_SERVICE_NAME` | `cutie-*-provider.service` | systemd unit name |
| `CUTIE_BACKTEST_MANAGED_INSTALL` | `0` | `1` requires a non-default persisted provider token |
| `PYTHON_BIN` | `python3` | Python interpreter |

backtesting.py-only: `CUTIE_BACKTEST_SUPPORTED_SYMBOLS`
(`BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,TONUSDT`).
Managed backtesting.py installs may also set `CUTIE_CENTRAL_MARKET_DATA_URL`,
the independent read-only Bearer credential `CUTIE_CENTRAL_MARKET_DATA_TOKEN`,
and `CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC` (default `5`, maximum `60`).
The installer persists runtime credentials in a mode-`0600` environment file;
re-running without credential variables preserves the existing values.

Freqtrade-only: `CUTIE_FREQTRADE_EXCHANGE` (`okx`), `CUTIE_FREQTRADE_PAIRS`
(`BTC/USDT`), `CUTIE_FREQTRADE_TIMEFRAMES` (`1h 4h`).

Before spawning a Freqtrade backtest, the provider checks the exact local
exchange + pair + timeframe file. For readable Freqtrade OHLCV formats it also
requires the stored first/last candle range to cover the requested interval.
Missing or stale data returns `NO_DATA` with the observed UTC coverage and a
copy-paste `freqtrade download-data` command; it is not reported as a platform
engine fault. If Freqtrade itself exits non-zero, diagnostics keep the final
1,000 characters because the actionable cause is normally at the end of its
log output.

### Quick Start (backtesting.py)

```bash
PROVIDER_REPO_DIR="$HOME/.cutie-backtest-providers/cutie-backtest-providers"
if [ -d "$PROVIDER_REPO_DIR/.git" ]; then
  git -C "$PROVIDER_REPO_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$PROVIDER_REPO_DIR")"
  git clone https://github.com/cutie-crypto/cutie-backtest-providers.git "$PROVIDER_REPO_DIR"
fi

CUTIE_BACKTEST_PROVIDER_TOKEN="local-dev-token" \
  "$PROVIDER_REPO_DIR/scripts/install-backtesting-py-provider.sh"
```

### Quick Start (Freqtrade)

Freqtrade may require additional system dependencies on some hosts. If the
install fails, the `FAILED` diagnostic points ops at the official Freqtrade
installation guide; install those first, then re-run the script.

```bash
PROVIDER_REPO_DIR="$HOME/.cutie-backtest-providers/cutie-backtest-providers"
if [ -d "$PROVIDER_REPO_DIR/.git" ]; then
  git -C "$PROVIDER_REPO_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$PROVIDER_REPO_DIR")"
  git clone https://github.com/cutie-crypto/cutie-backtest-providers.git "$PROVIDER_REPO_DIR"
fi

CUTIE_BACKTEST_PROVIDER_TOKEN="local-dev-token" \
  "$PROVIDER_REPO_DIR/scripts/install-freqtrade-provider.sh"
```

## Writing Your Own Provider

You don't have to use these reference implementations. Any HTTP service that
implements the three endpoints above will work. See
`templates/python-provider/` for a production-shaped wrapper template.

Once your own provider is running on the OpenClaw / Hermes machine or trusted
intranet, register it with Cutie in one step:

```bash
PROVIDER_REPO_DIR="$HOME/.cutie-backtest-providers/cutie-backtest-providers"
if [ -d "$PROVIDER_REPO_DIR/.git" ]; then
  git -C "$PROVIDER_REPO_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$PROVIDER_REPO_DIR")"
  git clone https://github.com/cutie-crypto/cutie-backtest-providers.git "$PROVIDER_REPO_DIR"
fi

CUTIE_BACKTEST_PROVIDER_URL="http://127.0.0.1:8767" \
  CUTIE_BACKTEST_PROVIDER_TOKEN="replace-with-provider-token" \
  CUTIE_BACKTEST_SOURCE_ID="my-backtest-provider" \
  "$PROVIDER_REPO_DIR/scripts/register-custom-provider.sh"
```

The registration script installs/runs the validator, executes
`cutie-connector backtest-tool add --default`, refreshes the catalog, and prints
`READY` only when the tool can be selected from Cutie.

## License

MIT

<!-- 2026-07-20 62-3 §9 受控 revision 迁移演练发版：行为等价 no-op revision（仅本注释行），用于真实跨 revision 迁移的「等价放行」分支 direct 验证。 -->
