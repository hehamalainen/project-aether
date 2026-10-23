#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT_DIR}/.run"
LOG_DIR="${RUN_DIR}/logs"
DEMO_HOME="${AETHER_DEMO_HOME:-${HOME}}"
BACKEND_PID_FILE="${RUN_DIR}/aether_server.pid"
UI_PID_FILE="${RUN_DIR}/aether_ui.pid"
BACKEND_LOG="${LOG_DIR}/aether_server.log"
UI_LOG="${LOG_DIR}/aether_ui.log"
BACKEND_PORT=8100
UI_PORT=3000

BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}/v1/health"
UI_URL="http://127.0.0.1:${UI_PORT}"

mkdir -p "${LOG_DIR}"
mkdir -p "${DEMO_HOME}"

is_pid_running() {
  local pid="${1:-}"
  [[ -n "${pid}" ]] && [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null
}

read_pid() {
  local file="$1"
  if [[ -f "${file}" ]]; then
    tr -d '[:space:]' < "${file}"
    return 0
  fi
  return 1
}

wait_for_http() {
  local url="$1"
  local timeout_seconds="$2"
  local start_ts
  start_ts="$(date +%s)"
  while true; do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    if (( "$(date +%s)" - start_ts >= timeout_seconds )); then
      return 1
    fi
    sleep 1
  done
}

is_healthy() {
  local url="$1"
  curl -fsS "${url}" >/dev/null 2>&1
}

discover_listen_pid() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
  fi
}

start_backend() {
  local pid=""
  pid="$(read_pid "${BACKEND_PID_FILE}" || true)"
  if is_healthy "${BACKEND_URL}"; then
    local existing_pid=""
    existing_pid="$(discover_listen_pid "${BACKEND_PORT}")"
    if is_pid_running "${existing_pid}"; then
      echo "${existing_pid}" > "${BACKEND_PID_FILE}"
      echo "backend: already healthy (pid=${existing_pid})"
    else
      echo "backend: already healthy (pid=unknown)"
    fi
    return 0
  fi
  if is_pid_running "${pid}"; then
    echo "backend: already running (pid=${pid})"
    return 0
  fi
  rm -f "${BACKEND_PID_FILE}"

  echo "backend: starting..."
  cd "${ROOT_DIR}"
  HOME="${DEMO_HOME}" nohup "${ROOT_DIR}/.venv/bin/python" -m apps.aether_server.main >"${BACKEND_LOG}" 2>&1 &
  pid="$!"
  echo "${pid}" > "${BACKEND_PID_FILE}"
  if wait_for_http "${BACKEND_URL}" 30; then
    echo "backend: healthy (pid=${pid})"
  else
    echo "backend: started but health check failed, see ${BACKEND_LOG}"
    return 1
  fi
}

start_ui() {
  local pid=""
  pid="$(read_pid "${UI_PID_FILE}" || true)"
  if is_healthy "${UI_URL}"; then
    local existing_pid=""
    existing_pid="$(discover_listen_pid "${UI_PORT}")"
    if is_pid_running "${existing_pid}"; then
      echo "${existing_pid}" > "${UI_PID_FILE}"
      echo "ui: already healthy (pid=${existing_pid})"
    else
      echo "ui: already healthy (pid=unknown)"
    fi
    return 0
  fi
  if is_pid_running "${pid}"; then
    echo "ui: already running (pid=${pid})"
    return 0
  fi
  rm -f "${UI_PID_FILE}"

  echo "ui: starting..."
  (
    cd "${ROOT_DIR}/apps/aether_ui"
    nohup npm run dev -- --port 3000 >"${UI_LOG}" 2>&1 &
    echo "$!" > "${UI_PID_FILE}"
  )
  pid="$(read_pid "${UI_PID_FILE}" || true)"
  if wait_for_http "${UI_URL}" 45; then
    echo "ui: healthy (pid=${pid})"
  else
    echo "ui: started but health check failed, see ${UI_LOG}"
    return 1
  fi
}

stop_service() {
  local name="$1"
  local pid_file="$2"
  local pid=""
  pid="$(read_pid "${pid_file}" || true)"

  if ! is_pid_running "${pid}"; then
    rm -f "${pid_file}"
    echo "${name}: not running"
    return 0
  fi

  kill "${pid}" 2>/dev/null || true
  for _ in {1..8}; do
    if ! is_pid_running "${pid}"; then
      rm -f "${pid_file}"
      echo "${name}: stopped"
      return 0
    fi
    sleep 1
  done
  kill -9 "${pid}" 2>/dev/null || true
  rm -f "${pid_file}"
  echo "${name}: stopped (forced)"
}

status() {
  local ok=0
  local backend_pid=""
  local ui_pid=""
  local discovered_pid=""

  backend_pid="$(read_pid "${BACKEND_PID_FILE}" || true)"
  ui_pid="$(read_pid "${UI_PID_FILE}" || true)"

  if is_pid_running "${backend_pid}"; then
    echo "backend: running (pid=${backend_pid})"
  elif is_healthy "${BACKEND_URL}"; then
    discovered_pid="$(discover_listen_pid "${BACKEND_PORT}")"
    if is_pid_running "${discovered_pid}"; then
      echo "${discovered_pid}" > "${BACKEND_PID_FILE}"
      echo "backend: running (pid=${discovered_pid}, recovered)"
    else
      echo "backend: running (pid=unknown)"
    fi
  else
    echo "backend: down"
    ok=1
  fi

  if is_healthy "${BACKEND_URL}"; then
    echo "backend health: ok (${BACKEND_URL})"
  else
    echo "backend health: fail (${BACKEND_URL})"
    ok=1
  fi

  if is_pid_running "${ui_pid}"; then
    echo "ui: running (pid=${ui_pid})"
  elif is_healthy "${UI_URL}"; then
    discovered_pid="$(discover_listen_pid "${UI_PORT}")"
    if is_pid_running "${discovered_pid}"; then
      echo "${discovered_pid}" > "${UI_PID_FILE}"
      echo "ui: running (pid=${discovered_pid}, recovered)"
    else
      echo "ui: running (pid=unknown)"
    fi
  else
    echo "ui: down"
    ok=1
  fi

  if is_healthy "${UI_URL}"; then
    echo "ui health: ok (${UI_URL})"
  else
    echo "ui health: fail (${UI_URL})"
    ok=1
  fi

  return "${ok}"
}

usage() {
  cat <<'EOF'
Usage: scripts/demo_stack.sh <start|stop|restart|status>
Env:
  AETHER_DEMO_HOME=/path  # optional HOME override for backend process
EOF
}

cmd="${1:-status}"
case "${cmd}" in
  start)
    start_backend
    start_ui
    status
    ;;
  stop)
    stop_service "ui" "${UI_PID_FILE}"
    stop_service "backend" "${BACKEND_PID_FILE}"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status
    ;;
  *)
    usage
    exit 1
    ;;
esac
