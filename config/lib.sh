#!/usr/bin/env bash

# Shared config access for NixLoom's internal launchers. The user-owned
# config.yaml lives under XDG_CONFIG_HOME/nixloom, model data under
# XDG_DATA_HOME/nixloom, runtime state under XDG_STATE_HOME/nixloom, and
# re-downloadable caches under XDG_CACHE_HOME/nixloom.
# Resolution order is --config, NIXLOOM_CONFIG_FILE, user config, then the
# packaged template.

# require_opt_value <option> <argc> - fail unless the option has a value.
require_opt_value() {
  if (( $2 < 2 )); then
    printf '%s requires a value\n' "$1" >&2
    exit 2
  fi
}

# generate_api_key - 64 hex characters from the kernel UUID source.
generate_api_key() {
  printf '%s%s' \
    "$(tr -d '-' </proc/sys/kernel/random/uuid)" \
    "$(tr -d '-' </proc/sys/kernel/random/uuid)"
}

# Scientific notation is valid YAML and llama.cpp accepts it, so 1.0e-5 must
# not be rejected as a sampling value.
is_number() {
  [[ "$1" =~ ^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$ ]]
}

# Leading zeros are rejected: bash reads 08 as octal and aborts the arithmetic
# it is passed to, which reads as a false condition and skips the guard.
is_integer() {
  [[ "$1" =~ ^-?(0|[1-9][0-9]*)$ ]]
}

resolve_config_file() {
  local explicit="${1:-}"

  if [[ -n "${explicit}" ]]; then
    NIXLOOM_CONFIG_FILE="${explicit}"
  elif [[ -z "${NIXLOOM_CONFIG_FILE:-}" ]]; then
    if [[ -f "${XDG_CONFIG_HOME:-${HOME}/.config}/nixloom/config.yaml" ]]; then
      NIXLOOM_CONFIG_FILE="${XDG_CONFIG_HOME:-${HOME}/.config}/nixloom/config.yaml"
    elif [[ -f "${NIXLOOM_SHARE:-${NIXLOOM_LIBEXEC}}/config.yaml" ]]; then
      NIXLOOM_CONFIG_FILE="${NIXLOOM_SHARE:-${NIXLOOM_LIBEXEC}}/config.yaml"
    else
      NIXLOOM_CONFIG_FILE="${NIXLOOM_LIBEXEC}/config.yaml"
    fi
  fi

  if [[ ! -f "${NIXLOOM_CONFIG_FILE}" ]]; then
    printf 'Config file not found: %s\n' "${NIXLOOM_CONFIG_FILE}" >&2
    return 2
  fi
  NIXLOOM_CONFIG_FILE="$(cd "$(dirname "${NIXLOOM_CONFIG_FILE}")" && pwd)/$(basename "${NIXLOOM_CONFIG_FILE}")"

  if ! yq '.' "${NIXLOOM_CONFIG_FILE}" >/dev/null; then
    printf 'Config file is not valid YAML: %s\n' "${NIXLOOM_CONFIG_FILE}" >&2
    return 2
  fi
  export NIXLOOM_CONFIG_FILE
}

