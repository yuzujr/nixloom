#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROJECT_DIR="${NIXLOOM_ROOT:-${NIXLOOM_LIBEXEC}}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"

HOST="127.0.0.1"
PORT=""
CONFIG_ARG=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/swap.sh [options]

Starts llama-swap with the configured LLM and the optional hidden SD backend.
Thinking and vision use the same LLM process; only SD causes a VRAM swap.

Options:
  --host HOST     Bind host (default: 127.0.0.1)
  --port PORT     Bind port (default: ports.llama from config)
  --config FILE   Config file (default: ./config.yaml)
  --dry-run       Print the generated config and command without running
  -h, --help      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      require_opt_value "$1" $#
      HOST="$2"
      shift 2
      ;;
    --port)
      require_opt_value "$1" $#
      PORT="$2"
      shift 2
      ;;
    --config)
      require_opt_value "$1" $#
      CONFIG_ARG="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

resolve_config_file "${PROJECT_DIR}" "${CONFIG_ARG}"
use_project_caches "${PROJECT_DIR}"

PORT="${PORT:-$(cfg_required '.ports.llama' 'ports.llama')}"
SD="$(cfg_bool_required '.images.enabled' 'images.enabled')"
MODEL_ID="$(llm_id)"
SWAP_CONFIG_FILE="${PROJECT_DIR}/.cache/llama-swap.yaml"

# ${PORT} below is a llama-swap macro, not a shell variable: llama-swap picks
# a free internal port per model. No groups section: llama-swap's default is
# one loaded model at a time, which is the whole point on 8GB of VRAM.
config_content="healthCheckTimeout: 7200
logLevel: info
models:
  ${MODEL_ID}:
    cmd: |
      ${NIXLOOM_LIBEXEC}/scripts/llama.sh --config ${NIXLOOM_CONFIG_FILE} --port \${PORT}
    checkEndpoint: /health
"

# Everything below is interpolated into a YAML key and a command line that
# llama-swap re-parses. A name or path that needs quoting there produces a
# model entry that simply never starts, and llama-swap reports only that the
# model is failing — so reject it here, where the cause is still visible.
if [[ "${NIXLOOM_LIBEXEC}" == *[[:space:]]* || "${NIXLOOM_CONFIG_FILE}" == *[[:space:]]* ]]; then
  printf 'llama-swap splits the generated cmd on whitespace; the project path and config path must not contain any.\n' >&2
  printf '  launcher: %s\n  config:   %s\n' "${NIXLOOM_LIBEXEC}" "${NIXLOOM_CONFIG_FILE}" >&2
  exit 2
fi

if [[ "${SD}" == "1" ]]; then
  # unlisted: image generation goes through /upstream/sd, so keep sd out of
  # the chat model dropdowns fed by /v1/models.
  config_content+="  sd:
    cmd: |
      ${NIXLOOM_LIBEXEC}/scripts/sd.sh --config ${NIXLOOM_CONFIG_FILE} --port \${PORT}
    checkEndpoint: /api/v1/model
    ttl: 300
    unlisted: true
"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s' "${config_content}"
  printf '%q' llama-swap
  printf ' %q' --config "${SWAP_CONFIG_FILE}" --listen "${HOST}:${PORT}" --watch-config
  printf '\n'
  exit 0
fi

mkdir -p "$(dirname "${SWAP_CONFIG_FILE}")"
printf '%s' "${config_content}" >"${SWAP_CONFIG_FILE}"

exec llama-swap \
  --config "${SWAP_CONFIG_FILE}" \
  --listen "${HOST}:${PORT}" \
  --watch-config
