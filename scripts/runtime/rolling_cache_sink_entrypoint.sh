#!/bin/sh
set -eu

CACHE_ROOT="${ROLLING_CACHE_ROOT:-/media/rolling-cache}"
NAMESPACE="${ROLLING_CACHE_NAMESPACE:-midterm}"
EPOCH_ID="${ROLLING_CACHE_RUNTIME_EPOCH_ID:-}"

if [ -z "${EPOCH_ID}" ] && [ -n "${RUNTIME_EPOCH_STATE_PATH:-}" ] && [ -f "${RUNTIME_EPOCH_STATE_PATH}" ]; then
  EPOCH_ID="$(python - <<'PY'
import json
import os

path = os.getenv("RUNTIME_EPOCH_STATE_PATH", "")
try:
    with open(path, "r", encoding="utf-8") as fh:
        print(str((json.load(fh) or {}).get("runtime_epoch_id") or ""))
except Exception:
    print("")
PY
)"
fi

if [ -z "${EPOCH_ID}" ]; then
  EPOCH_ID="$(date -u +midterm-%Y%m%dT%H%M%SZ-rolling)"
fi

mkdir -p "${CACHE_ROOT}/${NAMESPACE}/epochs/${EPOCH_ID}"

ROLLING_CACHE_RETENTION_SECONDS="${ROLLING_CACHE_RETENTION_SECONDS:-300}"
ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS="${ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS:-30}"
ROLLING_CACHE_MAX_BYTES="${ROLLING_CACHE_MAX_BYTES:-0}"
ROLLING_CACHE_READ_PIN_TTL_SECONDS="${ROLLING_CACHE_READ_PIN_TTL_SECONDS:-600}"
ROLLING_CACHE_MAINTENANCE_OWNER="${ROLLING_CACHE_MAINTENANCE_OWNER:-true}"

case "$(printf '%s' "${ROLLING_CACHE_MAINTENANCE_OWNER}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    echo "[rolling-cache-maintenance] requested_owner=true root=${CACHE_ROOT} retention_s=${ROLLING_CACHE_RETENTION_SECONDS} max_bytes=${ROLLING_CACHE_MAX_BYTES} interval_s=${ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS} read_pin_ttl_s=${ROLLING_CACHE_READ_PIN_TTL_SECONDS}"
    python /opt/rolling-cache-maintenance.py &
    ;;
  *)
    echo "[rolling-cache-maintenance] requested_owner=false root=${CACHE_ROOT}"
    ;;
esac

SEGMENT_FRAMES="${ROLLING_CACHE_SEGMENT_FRAMES:-}"
if [ -z "${SEGMENT_FRAMES}" ]; then
  SEGMENT_FRAMES="$(python - <<'PY'
import os

try:
    seconds = float(os.getenv("ROLLING_CACHE_SEGMENT_SECONDS", "4"))
except ValueError:
    seconds = 4.0
try:
    fps = float(os.getenv("ROLLING_CACHE_FPS", "8"))
except ValueError:
    fps = 8.0
print(max(1, int(round(seconds * fps))))
PY
)"
fi

# Savant 0.6.0 tokens are prefix tokens (``%source_id``, ``%src_filename``),
# not printf-style tokens with a closing percent. A trailing percent becomes a
# literal path character and breaks exact source-id lookup in Media Worker.
export DIR_LOCATION="${CACHE_ROOT}/${NAMESPACE}/epochs/${EPOCH_ID}/%source_id/%src_filename/"
export CHUNK_SIZE="${SEGMENT_FRAMES}"
export METADATA_JSON_FORMAT="${METADATA_JSON_FORMAT:-native}"

exec python /opt/savant/adapters/gst/sinks/video_files.py
