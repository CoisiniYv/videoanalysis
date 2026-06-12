#!/bin/sh
set -eu

EPOCH_ID="$(python - <<'PY'
import datetime
import json
import os
import secrets
import socket

root = "/media/replay-sink-output/midterm"
epoch_id = (
    "midterm-"
    + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    + "-"
    + secrets.token_hex(4)
)
epochs_dir = os.path.join(root, "epochs", epoch_id)
os.makedirs(epochs_dir, exist_ok=True)
state_path = os.path.join(root, ".current_epoch.json")
previous = ""
try:
    with open(state_path, "r", encoding="utf-8") as f:
        previous = str((json.load(f) or {}).get("runtime_epoch_id") or "")
except Exception:
    previous = ""

state = {
    "runtime_epoch_id": epoch_id,
    "created_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    "created_by": "video-file-sink.startup",
    "reason": "container_start",
    "previous_runtime_epoch_id": previous,
}
tmp_path = state_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
os.replace(tmp_path, state_path)

try:
    value = epoch_id.encode("utf-8")
    key = b"video_analytics:midterm:runtime_epoch"
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

export DIR_LOCATION="/media/replay-sink-output/midterm/epochs/${EPOCH_ID}/%source_id%/%src_filename%/"
exec python /opt/savant/adapters/gst/sinks/video_files.py
