#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.pipsgox"
mkdir -p "$RUN_DIR"

CODESPACE_DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  WEB_URL="https://${CODESPACE_NAME}-3001.${CODESPACE_DOMAIN}"
  API_URL="https://${CODESPACE_NAME}-8000.${CODESPACE_DOMAIN}"
else
  WEB_URL="http://localhost:3001"
  API_URL="http://127.0.0.1:8000"
fi
FYERS_CALLBACK="${API_URL}/auth/broker/fyers/callback"

log_tail() {
  local file="$1"
  echo
  echo "----- LAST LOG OUTPUT: $file -----"
  if [[ -f "$file" ]]; then
    tail -n 40 "$file"
  else
    echo "Log file not found."
  fi
  echo "-----------------------------------"
}



status_pass() {
  echo "STATUS: PASS — $1"
}

status_warn() {
  echo "STATUS: WARN — $1"
}

status_fail() {
  echo "STATUS: FAIL — $1"
}

run_preflight() {
  local failed=0

  echo
  echo "PRE-FLIGHT CHECK"
  echo "================"

  if command -v python >/dev/null 2>&1; then
    status_pass "Python available: $(python --version 2>&1)"
  else
    status_fail "Python is not installed or not on PATH."
    failed=1
  fi

  if command -v npm >/dev/null 2>&1; then
    status_pass "npm available: $(npm --version 2>&1)"
  else
    status_fail "npm is not installed or not on PATH."
    failed=1
  fi

  if command -v curl >/dev/null 2>&1; then
    status_pass "curl available"
  else
    status_fail "curl is required for health checks."
    failed=1
  fi

  if [[ "$failed" -ne 0 ]]; then
    return 1
  fi

  if ! python -c "import uvicorn, cryptography, fastapi, pydantic" >/dev/null 2>&1; then
    status_warn "One or more backend dependencies are missing. Installing backend requirements..."
    if python -m pip install -r "$ROOT/backend/requirements.txt"; then
      status_pass "Backend Python dependencies installed"
    else
      status_fail "Backend dependency installation failed."
      echo "ACTION: python -m pip install -r backend/requirements.txt"
      return 1
    fi
  else
    status_pass "Backend Python dependencies ready"
  fi

  if [[ ! -x "$ROOT/frontend/node_modules/.bin/vite" ]]; then
    status_warn "Frontend dependencies are missing. Installing npm dependencies..."
    if npm --prefix "$ROOT/frontend" install; then
      status_pass "Frontend npm dependencies installed"
    else
      status_fail "Frontend npm dependency installation failed."
      echo "ACTION: npm --prefix frontend install"
      return 1
    fi
  else
    status_pass "Frontend npm dependencies ready"
  fi

  return 0
}

port_owner() {
  local port="$1"
  ss -ltnp 2>/dev/null | grep -E ":$port([[:space:]]|$)" || true
}

service_running() {
  local name="$1" port="$2" url="$3" pidfile="$4"
  if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
    local pid=""
    pid="$(port_owner "$port" | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2 || true)"
    if [[ -n "$pid" ]]; then echo "$pid" > "$pidfile"; fi
    if [[ -n "$pid" ]]; then
      echo "STATUS: PASS — $name already running on :$port (PID $pid)"
    else
      echo "STATUS: PASS — $name already running on :$port"
    fi
    return 0
  fi
  return 1
}

check_port_free() {
  local name="$1" port="$2"
  if port_owner "$port" | grep -q "LISTEN"; then
    echo "STATUS: FAIL — $name port :$port is already occupied."
    port_owner "$port"
    return 1
  fi
  return 0
}
start_backend() {
  local pidfile="$RUN_DIR/backend.pid"
  local logfile="$RUN_DIR/backend.log"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    echo "STATUS: PASS — Backend already running (PID $(cat "$pidfile"))"
    return 0
  fi

  rm -f "$pidfile"
  : > "$logfile"
  echo "ACTION: Starting Backend..."

  (
    cd "$ROOT/backend" || exit 1
    exec env PIPSGOX_WEB_URL="$WEB_URL" FYERS_REDIRECT_URI="$FYERS_CALLBACK" \
      python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  ) >"$logfile" 2>&1 &
  local pid=$!
  echo "$pid" > "$pidfile"

  for _ in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
      echo "STATUS: PASS — Backend :8000 (PID $pid)"
      return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
      echo "STATUS: FAIL — Backend exited during startup (PID $pid)."
      log_tail "$logfile"
      rm -f "$pidfile"
      return 1
    fi
    sleep 1
  done

  echo "STATUS: FAIL — Backend did not become ready within 30 seconds."
  log_tail "$logfile"
  return 1
}

