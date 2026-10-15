import collections
import hashlib
import json
import os

roots = {
    "1013": "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2",
    "1012": "/data/video-analytics/artifacts/pressure60_remux_refill_t4_4fps_1h_20261011_r2",
}
out = {}
for label, root in roots.items():
    result = {"logs": {}, "samples": {}, "extra": {}}
    for name, marker in (("rolling_cache_sink_a_logs_since_start.txt", "segment published"), ("rolling_cache_sink_b_logs_since_start.txt", "segment published"), ("media_worker_logs_since_start.txt", "media_event_finalized")):
        first = []
        last = collections.deque(maxlen=3)
        count = 0
        with open(os.path.join(root, name), encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if marker in line:
                    count += 1
                    item = {"line": number, "text": line.rstrip()}
                    if len(first) < 3:
                        first.append(item)
                    last.append(item)
        result["logs"][name] = {"count": count, "first": first, "last": list(last)}
    sample_root = os.path.join(root, "samples")
    if os.path.isdir(sample_root):
        names = sorted(os.listdir(sample_root))
        result["samples"]["entries"] = [{"name": n, "bytes": os.stat(os.path.join(sample_root, n)).st_size} for n in names]
        for name in names[:1]:
            path = os.path.join(sample_root, name)
            if os.path.isfile(path) and os.stat(path).st_size < 2000000:
                result["samples"]["first_file"] = {"name": name, "text": open(path, encoding="utf-8").read()}
    for name in ("non_materialized_task_details.json", "sample_summary.json", "downstream_observability_summary.json", "savant_ablation_manifest.json"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            raw = open(path, "rb").read()
            result["extra"][name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "data": json.loads(raw) if len(raw) < 3000000 else None}
    out[label] = result
print(json.dumps(out, ensure_ascii=False, indent=2))
