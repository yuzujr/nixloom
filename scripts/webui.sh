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
Usage: ./scripts/webui.sh [options]

Starts Open WebUI configured from config.yaml. Per-model generation settings
live in the llama-server profiles, not here; this script wires up the
backends, the built-in tools and the compaction threshold.

Options:
  --config FILE   Config file (default: ./config.yaml)
  --dry-run       Print the command and key settings without running it
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
use_project_caches "${PROJECT_DIR}"
load_env_file "${PROJECT_DIR}"

REMOTE="${NIXLOOM_REMOTE:-$(cfg_bool_required '.deployment.remote' 'deployment.remote')}"
case "${REMOTE,,}" in
  1|true|yes|on) REMOTE=1 ;;
  0|false|no|off) REMOTE=0 ;;
  *)
    printf 'NIXLOOM_REMOTE must be a boolean (got %q).\n' "${REMOTE}" >&2
    exit 2
    ;;
esac
PORT="$(cfg_required '.ports.webui' 'ports.webui')"
LLAMA_PORT="$(cfg_required '.ports.llama' 'ports.llama')"
LLAMA_URL="http://127.0.0.1:${LLAMA_PORT}/v1"
MODEL_ID="$(llm_id)"
THINK_MODEL_ID="$(thinking_model_id)"
AUTH="$(cfg_bool_required '.webui.auth' 'webui.auth')"
TITLE_GENERATION="$(cfg_bool_required '.webui.title_generation' 'webui.title_generation')"
WEB_SEARCH="$(cfg_bool_required '.webui.web_search' 'webui.web_search')"
WEB_SEARCH_RESULT_COUNT="$(cfg_required '.webui.web_search_results' 'webui.web_search_results')"
COMPACTION_THRESHOLD="$(cfg_required '.webui.compaction_threshold' 'webui.compaction_threshold')"
COMPACTION_MARGIN="$(cfg_required '.webui.compaction_margin' 'webui.compaction_margin')"
if [[ "${WEB_SEARCH}" == "1" && -z "${TAVILY_API_KEY:-}" ]]; then
  printf 'warning: TAVILY_API_KEY is absent; web search is disabled.\n' >&2
  WEB_SEARCH=0
fi
SYSTEM_PROMPT="$(webui_system_prompt)"
BUILTIN_TOOLS="$(yq -o=json -I0 '.webui.builtin_tools' "${NIXLOOM_CONFIG_FILE}")"
if [[ "${WEB_SEARCH}" == "0" ]]; then
  BUILTIN_TOOLS="$(jq -c '.web_search = false' <<<"${BUILTIN_TOOLS}")"
fi
MAX_TOKENS="$(llm_max_tokens)"
THINKING_MAX_TOKENS="$(llm_cfg '.thinking_max_tokens')"
THINKING_SAMPLING="$(yq -o=json -I0 '.llm.thinking_sampling' "${NIXLOOM_CONFIG_FILE}")"
if [[ -n "${NIXLOOM_WEBUI_ORIGINS:-}" ]]; then
  IFS=';' read -r -a CORS_ALLOW_ORIGINS <<<"${NIXLOOM_WEBUI_ORIGINS}"
else
  mapfile -t CORS_ALLOW_ORIGINS < <(cfg_list '.webui.cors_allow_origins')
fi
if (( ${#CORS_ALLOW_ORIGINS[@]} == 0 )); then
  CORS_ALLOW_ORIGINS=("http://127.0.0.1:${PORT}" "http://localhost:${PORT}")
fi
CORS_ALLOW_ORIGIN="$(IFS=';'; printf '%s' "${CORS_ALLOW_ORIGINS[*]}")"
SD="$(cfg_bool_required '.images.enabled' 'images.enabled')"
if [[ "${SD}" == "1" ]]; then
  IMAGE_PROFILE="$(image_profile)"
  IMAGE_SIZE="$(cfg_required '.images.size' 'images.size')"
  IMAGE_STEPS="$(cfg_required ".images.profiles.\"${IMAGE_PROFILE}\".steps" \
    "images.profiles.${IMAGE_PROFILE}.steps")"
  IMAGE_CFG_SCALE="$(cfg_required ".images.profiles.\"${IMAGE_PROFILE}\".cfg_scale" \
    "images.profiles.${IMAGE_PROFILE}.cfg_scale")"
  IMAGE_SAMPLER="$(cfg_required ".images.profiles.\"${IMAGE_PROFILE}\".sampler" \
    "images.profiles.${IMAGE_PROFILE}.sampler")"
  IMAGE_NEGATIVE_PROMPT="$(cfg_required ".images.profiles.\"${IMAGE_PROFILE}\".negative_prompt" \
    "images.profiles.${IMAGE_PROFILE}.negative_prompt")"
