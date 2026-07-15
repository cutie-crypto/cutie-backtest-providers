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