start_frontend() {
  local pidfile="$RUN_DIR/frontend.pid"
  local logfile="$RUN_DIR/frontend.log"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    echo "STATUS: PASS — Frontend already running (PID $(cat "$pidfile"))"
    return 0
  fi

  rm -f "$pidfile"
  : > "$logfile"
  echo "ACTION: Starting Frontend..."

  (
    cd "$ROOT/frontend" || exit 1
    exec npm run dev -- --host 0.0.0.0 --port 3001
  ) >"$logfile" 2>&1 &
  local pid=$!
  echo "$pid" > "$pidfile"

  for _ in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:3001/ >/dev/null 2>&1; then
      echo "STATUS: PASS — Frontend :3001 (PID $pid)"
      return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
      echo "STATUS: FAIL — Frontend exited during startup (PID $pid)."
      log_tail "$logfile"
      rm -f "$pidfile"
      return 1
    fi
    sleep 1
  done

  echo "STATUS: FAIL — Frontend did not become ready within 30 seconds."
  log_tail "$logfile"
  return 1
}

echo
echo "PIPSGOX SERVER"
echo "=================="
echo "Time: $(date)"
echo "Web: $WEB_URL"
echo "API: $API_URL"
echo "FYERS callback: $FYERS_CALLBACK"
echo


if ! run_preflight; then
  status_fail "PIPSGOX pre-flight check failed. Startup stopped."
  echo "ACTION: Fix the item marked FAIL above, then run ./scripts/run-pipsgox.sh again."
  exit 1
fi

if service_running "Backend" 8000 "http://127.0.0.1:8000/health" "$RUN_DIR/backend.pid"; then
  :
elif ! check_port_free "Backend" 8000; then
  echo "ACTION: Use ./scripts/stop-pipsgox.sh first, or inspect with ./scripts/pipsgox-doctor.sh."
  exit 1
fi

if service_running "Frontend" 3001 "http://127.0.0.1:3001/" "$RUN_DIR/frontend.pid"; then
  :
elif ! check_port_free "Frontend" 3001; then
  echo "ACTION: Use ./scripts/stop-pipsgox.sh first, or inspect with ./scripts/pipsgox-doctor.sh."
  exit 1
fi

if ! start_backend; then
  echo
  echo "STATUS: FAIL — PIPSGOX start failed: backend"
  echo "ACTION: Check .pipsgox/backend.log for the full error."
  echo "ACTION: Run ./scripts/pipsgox-doctor.sh"
  exit 1
fi

if ! start_frontend; then
  echo
  echo "STATUS: FAIL — PIPSGOX start failed: frontend"
  echo "ACTION: Check .pipsgox/frontend.log for the full error."
  echo "ACTION: Run ./scripts/pipsgox-doctor.sh"
  exit 1
fi

echo
echo "FINAL STATUS"
echo "============"
echo "STATUS: PASS — Backend process and Frontend process started"
echo
echo "HEALTH CHECK"
echo "============"
HEALTH="$(curl -fsS --max-time 3 http://127.0.0.1:8000/health || true)"
if [[ -n "$HEALTH" ]]; then
  echo "STATUS: PASS — Backend health"
  echo "$HEALTH"
  python - "$HEALTH" <<'PY'
import json
import sys

try:
    health = json.loads(sys.argv[1])
except Exception:
    print("STATUS: WARN — Health response could not be parsed.")
    raise SystemExit(0)

print(f"STATUS: {'PASS' if health.get('server') == 'running' else 'WARN'} — Server: {health.get('server', 'unknown')}")
print(f"STATUS: {'PASS' if health.get('user_configured') else 'INFO'} — Owner account: {'configured' if health.get('user_configured') else 'not configured'}")
print(
    f"STATUS: {'PASS' if health.get('broker_accounts_configured') else 'INFO'} — "
    f"Broker accounts: {health.get('broker_account_count', 0)}"
)
print(
    f"STATUS: {'PASS' if health.get('broker_connected') else 'INFO'} — "
    f"Connected brokers: {health.get('connected_broker_count', 0)}"
)
print(f"STATUS: {'PASS' if health.get('ready') else 'INFO'} — System ready: {'yes' if health.get('ready') else 'no'}")
PY
else
  echo "STATUS: FAIL — Backend health unavailable"
fi

# FYERS status is account/session protected now, so the dev startup script
# must not treat an unauthenticated 401 as "credentials not configured".
echo "STATUS: INFO — Broker connection is checked from the PIPSGOX web UI after owner login."

echo
echo "STATUS: PASS — PIPSGOX is running and health checks completed"

echo "WEB: $WEB_URL"
echo "API: $API_URL"