fi

HOST="127.0.0.1"
if [[ "${REMOTE}" == "1" ]]; then
  HOST="0.0.0.0"
  AUTH=1
fi

if ! [[ "${WEB_SEARCH_RESULT_COUNT}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'webui.web_search_results must be a positive integer\n' >&2
  exit 2
fi

CHAT_THRESHOLD="$(llm_compaction_threshold \
  "${MAX_TOKENS}" "${COMPACTION_THRESHOLD}" "${COMPACTION_MARGIN}")"
THINK_THRESHOLD="$(llm_compaction_threshold \
  "${THINKING_MAX_TOKENS}" "${COMPACTION_THRESHOLD}" "${COMPACTION_MARGIN}")"
MODEL_CONFIG_JSON="$(jq -cn \
  --arg model "${MODEL_ID}" \
  --arg think "${THINK_MODEL_ID}" \
  --argjson chat_threshold "${CHAT_THRESHOLD}" \
  --argjson think_threshold "${THINK_THRESHOLD}" \
  --argjson think_max "${THINKING_MAX_TOKENS}" \
  --argjson sampling "${THINKING_SAMPLING}" \
  '{
    ($model): {
      name: $model,
      base_model_id: null,
      params: {
        compact_token_threshold: $chat_threshold,
        custom_params: {chat_template_kwargs: {enable_thinking: false}}
      }
    },
    ($think): {
      name: ($model + " (think)"),
      base_model_id: $model,
      params: {
        compact_token_threshold: $think_threshold,
        max_tokens: $think_max,
        temperature: $sampling.temperature,
        top_p: $sampling.top_p,
        frequency_penalty: $sampling.frequency_penalty,
        presence_penalty: $sampling.presence_penalty,
        custom_params: {
          top_k: $sampling.top_k,
          min_p: $sampling.min_p,
          repeat_penalty: $sampling.repeat_penalty,
          thinking_budget_tokens: -1,
          chat_template_kwargs: {enable_thinking: true}
        }
      }
    }
  }')"

# llama-server owns normal sampling defaults. The virtual thinking profile
# above only supplies request-level overrides; both IDs use the same process.
DEFAULT_MODEL_PARAMS='{}'
DEFAULT_MODEL_METADATA="$(jq -cn --argjson tools "${BUILTIN_TOOLS}" '{builtinTools: $tools}')"

STATE_DIR="${PROJECT_DIR}/.webui"

bool_env() {
  if [[ "$1" == "1" ]]; then
    printf 'True'
  else
    printf 'False'
  fi
}

# WEBUI_URL is the advertised origin, not the bind address; 0.0.0.0 is never
# reachable, so in remote mode use the first non-loopback CORS origin.
WEBUI_PUBLIC_URL="http://127.0.0.1:${PORT}"
if [[ "${REMOTE}" == "1" ]]; then
  for origin in "${CORS_ALLOW_ORIGINS[@]}"; do
    case "${origin}" in
      http://127.*|https://127.*|http://localhost*|https://localhost*) ;;
      *)
        WEBUI_PUBLIC_URL="${origin}"
        break
        ;;
    esac
  done
fi

