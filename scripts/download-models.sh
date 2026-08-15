#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROJECT_DIR="${NIXLOOM_ROOT:-${NIXLOOM_LIBEXEC}}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"

LOCK_FILE="${NIXLOOM_ASSET_LOCK_FILE:-${NIXLOOM_LIBEXEC}/config/models.lock.yaml}"
MODE="download"
declare -a REQUESTED=()

# The token is written to curl's stdin as a config file rather than passed as
# -H on the command line: argv is world-readable through /proc for as long as
# the download runs, which is minutes for the multi-gigabyte checkpoints.
curl_config_for_url() {
  if [[ "$1" == https://civitai.com/* && -n "${CIVITAI_API_TOKEN:-}" ]]; then
    printf 'header = "Authorization: Bearer %s"\n' "${CIVITAI_API_TOKEN}"
  fi
}

# fetch <url> <dest> [extra curl args...]
fetch() {
  local url="$1" dest="$2"
  shift 2
  curl_config_for_url "${url}" \
    | curl -fL --retry 3 --config - "$@" -o "${dest}" "${url}"
}

usage() {
  cat <<'EOF'
Usage: ./scripts/download-models.sh [--check] [ASSET...]

Downloads assets pinned in the configured asset lock and verifies exact size
and SHA256. With no ASSET arguments, processes every asset.

Options:
  --check    Verify local files without downloading
  -h, --help Show this help

Examples:
  ./scripts/download-models.sh
  ./scripts/download-models.sh qwen36_q4 qwen36_mmproj
  ./scripts/download-models.sh --check
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      MODE="check"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      REQUESTED+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "${LOCK_FILE}" ]]; then
  printf 'Missing asset lock file: %s\n' "${LOCK_FILE}" >&2
  exit 2
fi

# After argument parsing, so --help never sources .env. civitai downloads need
# an account API token (CIVITAI_API_TOKEN).
load_env_file "${PROJECT_DIR}"

if (( ${#REQUESTED[@]} == 0 )); then
  mapfile -t REQUESTED < <(yq -r '.assets | keys | .[]' "${LOCK_FILE}")
fi

verify_file() {
  local path="$1"
  local expected_size="$2"
  local expected_sha="$3"
  local actual_size actual_sha

  [[ -f "${path}" ]] || return 1
  actual_size="$(stat -c '%s' "${path}")"
  [[ "${actual_size}" == "${expected_size}" ]] || return 1
  actual_sha="$(sha256sum "${path}" | cut -d' ' -f1)"
  [[ "${actual_sha}" == "${expected_sha}" ]]
}

failures=0
for asset in "${REQUESTED[@]}"; do
  # strenv keeps the asset name a value, not part of the yq expression.
  if [[ "$(ASSET="${asset}" yq -r '.assets | has(strenv(ASSET))' "${LOCK_FILE}")" != "true" ]]; then
    printf 'Unknown asset: %s\n' "${asset}" >&2
    failures=1
    continue
  fi

  relative_path="$(ASSET="${asset}" yq -r '.assets[strenv(ASSET)].path' "${LOCK_FILE}")"
  url="$(ASSET="${asset}" yq -r '.assets[strenv(ASSET)].url' "${LOCK_FILE}")"
  expected_size="$(ASSET="${asset}" yq -r '.assets[strenv(ASSET)].size' "${LOCK_FILE}")"
  expected_sha="$(ASSET="${asset}" yq -r '.assets[strenv(ASSET)].sha256' "${LOCK_FILE}")"
  target="${PROJECT_DIR}/${relative_path}"

  # A missing size would otherwise reach the arithmetic below as the bare word
  # `null`, which bash reads as 0 — every .part would look complete, get
  # deleted, and the asset would fail only after two full downloads.
  if ! [[ "${expected_size}" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "${expected_sha}" =~ ^[0-9a-f]{64}$ ]] \
    || [[ -z "${relative_path}" || "${relative_path}" == "null" ]] \
    || [[ -z "${url}" || "${url}" == "null" ]]; then
    printf 'Asset %s is missing a valid path/url/size/sha256 in %s\n' "${asset}" "${LOCK_FILE}" >&2
    failures=1
    continue
  fi

  if verify_file "${target}" "${expected_size}" "${expected_sha}"; then
    printf 'verified %-14s %s\n' "${asset}" "${relative_path}"
    continue
  fi

  if [[ "${MODE}" == "check" ]]; then
    printf 'missing or invalid %-14s %s\n' "${asset}" "${relative_path}" >&2
    failures=1
    continue
  fi

  mkdir -p "$(dirname "${target}")"
  partial="${target}.part"
  if [[ -f "${partial}" ]]; then
    # A .part may already be the complete file (killed between download and
    # rename); keep it if it verifies, discard it if it is full-size garbage.
    if verify_file "${partial}" "${expected_size}" "${expected_sha}"; then
      mv -f -- "${partial}" "${target}"
      printf 'verified %-14s %s\n' "${asset}" "${relative_path}"
      continue
    fi
    if (( $(stat -c '%s' "${partial}") >= expected_size )); then
      rm -- "${partial}"
    fi
  fi

  if [[ "${url}" == https://civitai.com/* && -z "${CIVITAI_API_TOKEN:-}" ]]; then
    printf 'note: %s needs CIVITAI_API_TOKEN in .env if the download returns 401.\n' "${asset}" >&2
  fi

  printf 'downloading %-14s %s\n' "${asset}" "${relative_path}"
  if ! fetch "${url}" "${partial}" -C - \
    || ! verify_file "${partial}" "${expected_size}" "${expected_sha}"; then
    # A corrupt resumed partial fails the SHA check; retry once from scratch
    # instead of making the user run the script a third time.
    printf 'retrying %-14s with a fresh download\n' "${asset}" >&2
    rm -f -- "${partial}"
    if ! fetch "${url}" "${partial}" \
      || ! verify_file "${partial}" "${expected_size}" "${expected_sha}"; then
      printf 'Asset failed size/SHA256 verification after retry: %s\n' "${asset}" >&2
      rm -f -- "${partial}"
      failures=1
      continue
    fi
  fi
  mv -f -- "${partial}" "${target}"
  printf 'verified %-14s %s\n' "${asset}" "${relative_path}"
done

exit "${failures}"
