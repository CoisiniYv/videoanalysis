import collections
import datetime
import hashlib
import json
import os
import re

ROOT = "/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2"
UTC = datetime.timezone.utc
KST = datetime.timezone(datetime.timedelta(hours=9))
DETAILS = "non_materialized_task_details.json"
LOG = "media_worker_logs_since_start.txt"

def stamp(value):
    return datetime.datetime.fromisoformat(value).timestamp() if value else None

def iso(value, timezone=UTC):
    return datetime.datetime.fromtimestamp(value, timezone).isoformat() if value is not None else None

detail_raw = open(os.path.join(ROOT, DETAILS), "rb").read()
details = json.loads(detail_raw)
by_event = collections.defaultdict(list)
for row in details:
    by_event[row["event_id"]].append(row)

records = collections.defaultdict(list)
event_counts = collections.Counter()
format_errors = []
sha = hashlib.sha256()
deferred = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) INFO [^ ]+ rolling_cache_coverage_deferred event_id=([^ ]+) reason=(.*?) retry_after_s=([^ ]+) persisted=(True|False) generation=([0-9]+)(?:\s|$)")
for number, raw in enumerate(open(os.path.join(ROOT, LOG), "rb"), 1):
    sha.update(raw)
    line = raw.decode("utf-8", errors="replace").rstrip()
    if "rolling_cache_coverage_deferred" not in line:
        continue
    match = deferred.match(line)
    if not match:
        format_errors.append({"line": number, "text": line})
        continue
    time_text, event_id, reason, retry_after, persisted, generation = match.groups()
    at = datetime.datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC).timestamp()
    event_counts["parsed_lines"] += 1
    event_counts["persisted_" + persisted.lower()] += 1
    event_counts["cohort_lines" if event_id in by_event else "outside_cohort_lines"] += 1
    if event_id in by_event:
        records[event_id].append({"line": number, "at_epoch_s": at, "at_utc": iso(at), "at_kst": iso(at, KST), "reason_detail": reason, "retry_after_s": float(retry_after), "persisted": persisted == "True", "generation": int(generation), "raw_line": line})

joined = []
for row in details:
    cutoff = stamp(row.get("task_updated_at"))
    event_records = records[row["event_id"]]
    before = [record for record in event_records if cutoff is not None and record["at_epoch_s"] <= cutoff]
    persisted_before = [record for record in before if record["persisted"]]
    latest = max(persisted_before, key=lambda record: (record["at_epoch_s"], record["line"])) if persisted_before else None
    latest_any = max(before, key=lambda record: (record["at_epoch_s"], record["line"])) if before else None
    media = row.get("event_media") or {}
    joined.append({
        "task_id": row["task_id"], "event_id": row["event_id"], "event_type": row.get("event_type"), "task_type": row.get("task_type"),
        "materialization_status": row.get("materialization_status"), "terminal_expired_reason": row.get("materialization_expired_reason"), "failure_reason": row.get("materialization_failure_reason"),
        "attempt_count": row.get("materialization_attempt_count"), "never_attempted_in_artifact": row.get("materialization_attempt_count") == 0,
        "task_updated_at_utc": row.get("task_updated_at"), "ready_at_utc": row.get("materialization_ready_at"), "artifact_clip_required": media.get("clip_required"), "artifact_snapshot_required": media.get("snapshot_required"),
        "matching_retry_log_count": len(event_records), "retry_logs_before_artifact_update": len(before), "persisted_retry_logs_before_artifact_update": len(persisted_before), "retry_logs_after_artifact_update": len(event_records) - len(before),
        "last_observed_persisted_retry_before_artifact_update": latest,
        "last_observed_retry_before_artifact_update": latest_any,
        "missing_reason": None if latest else ("no_retry_log_and_artifact_attempt_count_zero" if row.get("materialization_attempt_count") == 0 else "no_matching_persisted_retry_log_before_artifact_update"),
    })

out = {
    "captured_at_utc": datetime.datetime.now(UTC).isoformat(), "source_root": ROOT,
    "inputs": {DETAILS: {"bytes": len(detail_raw), "sha256": hashlib.sha256(detail_raw).hexdigest(), "count": len(details)}, LOG: {"bytes": os.stat(os.path.join(ROOT, LOG)).st_size, "sha256": sha.hexdigest()}},
    "method": "Exact event_id join to the historical non-materialized ART cohort; last persisted=True retry log at or before each artifact task_updated_at. This is a log observation, not recovered database lifecycle_v2_retry audit.",
    "timestamp_interpretation": "Naive container timestamps interpreted as UTC, consistent with earlier last_pts cross-check; historical TZ snapshot unavailable.",
    "duplicate_event_ids_in_details": {event_id: len(rows) for event_id, rows in by_event.items() if len(rows) > 1},
    "log_counts": dict(event_counts), "unparsed_deferred_lines": format_errors,
    "cohort_rows": joined,
}
print(json.dumps(out, ensure_ascii=False, indent=2))