export STATIC_DIR="${STATE_DIR}/static"
export DATA_DIR="${STATE_DIR}/data"
export HF_HOME="${STATE_DIR}/hf_home"
export SENTENCE_TRANSFORMERS_HOME="${STATE_DIR}/transformers_home"
export WEBUI_URL="${WEBUI_PUBLIC_URL}"
export WEBUI_NAME="NixLoom"
export ENABLE_PERSISTENT_CONFIG="False"
export ENABLE_OLLAMA_API="False"
export OLLAMA_BASE_URLS=""
export ENABLE_OPENAI_API="True"
export OPENAI_API_BASE_URL="${LLAMA_URL}"
export OPENAI_API_KEY="none"
export DEFAULT_MODEL_PARAMS
export DEFAULT_MODEL_METADATA
export DEFAULT_MODELS="${MODEL_ID}"
export SCARF_NO_ANALYTICS="True"
export DO_NOT_TRACK="True"
export ANONYMIZED_TELEMETRY="False"
export CORS_ALLOW_ORIGIN

# Hermes appears as a second OpenAI backend; its per-run API key comes from
# the service through the environment (it is a runtime secret, not config).
if has_frontend hermes; then
  if [[ -z "${HERMES_API_KEY:-}" ]]; then
    printf 'The hermes frontend requires HERMES_API_KEY in the service environment.\n' >&2
    exit 2
  fi
  HERMES_PORT="$(cfg_required '.ports.hermes' 'ports.hermes')"
  export OPENAI_API_BASE_URLS="${LLAMA_URL};http://127.0.0.1:${HERMES_PORT}/v1"
  export OPENAI_API_KEYS="none;${HERMES_API_KEY}"
  # Keep llama.cpp on Chat Completions, but use Hermes' structured Responses
  # API so Open WebUI can render its function_call/function_call_output items.
  export OPENAI_API_CONFIGS='{"1":{"api_type":"responses"}}'
else
  export OPENAI_API_BASE_URLS="${LLAMA_URL}"
  export OPENAI_API_KEYS="none"
  export OPENAI_API_CONFIGS='{}'
fi

ENABLE_TITLE_GENERATION="$(bool_env "${TITLE_GENERATION}")"
ENABLE_WEB_SEARCH="$(bool_env "${WEB_SEARCH}")"
export \
  ENABLE_TITLE_GENERATION ENABLE_WEB_SEARCH WEB_SEARCH_RESULT_COUNT
# Background tasks use the chat's already-loaded model.
export ENABLE_TAGS_GENERATION="False"
export ENABLE_FOLLOW_UP_GENERATION="False"
export ENABLE_SEARCH_QUERY_GENERATION="False"
export ENABLE_RETRIEVAL_QUERY_GENERATION="False"
export ENABLE_AUTOCOMPLETE_GENERATION="False"
export ENABLE_IMAGE_PROMPT_GENERATION="False"
export ENABLE_VOICE_MODE_PROMPT="False"
export ENABLE_WEB_SEARCH_CONFIRMATION="False"
export WEB_SEARCH_ENGINE="tavily"
export TAVILY_EXTRACT_DEPTH="basic"
export TAVILY_API_KEY
export ENABLE_CONTEXT_COMPACTION="True"
export CONTEXT_COMPACTION_TOKEN_THRESHOLD="${COMPACTION_THRESHOLD}"
WEBUI_AUTH="$(bool_env "${AUTH}")"
export WEBUI_AUTH