# Asset paths are relative to the model data directory so the config can be
# moved independently from downloaded weights. Only segment-level checks are
# safe: a filename such as `foo..bar` is valid, while a `..` segment is not.
valid_asset_path() {
  local path="$1" segment
  [[ -n "${path}" && "${path}" != /* ]] || return 1
  [[ "${path}" != *"\\"* && "${path}" != *$'\n'* ]] || return 1
  IFS='/' read -r -a segments <<<"${path}"
  for segment in "${segments[@]}"; do
    [[ -n "${segment}" && "${segment}" != "." && "${segment}" != ".." ]] || return 1
  done
  return 0
}

# cfg <yq-expr> [default] - print a scalar; empty/null falls back to default.
cfg() {
  local value
  value="$(yq -r "$1" "${NIXLOOM_CONFIG_FILE}")"
  if [[ -z "${value}" || "${value}" == "null" ]]; then
    value="${2:-}"
  fi
  printf '%s' "${value}"
}

cfg_bool_required() {
  local value
  value="$(cfg_required "$1" "$2")" || return 2
  case "${value,,}" in
    1|true|yes|on) printf '1' ;;
    0|false|no|off) printf '0' ;;
    *)
      printf 'Expected a boolean for %s (got %q).\n' "$2" "${value}" >&2
      return 2
      ;;
  esac
}

# cfg_required <yq-expr> <label> - like cfg but fail when missing.
cfg_required() {
  local value
  value="$(cfg "$1" "")"
  if [[ -z "${value}" ]]; then
    printf 'Missing required config value: %s (%s)\n' "$2" "$1" >&2
    return 2
  fi
  printf '%s' "${value}"
}

# cfg_list <yq-expr> - print list entries, one per line.
cfg_list() {
  yq -r "$1 // [] | .[]" "${NIXLOOM_CONFIG_FILE}"
}

# has_frontend <name> - test membership in deployment.frontends. Written as a read
# loop rather than `| grep -q`: grep exits on the first match, and the SIGPIPE
# that kills yq would surface as a failed pipeline under `set -o pipefail`,
# reporting a frontend that is present as absent.
has_frontend() {
  local entry
  while IFS= read -r entry; do
    [[ "${entry}" == "$1" ]] && return 0
  done < <(cfg_list '.deployment.frontends')
  return 1
}

# llm_cfg <subpath> - all LLM launchers read the same required value. There is
# intentionally no model registry or alias inheritance: this stack has one
# loaded LLM, and capabilities are selected per request.
llm_cfg() {
  cfg_required ".llm$1" "llm$1"
}

llm_id() {
  local id
  id="$(llm_cfg '.id')" || return 2
  if ! [[ "${id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    printf 'llm.id must match [A-Za-z0-9][A-Za-z0-9._-]* (got %q).\n' "${id}" >&2
    return 2
  fi
  printf '%s' "${id}"
}

thinking_model_id() {
  printf '%s-think' "$(llm_id)"
}

# webui_system_prompt [date] - compose the single Open WebUI system message
# from config-owned modules. Tool-specific instructions are present only when
# the corresponding built-in tool is enabled. Open WebUI expands its date
# placeholder per request; direct benchmark callers pass an ISO date instead.
webui_system_prompt() {
  local current_date="${1:-}" section value result=""
  local web_search_enabled builtin_web_search images_enabled builtin_image_generation
  local -a sections=(identity conversation reliability roleplay)
  [[ -n "${current_date}" ]] || current_date='{{CURRENT_DATE}}'

  web_search_enabled="$(cfg_bool_required '.webui.web_search' \
    'webui.web_search')" || return 2
  builtin_web_search="$(cfg_bool_required '.webui.builtin_tools.web_search' \
    'webui.builtin_tools.web_search')" || return 2
  tavily_api_key="$(cfg '.credentials.tavily_api_key' '')"
  images_enabled="$(cfg_bool_required '.images.enabled' 'images.enabled')" || return 2
  builtin_image_generation="$(cfg_bool_required \
    '.webui.builtin_tools.image_generation' \
    'webui.builtin_tools.image_generation')" || return 2

  if [[ "${web_search_enabled}" == "1" && "${builtin_web_search}" == "1" \
    && -n "${tavily_api_key}" ]]; then
    sections+=(web_search)
  fi
  if [[ "${images_enabled}" == "1" && "${builtin_image_generation}" == "1" ]]; then
    sections+=(image_generation)
  fi
  sections+=(examples)

  result="当前日期：${current_date}。"
  for section in "${sections[@]}"; do
    value="$(cfg_required ".webui.system_prompt.${section}" \
      "webui.system_prompt.${section}")" || return 2
    result+=$'\n\n'
    result+="${value}"
  done
  printf '%s' "${result}"
}

llm_context() {
  llm_cfg '.context'
}

llm_max_tokens() {
  llm_cfg '.max_tokens'
}

# image_profile - the profile named by images.profile; switching checkpoints
# means changing that single key.
image_profile() {
  local name
  name="$(cfg_required '.images.profile' 'images.profile')" || return 2
  if [[ "$(cfg ".images.profiles | has(\"${name}\")")" != "true" ]]; then
    printf 'images.profile %q is not defined under images.profiles\n' "${name}" >&2
    return 2
  fi
  printf '%s' "${name}"
}

# llm_compaction_threshold <max_tokens> <global ceiling> <margin> - derive the
# largest safe Open WebUI threshold for a frontend profile.
llm_compaction_threshold() {
  local max_tokens="$1" threshold="$2" margin="$3" ctx min_ctx
  ctx="$(llm_context)" || return 2
  if ! [[ "${threshold}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'webui.compaction_threshold must be a positive integer (got %q).\n' "${threshold}" >&2
    return 2
  fi
  if ! [[ "${ctx}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'llm.context must be a positive integer (got %q).\n' "${ctx}" >&2
    return 2
  fi
  if ! [[ "${max_tokens}" =~ ^[0-9]+$ ]]; then
    printf 'LLM max_tokens must be a non-negative integer (got %q).\n' \
      "${max_tokens}" >&2
    return 2
  fi
  if ! [[ "${margin}" =~ ^[0-9]+$ ]]; then
    printf 'webui.compaction_margin must be a non-negative integer (got %q).\n' \
      "${margin}" >&2
    return 2
  fi
  min_ctx=$((threshold + max_tokens + margin))
  if (( ctx < min_ctx )); then
    threshold=$((ctx - max_tokens - margin))
  fi
  if (( threshold < 1 )); then
    printf 'llm.context has no headroom after max output %s and margin %s.\n' \
      "${max_tokens}" "${margin}" >&2
    return 2
  fi
  printf '%s' "${threshold}"
}

# Hermes manages compaction as a fraction of the shared LLM context.
check_hermes_ctx() {
  local ctx max_tokens
  ctx="$(llm_context)" || return 2
  max_tokens="$(llm_max_tokens)" || return 2
  if ! [[ "${ctx}" =~ ^[1-9][0-9]*$ && "${max_tokens}" =~ ^[0-9]+$ ]]; then
    printf 'Hermes needs positive llm.context and non-negative llm.max_tokens.\n' >&2
    return 2
  fi
  if (( max_tokens >= ctx )); then
    printf 'Hermes needs llm.max_tokens < llm.context (got %s >= %s).\n' \
      "${max_tokens}" "${ctx}" >&2
    return 2
  fi
}

# _check_section_keys <yq-map-expr> <allowed keys> <label> - report keys the
# scripts do not read.
_check_section_keys() {
  local expr="$1" allowed=" $2 " label="$3" key bad=0
  while IFS= read -r key; do
    if [[ "${allowed}" != *" ${key} "* ]]; then
      printf 'Unknown config key %q under %s in %s\n' "${key}" "${label}" "${NIXLOOM_CONFIG_FILE}" >&2
      bad=1
    fi
  done < <(yq -r "(${expr} // {}) | keys | .[]" "${NIXLOOM_CONFIG_FILE}" 2>/dev/null)
  return "${bad}"
}

# check_assets - validate the user-owned asset manifest. Asset entries are
# validated as a whole so downloaders can construct paths only after the
# configured data root has been proven safe.
check_assets() {
  local name path url size sha256 failed=0
  declare -A seen_paths=()
  while IFS= read -r name; do
    path="$(yq -r ".assets.\"${name}\".path // \"\"" "${NIXLOOM_CONFIG_FILE}")"
    url="$(yq -r ".assets.\"${name}\".url // \"\"" "${NIXLOOM_CONFIG_FILE}")"
    size="$(yq -r ".assets.\"${name}\".size // \"\"" "${NIXLOOM_CONFIG_FILE}")"
    sha256="$(yq -r ".assets.\"${name}\".sha256 // \"\"" "${NIXLOOM_CONFIG_FILE}")"

    if ! valid_asset_path "${path}"; then
      printf 'Asset %s has an invalid path (must be relative and contain no . or .. segments): %s\n' \
        "${name}" "${path}" >&2
      failed=1
    elif [[ -n "${seen_paths[${path}]+x}" ]]; then
      printf 'Asset %s duplicates the path %s.\n' "${name}" "${path}" >&2
      failed=1
    else
      seen_paths["${path}"]=1
    fi

    if ! [[ "${url}" =~ ^https?://[^[:space:]]+$ ]]; then
      printf 'Asset %s has an invalid URL: %s\n' "${name}" "${url}" >&2
      failed=1
    fi
    if ! [[ "${size}" =~ ^[1-9][0-9]*$ ]]; then
      printf 'Asset %s has an invalid size (positive integer required): %s\n' "${name}" "${size}" >&2
      failed=1
    fi
    if ! [[ "${sha256}" =~ ^[0-9a-f]{64}$ ]]; then
      printf 'Asset %s has an invalid SHA-256 value.\n' "${name}" >&2
      failed=1
    fi
  done < <(yq -r '(.assets // {}) | keys | .[]' "${NIXLOOM_CONFIG_FILE}")
  return "${failed}"
}

# check_config_keys - fail on unknown keys anywhere in config.yaml, so a
# typo'd key cannot silently fall back to a script default.
check_config_keys() {
  local failed=0 name
  _check_section_keys '.' \
    'deployment ports llm images webui hermes sillytavern credentials assets' \
    'the top level' || failed=1
  _check_section_keys '.deployment' 'remote frontends' 'deployment' || failed=1
  _check_section_keys '.ports' 'llama webui sillytavern hermes' 'ports' || failed=1
  _check_section_keys '.llm' \
    'id model_file mmproj_file context max_tokens thinking_max_tokens gpu_layers n_cpu_moe fit_target mmap threads threads_batch flash_attention cache_type_k cache_type_v mmproj_offload image_tokens reasoning_preserve sampling thinking_sampling' \
    'llm' || failed=1
  _check_section_keys '.llm.sampling' \
    'temperature top_k top_p min_p frequency_penalty presence_penalty repeat_penalty' \
    'llm.sampling' || failed=1
  _check_section_keys '.llm.thinking_sampling' \
    'temperature top_k top_p min_p frequency_penalty presence_penalty repeat_penalty' \
    'llm.thinking_sampling' || failed=1
  _check_section_keys '.images' \
    'enabled profile profiles quant_level accel size max_res' \
    'images' || failed=1
  while IFS= read -r name; do
    _check_section_keys ".images.profiles.\"${name}\"" \
      'model_file lora lora_mult steps cfg_scale sampler negative_prompt' \
      "images.profiles.${name}" || failed=1
  done < <(yq -r '(.images.profiles // {}) | keys | .[]' "${NIXLOOM_CONFIG_FILE}")
  _check_section_keys '.webui' \
    'auth cors_allow_origins title_generation web_search web_search_results system_prompt builtin_tools compaction_threshold compaction_margin' \
    'webui' || failed=1
  _check_section_keys '.webui.system_prompt' \
    'identity conversation reliability roleplay examples web_search image_generation' \
    'webui.system_prompt' || failed=1
  _check_section_keys '.webui.builtin_tools' \
    'knowledge memory notes chats channels automations calendar code_interpreter tasks time web_search image_generation' \
    'webui.builtin_tools' || failed=1
  _check_section_keys '.hermes' 'self_improvement sandbox compression' 'hermes' || failed=1
  _check_section_keys '.hermes.compression' 'threshold abort_on_summary_failure' \
    'hermes.compression' || failed=1
  _check_section_keys '.sillytavern' 'auth_user auth_password preset' \
    'sillytavern' || failed=1
  _check_section_keys '.credentials' 'tavily_api_key civitai_api_token' \
    'credentials' || failed=1
  while IFS= read -r name; do
    _check_section_keys ".assets.\"${name}\"" 'path url size sha256' \
      "assets.${name}" || failed=1
  done < <(yq -r '(.assets // {}) | keys | .[]' "${NIXLOOM_CONFIG_FILE}")
  check_assets || failed=1
  if (( failed )); then
    printf 'Unknown config keys are errors so typos cannot silently use defaults.\n' >&2
    return 2
  fi
}

# use_runtime_paths <state_dir> <data_dir> <cache_dir> - keep model data under
# the XDG data directory and re-downloadable caches under the XDG cache
# directory. Respects values already set in the environment.
use_runtime_paths() {
  export HF_HOME="${HF_HOME:-$2/.hf}"
  export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$3}"
}
