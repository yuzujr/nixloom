#!/usr/bin/env bash
set -euo pipefail

# Back up the irreplaceable user-owned config and runtime state: chats,
# accounts, characters, Hermes memory and the WebUI secret. Models and caches
# are excluded; models re-download via `nixloom models download`.
#
# The archive contains config.yaml and .webui_secret_key, so every file this
# script creates is private to the invoking user.
umask 077

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${NIXLOOM_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/nixloom}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"
resolve_config_file ""
CONFIG_FILE="${NIXLOOM_CONFIG_FILE}"
CONFIG_DIR="$(dirname "${CONFIG_FILE}")"

DEFAULT_DEST="${NIXLOOM_BACKUP_DIR:-${HOME}/backups/nixloom}"
FORCE=0
DEST=""

usage() {
  cat <<EOF
Usage: ./scripts/backup.sh [--force] [DEST_DIR]

Archives the user config and state that cannot be re-downloaded:
  config.yaml             NixLoom settings, credentials and asset manifest
  .webui/data             Open WebUI chats and accounts
  .sillytavern/xdg-data   SillyTavern characters, chats, personas
  .hermes                 Hermes memory, skills, sessions
  .webui_secret_key       WebUI runtime secret

SQLite databases are copied through SQLite's online backup API rather than
read off disk, so the archive never contains a torn .db/-wal pair. The
archive is written to a temporary name, listed back to prove it is readable,
and only then moved into place.

Options:
  --force     Back up even while the stack is running
  -h, --help  Show this help

DEST_DIR defaults to ${DEFAULT_DEST}
(override with the NIXLOOM_BACKUP_DIR environment variable).

Restore config into the config directory and state into the state directory:
  tar -xzf DEST_DIR/nixloom-<timestamp>.tar.gz -C ${CONFIG_DIR} config.yaml
  tar -xzf DEST_DIR/nixloom-<timestamp>.tar.gz -C ${STATE_DIR} --strip-components=1 state
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -*)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "${DEST}" ]]; then
        printf 'Only one destination directory may be given (got %q and %q)\n' "${DEST}" "$1" >&2
        exit 2
      fi
      DEST="$1"
      shift
      ;;
  esac
done

DEST="${DEST:-${DEFAULT_DEST}}"

# A live stack rewrites the SQLite databases continuously. The snapshot below
# keeps each database internally consistent, but files added or renamed while
# tar walks the tree can still be missed, so refuse by default.
if systemctl --user --quiet is-active nixloom.target 2>/dev/null; then
  if (( FORCE )); then
    printf 'warning: backing up a running stack; databases are consistent but other files may be caught mid-write.\n' >&2
  else
    printf 'NixLoom is running. Stop it with `nixloom stop`, or pass --force.\n' >&2
    exit 1
  fi
fi

candidates=(
  .webui/data
  .sillytavern/xdg-data
  .hermes
  .webui_secret_key
)
entries=()
for entry in "${candidates[@]}"; do
  if [[ -e "${STATE_DIR}/${entry}" ]]; then
    entries+=("state/${entry}")
  else
    printf 'skipping %s (not present)\n' "${entry}"
  fi
done
if [[ -f "${CONFIG_FILE}" && "${CONFIG_FILE}" != "${NIXLOOM_SHARE:-${NIXLOOM_LIBEXEC}}/config.yaml" ]]; then
  entries+=("config.yaml")
fi

if (( ${#entries[@]} == 0 )); then
  printf 'Nothing to back up; no config or state directories exist yet.\n' >&2
  exit 1
fi

STAGE=""
archive_tmp=""
cleanup() {
  # STAGE holds hardlinks to the live files, so removing it never touches the
  # originals' contents. Some skill directories under .hermes are mode 555, and
  # cp -a reproduces that, so make the staged tree writable before unlinking it.
  if [[ -n "${STAGE}" ]]; then
    # Only the directories: the staged files are hardlinks, so chmod on one
    # would change the mode of the live file it shares an inode with.
    find "${STAGE}" -type d -exec chmod u+rwx {} + 2>/dev/null || true
    rm -rf -- "${STAGE}"
  fi
  [[ -n "${archive_tmp}" ]] && rm -f -- "${archive_tmp}"
  return 0
}
trap cleanup EXIT

STAGE="$(mktemp -d)"

# Hardlink the tree instead of copying it: the archive is assembled from a
# single directory (tar's --exclude is global, so it cannot skip the live
# databases in one source while keeping the snapshots from another).
mkdir -p "${STAGE}/state"
for entry in "${entries[@]}"; do
  if [[ "${entry}" == config.yaml ]]; then
    cp -a "${CONFIG_FILE}" "${STAGE}/config.yaml"
  else
    source_entry="${STATE_DIR}/${entry#state/}"
    mkdir -p "${STAGE}/$(dirname "${entry}")"
    cp -a --link "${source_entry}" "${STAGE}/${entry}" 2>/dev/null \
      || cp -a "${source_entry}" "${STAGE}/${entry}"
  fi
done

# The -wal/-shm sidecars belong to the live databases; the snapshots below are
# self-contained, so shipping the sidecars would only invite a torn restore.
find "${STAGE}" \( -name '*.db-wal' -o -name '*.db-shm' \) -delete

snapshot_db() {
  local src="$1" dst="$2"
  python3 - "${src}" "${dst}" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
try:
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
finally:
    source.close()
PY
}

if command -v python3 >/dev/null && python3 -c 'import sqlite3' 2>/dev/null; then
  while IFS= read -r -d '' staged_db; do
    rel="${staged_db#"${STAGE}/"}"
    # Break the hardlink before writing, or the snapshot would be written
    # straight into the live database.
    rm -f -- "${staged_db}"
    if snapshot_db "${STATE_DIR}/${rel#state/}" "${staged_db}"; then
      printf 'snapshot %s\n' "${rel}"
    else
      printf 'warning: %s is not readable as SQLite; copying the raw file.\n' "${rel}" >&2
      cp -a "${STATE_DIR}/${rel#state/}" "${staged_db}"
    fi
  done < <(find "${STAGE}" -name '*.db' -type f -print0)
  # A clean close leaves no journal behind, but never ship one if it appears.
  find "${STAGE}" \( -name '*.db-wal' -o -name '*.db-shm' \) -delete
else
  printf 'warning: python3 with the sqlite3 module is unavailable; databases are copied as raw files.\n' >&2
fi

mkdir -p "${DEST}"
chmod 700 "${DEST}" 2>/dev/null || true
archive="${DEST}/nixloom-$(date +%Y%m%d-%H%M%S).tar.gz"
archive_tmp="${archive}.tmp"

# Build under a temporary name: a tar that fails partway (exit 1 on a file that
# changed underneath it) must not leave something that looks like a backup.
tar -czf "${archive_tmp}" -C "${STAGE}" "${entries[@]}"
tar -tzf "${archive_tmp}" >/dev/null
chmod 600 "${archive_tmp}"
mv -- "${archive_tmp}" "${archive}"
archive_tmp=""

printf 'backup written: %s (%s)\n' "${archive}" "$(du -h "${archive}" | cut -f1)"
printf 'restore config: tar -xzf %q -C %q config.yaml\n' "${archive}" "${CONFIG_DIR}"
printf 'restore state:  tar -xzf %q -C %q --strip-components=1 state\n' "${archive}" "${STATE_DIR}"
