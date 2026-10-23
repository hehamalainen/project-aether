#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

artifact_root="${AETHER_RUNTIME_ARTIFACT_ROOT:-$HOME/.aether/runtime_artifacts}"
phase1_artifact_dir="${artifact_root%/}/phase1"
mkdir -p "$phase1_artifact_dir"
timestamp="$(date +%Y%m%d-%H%M%S)"
run_class="${AETHER_SWARM_RUN_CLASS:-canonical_baseline}"
artifact_prefix="swarm-safe"
if [[ "${run_class}" != "canonical_baseline" ]]; then
  artifact_prefix="swarm-safe-${run_class}"
fi
default_json="${phase1_artifact_dir}/${artifact_prefix}-${timestamp}.json"
log_path="${phase1_artifact_dir}/${artifact_prefix}-${timestamp}.log"
heartbeat_path="${phase1_artifact_dir}/${artifact_prefix}-${timestamp}.heartbeat.log"

heartbeat_seconds="${AETHER_SAFE_HEARTBEAT_SECONDS:-30}"
term_grace_seconds="${AETHER_SAFE_TERM_GRACE_SECONDS:-30}"
post_json_grace_seconds="${AETHER_SAFE_POST_JSON_GRACE_SECONDS:-90}"
timeout_buffer_seconds="${AETHER_SAFE_TIMEOUT_BUFFER_SECONDS:-600}"

