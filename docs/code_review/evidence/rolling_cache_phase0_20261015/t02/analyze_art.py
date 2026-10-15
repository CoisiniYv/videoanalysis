import collections
import datetime
import hashlib
import json
import math
import os
import re

ROOTS = {
    "1013": "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2",
    "1012": "/data/video-analytics/artifacts/pressure60_remux_refill_t4_4fps_1h_20261011_r2",
}
FIELDS = ["publication_queue_residence_ms", "publication_capacity_wait_ms", "publish_total_ms", "publish_metadata_fsync_ms", "publish_manifest_fsync_ms", "publish_staging_dir_fsync_ms", "publish_parent_dir_fsync_ms"]
UTC = datetime.timezone.utc

def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat()

def dist(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    def percentile(p):
        pos = (len(values) - 1) * p
        lo = int(pos)
        hi = min(lo + 1, len(values) - 1)
        return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 6)
    return {"count": len(values), "min": round(values[0], 6), "p50": percentile(0.5), "p95": percentile(0.95), "max": round(values[-1], 6)}

def regression(points):
    if len(points) < 2:
        return {"count": len(points)}
    x0 = points[0][0]
    xs, ys = [p[0] - x0 for p in points], [p[1] for p in points]
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    xy = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    return {"count": len(points), "slope_seconds_per_second": xy / xx if xx else None, "r_squared": xy * xy / xx / yy if xx and yy else None}

