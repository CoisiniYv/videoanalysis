import datetime
import json
import os

roots = {
    "1013": "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2",
    "1012": "/data/video-analytics/artifacts/pressure60_remux_refill_t4_4fps_1h_20261011_r2",
}
out = {"captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "roots": {}}
for label, root in roots.items():
    entries = []
    try:
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            stat = os.stat(path)
            entries.append({"name": name, "bytes": stat.st_size, "is_file": os.path.isfile(path), "modified_utc": datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc).isoformat()})
        out["roots"][label] = {"path": root, "entries": entries}
    except Exception as exc:
        out["roots"][label] = {"path": root, "error": str(exc)}
print(json.dumps(out, ensure_ascii=False, indent=2))
