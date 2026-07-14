#!/usr/bin/env bash
#
# Copy-and-run installer + self-check for the Cutie backtesting.py + ccxt provider.
# (IMPL W3.9 §3.1 普通 KOL 流程 + §13 P0c)
#
# A non-developer KOL can paste this into an OpenClaw / Hermes terminal, hand it
# to ops, or attach it to a ticket. It installs deps, starts the provider on
# 127.0.0.1, runs the validator self-check, registers with cutie-connector if
# present, and prints exactly ONE outcome: READY / FAILED / AWAITING_CONNECTOR.
#
# Configurable via env (all optional):
#   CUTIE_BACKTEST_PROVIDER_PORT   (default 8765)
#   CUTIE_BACKTEST_PROVIDER_TOKEN  (default local-dev-token)
#   CUTIE_BACKTEST_SOURCE_ID       (default local-backtesting-py)
#   CUTIE_BACKTEST_SERVICE_NAME    (default cutie-backtesting-provider.service)
#   CUTIE_BACKTEST_SUPPORTED_SYMBOLS
#     (default BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,TONUSDT)
#   CUTIE_CENTRAL_MARKET_DATA_URL
#   CUTIE_CENTRAL_MARKET_DATA_TOKEN (Bearer token; persisted mode 0600)
#   CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC (default 5; max 60)
#   CUTIE_BACKTEST_MANAGED_INSTALL (1 = require a non-default provider token)
#   CUTIE_BACKTEST_RESTART_PROVIDER (default 1; restart healthy service to apply latest config)
#   PYTHON_BIN                     (default python3)
#
# Re-running is safe (idempotent): the provider is restarted to pick up latest
# code/config.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/cutie-provider-install.sh
. "$SCRIPT_DIR/lib/cutie-provider-install.sh"

PROVIDER_LABEL="backtesting.py + ccxt"
PROVIDER_DIR="$REPO_DIR/backtesting-py"
PROVIDER_MODULE="cutie_backtesting_provider:app"
DEFAULT_PORT="8765"
DEFAULT_SOURCE_ID="local-backtesting-py"
DEFAULT_SERVICE_NAME="cutie-backtesting-provider.service"
DEFAULT_SUPPORTED_SYMBOLS="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,TONUSDT"

export CUTIE_BACKTEST_RESTART_PROVIDER="${CUTIE_BACKTEST_RESTART_PROVIDER:-1}"
PROVIDER_PERSISTED_ENV_NAMES="CUTIE_BACKTEST_SUPPORTED_SYMBOLS CUTIE_CENTRAL_MARKET_DATA_URL CUTIE_CENTRAL_MARKET_DATA_TOKEN CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC"

provider_run_install
exit $?
