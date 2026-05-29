# cutie-backtest-provider-validator

Validator CLI for Cutie BYO backtest providers (IMPL W3.9 §8, migration P0b).

It checks that a provider's **HTTP response layer** conforms to the W3.9 v1
contract before a KOL registers it with `cutie-connector`. It does not run on
Cutie Server and never sees provider secrets — it talks only to a local/internal
provider URL (or drives the provider app in-process).

## Install

```bash
pip install -e .                 # core (httpx only)
pip install -e ".[asgi]"         # + fastapi, for in-process (--app) mode
```

## Usage

Live HTTP mode (provider already running):

```bash
cutie-backtest-provider-validator \
  --base-url http://127.0.0.1:8765 \
  --token "$CUTIE_BACKTEST_PROVIDER_TOKEN" \
  --tool-id local.backtesting_py.ema_cross \
  --smoke-symbol BTCUSDT \
  --smoke-timeframe 1h \
  --start-at 1704067200 \
  --end-at 1704672000
```

In-process mode (point at a provider's FastAPI module, no server needed):

```bash
cutie-backtest-provider-validator \
  --app ../backtesting-py/cutie_backtesting_provider.py \
  --token local-dev-token
```

The `--app` value is a path to a `.py` file (or `module:attr`) exposing a
FastAPI `app`. Default attribute is `app`.

Exit code is `0` when all checks pass, `1` otherwise. `--json` prints only the
machine-readable report.

## Checks (IMPL §8)

1. `GET /health` returns a JSON object and leaks no secret/path.
2. `GET /catalog` rejects requests without a token (401/403) and returns
   `cutie.backtest_provider_catalog.v1` with a token.
3. Each catalog tool's required fields / lengths / array sizes / `param_schema`
   subset / `security` fields are valid.
4. No secret-like keys, high-entropy token values, or local paths anywhere in
   the catalog (key normalize + sensitive name/suffix + value entropy + path
   patterns — mirrors the connector scrub contract).
5. The smoke request is a valid `cutie.external_backtest.request.v1` envelope
   and the provider returns `success` or a standard business `failed`.
6. Success response: required fields present; money/quantity fields are decimal
   strings; ratio metrics are finite JSON numbers (or `null` = not applicable).
7. Failed response: `error_type` is a standard §6.3 code, `error_message` and
   provider metadata present.
8. `report_url` is a relative path/ref, or an absolute URL whose host is
   loopback / RFC1918 (`10/8`, `172.16/12`, `192.168/16`); public IP/domain or
   local absolute path is blocked.
9. Unknown `wrapper_type` fails; `manual_export_parser` / `cloud_api_proxy` are
   off the W3.9 main line and rejected.
10. Output is both machine-readable JSON and a human-readable summary.

## Error code casing

The validator checks the **provider HTTP response layer**, so `error_type` is
the UPPERCASE §6.3 protocol code (`NO_DATA`, `PROVIDER_CONTRACT_VIOLATION`, ...).
Lowercase snake_case normalization is the connector→server (落库) concern and is
out of scope here.

## Tests

```bash
pip install pytest fastapi
python -m pytest tests/ -q
```

A conforming mock provider passes all 10 checks; each non-conforming mock fails
the targeted check.
