# shellcheck shell=bash
#
# Shared install/check library for Cutie BYO backtest providers (IMPL W3.9 §3.1 + §13 P0c).
#
# This file is sourced by the per-provider entrypoints
# (install-backtesting-py-provider.sh / install-freqtrade-provider.sh).
# It implements the "copy-and-run" install + self-check flow a non-developer KOL
# can paste into an OpenClaw / Hermes terminal, hand to ops, or attach to a ticket:
#
#   1. install provider deps into an isolated venv (idempotent)
#   2. start the provider on 127.0.0.1:PORT (idempotent: reuse a healthy one)
#   3. run cutie-backtest-provider-validator self-check
#   4. run cutie-connector backtest-tool add/refresh (gracefully skip if absent)
#   5. print ONE of three productized outcomes:
#        - READY               (已可用)
#        - FAILED              (安装/检测失败)         + copy-paste diagnostic
#        - AWAITING_CONNECTOR  (等待 connector 上报)   + copy-paste next step
#
# Design notes:
#   - No `set -e`. We classify failures into the three §3.1 outcomes ourselves and
#     never let a raw stack trace / long log leak to the KOL. Verbose command
#     output goes to a per-run log file; only a short readable summary is shown.
#   - macOS bash 3.2 compatible: no associative arrays, no ${var,,}.
#   - Provider is launched detached (nohup) and tracked by a PID file, so the
#     flow works on hosts without systemd / launchd. Optional systemd unit is
#     installed when `systemctl --user` is available, for restart-on-reboot.

# --- diagnostic accumulation -------------------------------------------------

CUTIE_DIAG_LINES=""

_diag() {
  # Append one line to the copy-paste diagnostic summary.
  if [ -z "$CUTIE_DIAG_LINES" ]; then
    CUTIE_DIAG_LINES="$1"
  else
    CUTIE_DIAG_LINES="$CUTIE_DIAG_LINES
$1"
  fi
}

_log_tail() {
  # Last N lines of the run log, indented, for the diagnostic block.
  local n="${1:-20}"
  if [ -f "$CUTIE_RUN_LOG" ]; then
    tail -n "$n" "$CUTIE_RUN_LOG" 2>/dev/null | sed 's/^/    | /'
  fi
}

# --- outcome printers (§3.1 three states) ------------------------------------

print_ready() {
  echo ""
  echo "=================================================================="
  echo " [OK] $PROVIDER_LABEL provider is READY (已可用)"
  echo "=================================================================="
  echo "  provider URL  : http://127.0.0.1:$PORT"
  echo "  source id     : $SOURCE_ID"
  echo "  service       : $SERVICE_NAME ($CUTIE_SERVICE_MODE)"
  echo "  run log       : $CUTIE_RUN_LOG"
  echo ""
  echo "  In Cutie (Web / App) this tool should now appear under"
  echo "  \"我的回测工具\" within ~1 connector heartbeat."
  echo "=================================================================="
}

print_awaiting_connector() {
  echo ""
  echo "=================================================================="
  echo " [..] $PROVIDER_LABEL provider STARTED, waiting for connector (等待 connector 上报)"
  echo "=================================================================="
  echo "  provider URL : http://127.0.0.1:$PORT"
  echo "  run log      : $CUTIE_RUN_LOG"
  echo ""
  echo "  The provider is healthy and passed the self-check, but"
  echo "  'cutie-connector' is not installed on this machine, so the"
  echo "  tool has not been reported to Cutie yet."
  echo ""
  echo "  ---- COPY THIS to OpenClaw / Hermes / ops ----"
  echo "  Install/upgrade Cutie Connector on this machine, then run:"
  echo ""
  echo "    cutie-connector backtest-tool add \\"
  echo "      --id $SOURCE_ID \\"
  echo "      --base-url http://127.0.0.1:$PORT \\"
  echo "      --api-key '$TOKEN' \\"
  echo "      --default"
  echo "    cutie-connector backtest-tool refresh"
  echo "  ----------------------------------------------"
  echo "=================================================================="
}

