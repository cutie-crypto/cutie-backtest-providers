#!/usr/bin/env bash
set -euo pipefail

PORT="${CUTIE_BACKTEST_PROVIDER_PORT:-8766}"
TOKEN="${CUTIE_BACKTEST_PROVIDER_TOKEN:-local-dev-token}"
SOURCE_ID="${CUTIE_BACKTEST_SOURCE_ID:-local-freqtrade}"
SERVICE_NAME="${CUTIE_BACKTEST_SERVICE_NAME:-cutie-freqtrade-provider.service}"
EXCHANGE="${CUTIE_FREQTRADE_EXCHANGE:-okx}"
PAIRS="${CUTIE_FREQTRADE_PAIRS:-BTC/USDT}"
TIMEFRAMES="${CUTIE_FREQTRADE_TIMEFRAMES:-1h 4h}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROVIDER_DIR="$REPO_DIR/freqtrade"
VENV_DIR="$PROVIDER_DIR/.venv"
USERDIR="$PROVIDER_DIR/user_data"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[1/6] Installing Freqtrade provider dependencies"
cd "$PROVIDER_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install freqtrade -r requirements.txt

echo "[2/6] Preparing Freqtrade user_data and sample strategy"
"$VENV_DIR/bin/freqtrade" create-userdir --userdir "$USERDIR" >/dev/null 2>&1 || true
mkdir -p "$USERDIR/strategies"
cp -f sample_strategies/SampleStrategy.py "$USERDIR/strategies/SampleStrategy.py"

echo "[3/6] Downloading OHLCV data: exchange=$EXCHANGE pairs=$PAIRS timeframes=$TIMEFRAMES"
read -r -a PAIR_ARGS <<< "$PAIRS"
read -r -a TIMEFRAME_ARGS <<< "$TIMEFRAMES"
"$VENV_DIR/bin/freqtrade" download-data \
  --userdir "$USERDIR" \
  --exchange "$EXCHANGE" \
  --pairs "${PAIR_ARGS[@]}" \
  --timeframes "${TIMEFRAME_ARGS[@]}"

echo "[4/6] Installing user systemd service: $SERVICE_NAME"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SERVICE_NAME" <<SERVICE
[Unit]
Description=Cutie Freqtrade Provider
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROVIDER_DIR
Environment=CUTIE_BACKTEST_PROVIDER_TOKEN=$TOKEN
Environment=CUTIE_BACKTEST_PROVIDER_PORT=$PORT
Environment=FREQTRADE_USERDIR=$USERDIR
Environment=FREQTRADE_CMD=$VENV_DIR/bin/freqtrade
Environment=CUTIE_FREQTRADE_DEFAULT_EXCHANGE=$EXCHANGE
ExecStart=$VENV_DIR/bin/uvicorn cutie_freqtrade_provider:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
SERVICE

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
fi

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo "[5/6] Waiting for provider health"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/tmp/cutie-freqtrade-health.json 2>/dev/null; then
    cat /tmp/cutie-freqtrade-health.json
    echo
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null

echo "[6/6] Registering provider with cutie-connector"
if command -v cutie-connector >/dev/null 2>&1; then
  cutie-connector backtest-tool add \
    --id "$SOURCE_ID" \
    --base-url "http://127.0.0.1:$PORT" \
    --api-key "$TOKEN" \
    --default
  cutie-connector backtest-tool test "$SOURCE_ID"
  cutie-connector backtest-tool refresh
  systemctl --user restart cutie-connector >/dev/null 2>&1 || true
else
  echo "cutie-connector command not found; install or upgrade Cutie Connector first." >&2
  exit 1
fi

echo "Done. Provider source: $SOURCE_ID, service: $SERVICE_NAME"
