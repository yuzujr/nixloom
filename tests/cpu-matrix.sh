#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${NIXLOOM_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/nixloom}"
mkdir -p "${STATE_DIR}"
cd "${STATE_DIR}"

PORT=18080
PROMPT_TOKENS=4096
MAX_TOKENS=256
REPEATS=2

usage() {
  cat <<'EOF'
Usage: ./tests/run.sh cpu-matrix [options]

Stops the managed stack if necessary, benchmarks 8/12/16 llama.cpp CPU
threads unbound and pinned across both 9955HX CCDs, compares single-CCD
controls, then restores the stack. Results are saved under .benchmarks/.

Options:
  --prompt-tokens N  Long-prompt target (default: 4096)
  --max-tokens N     Generated-token cap (default: 256)
  --repeats N        Runs per profile, prompt cache disabled (default: 2)
  --port N           Temporary direct llama-server port (default: 18080)
  -h, --help         Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-tokens)
      [[ $# -ge 2 ]] || { printf '%s requires a value\n' "$1" >&2; exit 2; }
      PROMPT_TOKENS="$2"
      shift 2
      ;;
    --max-tokens)
      [[ $# -ge 2 ]] || { printf '%s requires a value\n' "$1" >&2; exit 2; }
      MAX_TOKENS="$2"
      shift 2
      ;;
    --repeats)
      [[ $# -ge 2 ]] || { printf '%s requires a value\n' "$1" >&2; exit 2; }
      REPEATS="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { printf '%s requires a value\n' "$1" >&2; exit 2; }
      PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

for value_name in PORT PROMPT_TOKENS MAX_TOKENS REPEATS; do
  if ! [[ "${!value_name}" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s must be a positive integer (got %q).\n' "${value_name}" "${!value_name}" >&2
    exit 2
  fi
done
if (( PROMPT_TOKENS < 256 || MAX_TOKENS < 32 )); then
  printf 'prompt tokens must be >= 256 and max tokens must be >= 32.\n' >&2
  exit 2
fi
if curl -fsS -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  printf 'Port %s already has a healthy service; choose another --port.\n' "${PORT}" >&2
  exit 1
fi

# Verified on the Ryzen 9 9955HX:
#   CCD0 physical 0-7, SMT siblings 16-23
#   CCD1 physical 8-15, SMT siblings 24-31
if [[ "$(lscpu -p=CPU,CORE,CACHE | grep -v '^#' | wc -l)" -ne 32 ]] \
  || [[ "$(< /sys/devices/system/cpu/cpu0/cache/index3/shared_cpu_list)" != "0-7,16-23" ]] \
  || [[ "$(< /sys/devices/system/cpu/cpu8/cache/index3/shared_cpu_list)" != "8-15,24-31" ]]; then
  printf 'CPU topology is not the expected 9955HX dual-CCD layout; refusing hard-coded affinity masks.\n' >&2
  exit 1
fi

stack_was_running=0
if systemctl --user --quiet is-active nixloom.target 2>/dev/null; then
  stack_was_running=1
fi

stamp="$(date +%Y%m%d-%H%M%S)"
RESULT_DIR="${STATE_DIR}/.benchmarks/cpu-${stamp}"
mkdir -p "${RESULT_DIR}"
server_pid=""

stop_server() {
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill -TERM -- "-${server_pid}" 2>/dev/null || kill -TERM "${server_pid}" 2>/dev/null || true
    for _ in $(seq 1 100); do
      kill -0 "${server_pid}" 2>/dev/null || break
      sleep 0.1
    done
  fi
  server_pid=""
}

restore_stack() {
  local rc=$?
  stop_server
  if [[ "${stack_was_running}" == "1" ]] \
    && ! systemctl --user --quiet is-active nixloom.target 2>/dev/null; then
    printf '\n==> restoring NixLoom services\n'
    systemctl --user start nixloom.target
  fi
  exit "${rc}"
}
trap restore_stack EXIT INT TERM

if [[ "${stack_was_running}" == "1" ]]; then
  printf '==> stopping NixLoom services for direct, isolated measurements\n'
  systemctl --user stop nixloom.target
fi

labels=(
  current-t16-b32-unbound
  t8-b8-unbound
  t12-b12-unbound
  t16-b16-unbound
  t8-b8-dual-ccd
  t12-b12-dual-ccd
  t16-b16-dual-ccd
  t8-b8-ccd0
  t16-b16-ccd0-smt
  current-t16-b32-unbound-repeat
)
threads=(16 8 12 16 8 12 16 8 16 16)
batch_threads=(32 8 12 16 8 12 16 8 16 32)
affinities=("" "" "" "" "0-3,8-11" "0-5,8-13" "0-15" "0-7" "0-7,16-23" "")

printf 'label\tthreads\tthreads_batch\taffinity\tttft_s\tprompt_tps\tgeneration_tps\tprompt_n\tpredicted_n\tresult\n' \
  >"${RESULT_DIR}/summary.tsv"

for index in "${!labels[@]}"; do
  label="${labels[index]}"
  thread_count="${threads[index]}"
  batch_count="${batch_threads[index]}"
  affinity="${affinities[index]}"
  result_file="${RESULT_DIR}/${label}.json"
  log_file="${RESULT_DIR}/${label}.server.log"
  printf '\n==> %s (threads=%s, batch=%s, affinity=%s)\n' \
    "${label}" "${thread_count}" "${batch_count}" "${affinity:-unbound}"

  command=(
    "${NIXLOOM_LIBEXEC}/scripts/llama.sh"
    --port "${PORT}"
    --threads "${thread_count}"
    --threads-batch "${batch_count}"
  )
  if [[ -n "${affinity}" ]]; then
    setsid taskset -c "${affinity}" "${command[@]}" >"${log_file}" 2>&1 &
  else
    setsid "${command[@]}" >"${log_file}" 2>&1 &
  fi
  server_pid="$!"

  ready=0
  for _ in $(seq 1 240); do
    if ! kill -0 "${server_pid}" 2>/dev/null; then
      break
    fi
    if curl -fsS -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "${ready}" != "1" ]]; then
    printf 'llama-server failed to become ready; see %s\n' "${log_file}" >&2
    exit 1
  fi

  "${NIXLOOM_LIBEXEC}/tests/benchmark.py" \
    --base-url "http://127.0.0.1:${PORT}" \
    --suite performance \
    --prompt-tokens "${PROMPT_TOKENS}" \
    --max-tokens "${MAX_TOKENS}" \
    --repeats "${REPEATS}" \
    --label "${label}" \
    --server-pid "${server_pid}" \
    --output "${result_file}"

  jq -r --arg threads "${thread_count}" --arg batch "${batch_count}" \
    --arg affinity "${affinity:-unbound}" --arg result "${result_file}" '
      [.label, $threads, $batch, $affinity,
       .performance.summary.ttft_seconds_median,
       .performance.summary.prompt_tokens_per_second_median,
       .performance.summary.generation_tokens_per_second_median,
       .performance.summary.prompt_tokens_median,
       .performance.summary.predicted_tokens_median,
       $result] | @tsv
    ' "${result_file}" >>"${RESULT_DIR}/summary.tsv"
  stop_server
done

jq -s '{schema_version: 1, results: .}' "${RESULT_DIR}"/*.json \
  >"${RESULT_DIR}/matrix.json"

printf '\n%-38s %8s %10s %10s %10s\n' 'profile' 'TTFT(s)' 'prompt/s' 'gen/s' 'affinity'
tail -n +2 "${RESULT_DIR}/summary.tsv" \
  | sort -t $'\t' -k7,7nr \
  | awk -F '\t' '{printf "%-38s %8.3f %10.1f %10.1f %10s\n", $1, $5, $6, $7, $4}'
printf '\nSaved matrix: %s\n' "${RESULT_DIR}/matrix.json"