print_failed() {
  # $1 = short reason category, $2 = one-line human message
  local category="$1"
  local message="$2"
  echo ""
  echo "=================================================================="
  echo " [X] $PROVIDER_LABEL provider install/check FAILED (安装/检测失败)"
  echo "=================================================================="
  echo "  $message"
  echo ""
  echo "  ---- COPY THIS DIAGNOSTIC and send to OpenClaw / Hermes / ops / 工单 ----"
  echo "  Cutie backtest provider : $PROVIDER_LABEL"
  echo "  Failure category        : $category"
  echo "  Provider port           : $PORT"
  echo "  Provider source id      : $SOURCE_ID"
  echo "  Provider dir            : $PROVIDER_DIR"
  echo "  Run log                 : $CUTIE_RUN_LOG"
  if [ -n "$CUTIE_DIAG_LINES" ]; then
    echo "  Details:"
    printf '%s\n' "$CUTIE_DIAG_LINES" | sed 's/^/    - /'
  fi
  local logtail
  logtail="$(_log_tail 20)"
  if [ -n "$logtail" ]; then
    echo "  Last log lines:"
    echo "$logtail"
  fi
  echo "  -----------------------------------------------------------------------"
  echo ""
  echo "  This usually means one of: dependency install failed, historical"
  echo "  data not downloaded, provider token mismatch, port $PORT already in"
  echo "  use, or the self-check (validator) rejected the provider response."
  echo "  A developer / provider maintainer can use the diagnostic above."
  echo "=================================================================="
}

# --- step 0: config ----------------------------------------------------------

# Caller must set before sourcing-and-running:
#   PROVIDER_LABEL, PROVIDER_DIR, PROVIDER_MODULE (python module:app for uvicorn),
#   DEFAULT_PORT, DEFAULT_SOURCE_ID, DEFAULT_SERVICE_NAME
#
# These env vars are honored (KOL-configurable, idempotent across re-runs):
#   CUTIE_BACKTEST_PROVIDER_PORT   -> PORT          (default: provider DEFAULT_PORT)
#   CUTIE_BACKTEST_PROVIDER_TOKEN  -> TOKEN         (default: local-dev-token)
#   CUTIE_BACKTEST_SOURCE_ID       -> SOURCE_ID
#   CUTIE_BACKTEST_SERVICE_NAME    -> SERVICE_NAME
#   CUTIE_BACKTEST_RESTART_PROVIDER
#     1 = restart a healthy provider to apply latest code/config (default from entrypoints)
#   PYTHON_BIN                     -> python interpreter (default python3)

provider_init_config() {
  PORT="${CUTIE_BACKTEST_PROVIDER_PORT:-$DEFAULT_PORT}"
  TOKEN="${CUTIE_BACKTEST_PROVIDER_TOKEN:-local-dev-token}"
  SOURCE_ID="${CUTIE_BACKTEST_SOURCE_ID:-$DEFAULT_SOURCE_ID}"
  SERVICE_NAME="${CUTIE_BACKTEST_SERVICE_NAME:-$DEFAULT_SERVICE_NAME}"
  PYTHON_BIN="${PYTHON_BIN:-python3}"

  VENV_DIR="$PROVIDER_DIR/.venv"
  VENV_PY="$VENV_DIR/bin/python"
  RUNTIME_DIR="$PROVIDER_DIR/.runtime"
  PID_FILE="$RUNTIME_DIR/provider.pid"
  CUTIE_RUN_LOG="$RUNTIME_DIR/install.log"
  CUTIE_SERVICE_MODE="nohup"

  mkdir -p "$RUNTIME_DIR"
  : > "$CUTIE_RUN_LOG"

  if ! printf '%s' "$PORT" | grep -Eq '^[0-9]+$'; then
    _diag "Invalid port '$PORT' (must be numeric)."
    print_failed "BAD_CONFIG" "CUTIE_BACKTEST_PROVIDER_PORT must be a number, got: $PORT"
    return 1
  fi
  return 0
}

# --- step 1: deps ------------------------------------------------------------

provider_system_packages() {
  # Ubuntu/OpenClaw/Hermes baseline deps for Python venv + native wheels +
  # connector/provider self-check tooling. Keep the list explicit so the FAILED
  # diagnostic is copy-pasteable for ops when sudo/root is unavailable.
  printf '%s\n' \
    python3 \
    python3-venv \
    python3-dev \
    build-essential \
    pkg-config \
    curl \
    git \
    lsof
}

