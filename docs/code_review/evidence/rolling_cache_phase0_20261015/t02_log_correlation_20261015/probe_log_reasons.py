import collections
import datetime
import hashlib
import json
import os
import re

root = "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2"
markers = ["materialization_deferred", "materialization_expired", "materialization_failed", "business_deadline_expired", "coverage_not_complete", "no_overlapping_final_segment", "capacity_unavailable", "segment_not_stable", "segment_disappeared", "lifecycle_v2_retry"]
out = {"captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "source_root": root, "logs": {}}
for name in ("media_worker_logs_since_start.txt", "event_worker_logs_since_start.txt"):
    path = os.path.join(root, name)
    sha = hashlib.sha256()
    counts = collections.Counter()
    first = collections.defaultdict(list)
    last = collections.defaultdict(lambda: collections.deque(maxlen=2))
    for number, raw in enumerate(open(path, "rb"), 1):
        sha.update(raw)
        line = raw.decode("utf-8", errors="replace").rstrip()
        for marker in markers:
            if marker in line:
                counts[marker] += 1
                item = {"line": number, "text": line}
                if len(first[marker]) < 2:
                    first[marker].append(item)
                last[marker].append(item)
    out["logs"][name] = {"bytes": os.stat(path).st_size, "sha256": sha.hexdigest(), "counts": dict(counts), "first": dict(first), "last": {key: list(values) for key, values in last.items()}}
print(json.dumps(out, ensure_ascii=False, indent=2))
