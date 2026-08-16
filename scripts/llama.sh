#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${NIXLOOM_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/nixloom}"
DATA_DIR="${NIXLOOM_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/nixloom}"
CACHE_DIR="${NIXLOOM_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/nixloom}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"

HOST="127.0.0.1"
PORT=""
CONFIG_ARG=""
DRY_RUN=0
THREADS_OVERRIDE=""
THREADS_BATCH_OVERRIDE=""
N_CPU_MOE_OVERRIDE=""
FIT_TARGET_OVERRIDE=""

usage() {
  cat <<'EOF'
Usage: ./scripts/llama.sh [options]

Starts the single LLM configured under llm: in config.yaml. Vision is always
available, and thinking is selected per request with:
  "chat_template_kwargs": {"enable_thinking": true}

Options:
  --host HOST     Bind host (default: 127.0.0.1)
  --port PORT     Bind port (default: ports.llama from config)
  --config FILE   Config file (default: ./config.yaml)
  --threads N     Override llm.threads for a benchmark run
  --threads-batch N
                  Override llm.threads_batch for a benchmark run
  --n-cpu-moe N  Override llm.n_cpu_moe for a placement benchmark
  --fit-target N Override llm.fit_target MiB for a placement benchmark
  --dry-run       Print the llama-server command without running it
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
    --threads)
      require_opt_value "$1" $#
      THREADS_OVERRIDE="$2"
      shift 2
      ;;
    --threads-batch)
      require_opt_value "$1" $#
      THREADS_BATCH_OVERRIDE="$2"
      shift 2
      ;;
    --n-cpu-moe)
      require_opt_value "$1" $#
      N_CPU_MOE_OVERRIDE="$2"
      shift 2
      ;;
    --fit-target)
      require_opt_value "$1" $#
      FIT_TARGET_OVERRIDE="$2"
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

resolve_config_file "${CONFIG_ARG}"
use_runtime_paths "${STATE_DIR}" "${DATA_DIR}" "${CACHE_DIR}"

MODEL_ID="$(llm_id)"
PORT="${PORT:-$(cfg_required '.ports.llama' 'ports.llama')}"
MODEL_FILE="$(llm_cfg '.model_file')"
MMPROJ_FILE="$(llm_cfg '.mmproj_file')"
CTX_SIZE="$(llm_context)"
MAX_TOKENS="$(llm_max_tokens)"
GPU_LAYERS="$(llm_cfg '.gpu_layers')"
N_CPU_MOE="$(llm_cfg '.n_cpu_moe')"
FIT_TARGET="$(llm_cfg '.fit_target')"
N_CPU_MOE="${N_CPU_MOE_OVERRIDE:-${N_CPU_MOE}}"
FIT_TARGET="${FIT_TARGET_OVERRIDE:-${FIT_TARGET}}"
MMAP="$(cfg_bool_required '.llm.mmap' 'llm.mmap')"
THREADS="$(llm_cfg '.threads')"
THREADS_BATCH="$(llm_cfg '.threads_batch')"
THREADS="${THREADS_OVERRIDE:-${THREADS}}"
THREADS_BATCH="${THREADS_BATCH_OVERRIDE:-${THREADS_BATCH}}"
FLASH_ATTENTION="$(cfg_bool_required '.llm.flash_attention' 'llm.flash_attention')"
CACHE_TYPE_K="$(llm_cfg '.cache_type_k')"
CACHE_TYPE_V="$(llm_cfg '.cache_type_v')"
MMPROJ_OFFLOAD="$(cfg_bool_required '.llm.mmproj_offload' 'llm.mmproj_offload')"
IMAGE_TOKENS="$(llm_cfg '.image_tokens')"
REASONING_PRESERVE="$(cfg_bool_required '.llm.reasoning_preserve' 'llm.reasoning_preserve')"

TEMPERATURE="$(llm_cfg '.sampling.temperature')"
TOP_K="$(llm_cfg '.sampling.top_k')"
TOP_P="$(llm_cfg '.sampling.top_p')"
MIN_P="$(llm_cfg '.sampling.min_p')"
FREQUENCY_PENALTY="$(llm_cfg '.sampling.frequency_penalty')"
PRESENCE_PENALTY="$(llm_cfg '.sampling.presence_penalty')"
REPEAT_PENALTY="$(llm_cfg '.sampling.repeat_penalty')"

