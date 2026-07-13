#!/bin/sh
set -eu

EPOCH_ROOT="${VIDEO_FILE_SINK_EPOCH_ROOT:-/media/replay-sink-output/midterm}"

EPOCH_ID="$(python - <<'PY'
import datetime
import json
import os
import secrets
import socket

root = os.getenv("VIDEO_FILE_SINK_EPOCH_ROOT", "/media/replay-sink-output/midterm")
state_path = os.path.join(root, ".current_epoch.json")
previous = ""
existing_state = {}
try:
    with open(state_path, "r", encoding="utf-8") as f:
        existing_state = json.load(f) or {}
        previous = str(existing_state.get("runtime_epoch_id") or "")
except Exception:
    previous = ""

reuse_current = str(os.getenv("VIDEO_FILE_SINK_REUSE_CURRENT_EPOCH") or "").lower() in {
    "1",
    "true",
    "yes",
}
if reuse_current and previous:
    epoch_id = previous
    publish_epoch = False
else:
    epoch_id = (
        "midterm-"
        + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(4)
    )
    publish_epoch = True

epochs_dir = os.path.join(root, "epochs", epoch_id)
os.makedirs(epochs_dir, exist_ok=True)

if publish_epoch:
    state = {
        "runtime_epoch_id": epoch_id,
        "created_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "created_by": os.getenv("VIDEO_FILE_SINK_CREATED_BY", "video-file-sink.startup"),
        "reason": "container_start",
        "previous_runtime_epoch_id": previous,
    }
    os.makedirs(root, exist_ok=True)
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, state_path)

    try:
        value = epoch_id.encode("utf-8")
        key = os.getenv(
            "VIDEO_FILE_SINK_REDIS_EPOCH_KEY",
            "video_analytics:midterm:runtime_epoch",
        ).encode("utf-8")
        crlf = bytes([13, 10])
        dollar = bytes([36])
        payload = crlf.join(
            [
                b"*3",
                dollar + b"3",
                b"SET",
                dollar + str(len(key)).encode("ascii"),
                key,
                dollar + str(len(value)).encode("ascii"),
                value,
            ]
        ) + crlf
        with socket.create_connection(("redis", 6379), timeout=1.0) as sock:
            sock.sendall(payload)
            sock.recv(256)
    except Exception:
        pass

print(epoch_id)
PY
)"

# Savant replaces the prefix tokens themselves; a closing percent is retained
# literally in the directory name and violates the Replay/source-id contract.
export DIR_LOCATION="${EPOCH_ROOT}/epochs/${EPOCH_ID}/%source_id/%src_filename/"
exec python /opt/savant/adapters/gst/sinks/video_files.py
