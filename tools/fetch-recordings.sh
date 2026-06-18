#!/usr/bin/env bash
#
# fetch-recordings.sh -- pull recorded video off the ARC camera Senders (Pis).
#
# Each Sender records to /var/arc/recordings/ as "<name>-<timestamp>.mp4"
# (see control-plane/arc/config.py VideoConfig.recording_path and setup.sh).
# This script copies those files down to a local folder so you can grab them
# all in one shot. Uses rsync when available (resumable, skips already-pulled
# files); falls back to scp.
#
# Usage:
#   tools/fetch-recordings.sh [options] [host ...]
#
# Hosts default to the bench set below; override by passing them as args or
# via the ARC_HOSTS env var (space-separated).
#
# Options:
#   -d DEST      Local destination directory (default: ./recordings)
#   -u USER      SSH user on the Pis (default: pi)
#   -r DIR       Remote recordings dir (default: /var/arc/recordings)
#   -p PATTERN   Only fetch files matching this glob, e.g. '*-20260617*.mp4'
#   -l           List remote recordings (with sizes) and exit; copy nothing
#   -m           Move: delete each file from the Pi after a verified copy
#   -n           Dry run: show what would be copied, transfer nothing
#   -h           Show this help
#
# Examples:
#   tools/fetch-recordings.sh                         # all Pis, all videos
#   tools/fetch-recordings.sh arcpi2.local            # just one Pi
#   tools/fetch-recordings.sh -l                      # see what's there first
#   tools/fetch-recordings.sh -p '*-20260617*.mp4'    # only today's flight
#   tools/fetch-recordings.sh -d /mnt/usb/flights -m  # archive + free the SD
#
set -euo pipefail

# --- defaults -----------------------------------------------------------------
DEFAULT_HOSTS=(arcpi1.local arcpi2.local arcpi3.local)
DEST="./recordings"
SSH_USER="pi"
REMOTE_DIR="/var/arc/recordings"
PATTERN="*.mp4"
LIST_ONLY=0
MOVE=0
DRY_RUN=0

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while getopts ":d:u:r:p:lmnh" opt; do
  case "$opt" in
    d) DEST="$OPTARG" ;;
    u) SSH_USER="$OPTARG" ;;
    r) REMOTE_DIR="$OPTARG" ;;
    p) PATTERN="$OPTARG" ;;
    l) LIST_ONLY=1 ;;
    m) MOVE=1 ;;
    n) DRY_RUN=1 ;;
    h) usage 0 ;;
    \?) echo "unknown option: -$OPTARG" >&2; usage 1 ;;
    :)  echo "option -$OPTARG needs an argument" >&2; usage 1 ;;
  esac
done
shift $((OPTIND - 1))

# Hosts: positional args > ARC_HOSTS env > built-in default.
if [[ $# -gt 0 ]]; then
  HOSTS=("$@")
elif [[ -n "${ARC_HOSTS:-}" ]]; then
  # shellcheck disable=SC2206
  HOSTS=(${ARC_HOSTS})
else
  HOSTS=("${DEFAULT_HOSTS[@]}")
fi

have_rsync=0
command -v rsync >/dev/null 2>&1 && have_rsync=1

SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

ok=0; fail=0
for host in "${HOSTS[@]}"; do
  target="${SSH_USER}@${host}"
  echo "=== ${host} ==="

  # Probe reachability so one offline Pi doesn't abort the whole run.
  if ! ssh "${SSH_OPTS[@]}" "$target" true 2>/dev/null; then
    echo "  unreachable (skipping)"; ((fail++)); continue
  fi

  if [[ $LIST_ONLY -eq 1 ]]; then
    ssh "${SSH_OPTS[@]}" "$target" \
      "ls -lh --time-style=long-iso ${REMOTE_DIR}/${PATTERN} 2>/dev/null" \
      || echo "  (no matching recordings)"
    ((ok++)); continue
  fi

  mkdir -p "$DEST"

  if [[ $have_rsync -eq 1 ]]; then
    rsync_flags=(-avh --progress --ignore-existing)
    [[ $DRY_RUN -eq 1 ]] && rsync_flags+=(--dry-run)
    [[ $MOVE -eq 1 && $DRY_RUN -eq 0 ]] && rsync_flags+=(--remove-source-files)
    if rsync "${rsync_flags[@]}" -e "ssh ${SSH_OPTS[*]}" \
         "${target}:${REMOTE_DIR}/${PATTERN}" "$DEST/"; then
      ((ok++))
    else
      echo "  nothing matched or transfer failed"; ((fail++))
    fi
  else
    # scp fallback: enumerate first so MOVE can delete only verified files.
    files=$(ssh "${SSH_OPTS[@]}" "$target" \
              "ls -1 ${REMOTE_DIR}/${PATTERN} 2>/dev/null" || true)
    if [[ -z "$files" ]]; then
      echo "  (no matching recordings)"; ((ok++)); continue
    fi
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      base=$(basename "$f")
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "  would copy $base"; continue
      fi
      if scp "${SSH_OPTS[@]}" "${target}:${f}" "$DEST/$base"; then
        echo "  got $base"
        [[ $MOVE -eq 1 ]] && ssh "${SSH_OPTS[@]}" "$target" "rm -f -- '$f'"
      else
        echo "  FAILED $base" >&2; fail=1
      fi
    done <<< "$files"
    ((ok++))
  fi
done

echo
echo "done: ${ok} host(s) processed, ${fail} skipped/failed -> ${DEST}"
[[ $LIST_ONLY -eq 0 && $DRY_RUN -eq 0 ]] && ls -lh "$DEST" 2>/dev/null || true