out = {"captured_at_utc": datetime.datetime.now(UTC).isoformat(), "timestamp_interpretation": "container Python log timestamps interpreted as UTC; cross-checked against epoch-ns last_pts, but no historic timezone environment snapshot", "percentile_method": "linear interpolation at (n-1)*p", "runs": {}}
for label, root in ROOTS.items():
    report_raw = open(os.path.join(root, "report.json"), "rb").read()
    report = json.loads(report_raw)
    config = json.load(open(os.path.join(root, "run_config.json")))
    window = report["pressure_sampling_window"]
    start = datetime.datetime.fromisoformat(window["started_at"]).timestamp()
    end = datetime.datetime.fromisoformat(window["ended_at"]).timestamp()
    run = {"root": root, "report_sha256": hashlib.sha256(report_raw).hexdigest(), "formal_window_utc": window, "logs": {}, "report_host_override_token_count": report_raw.count(b"PRESSURE_ROLLING_CACHE_ROOT_HOST")}
    for suffix in ("a", "b"):
        name = "rolling_cache_sink_" + suffix + "_logs_since_start.txt"
        path = os.path.join(root, name)
        sha = hashlib.sha256()
        records = []
        buckets = collections.defaultdict(list)
        for number, raw in enumerate(open(path, "rb"), 1):
            sha.update(raw)
            line = raw.decode("utf-8", errors="replace")
            if "segment published" not in line or not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}", line):
                continue
            stamp = datetime.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC).timestamp()
            fields = dict(re.findall(r"([a-zA-Z_]+)=([^\s]+)", line))
            record = {"line": number, "log_at_epoch_s": stamp, "source": fields.get("source"), "last_pts": int(fields["last_pts"])}
            record.update({field: float(fields[field]) for field in FIELDS if field in fields})
            record["log_minus_last_pts_s"] = stamp - record["last_pts"] / 1e9
            # Kept only as a literal taskbook request; queue residence is already included in log-minus-PTS.
            record["lag_plus_queue_residence_s"] = record["log_minus_last_pts_s"] + record.get("publication_queue_residence_ms", 0) / 1000
            records.append(record)
            buckets[int(stamp // 30) * 30].append(record)
        def metrics(rows):
            return {field: dist([r[field] for r in rows if field in r]) for field in FIELDS + ["log_minus_last_pts_s", "lag_plus_queue_residence_s"]}
        formal = [r for r in records if start <= r["log_at_epoch_s"] <= end]
        bucket_rows = [{"start_utc": iso(t), "start_kst": datetime.datetime.fromtimestamp(t, datetime.timezone(datetime.timedelta(hours=9))).isoformat(), "epoch_s": t, "formal_window_overlap": t < end and t + 30 > start, "metrics": metrics(rows)} for t, rows in sorted(buckets.items())]
        formal_buckets = [b for b in bucket_rows if b["formal_window_overlap"]]
        run["logs"][suffix] = {"source_file": name, "bytes": os.stat(path).st_size, "sha256": sha.hexdigest(), "all": metrics(records), "formal_by_publication_time": metrics(formal), "first_utc": iso(records[0]["log_at_epoch_s"]) if records else None, "last_utc": iso(records[-1]["log_at_epoch_s"]) if records else None, "buckets_30s": bucket_rows, "formal_lag_p50_regression": regression([(b["epoch_s"], b["metrics"]["log_minus_last_pts_s"]["p50"]) for b in formal_buckets]), "formal_first_bucket": formal_buckets[0] if formal_buckets else None, "formal_last_bucket": formal_buckets[-1] if formal_buckets else None, "samples": records[:2] + records[-2:]}
    path = os.path.join(root, "media_worker_logs_since_start.txt")
    sha = hashlib.sha256()
    samples = []
    for number, raw in enumerate(open(path, "rb"), 1):
        sha.update(raw)
        line = raw.decode("utf-8", errors="replace")
        if not re.search(r"media_event_finalized .* remux_ms=[0-9]+(?:\s|$)", line) or not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}", line):
            continue
        fields = dict(re.findall(r"([a-zA-Z_]+)=([^\s]+)", line))
        stamp = datetime.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC).timestamp()
        samples.append({"line": number, "at_utc": iso(stamp), "event_id": fields.get("event_id"), "source_id": fields.get("source_id"), "remux_ms": int(fields["remux_ms"]), "in_formal_window": start <= stamp <= end})
    run["remux"] = {"source_file": os.path.basename(path), "bytes": os.stat(path).st_size, "sha256": sha.hexdigest(), "all": dist([s["remux_ms"] for s in samples]), "formal_by_completion_time": dist([s["remux_ms"] for s in samples if s["in_formal_window"]]), "first_utc": samples[0]["at_utc"] if samples else None, "last_utc": samples[-1]["at_utc"] if samples else None, "samples": samples if label == "1013" else samples[:2] + samples[-2:]}
    samples_root = os.path.join(root, "samples")
    runtime_files = [n for n in sorted(os.listdir(samples_root)) if n.startswith("runtime_") and n.endswith(".json")]
    shard_max = collections.defaultdict(float)
    metric_name_occurrences = collections.Counter()
    metric_name_samples = []
    def walk(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                p = path + "." + key if path else key
                if "stale_dropped" in key or "gop_resync" in key or "head_age" in key:
                    metric_name_occurrences[key] += 1
                    if len(metric_name_samples) < 20:
                        metric_name_samples.append({"path": p, "value": child})
                walk(child, p)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, path + "[" + str(i) + "]")
    for name in runtime_files:
        data = json.load(open(os.path.join(samples_root, name)))
        walk(data)
        for shard in data.get("forwarder", {}).get("shards", []):
            shard_name = shard.get("replay_shard_id", shard.get("container", shard.get("name", "unknown")))
            global_metrics = shard.get("global", {})
            depth = global_metrics.get("queue_depth", global_metrics.get("va_forwarder_queue_depth", 0))
            shard_max[str(shard_name)] = max(shard_max[str(shard_name)], float(depth or 0))
    run["runtime_samples"] = {"count": len(runtime_files), "first": runtime_files[0] if runtime_files else None, "metric_name_occurrences": dict(metric_name_occurrences), "metric_name_samples": metric_name_samples, "shard_queue_max_initial_parse": dict(shard_max)}
    details_path = os.path.join(root, "non_materialized_task_details.json")
    if os.path.exists(details_path):
        raw = open(details_path, "rb").read()
        details = json.loads(raw)
        key_counts = collections.Counter(k for row in details for k in row)
        groups = collections.Counter((row.get("event_type"), row.get("materialization_status"), row.get("materialization_expired_reason"), row.get("materialization_attempt_count") == 0) for row in details)
        run["non_materialized_details"] = {"source_file": os.path.basename(details_path), "sha256": hashlib.sha256(raw).hexdigest(), "count": len(details), "key_counts": dict(key_counts), "groups": [{"event_type": k[0], "status": k[1], "expired_reason": k[2], "never_attempted": k[3], "count": v} for k, v in groups.items()], "task_ids": [r["task_id"] for r in details], "last_reason_fields_present": [k for k in key_counts if "retry" in k or "audit" in k], "attempts_distribution": dist([r["materialization_attempt_count"] for r in details if isinstance(r.get("materialization_attempt_count"), (int, float))])}
    out["runs"][label] = run
print(json.dumps(out, ensure_ascii=False, indent=2))
