#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROJECT_DIR="${NIXLOOM_ROOT:-${NIXLOOM_LIBEXEC}}"

usage() {
  cat <<'EOF'
Usage: ./tests/run.sh <command> [options]

Commands:
  smoke         Fast functional regression: chat, thinking, vision, optional SD
  hermes        Hermes tool-call regression (requires HERMES_API_KEY)
  benchmark     Fixed performance + quality benchmark against a running stack
  cpu-matrix    Restart an isolated server across 9955HX thread/affinity profiles

Examples:
  ./tests/run.sh smoke --skip-sd
  ./tests/run.sh benchmark --label before-change
  ./tests/run.sh benchmark --suite quality --prompt-profile control
  ./tests/run.sh benchmark --suite quality --prompt-profile candidate \
    --candidate-prompt-file /path/to/prompt.txt
  ./tests/run.sh cpu-matrix
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

command_name="$1"
shift
case "${command_name}" in
  smoke)
    exec "${NIXLOOM_LIBEXEC}/tests/gpu-smoke.sh" "$@"
    ;;
  hermes)
    exec "${NIXLOOM_LIBEXEC}/tests/hermes-smoke.sh" "$@"
    ;;
  benchmark)
    has_base_url=0
    has_boundary_tokens=0
    has_system_prompt=0
    prompt_profile=actual
    read_prompt_profile=0
    for arg in "$@"; do
      if [[ "${read_prompt_profile}" == "1" ]]; then
        prompt_profile="${arg}"
        read_prompt_profile=0
        continue
      fi
      [[ "${arg}" == "--base-url" || "${arg}" == --base-url=* ]] && has_base_url=1
      [[ "${arg}" == "--boundary-tokens" || "${arg}" == --boundary-tokens=* ]] \
        && has_boundary_tokens=1
      [[ "${arg}" == "--system-prompt" || "${arg}" == --system-prompt=* ]] \
        && has_system_prompt=1
      [[ "${arg}" == "--prompt-profile" ]] && read_prompt_profile=1
      [[ "${arg}" == --prompt-profile=* ]] && prompt_profile="${arg#*=}"
    done
    if [[ "${has_base_url}" == "0" || "${has_boundary_tokens}" == "0" \
      || "${prompt_profile}" == "actual" ]]; then
      # shellcheck source=config/lib.sh
      source "${NIXLOOM_LIBEXEC}/config/lib.sh"
      resolve_config_file "${PROJECT_DIR}" ""
    fi
    if [[ "${has_base_url}" == "0" ]]; then
      llama_port="$(cfg_required '.ports.llama' 'ports.llama')"
      set -- --base-url "http://127.0.0.1:${llama_port}" "$@"
    fi
    if [[ "${has_boundary_tokens}" == "0" ]]; then
      boundary_tokens="$(cfg_required '.webui.compaction_threshold' 'webui.compaction_threshold')"
      set -- --boundary-tokens "${boundary_tokens}" "$@"
    fi
    if [[ "${prompt_profile}" == "actual" && "${has_system_prompt}" == "0" ]]; then
      system_prompt="$(webui_system_prompt "$(date +%F)")"
      set -- --system-prompt "${system_prompt}" "$@"
    fi
    exec "${NIXLOOM_LIBEXEC}/tests/benchmark.py" "$@"
    ;;
  cpu-matrix)
    exec "${NIXLOOM_LIBEXEC}/tests/cpu-matrix.sh" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    printf 'Unknown test command: %s\n' "${command_name}" >&2
    usage >&2
    exit 2
    ;;
esac
