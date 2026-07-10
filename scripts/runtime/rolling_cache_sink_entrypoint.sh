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

python - <<'PY' &
import os
import shutil
import time
from pathlib import Path

root = Path(os.getenv("ROLLING_CACHE_ROOT", "/media/rolling-cache"))
try:
    retention_s = max(0.0, float(os.getenv("ROLLING_CACHE_RETENTION_SECONDS", "300")))
except ValueError:
    retention_s = 300.0
try:
    interval_s = max(5.0, float(os.getenv("ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS", "30")))
except ValueError:
    interval_s = 30.0

if retention_s <= 0:
    raise SystemExit(0)

def cleanup_once() -> None:
    now = time.time()
    cutoff = now - retention_s
    if not root.exists():
        return
    for path in list(root.rglob("*")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue
        except Exception as exc:
            print(f"[rolling-cache-cleanup] file_cleanup_failed path={path} error={exc}", flush=True)
    dirs = [p for p in root.rglob("*") if p.is_dir()]
    for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if path == root:
            continue
        try:
            path.rmdir()
        except OSError:
            pass

while True:
    cleanup_once()
    time.sleep(interval_s)
PY

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

export DIR_LOCATION="${CACHE_ROOT}/${NAMESPACE}/epochs/${EPOCH_ID}/%source_id%/%src_filename%/"
export CHUNK_SIZE="${SEGMENT_FRAMES}"
export METADATA_JSON_FORMAT="${METADATA_JSON_FORMAT:-native}"

exec python /opt/savant/adapters/gst/sinks/video_files.py
