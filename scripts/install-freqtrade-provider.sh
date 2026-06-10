#!/usr/bin/env bash
#
# Copy-and-run installer + self-check for the Cutie Freqtrade provider.
# (IMPL W3.9 §3.1 普通 KOL 流程 + §13 P0c)
#
# A non-developer KOL can paste this into an OpenClaw / Hermes terminal, hand it
# to ops, or attach it to a ticket. It installs Freqtrade + deps, prepares a
# user_data dir, downloads sample OHLCV data, starts the provider on 127.0.0.1,
# runs the validator self-check, registers with cutie-connector if present, and
# prints exactly ONE outcome: READY / FAILED / AWAITING_CONNECTOR.
#
# Configurable via env (all optional):
#   CUTIE_BACKTEST_PROVIDER_PORT   (default 8766)
#   CUTIE_BACKTEST_PROVIDER_TOKEN  (default local-dev-token)
#   CUTIE_BACKTEST_SOURCE_ID       (default local-freqtrade)
#   CUTIE_BACKTEST_SERVICE_NAME    (default cutie-freqtrade-provider.service)
#   CUTIE_FREQTRADE_EXCHANGE       (default okx)
#   CUTIE_FREQTRADE_PAIRS          (default "BTC/USDT")
#   CUTIE_FREQTRADE_TIMEFRAMES     (default "1h 4h")
#   CUTIE_BACKTEST_RESTART_PROVIDER (default 1; restart healthy service to apply latest config)
#   PYTHON_BIN                     (default python3)
#
# Re-running is safe (idempotent): the provider is restarted to pick up latest
# code/config; data already present is not re-downloaded by Freqtrade.
#
# Note: Freqtrade may need extra system libraries on some hosts. If dependency
# install fails, the FAILED diagnostic will point ops at the Freqtrade install
# guide; install those first, then re-run this script.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/cutie-provider-install.sh
. "$SCRIPT_DIR/lib/cutie-provider-install.sh"

PROVIDER_LABEL="Freqtrade"
PROVIDER_DIR="$REPO_DIR/freqtrade"
PROVIDER_MODULE="cutie_freqtrade_provider:app"
DEFAULT_PORT="8766"
DEFAULT_SOURCE_ID="local-freqtrade"
DEFAULT_SERVICE_NAME="cutie-freqtrade-provider.service"

# Freqtrade is not pinned in requirements.txt; pull it in alongside.
EXTRA_PIP_ARGS="freqtrade"
export CUTIE_BACKTEST_RESTART_PROVIDER="${CUTIE_BACKTEST_RESTART_PROVIDER:-1}"

EXCHANGE="${CUTIE_FREQTRADE_EXCHANGE:-okx}"
PAIRS="${CUTIE_FREQTRADE_PAIRS:-BTC/USDT}"
TIMEFRAMES="${CUTIE_FREQTRADE_TIMEFRAMES:-1h 4h}"

# provider_prepare: set up Freqtrade user_data, strategy, and OHLCV data.
# Called by the shared lib after deps install, before starting the provider.
# Must print_failed + return 1 on hard failure.
provider_prepare() {
  local userdir="$PROVIDER_DIR/user_data"
  local freqtrade_bin="$VENV_DIR/bin/freqtrade"

  # Tell the running provider where its data lives + which binary to use.
  PROVIDER_EXTRA_ENV="FREQTRADE_USERDIR=$userdir FREQTRADE_CMD=$freqtrade_bin CUTIE_FREQTRADE_DEFAULT_EXCHANGE=$EXCHANGE"
  PROVIDER_SERVICE_ENV="Environment=FREQTRADE_USERDIR=$userdir
Environment=FREQTRADE_CMD=$freqtrade_bin
Environment=CUTIE_FREQTRADE_DEFAULT_EXCHANGE=$EXCHANGE"

  echo "[1b/5] Preparing Freqtrade user_data + sample strategy"
  "$freqtrade_bin" create-userdir --userdir "$userdir" >>"$CUTIE_RUN_LOG" 2>&1 || _diag "freqtrade create-userdir failed (non-fatal if userdir already exists; see run log)."
  mkdir -p "$userdir/strategies"
  if [ -f "$PROVIDER_DIR/sample_strategies/SampleStrategy.py" ]; then
    cp -f "$PROVIDER_DIR/sample_strategies/SampleStrategy.py" "$userdir/strategies/SampleStrategy.py"
  fi

  echo "[1c/5] Downloading OHLCV data: exchange=$EXCHANGE pairs=$PAIRS timeframes=$TIMEFRAMES"
  # bash 3.2 compatible word-splitting of the space-separated lists.
  # shellcheck disable=SC2086
  set -- $PAIRS
  local pair_args="$*"
  # shellcheck disable=SC2086
  set -- $TIMEFRAMES
  local tf_args="$*"
  # shellcheck disable=SC2086
  if ! "$freqtrade_bin" download-data \
      --userdir "$userdir" \
      --exchange "$EXCHANGE" \
      --pairs $pair_args \
      --timeframes $tf_args >>"$CUTIE_RUN_LOG" 2>&1; then
    _diag "Freqtrade download-data failed for $EXCHANGE $PAIRS [$TIMEFRAMES] (see run log)."
    _diag "Check network access to $EXCHANGE and that the pairs/timeframes are valid."
    print_failed "DATA_NOT_DOWNLOADED" "Could not download Freqtrade historical OHLCV data."
    return 1
  fi
  return 0
}

provider_run_install
exit $?