cmd=(.venv/bin/python -u -m apps.aether_cli.main swarm-harness)
has_output_json=0
resolved_json_path="$default_json"
resolved_run_seconds=0
resolved_drain_seconds=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  arg="${args[$i]}"
  if [[ "$arg" == "--run-seconds" ]]; then
    if (( i + 1 < ${#args[@]} )); then
      resolved_run_seconds="${args[$((i + 1))]}"
    fi
    continue
  fi
  if [[ "$arg" == --run-seconds=* ]]; then
    resolved_run_seconds="${arg#--run-seconds=}"
    continue
  fi
  if [[ "$arg" == "--drain-seconds" ]]; then
    if (( i + 1 < ${#args[@]} )); then
      resolved_drain_seconds="${args[$((i + 1))]}"
    fi
    continue
  fi
  if [[ "$arg" == --drain-seconds=* ]]; then
    resolved_drain_seconds="${arg#--drain-seconds=}"
    continue
  fi
  if [[ "$arg" == "--output-json" ]]; then
    has_output_json=1
    if (( i + 1 < ${#args[@]} )); then
      resolved_json_path="${args[$((i + 1))]}"
    fi
    break
  fi
  if [[ "$arg" == --output-json=* ]]; then
    has_output_json=1
    resolved_json_path="${arg#--output-json=}"
    break
  fi
done
cmd+=("$@")
if [[ "$has_output_json" -eq 0 ]]; then
  cmd+=(--output-json "$resolved_json_path")
fi

if [[ "${run_class}" == "mixed_source_stress" ]]; then
  cmd+=(--traffic-mix "mixed")
fi

if [[ -n "${AETHER_EXTERNAL_API_URL:-}" ]]; then
  cmd+=(--target-mode "external" --external-api-url "${AETHER_EXTERNAL_API_URL}")
fi

if [[ -n "${AETHER_SAFE_TIMEOUT_SECONDS:-}" ]]; then
  timeout_seconds="${AETHER_SAFE_TIMEOUT_SECONDS}"
elif (( resolved_run_seconds > 0 )); then
  timeout_seconds="$((resolved_run_seconds + resolved_drain_seconds + post_json_grace_seconds + term_grace_seconds + timeout_buffer_seconds))"
else
  timeout_seconds=7200
fi

{
  echo "start=$(date '+%Y-%m-%d %H:%M:%S')"
  printf "cmd="
  printf "%q " "${cmd[@]}"
  echo
  echo "timeout_seconds=${timeout_seconds}"
  echo "heartbeat_seconds=${heartbeat_seconds}"
  echo "term_grace_seconds=${term_grace_seconds}"
  echo "post_json_grace_seconds=${post_json_grace_seconds}"
  echo "timeout_buffer_seconds=${timeout_buffer_seconds}"
  echo "json_path=${resolved_json_path}"
  echo "artifact_root=${artifact_root}"
  echo "run_class=${run_class}"
  echo "run_seconds=${resolved_run_seconds}"
  echo "drain_seconds=${resolved_drain_seconds}"
} > "$heartbeat_path"

policy_output="$(.venv/bin/python -m packages.aether.soak_policy --run-seconds "${resolved_run_seconds}" --run-class "${run_class}" 2>&1)" || {
  printf '%s\n' "${policy_output}" | tee -a "$heartbeat_path" >&2
  exit 1
}
printf '%s\n' "${policy_output}" >> "$heartbeat_path"

echo "LOG=$log_path"
echo "HEARTBEAT=$heartbeat_path"
echo "JSON=$resolved_json_path"

# A stale JSON from a prior aborted run would trigger the post-json watchdog immediately.
rm -f "$resolved_json_path"

start_epoch="$(date +%s)"

/usr/bin/caffeinate -dimsu &
caffeinate_pid=$!
"${cmd[@]}" >"$log_path" 2>&1 &
pid=$!
echo "pid=${pid}" >> "$heartbeat_path"
echo "caffeinate_pid=${caffeinate_pid}" >> "$heartbeat_path"

stop_keep_awake() {
  if kill -0 "$caffeinate_pid" >/dev/null 2>&1; then
    kill "$caffeinate_pid" >/dev/null 2>&1 || true
    wait "$caffeinate_pid" >/dev/null 2>&1 || true
  fi
}

terminate_main_process() {
  local reason="$1"
  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "${reason}=1 sending_sigterm" | tee -a "$heartbeat_path" >/dev/null
    kill -TERM "$pid" >/dev/null 2>&1 || true
    local grace_start
    grace_start="$(date +%s)"
    while kill -0 "$pid" >/dev/null 2>&1; do
      local grace_now
      grace_now="$(date +%s)"
      if (( grace_now - grace_start >= term_grace_seconds )); then
        echo "term_grace_exceeded=1 sending_sigkill" | tee -a "$heartbeat_path" >/dev/null
        kill -KILL "$pid" >/dev/null 2>&1 || true
        break
      fi
      sleep 1
    done
  fi
}

cleanup() {
  terminate_main_process "signal_interrupt"
  stop_keep_awake
}
trap cleanup INT TERM

timed_out=0
post_json_forced_stop=0
json_seen_epoch=0
while kill -0 "$pid" >/dev/null 2>&1; do
  now_epoch="$(date +%s)"
  elapsed="$((now_epoch - start_epoch))"
  if [[ -f "$log_path" ]]; then
    log_bytes="$(wc -c < "$log_path")"
  else
    log_bytes=0
  fi
  echo "t+${elapsed}s log_bytes=${log_bytes} rc=None" | tee -a "$heartbeat_path" >/dev/null

  if [[ -s "$resolved_json_path" ]]; then
    if (( json_seen_epoch == 0 )); then
      json_seen_epoch="$now_epoch"
      echo "json_detected=1 t+${elapsed}s" | tee -a "$heartbeat_path" >/dev/null
    elif (( now_epoch - json_seen_epoch >= post_json_grace_seconds )); then
      post_json_forced_stop=1
      terminate_main_process "post_json_process_still_alive"
      break
    fi
  fi

  if (( elapsed >= timeout_seconds )); then
    timed_out=1
    terminate_main_process "timeout_reached"
    break
  fi

  sleep "$heartbeat_seconds"
done

if wait "$pid" >/dev/null 2>&1; then
  rc=0
else
  rc=$?
fi
stop_keep_awake

end_epoch="$(date +%s)"
total_elapsed="$((end_epoch - start_epoch))"
{
  echo "end_rc=${rc}"
  echo "timed_out=${timed_out}"
  echo "post_json_forced_stop=${post_json_forced_stop}"
  echo "elapsed_seconds=${total_elapsed}"
  echo "end=$(date '+%Y-%m-%d %H:%M:%S')"
} | tee -a "$heartbeat_path" >/dev/null

echo "done json=$resolved_json_path log=$log_path hb=$heartbeat_path rc=$rc"
exit "$rc"
