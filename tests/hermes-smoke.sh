#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROJECT_DIR="${NIXLOOM_ROOT:-${NIXLOOM_LIBEXEC}}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"
resolve_config_file "${PROJECT_DIR}" ""

BASE_URL=""

usage() {
  cat <<'EOF'
Usage: HERMES_API_KEY=... ./tests/run.sh hermes [--base-url URL]

Runs a real Hermes Responses API turn and requires the local model to invoke
the terminal tool successfully. Pass the gateway key through the environment
so it never appears in the process list.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      [[ $# -ge 2 ]] || { printf '%s requires a value\n' "$1" >&2; exit 2; }
      BASE_URL="${2%/}"
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

BASE_URL="${BASE_URL:-http://127.0.0.1:$(cfg_required '.ports.hermes' 'ports.hermes')}"

if [[ -z "${HERMES_API_KEY:-}" ]]; then
  printf 'HERMES_API_KEY is required.\n' >&2
  exit 2
fi

work_dir="$(mktemp -d)"
trap 'rm -r -- "${work_dir}"' EXIT
output="${work_dir}/response.json"
payload="$(jq -cn '{
  model: "hermes-agent",
  input: "必须调用 terminal 工具执行 pwd，然后只报告工具实际返回的绝对路径。不要凭记忆猜测。",
  stream: false,
  store: false
}')"

curl --max-time 10 -fsS "${BASE_URL}/health" >/dev/null
status="$(curl --max-time 900 -sS -o "${output}" -w '%{http_code}' \
  -H "Authorization: Bearer ${HERMES_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d "${payload}" \
  "${BASE_URL}/v1/responses" || true)"

if [[ "${status}" != "200" ]]; then
  printf 'Hermes returned HTTP %s\n' "${status:-000}" >&2
  sed -n '1,40p' "${output}" >&2
  exit 1
fi

if ! jq -e '
  .status == "completed"
  and any(.output[]?; .type == "function_call")
  and any(.output[]?; .type == "function_call_output")
  and ([.output[]? | select(.type == "message") | .content[]?.text // empty] | join("") | length > 0)
' "${output}" >/dev/null; then
  printf 'Hermes did not complete a terminal-backed response.\n' >&2
  jq '{status,output,usage}' "${output}" >&2
  exit 1
fi

jq -r '
  "hermes: ok, tools="
  + ([.output[]? | select(.type == "function_call") | .name] | join(","))
  + ", input=" + (.usage.input_tokens | tostring)
  + ", output=" + (.usage.output_tokens | tostring)
' "${output}"