_missing_system_packages() {
  if ! command -v dpkg >/dev/null 2>&1; then
    return 0
  fi
  provider_system_packages | while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    dpkg -s "$pkg" >/dev/null 2>&1 || printf '%s\n' "$pkg"
  done
}

_apt_install_command() {
  local pkgs
  pkgs="$(provider_system_packages | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
  printf 'sudo apt-get update && sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y %s' "$pkgs"
}

_install_missing_system_packages() {
  local pkgs="$1"
  if [ -z "$pkgs" ]; then
    return 0
  fi

  echo "[0/5] Installing missing system dependencies: $(printf '%s' "$pkgs" | tr '\n' ' ')"
  if [ "$(id -u)" = "0" ]; then
    if ! apt-get update >>"$CUTIE_RUN_LOG" 2>&1; then
      _diag "apt-get update failed while installing system dependencies."
      return 1
    fi
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs >>"$CUTIE_RUN_LOG" 2>&1
    return $?
  fi

  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    if ! sudo apt-get update >>"$CUTIE_RUN_LOG" 2>&1; then
      _diag "sudo apt-get update failed while installing system dependencies."
      return 1
    fi
    # shellcheck disable=SC2086
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs >>"$CUTIE_RUN_LOG" 2>&1
    return $?
  fi

  return 2
}

provider_install_system_deps() {
  # Only auto-manage packages on apt/dpkg hosts. Other platforms keep the old
  # behavior and fail later with the concrete venv/pip/provider diagnostic.
  if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
    return 0
  fi

  local missing
  missing="$(_missing_system_packages)"
  if [ -z "$missing" ]; then
    return 0
  fi

  if _install_missing_system_packages "$missing"; then
    return 0
  fi

  local rc=$?
  _diag "Missing Ubuntu packages: $(printf '%s' "$missing" | tr '\n' ' ')"
  _diag "Run this command on the OpenClaw/Hermes machine, then re-run this installer:"
  _diag "$(_apt_install_command)"
  if [ "$rc" = "2" ]; then
    print_failed "SYSTEM_DEPENDENCY_PERMISSION" "Missing system dependencies and this user cannot run passwordless sudo."
  else
    print_failed "SYSTEM_DEPENDENCY_INSTALL_FAILED" "Could not install required Ubuntu system dependencies."
  fi
  return 1
}

# Creates/repairs the venv and installs requirements + validator deps.
# Idempotent: reuses an existing healthy venv.
# Caller may export EXTRA_PIP_ARGS for extra packages (e.g. "freqtrade").
provider_install_deps() {
  echo "[1/5] Installing $PROVIDER_LABEL dependencies (log: $CUTIE_RUN_LOG)"

  if [ ! -x "$VENV_PY" ]; then
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR" >>"$CUTIE_RUN_LOG" 2>&1; then
      _diag "Could not create venv with '$PYTHON_BIN -m venv'. Is python3 + venv installed?"
      print_failed "DEPENDENCY_MISSING" "Failed to create the Python virtual environment."
      return 1
    fi
  fi

  if ! "$VENV_PY" -m pip install --upgrade pip >>"$CUTIE_RUN_LOG" 2>&1; then
    _diag "pip self-upgrade failed; continuing with bundled pip."
  fi

  # Provider requirements (+ optional extras like 'freqtrade').
  # shellcheck disable=SC2086
  if ! "$VENV_PY" -m pip install ${EXTRA_PIP_ARGS:-} -r "$PROVIDER_DIR/requirements.txt" >>"$CUTIE_RUN_LOG" 2>&1; then
    _diag "pip install of provider requirements failed (see run log)."
    print_failed "DEPENDENCY_MISSING" "Could not install provider Python dependencies."
    return 1
  fi

  # Validator + its httpx dep, into the same venv, so the self-check needs no
  # global install. We point the validator at the running provider over HTTP.
  if ! "$VENV_PY" -m pip install "$REPO_DIR/validator" >>"$CUTIE_RUN_LOG" 2>&1; then
    _diag "pip install of cutie-backtest-provider-validator failed (see run log)."
    print_failed "DEPENDENCY_MISSING" "Could not install the provider self-check (validator)."
    return 1
  fi

  return 0
}

