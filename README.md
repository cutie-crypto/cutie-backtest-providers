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

## Quick Start (backtesting.py)

Recommended install. This creates a user `systemd` service, starts the provider,
checks `/health`, registers it with `cutie-connector`, and refreshes the tool
catalog.

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

## Quick Start (Freqtrade)

Freqtrade may require additional system dependencies on some hosts. If `pip install freqtrade` fails, follow the official Freqtrade installation guide for the target OS, then run the provider service from this directory.

Recommended install. This creates a Freqtrade `user_data` directory, installs the
sample strategy, downloads BTC/USDT data from OKX for `1h` and `4h`, creates a
user `systemd` service, checks `/health`, registers it with `cutie-connector`,
and refreshes the tool catalog.

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

You don't have to use these reference implementations. Any HTTP service that implements the three endpoints above will work. See the backtesting-py provider as a template.

## License

MIT
