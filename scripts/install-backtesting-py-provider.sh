#!/usr/bin/env bash
set -euo pipefail

PORT="${CUTIE_BACKTEST_PROVIDER_PORT:-8765}"
TOKEN="${CUTIE_BACKTEST_PROVIDER_TOKEN:-local-dev-token}"
SOURCE_ID="${CUTIE_BACKTEST_SOURCE_ID:-local-backtesting-py}"
SERVICE_NAME="${CUTIE_BACKTEST_SERVICE_NAME:-cutie-backtesting-provider.service}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROVIDER_DIR="$REPO_DIR/backtesting-py"
VENV_DIR="$PROVIDER_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[1/5] Installing backtesting.py provider dependencies"
cd "$PROVIDER_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

echo "[2/5] Installing user systemd service: $SERVICE_NAME"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SERVICE_NAME" <<SERVICE
[Unit]
Description=Cutie backtesting.py Provider
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROVIDER_DIR
Environment=CUTIE_BACKTEST_PROVIDER_TOKEN=$TOKEN
Environment=CUTIE_BACKTEST_PROVIDER_PORT=$PORT
ExecStart=$VENV_DIR/bin/uvicorn cutie_backtesting_provider:app --host 127.0.0.1 --port $PORT
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

echo "[3/5] Waiting for provider health"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/tmp/cutie-backtesting-py-health.json 2>/dev/null; then
    cat /tmp/cutie-backtesting-py-health.json
    echo
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null

echo "[4/5] Registering provider with cutie-connector"
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

echo "[5/5] Done. Provider source: $SOURCE_ID, service: $SERVICE_NAME"