# --- step 2: start provider (idempotent) ------------------------------------

_port_in_use() {
  # Returns 0 if something is already listening on $PORT.
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  # Fallback: probe with python (works without lsof/netstat).
  "$VENV_PY" - "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

_health_ok() {
  # Curl /health and check the JSON "ok" field is true.
  # Providers return HTTP 200 with {"ok": false, ...} for dependency/data issues,
  # so we must inspect the body, not just the HTTP status.
  # --noproxy: a localhost provider must never be routed through an HTTP/SOCKS proxy.
  local body
  body="$(curl -fsS --noproxy '*' "http://127.0.0.1:$PORT/health" 2>/dev/null)" || return 1
  printf '%s' "$body" > "$RUNTIME_DIR/health.json"
  printf '%s' "$body" | "$VENV_PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)' 2>/dev/null
}

_health_reason() {
  # Extract a human-readable reason from the last /health body, if unhealthy.
  if [ -f "$RUNTIME_DIR/health.json" ]; then
    "$VENV_PY" -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
et=d.get("error_type") or ""
em=d.get("error_message") or ""
if et or em:
    print((et+": "+em).strip(": "))' "$RUNTIME_DIR/health.json" 2>/dev/null
  fi
}

_provider_running() {
  # True if our tracked PID is alive.
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

_stop_provider_for_restart() {
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi

  # Stop a stale provider that is already listening on our port. Prefer the PID
  # file from previous installer runs; fall back to the listener PID when lsof is
  # available. This is only used after validator rejects a healthy provider,
  # which usually means the old process is still serving an older protocol.
  if _provider_running; then
    kill "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true
    sleep 1
  fi

  if command -v lsof >/dev/null 2>&1; then
    local pid
    pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1)"
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      sleep 1
    fi
  fi
  return 0
}

provider_start() {
  echo "[2/5] Starting $PROVIDER_LABEL provider on 127.0.0.1:$PORT"

  # Idempotent: if a healthy provider already answers on this port, reuse it.
  # Health-based (not PID-based) so it also reuses a systemd-managed instance
  # from a previous run, where this process holds no PID file.
  if _health_ok; then
    if [ "${CUTIE_BACKTEST_RESTART_PROVIDER:-0}" = "1" ]; then
      echo "      already running and healthy; restarting to apply latest provider code/config."
      _stop_provider_for_restart
    else
      echo "      already running and healthy (reusing)."
      return 0
    fi
  fi

  # Stop a stale instance we started before (nohup mode) before re-launching.
  if _provider_running; then
    kill "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true
    sleep 1
  fi

  # If the port is taken but does NOT serve a healthy Cutie provider, it is a
  # foreign process — fail clearly instead of fighting over the port.
  if _port_in_use && ! _provider_running; then
    _diag "Port $PORT is already in use by another (non-Cutie-provider) process."
    _diag "Set CUTIE_BACKTEST_PROVIDER_PORT=<free port> and re-run, or free port $PORT."
    print_failed "PORT_IN_USE" "Port $PORT is already in use on this machine."
    return 1
  fi

  # Prefer a user systemd unit (survives reboot) when available; otherwise nohup.
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    _start_via_systemd
  else
    _start_via_nohup
  fi

  # Wait for health (parses JSON ok, so reports real dependency/data problems).
  echo "[3a/5] Waiting for provider /health"
  local i=0
  while [ "$i" -lt 30 ]; do
    if _health_ok; then
      echo "       healthy."
      return 0
    fi
    if ! _provider_running && [ "$CUTIE_SERVICE_MODE" = "nohup" ]; then
      _diag "Provider process exited during startup (see run log)."
      print_failed "PROVIDER_CRASHED" "The provider failed to start. See the run log."
      return 1
    fi
    sleep 1
    i=$((i + 1))
  done

  local reason
  reason="$(_health_reason)"
  if [ -n "$reason" ]; then
    _diag "Provider /health reports: $reason"
  else
    _diag "Provider /health did not become healthy within 30s."
  fi
  print_failed "PROVIDER_UNHEALTHY" "Provider started but is not healthy: ${reason:-no /health response}"
  return 1
}

_start_via_nohup() {
  CUTIE_SERVICE_MODE="nohup"
  (
    cd "$PROVIDER_DIR" || exit 1
    CUTIE_BACKTEST_PROVIDER_TOKEN="$TOKEN" \
    CUTIE_BACKTEST_PORT="$PORT" \
    ${PROVIDER_EXTRA_ENV:-} \
    nohup "$VENV_DIR/bin/uvicorn" "$PROVIDER_MODULE" \
      --host 127.0.0.1 --port "$PORT" >>"$CUTIE_RUN_LOG" 2>&1 &
    echo $! > "$PID_FILE"
  )
}

_start_via_systemd() {
  CUTIE_SERVICE_MODE="systemd-user"
  mkdir -p "$HOME/.config/systemd/user"
  {
    echo "[Unit]"
    echo "Description=Cutie $PROVIDER_LABEL Provider"
    echo "After=network-online.target"
    echo ""
    echo "[Service]"
    echo "Type=simple"
    echo "WorkingDirectory=$PROVIDER_DIR"
    echo "Environment=CUTIE_BACKTEST_PROVIDER_TOKEN=$TOKEN"
    echo "Environment=CUTIE_BACKTEST_PORT=$PORT"
    # PROVIDER_SERVICE_ENV (multi-line "Environment=..." entries) from caller.
    if [ -n "${PROVIDER_SERVICE_ENV:-}" ]; then
      printf '%s\n' "$PROVIDER_SERVICE_ENV"
    fi
    echo "ExecStart=$VENV_DIR/bin/uvicorn $PROVIDER_MODULE --host 127.0.0.1 --port $PORT"
    echo "Restart=always"
    echo "RestartSec=3"
    echo ""
    echo "[Install]"
    echo "WantedBy=default.target"
  } > "$HOME/.config/systemd/user/$SERVICE_NAME"

  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  fi
  systemctl --user daemon-reload >>"$CUTIE_RUN_LOG" 2>&1 || true
  systemctl --user enable --now "$SERVICE_NAME" >>"$CUTIE_RUN_LOG" 2>&1 || true
}

# --- step 3: validator self-check -------------------------------------------

provider_validate() {
  echo "[3/5] Running self-check (cutie-backtest-provider-validator)"
  # The validator only talks to the localhost provider; disable any HTTP/SOCKS
  # proxy so httpx connects directly (avoids httpx[socks] requirement and
  # proxying loopback traffic).
  local out
  out="$(env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
        -u HTTPS_PROXY -u https_proxy NO_PROXY='*' no_proxy='*' \
    "$VENV_PY" -m cutie_backtest_provider_validator \
    --base-url "http://127.0.0.1:$PORT" \
    --token "$TOKEN" \
    --json 2>>"$CUTIE_RUN_LOG")"
  local rc=$?
  printf '%s' "$out" > "$RUNTIME_DIR/validator.json"
  if [ "$rc" -ne 0 ]; then
    # Pull validator errors into the diagnostic (short, readable).
    local errs
    errs="$(printf '%s' "$out" | "$VENV_PY" -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
for e in (d.get("errors") or [])[:5]:
    print(e)' 2>/dev/null)"
    if [ -n "$errs" ]; then
      while IFS= read -r line; do
        _diag "validator: $line"
      done <<EOF
$errs
EOF
    else
      _diag "validator self-check failed (see run log / $RUNTIME_DIR/validator.json)."
    fi
    return 1
  fi
  echo "      self-check passed."
  return 0
}

