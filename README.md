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

See [IMPL W3.8 §5](https://github.com/cutie-crypto/cutie-backtest-providers/blob/main/backtesting-py/cutie_backtesting_provider.py) for the full schema specification.

## Quick Start (backtesting.py)

```bash
cd backtesting-py
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

CUTIE_BACKTEST_PROVIDER_TOKEN="your-token" \
  uvicorn cutie_backtesting_provider:app --host 127.0.0.1 --port 8765
```

Then register with the connector:

```bash
cutie-connector backtest-tool add \
  --id local-backtesting-py \
  --base-url http://127.0.0.1:8765 \
  --api-key your-token \
  --default
```

## Writing Your Own Provider

You don't have to use these reference implementations. Any HTTP service that implements the three endpoints above will work. See the backtesting-py provider as a template.

## License

MIT