for path_var in MODEL_FILE MMPROJ_FILE; do
  if [[ "${!path_var}" != /* ]]; then
    printf -v "${path_var}" '%s/%s' "${DATA_DIR}" "${!path_var}"
  fi
done

for integer_var in CTX_SIZE MAX_TOKENS N_CPU_MOE FIT_TARGET THREADS THREADS_BATCH IMAGE_TOKENS TOP_K; do
  if ! [[ "${!integer_var}" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s must be a positive integer (got %q).\n' "${integer_var}" "${!integer_var}" >&2
    exit 2
  fi
done
if (( MAX_TOKENS >= CTX_SIZE )); then
  printf 'llm.max_tokens must be smaller than llm.context.\n' >&2
  exit 2
fi
for numeric_var in TEMPERATURE TOP_P MIN_P FREQUENCY_PENALTY PRESENCE_PENALTY REPEAT_PENALTY; do
  if ! is_number "${!numeric_var}"; then
    printf '%s must be a number (got %q).\n' "${numeric_var}" "${!numeric_var}" >&2
    exit 2
  fi
done
for cache_type_var in CACHE_TYPE_K CACHE_TYPE_V; do
  case "${!cache_type_var}" in
    f32|f16|bf16|q8_0|q4_0|q4_1|iq4_nl|q5_0|q5_1) ;;
    *)
      printf '%s has unsupported value %q.\n' "${cache_type_var}" "${!cache_type_var}" >&2
      exit 2
      ;;
  esac
done

if [[ "${DRY_RUN}" != "1" ]]; then
  for path_var in MODEL_FILE MMPROJ_FILE; do
    if [[ ! -f "${!path_var}" ]]; then
      printf 'Model asset not found: %s\nRun `nixloom models download` first.\n' "${!path_var}" >&2
      exit 2
    fi
  done
fi

flash_attention=off
[[ "${FLASH_ATTENTION}" == "1" ]] && flash_attention=on

args=(
  -m "${MODEL_FILE}"
  --mmproj "${MMPROJ_FILE}"
  --alias "${MODEL_ID}"
  --host "${HOST}"
  --port "${PORT}"
  -ngl "${GPU_LAYERS}"
  -c "${CTX_SIZE}"
  -n "${MAX_TOKENS}"
  --parallel 1
  --jinja
  --no-ui
  --fit on
  --fit-target "${FIT_TARGET}"
  --n-cpu-moe "${N_CPU_MOE}"
  --threads "${THREADS}"
  --threads-batch "${THREADS_BATCH}"
  --flash-attn "${flash_attention}"
  --cache-type-k "${CACHE_TYPE_K}"
  --cache-type-v "${CACHE_TYPE_V}"
  --image-min-tokens "${IMAGE_TOKENS}"
  --image-max-tokens "${IMAGE_TOKENS}"
  --reasoning auto
  --reasoning-budget -1
  --reasoning-format deepseek
  --chat-template-kwargs '{"enable_thinking":false}'
  --temp "${TEMPERATURE}"
  --top-k "${TOP_K}"
  --top-p "${TOP_P}"
  --min-p "${MIN_P}"
  --frequency-penalty "${FREQUENCY_PENALTY}"
  --presence-penalty "${PRESENCE_PENALTY}"
  --repeat-penalty "${REPEAT_PENALTY}"
)

[[ "${MMAP}" == "1" ]] && args+=(--mmap) || args+=(--no-mmap)
[[ "${MMPROJ_OFFLOAD}" == "1" ]] && args+=(--mmproj-offload) || args+=(--no-mmproj-offload)
[[ "${REASONING_PRESERVE}" == "1" ]] && args+=(--reasoning-preserve) || args+=(--no-reasoning-preserve)

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'model=%q\n' "${MODEL_ID}"
  printf '%q' llama-server
  printf ' %q' "${args[@]}"
  printf '\n'
  exit 0
fi

exec llama-server "${args[@]}"
