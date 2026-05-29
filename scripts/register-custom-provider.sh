#!/usr/bin/env bash
#
# Register an existing Cutie-compatible BYO backtest provider with Cutie.
#
# This script is for "other backtest tools": the tool vendor, developer, or ops
# team already runs a local/intranet HTTP provider that implements the Cutie
# W3.9 v1 provider contract. The script validates that provider, registers it
# with cutie-connector, refreshes the catalog, and prints one product outcome.
#
# Required:
#   CUTIE_BACKTEST_PROVIDER_URL     e.g. http://127.0.0.1:8767
#
# Optional:
#   CUTIE_BACKTEST_PROVIDER_TOKEN   Bearer token for the provider
#   CUTIE_BACKTEST_SOURCE_ID        Connector source id (default custom-backtest-provider)
#   CUTIE_BACKTEST_TOOL_ID          tool_id to smoke-test (default catalog default/first)
#   CUTIE_BACKTEST_SMOKE_SYMBOL     default BTCUSDT
#   CUTIE_BACKTEST_SMOKE_TIMEFRAME  default 1h
#   CUTIE_BACKTEST_SMOKE_MARKET     default spot
#   CUTIE_BACKTEST_VALIDATOR_VENV   default ~/.cutie-backtest-providers/validator-venv

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/cutie-provider-install.sh
. "$SCRIPT_DIR/lib/cutie-provider-install.sh"

PROVIDER_URL="${CUTIE_BACKTEST_PROVIDER_URL:-}"
TOKEN="${CUTIE_BACKTEST_PROVIDER_TOKEN:-}"
SOURCE_ID="${CUTIE_BACKTEST_SOURCE_ID:-custom-backtest-provider}"
TOOL_ID="${CUTIE_BACKTEST_TOOL_ID:-}"
SMOKE_SYMBOL="${CUTIE_BACKTEST_SMOKE_SYMBOL:-BTCUSDT}"
SMOKE_TIMEFRAME="${CUTIE_BACKTEST_SMOKE_TIMEFRAME:-1h}"
SMOKE_MARKET="${CUTIE_BACKTEST_SMOKE_MARKET:-spot}"
VALIDATOR_VENV="${CUTIE_BACKTEST_VALIDATOR_VENV:-$HOME/.cutie-backtest-providers/validator-venv}"
RUN_LOG="${CUTIE_BACKTEST_RUN_LOG:-$HOME/.cutie-backtest-providers/register-custom-provider.log}"

fail() {
  local code="$1"
  local message="$2"
  echo "FAILED"
  echo "category=$code"
  echo "message=$message"
  echo "run_log=$RUN_LOG"
}

if [ -z "$PROVIDER_URL" ]; then
  fail "MISSING_PROVIDER_URL" "Set CUTIE_BACKTEST_PROVIDER_URL to the local/intranet provider URL."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  fail "DEPENDENCY_MISSING" "python3 is required to run the Cutie provider validator."
  exit 1
fi

mkdir -p "$(dirname "$VALIDATOR_VENV")" "$(dirname "$RUN_LOG")"

echo "[1/4] Preparing provider validator"
if [ ! -x "$VALIDATOR_VENV/bin/python" ]; then
  python3 -m venv "$VALIDATOR_VENV" >>"$RUN_LOG" 2>&1 || {
    fail "DEPENDENCY_MISSING" "Could not create validator virtualenv."
    exit 1
  }
fi
"$VALIDATOR_VENV/bin/python" -m pip install --upgrade pip >>"$RUN_LOG" 2>&1 || true
if ! "$VALIDATOR_VENV/bin/python" -m pip install "$REPO_DIR/validator" >>"$RUN_LOG" 2>&1; then
  fail "DEPENDENCY_MISSING" "Could not install cutie-backtest-provider-validator."
  exit 1
fi

echo "[2/4] Validating provider contract"
VALIDATOR_ARGS=(
  --base-url "$PROVIDER_URL"
  --smoke-symbol "$SMOKE_SYMBOL"
  --smoke-timeframe "$SMOKE_TIMEFRAME"
  --smoke-market "$SMOKE_MARKET"
)
if [ -n "$TOKEN" ]; then
  VALIDATOR_ARGS+=(--token "$TOKEN")
fi
if [ -n "$TOOL_ID" ]; then
  VALIDATOR_ARGS+=(--tool-id "$TOOL_ID")
fi

if ! env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
    -u HTTPS_PROXY -u https_proxy NO_PROXY='*' no_proxy='*' \
    "$VALIDATOR_VENV/bin/python" -m cutie_backtest_provider_validator "${VALIDATOR_ARGS[@]}" >>"$RUN_LOG" 2>&1; then
  fail "SELF_CHECK_FAILED" "The provider did not pass the Cutie W3.9 v1 validator."
  exit 1
fi

echo "[3/4] Registering with cutie-connector"
CONNECTOR_BIN="$(resolve_cutie_connector_bin)" || true
if [ -z "$CONNECTOR_BIN" ]; then
  echo "AWAITING_CONNECTOR"
  echo "provider_url=$PROVIDER_URL"
  echo "message=cutie-connector is not installed yet. Install Connector first, then re-run this script."
  exit 0
fi

ADD_ARGS=(backtest-tool add --id "$SOURCE_ID" --base-url "$PROVIDER_URL" --default)
if [ -n "$TOKEN" ]; then
  ADD_ARGS+=(--api-key "$TOKEN")
fi

if ! "$CONNECTOR_BIN" "${ADD_ARGS[@]}" >>"$RUN_LOG" 2>&1; then
  fail "CONNECTOR_REGISTER_FAILED" "Provider passed validation, but cutie-connector registration failed."
  exit 1
fi

"$CONNECTOR_BIN" backtest-tool test "$SOURCE_ID" >>"$RUN_LOG" 2>&1 || true
"$CONNECTOR_BIN" backtest-tool refresh >>"$RUN_LOG" 2>&1 || true
systemctl --user restart cutie-connector >/dev/null 2>&1 || true

echo "[4/4] Done."
echo "READY"
echo "provider_url=$PROVIDER_URL"
echo "source_id=$SOURCE_ID"
