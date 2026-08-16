#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${NIXLOOM_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/nixloom}"
DATA_DIR="${NIXLOOM_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/nixloom}"
CACHE_DIR="${NIXLOOM_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/nixloom}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"

HOST="127.0.0.1"
PORT="7860"
CONFIG_ARG=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/sd.sh [options]

Starts KoboldCpp in image-only mode with an AUTOMATIC1111-compatible API
(/sdapi/v1/txt2img), configured by the images: section of config.yaml.
Normally launched on demand by llama-swap.

Options:
  --host HOST      Bind host (default: 127.0.0.1)
  --port PORT      Bind port (default: 7860)
  --config FILE    Config file (default: ./config.yaml)
  --dry-run        Print the command without running it
  -h, --help       Show this help
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

resolve_config_file "${CONFIG_ARG}"
use_runtime_paths "${STATE_DIR}" "${DATA_DIR}" "${CACHE_DIR}"

SD_PROFILE="$(image_profile)"
SD_MODEL_FILE="$(cfg_required ".images.profiles.\"${SD_PROFILE}\".model_file" \
  "images.profiles.${SD_PROFILE}.model_file")"
SD_LORA_FILE="$(cfg ".images.profiles.\"${SD_PROFILE}\".lora" '')"
SD_LORA_MULT="$(cfg ".images.profiles.\"${SD_PROFILE}\".lora_mult" '')"
SD_QUANT_LEVEL="$(cfg_required '.images.quant_level' 'images.quant_level')"
SD_ACCEL="$(cfg_required '.images.accel' 'images.accel')"
SD_MAX_RES="$(cfg_required '.images.max_res' 'images.max_res')"

if [[ "${SD_MODEL_FILE}" != /* ]]; then
  SD_MODEL_FILE="${DATA_DIR}/${SD_MODEL_FILE}"
fi
if [[ -n "${SD_LORA_FILE}" && "${SD_LORA_FILE}" != /* ]]; then
  SD_LORA_FILE="${DATA_DIR}/${SD_LORA_FILE}"
fi
if [[ -n "${SD_LORA_FILE}" && -z "${SD_LORA_MULT}" ]]; then
  printf 'images.profiles.%s.lora_mult is required when lora is set.\n' "${SD_PROFILE}" >&2
  exit 2
fi
if [[ -n "${SD_LORA_FILE}" ]] && ! is_number "${SD_LORA_MULT}"; then
  printf 'images.profiles.%s.lora_mult must be a number.\n' "${SD_PROFILE}" >&2
  exit 2
fi

case "${SD_ACCEL}" in
  cuda) accel_args=(--usecuda) ;;
  vulkan) accel_args=(--usevulkan) ;;
  cpu) accel_args=() ;;
  *)
    printf 'images.accel must be one of: cuda, vulkan, cpu\n' >&2
    exit 2
    ;;
esac

# quant_level 1 quantizes the UNet to q8 on load, which leaves more room
# alongside CLIP+VAE in 8GB of VRAM — but only for a profile without a LoRA
# (see the mutual exclusion below).
# KoboldCpp clamps images to 1024x1024 unless raised; the soft clamp allows
# width/height trade-offs within the same pixel budget (e.g. 832x1216).
args=(
  --skiplauncher
  --nomodel
  --host "${HOST}"
  --port "${PORT}"
  --sdmodel "${SD_MODEL_FILE}"
  --sdclampedsoft "${SD_MAX_RES}"
  "${accel_args[@]}"
)

# KoboldCpp cannot merge a LoRA into quantized weights: --sdlora and
# --sdquant are mutually exclusive. quant_level 0 means fp16, no quant flag.
if ! is_integer "${SD_QUANT_LEVEL}"; then
  printf 'images.quant_level must be an integer\n' >&2
  exit 2
fi
if [[ -n "${SD_LORA_FILE}" ]]; then
  args+=(--sdlora "${SD_LORA_FILE}" --sdloramult "${SD_LORA_MULT}")
  if (( SD_QUANT_LEVEL > 0 )); then
    # Say so rather than load fp16 silently: fp16 peaks around 7.0GB at
    # 1216x1216, so the difference matters on an 8GB card.
    printf 'note: profile %s has a lora, so images.quant_level %s is ignored and SDXL loads fp16.\n' \
      "${SD_PROFILE}" "${SD_QUANT_LEVEL}" >&2
  fi
elif (( SD_QUANT_LEVEL > 0 )); then
  args+=(--sdquant "${SD_QUANT_LEVEL}")
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'SD_PROFILE=%q\n' "${SD_PROFILE}"
  printf 'SD_MODEL_FILE=%q\n' "${SD_MODEL_FILE}"
  printf '%q ' koboldcpp "${args[@]}"
  printf '\n'
  exit 0
fi

if [[ ! -f "${SD_MODEL_FILE}" ]]; then
  printf 'Image model for profile %s not found: %s\nRun `nixloom models download` first.\n' \
    "${SD_PROFILE}" "${SD_MODEL_FILE}" >&2
  exit 2
fi
if [[ -n "${SD_LORA_FILE}" && ! -f "${SD_LORA_FILE}" ]]; then
  printf 'LoRA file not found: %s\nPin it under assets in config.yaml and run `nixloom models download`.\n' \
    "${SD_LORA_FILE}" >&2
  exit 2
fi

exec koboldcpp "${args[@]}"
