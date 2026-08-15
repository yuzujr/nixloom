#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROJECT_DIR="${NIXLOOM_ROOT:-${NIXLOOM_LIBEXEC}}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"

CONFIG_ARG=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/sillytavern.sh [options]

Starts SillyTavern configured from config.yaml. When deployment.remote is set it
listens for the Tailnet with basic auth and the 100.64.0.0/10 whitelist.
All state lives under .sillytavern/ in the writable state directory.

After the first start, configure SillyTavern in the UI:
  API:       Chat Completion
  Source:    Custom OpenAI-compatible
  Base URL:  http://127.0.0.1:8080/v1
  API key:   none
  Model:     qwen

Options:
  --config FILE   Config file (default: ./config.yaml)
  --dry-run       Print command and key settings without running it
  -h, --help      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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
load_env_file "${PROJECT_DIR}"

SILLY_MODEL="$(llm_id)"
SILLY_PRESET="$(cfg_required '.sillytavern.preset' 'sillytavern.preset')"
REMOTE="${NIXLOOM_REMOTE:-$(cfg_bool_required '.deployment.remote' 'deployment.remote')}"
case "${REMOTE,,}" in
  1|true|yes|on) REMOTE=1 ;;
  0|false|no|off) REMOTE=0 ;;
  *)
    printf 'NIXLOOM_REMOTE must be a boolean (got %q).\n' "${REMOTE}" >&2
    exit 2
    ;;
esac
PORT="$(cfg_required '.ports.sillytavern' 'ports.sillytavern')"
AUTH_USER="${SILLYTAVERN_AUTH_USER:-$(cfg_required '.sillytavern.auth_user' 'sillytavern.auth_user')}"
# The password is a secret and lives only in the gitignored .env, never in
# the tracked config.yaml.
AUTH_PASSWORD="${SILLYTAVERN_AUTH_PASSWORD:-}"

HOST="127.0.0.1"
AUTH=0
TAILNET_WHITELIST=0
if [[ "${REMOTE}" == "1" ]]; then
  HOST="0.0.0.0"
  AUTH=1
  TAILNET_WHITELIST=1
fi

SILLY_STATE_DIR="${PROJECT_DIR}/.sillytavern"
export XDG_DATA_HOME="${SILLY_STATE_DIR}/xdg-data"
export XDG_CONFIG_HOME="${SILLY_STATE_DIR}/xdg-config"
export XDG_CACHE_HOME="${SILLY_STATE_DIR}/xdg-cache"
export XDG_STATE_HOME="${SILLY_STATE_DIR}/xdg-state"
if [[ "${AUTH}" == "1" ]]; then
  if [[ -z "${AUTH_PASSWORD}" ]]; then
    printf 'deployment.remote requires SILLYTAVERN_AUTH_PASSWORD in %s/.env.\n' "${PROJECT_DIR}" >&2
    exit 2
  fi
  export SILLYTAVERN_BASICAUTHUSER_USERNAME="${AUTH_USER}"
  export SILLYTAVERN_BASICAUTHUSER_PASSWORD="${AUTH_PASSWORD}"
fi

export SILLYTAVERN_WHITELIST='["::1","127.0.0.1"]'
if [[ "${TAILNET_WHITELIST}" == "1" ]]; then
  export SILLYTAVERN_WHITELIST='["::1","127.0.0.1","100.64.0.0/10"]'
fi
# Block server-side requests to private networks except the loopback model
# proxy used by this deployment.
export SILLYTAVERN_PRIVATEADDRESSWHITELIST_ENABLED="true"
export SILLYTAVERN_PRIVATEADDRESSWHITELIST_ALLOWUNRESOLVEDHOSTS="false"
export SILLYTAVERN_PRIVATEADDRESSWHITELIST_ALLOWEDRANGES='["127.0.0.0/8","::1/128"]'

listen=false
if [[ "${HOST}" != "127.0.0.1" && "${HOST}" != "localhost" ]]; then
  listen=true
fi

args=(
  --port "${PORT}"
  --browserLaunchEnabled false
  --enableIPv4 true
  --enableIPv6 false
  --listen "${listen}"
  --listenAddressIPv4 "${HOST}"
  --whitelist true
)

if [[ "${AUTH}" == "1" ]]; then
  args+=(--basicAuthMode true)
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'XDG_DATA_HOME=%q\n' "${XDG_DATA_HOME}"
  printf 'SILLYTAVERN_WHITELIST=%q\n' "${SILLYTAVERN_WHITELIST}"
  printf 'SILLYTAVERN_PRIVATEADDRESSWHITELIST_ENABLED=%q\n' \
    "${SILLYTAVERN_PRIVATEADDRESSWHITELIST_ENABLED}"
  printf 'SILLYTAVERN_PRIVATEADDRESSWHITELIST_ALLOWEDRANGES=%q\n' \
    "${SILLYTAVERN_PRIVATEADDRESSWHITELIST_ALLOWEDRANGES}"
  printf 'SILLYTAVERN_MODEL=%q\n' "${SILLY_MODEL}"
  printf 'SILLYTAVERN_PRESET=%q\n' "${SILLY_PRESET}"
  if [[ "${AUTH}" == "1" ]]; then
    printf 'SILLYTAVERN_BASICAUTHUSER_USERNAME=%q\n' "${SILLYTAVERN_BASICAUTHUSER_USERNAME}"
    printf 'SILLYTAVERN_BASICAUTHUSER_PASSWORD=<set>\n'
  fi
  printf '%q ' sillytavern "${args[@]}"
  printf '\n'
  exit 0
fi

mkdir -p "${XDG_DATA_HOME}" "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${XDG_STATE_HOME}"

# SillyTavern persists its selected model and sampling preset in user state.
# Synchronize the managed local profile on startup so changing config.yaml is
# enough; character cards, prompts and unrelated connections are preserved.
python3 "${NIXLOOM_LIBEXEC}/scripts/sync-sillytavern.py" \
  --settings "${XDG_DATA_HOME}/SillyTavern/data/default-user/settings.json" \
  --model "${SILLY_MODEL}" \
  --preset "${SILLY_PRESET}" \
  --base-url "http://127.0.0.1:$(cfg_required '.ports.llama' 'ports.llama')/v1" \
  --context "$(llm_context)" \
  --max-tokens "$(llm_max_tokens)" \
  --temperature "$(llm_cfg '.sampling.temperature')" \
  --frequency-penalty "$(llm_cfg '.sampling.frequency_penalty')" \
  --presence-penalty "$(llm_cfg '.sampling.presence_penalty')" \
  --top-p "$(llm_cfg '.sampling.top_p')" \
  --top-k "$(llm_cfg '.sampling.top_k')" \
  --min-p "$(llm_cfg '.sampling.min_p')" \
  --repetition-penalty "$(llm_cfg '.sampling.repeat_penalty')" \
  || printf 'warning: SillyTavern local profile sync failed; continuing.\n' >&2

printf 'Starting SillyTavern on http://%s:%s\n' "${HOST}" "${PORT}"
exec sillytavern "${args[@]}"
