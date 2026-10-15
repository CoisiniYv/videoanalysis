import collections
import datetime
import hashlib
import json
import os
import re

root = "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2"
out = {"captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "source_root": root, "logs": {}}
for name in ("media_worker_logs_since_start.txt", "event_worker_logs_since_start.txt"):
    path = os.path.join(root, name)
    sha = hashlib.sha256()
    counts = collections.Counter()
    marker_counts = collections.Counter()
    examples = {}
    marker_examples = collections.defaultdict(list)
    first = []
    for number, raw in enumerate(open(path, "rb"), 1):
        sha.update(raw)
        line = raw.decode("utf-8", errors="replace").rstrip()
        if len(first) < 3:
            first.append({"line": number, "text": line})
        match = re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (\w+) ([^ ]+) ([^ ]+)", line)
        if match:
            token = match.group(3)
            counts[token] += 1
            if token not in examples:
                examples[token] = {"line": number, "text": line}
        for marker in ("rolling_cache_coverage_deferred", "retry", "defer", "expired", "reason=", "event_id=", "pre_gap_ns=", "post_gap_ns=", "internal_gap_ns=", "window_not_covered", "requested_window"):
            if marker in line:
                marker_counts[marker] += 1
                if len(marker_examples[marker]) < 3:
                    marker_examples[marker].append({"line": number, "text": line})
    out["logs"][name] = {"bytes": os.stat(path).st_size, "sha256": sha.hexdigest(), "event_counts": dict(counts), "event_examples": examples, "marker_counts": dict(marker_counts), "marker_examples": dict(marker_examples), "first_lines": first}
print(json.dumps(out, ensure_ascii=False, indent=2))
