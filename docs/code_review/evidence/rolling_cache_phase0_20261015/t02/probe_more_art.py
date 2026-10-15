import json
import os

root = "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2"
out = {}
for name in ("non_materialized_task_details.json", "report.json"):
    data = json.load(open(os.path.join(root, name)))
    if name == "non_materialized_task_details.json":
        out[name] = {"type": type(data).__name__, "count": len(data), "schema": list(data) if isinstance(data, dict) else None, "first": data[:2] if isinstance(data, list) else {k: (v[:2] if isinstance(v, list) else v) for k, v in data.items()}}
    else:
        out[name] = {k: data[k] for k in ("pressure_sampling_window", "harness_rolling_cache_full_rate_gate", "rolling_cache_segment_visibility", "rolling_cache_pressure", "pressure_runtime_cleanup_after_restore", "db_summary_before_cleanup", "rolling_cache_sinks_restore") if k in data}
        out[name]["other_keys"] = list(data)
sample_root = os.path.join(root, "samples")
for prefix in ("runtime_", "forwarder_", "metrics_"):
    names = [name for name in sorted(os.listdir(sample_root)) if name.startswith(prefix)]
    if names:
        path = os.path.join(sample_root, names[0])
        out["sample_" + prefix] = {"name": names[0], "text": open(path).read()}
print(json.dumps(out, ensure_ascii=False, indent=2))