# Image generation rides through llama-swap's /upstream passthrough, which
# swaps the LLM out and KoboldCpp in for the duration of the request.
if [[ "${SD}" == "1" ]]; then
  export ENABLE_IMAGE_GENERATION="True"
  export IMAGE_GENERATION_ENGINE="automatic1111"
  export AUTOMATIC1111_BASE_URL="http://127.0.0.1:${LLAMA_PORT}/upstream/sd"
  export IMAGE_SIZE IMAGE_STEPS
  # Merged verbatim into every txt2img payload by Open WebUI.
  AUTOMATIC1111_PARAMS="$(jq -cn \
    --argjson cfg "${IMAGE_CFG_SCALE}" \
    --arg sampler "${IMAGE_SAMPLER}" \
    --arg negative "${IMAGE_NEGATIVE_PROMPT}" \
    '{cfg_scale: $cfg, sampler_name: $sampler} + (if $negative != "" then {negative_prompt: $negative} else {} end)')"
  export AUTOMATIC1111_PARAMS
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'DATA_DIR=%q\n' "${DATA_DIR}"
  printf 'OPENAI_API_BASE_URL=%q\n' "${OPENAI_API_BASE_URL}"
  printf 'OPENAI_API_BASE_URLS=%q\n' "${OPENAI_API_BASE_URLS}"
  printf 'OPENAI_API_CONFIGS=%q\n' "${OPENAI_API_CONFIGS}"
  printf 'DEFAULT_MODEL_PARAMS=%q\n' "${DEFAULT_MODEL_PARAMS}"
  printf 'MODEL_SYSTEM_PROMPT=%q\n' "${SYSTEM_PROMPT}"
  printf 'DEFAULT_MODEL_METADATA=%q\n' "${DEFAULT_MODEL_METADATA}"
  printf 'DEFAULT_MODELS=%q\n' "${DEFAULT_MODELS}"
  printf 'MODEL_CONFIG_JSON=%q\n' "${MODEL_CONFIG_JSON}"
  printf 'ENABLE_TITLE_GENERATION=%q\n' "${ENABLE_TITLE_GENERATION}"
  printf 'ENABLE_WEB_SEARCH=%q\n' "${ENABLE_WEB_SEARCH}"
  printf 'WEB_SEARCH_ENGINE=%q\n' "${WEB_SEARCH_ENGINE}"
  if [[ -n "${TAVILY_API_KEY:-}" ]]; then
    printf 'TAVILY_API_KEY=<set>\n'
  else
    printf 'TAVILY_API_KEY=<missing>\n'
  fi
  printf 'WEB_SEARCH_RESULT_COUNT=%q\n' "${WEB_SEARCH_RESULT_COUNT}"
  printf 'CONTEXT_COMPACTION_TOKEN_THRESHOLD=%q\n' "${CONTEXT_COMPACTION_TOKEN_THRESHOLD}"
  printf 'WEBUI_URL=%q\n' "${WEBUI_URL}"
  printf 'WEBUI_AUTH=%q\n' "${WEBUI_AUTH}"
  printf 'CORS_ALLOW_ORIGIN=%q\n' "${CORS_ALLOW_ORIGIN}"
  if [[ "${SD}" == "1" ]]; then
    printf 'ENABLE_IMAGE_GENERATION=%q\n' "${ENABLE_IMAGE_GENERATION}"
    printf 'AUTOMATIC1111_BASE_URL=%q\n' "${AUTOMATIC1111_BASE_URL}"
    printf 'IMAGE_SIZE=%q IMAGE_STEPS=%q\n' "${IMAGE_SIZE}" "${IMAGE_STEPS}"
    printf 'AUTOMATIC1111_PARAMS=%q\n' "${AUTOMATIC1111_PARAMS}"
  fi
  printf '%q ' open-webui serve --host "${HOST}" --port "${PORT}"
  printf '\n'
  exit 0
fi

mkdir -p "${STATE_DIR}/data" "${STATE_DIR}/static" "${STATE_DIR}/hf_home" "${STATE_DIR}/transformers_home"

# Open WebUI creates both of these with the ambient umask (0644/0755 by
# default). The secret key signs session JWTs — anyone who can read it can mint
# a session — and webui.db holds the chats and account password hashes.
chmod 700 "${STATE_DIR}/data" 2>/dev/null || true
if [[ -f "${PROJECT_DIR}/.webui_secret_key" ]]; then
  chmod 600 "${PROJECT_DIR}/.webui_secret_key" 2>/dev/null || true
fi

# Best-effort: the sync writes into Open WebUI's private schema, and a failed
# sync must not keep the frontend from starting.
python3 "${NIXLOOM_LIBEXEC}/scripts/sync-webui-model-prompts.py" \
  --database "${DATA_DIR}/webui.db" \
  --system-prompt "${SYSTEM_PROMPT}" \
  --model-config-json "${MODEL_CONFIG_JSON}" \
  || printf 'warning: model prompt sync failed; continuing without it.\n' >&2

exec open-webui serve --host "${HOST}" --port "${PORT}"
