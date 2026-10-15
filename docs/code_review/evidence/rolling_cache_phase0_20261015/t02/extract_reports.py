import datetime
import hashlib
import json
import os
import re

roots = {
    "1013": "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2",
    "1012": "/data/video-analytics/artifacts/pressure60_remux_refill_t4_4fps_1h_20261011_r2",
}

def schema(value, path="", depth=0):
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            p = path + "." + key if path else key
            if isinstance(child, (dict, list)):
                result.append({"path": p, "type": type(child).__name__, "count": len(child)})
                if depth < 2:
                    result.extend(schema(child, p, depth + 1))
            else:
                result.append({"path": p, "value": child})
    return result

out = {"captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "runs": {}}
for label, root in roots.items():
    report_path = os.path.join(root, "report.json")
    raw = open(report_path, "rb").read()
    report = json.loads(raw)
    run = {"root": root, "report_sha256": hashlib.sha256(raw).hexdigest(), "schema": schema(report), "files": {}}
    for name in sorted(os.listdir(root)):
        if name == "run_config.json" or name.endswith(".override.yml") or name in ("dual_shard_metrics_ready.json", "active_evidence_guard_snapshots.json"):
            path = os.path.join(root, name)
            data = open(path, "rb").read()
            run["files"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "text": data.decode("utf-8")}
    keep = ["rolling_cache_segment_visibility", "rolling_cache_sinks_pressure", "harness_rolling_cache_full_rate_gate", "db_summary_before_cleanup", "cleanup", "rolling_cache_restore", "diagnostics", "run_config"]
    run["selected_report"] = {key: report[key] for key in keep if key in report}
    out["runs"][label] = run
print(json.dumps(out, ensure_ascii=False, indent=2))