provider_restart_after_validator_failure() {
  echo "[3b/5] Self-check failed; restarting provider once to clear a stale process"
  CUTIE_DIAG_LINES=""
  _stop_provider_for_restart
  provider_start
}

# --- step 4: connector registration (graceful when absent) ------------------

resolve_cutie_connector_bin() {
  local found
  found="$(command -v cutie-connector 2>/dev/null)" || true
  if [ -n "$found" ] && [ -x "$found" ]; then
    printf '%s\n' "$found"
    return 0
  fi

  local candidate
  for candidate in \
    "$HOME/.cutie-connector/bin/cutie-connector" \
    "$HOME/.npm-global/bin/cutie-connector" \
    "/usr/local/bin/cutie-connector" \
    "/usr/bin/cutie-connector"
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

resolve_existing_connector_source_id() {
  local config_file="$HOME/.cutie-connector/config.json"
  local base_url="http://127.0.0.1:$PORT"
  if [ ! -f "$config_file" ] || ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi

  python3 - "$config_file" "$base_url" <<'PY' 2>/dev/null
import json
import sys

config_path, base_url = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(config_path))
except Exception:
    sys.exit(1)

catalog_url = base_url.rstrip("/") + "/catalog"
backtest_url = base_url.rstrip("/") + "/cutie/backtest"
for source in data.get("backtest_provider_sources") or []:
    if not isinstance(source, dict):
        continue
    if source.get("base_url") == base_url or source.get("catalog_url") == catalog_url or source.get("backtest_url") == backtest_url:
        source_id = source.get("id")
        if isinstance(source_id, str) and source_id.strip():
            print(source_id.strip())
            sys.exit(0)
sys.exit(1)
PY
}

provider_register_connector() {
  echo "[4/5] Registering with cutie-connector"
  local connector_bin
  connector_bin="$(resolve_cutie_connector_bin)" || true
  if [ -z "$connector_bin" ]; then
    # Not a failure: provider is healthy + validated. §3.1 "等待 connector 上报".
    CUTIE_OUTCOME="AWAITING_CONNECTOR"
    return 0
  fi
  echo "      using connector: $connector_bin"

  local existing_source_id
  existing_source_id="$(resolve_existing_connector_source_id)" || true
  if [ -n "$existing_source_id" ] && [ "$existing_source_id" != "$SOURCE_ID" ]; then
    echo "      reusing existing provider source id: $existing_source_id"
    SOURCE_ID="$existing_source_id"
  fi

  if ! "$connector_bin" backtest-tool add \
      --id "$SOURCE_ID" \
      --base-url "http://127.0.0.1:$PORT" \
      --api-key "$TOKEN" \
      --default >>"$CUTIE_RUN_LOG" 2>&1; then
    _diag "cutie-connector backtest-tool add failed (see run log)."
    print_failed "CONNECTOR_REGISTER_FAILED" "Provider is healthy but 'cutie-connector backtest-tool add' failed."
    return 1
  fi

  # test + refresh are best-effort signal; add already refreshed the catalog.
  "$connector_bin" backtest-tool test "$SOURCE_ID" >>"$CUTIE_RUN_LOG" 2>&1 || \
    _diag "cutie-connector backtest-tool test reported a problem (non-fatal; see run log)."
  if ! "$connector_bin" backtest-tool refresh >>"$CUTIE_RUN_LOG" 2>&1; then
    _diag "cutie-connector backtest-tool refresh failed (catalog may be stale; see run log)."
  fi
  # Nudge a running connector to re-report on next heartbeat (best effort).
  systemctl --user restart cutie-connector >/dev/null 2>&1 || true

  CUTIE_OUTCOME="READY"
  return 0
}

# --- top-level orchestration -------------------------------------------------

# Caller runs this after setting the PROVIDER_* config + provider_prepare hook.
provider_run_install() {
  CUTIE_OUTCOME=""

  provider_init_config || return 1
  provider_install_system_deps || return 1
  provider_install_deps || return 1

  # Optional provider-specific preparation (e.g. freqtrade data download).
  # Defined by the entrypoint; must print_failed + return 1 on hard failure.
  if command -v provider_prepare >/dev/null 2>&1; then
    if ! provider_prepare; then
      return 1
    fi
  fi

  provider_start || return 1
  if ! provider_validate; then
    if provider_restart_after_validator_failure && provider_validate; then
      :
    else
      print_failed "SELF_CHECK_FAILED" "The provider self-check (validator) did not pass."
      return 1
    fi
  fi
  provider_register_connector || return 1

  echo "[5/5] Done."
  case "$CUTIE_OUTCOME" in
    READY)              print_ready ;;
    AWAITING_CONNECTOR) print_awaiting_connector ;;
    *)                  print_ready ;;
  esac
  return 0
}
