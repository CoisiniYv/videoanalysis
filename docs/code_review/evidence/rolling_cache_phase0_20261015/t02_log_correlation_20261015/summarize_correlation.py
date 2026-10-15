import collections
import datetime
import hashlib
import json
from pathlib import Path
import runpy

BASE = Path(__file__).resolve().parent
OLD = BASE.parent / "t02"
UTC = datetime.timezone.utc
KST = datetime.timezone(datetime.timedelta(hours=9))

def load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))

def write(name, value):
    (BASE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

capture = load("correlate_log_reasons.capture.json")
assert capture["status"] == 0
data = json.loads(capture["stdout"])
old = json.loads((OLD / "1013_log_analysis.json").read_text(encoding="utf-8"))
assert data["inputs"]["media_worker_logs_since_start.txt"]["sha256"] == old["remux"]["sha256"]
assert data["inputs"]["non_materialized_task_details.json"]["sha256"] == old["non_materialized_details"]["sha256"]
assert not data["duplicate_event_ids_in_details"]
assert not data["unparsed_deferred_lines"]
assert len(data["cohort_rows"]) == 2877

contract_path = BASE / "normalization_contract_98d388c.py"
contract = runpy.run_path(str(contract_path))
normalize = contract["normalize_reason_code"]
groups = collections.Counter()
subtypes = collections.Counter()
examples = collections.defaultdict(list)
buckets = collections.defaultdict(collections.Counter)
after_snapshot = []
for row in data["cohort_rows"]:
    log = row["last_observed_persisted_retry_before_artifact_update"]
    normalized = normalize(log["reason_detail"]) if log else None
    row["last_observed_normalized_reason"] = normalized
    detail = log["reason_detail"] if log else ""
    if normalized == "coverage_not_complete":
        if any(token in detail for token in ("internal_gap_ns=", "requested_window_internal_gap", "rolling_cache_output_duration_short", "rolling_cache_output_frame_rate_low")):
            subtype = "video_internal_or_output_problem"
        elif "pre_gap_ns=" in detail and "post_gap_ns=" in detail:
            subtype = "missing_pre_and_post_window"
        elif "post_gap_ns=" in detail:
            subtype = "missing_post_window"
        elif "pre_gap_ns=" in detail:
            subtype = "missing_pre_window"
        elif "window_not_covered" in detail or "event_frame_not_covered" in detail:
            subtype = "missing_requested_or_event_window"
        else:
            subtype = "coverage_detail_unclassified"
    else:
        subtype = normalized or row["missing_reason"]
    row["log_reason_subtype"] = subtype
    groups[(row["materialization_status"], row["event_type"], row["artifact_clip_required"], row["never_attempted_in_artifact"], normalized, subtype)] += 1
    subtypes[subtype] += 1
    if log:
        sample = {"task_id": row["task_id"], "event_id": row["event_id"], "event_type": row["event_type"], "artifact_clip_required": row["artifact_clip_required"], "task_updated_at_utc": row["task_updated_at_utc"], "normalized_reason": normalized, "subtype": subtype, "log": log}
        if len(examples[normalized]) < 10:
            examples[normalized].append(sample)
        if len(examples["subtype:" + subtype]) < 10:
            examples["subtype:" + subtype].append(sample)
    if row["retry_logs_after_artifact_update"]:
        after_snapshot.append({"event_id": row["event_id"], "count": row["retry_logs_after_artifact_update"], "status": row["materialization_status"], "failure_reason": row["failure_reason"]})
    if row["materialization_status"] == "materialization_expired" and row["ready_at_utc"]:
        epoch = datetime.datetime.fromisoformat(row["ready_at_utc"]).timestamp()
        start = int(epoch // 300) * 300
        buckets[start][(row["event_type"], row["artifact_clip_required"], normalized or "unavailable", subtype)] += 1

expired = [r for r in data["cohort_rows"] if r["materialization_status"] == "materialization_expired"]
clip_expired = [r for r in expired if r["artifact_clip_required"] is True]
coverage = [r for r in clip_expired if r["last_observed_normalized_reason"] == "coverage_not_complete"]
unattempted = [r for r in clip_expired if r["never_attempted_in_artifact"]]
summary = {
    "generated_at_utc": datetime.datetime.now(UTC).isoformat(),
    "capture_completed_at_utc": capture["captured_at_utc"],
    "capture_completed_at_kst": datetime.datetime.fromisoformat(capture["captured_at_utc"].replace("Z", "+00:00")).astimezone(KST).isoformat(),
    "source_root": data["source_root"], "inputs": data["inputs"], "inputs_same_sha256_as_first_t02_capture": True,
    "normalization": {"source_commit": "98d388c", "source_path": "libs/evidence_lifecycle/contract.py", "copied_source_file": contract_path.name, "copied_source_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(), "function": "normalize_reason_code", "meaning": "Normative mapping of observed reason text using the taskbook baseline contract; not a claim about recovered database audit or historical image code identity."},
    "method": data["method"], "timestamp_interpretation": data["timestamp_interpretation"], "log_counts": data["log_counts"],
    "cohort_count": len(data["cohort_rows"]), "expired_count": len(expired), "clip_required_expired_count": len(clip_expired),
    "clip_required_expired_with_observed_edge_coverage_reason": len(coverage), "clip_required_expired_never_attempted": len(unattempted),
    "clip_required_expired_observed_edge_coverage_fraction": len(coverage) / len(clip_expired),
    "clip_required_expired_never_attempted_fraction": len(unattempted) / len(clip_expired),
    "database_task_type_available": False, "database_lifecycle_audit_recovered": False,
    "unparsed_deferred_line_count": len(data["unparsed_deferred_lines"]), "duplicate_event_id_count": len(data["duplicate_event_ids_in_details"]),
    "groups": [{"materialization_status": key[0], "event_type": key[1], "artifact_clip_required": key[2], "never_attempted_in_artifact": key[3], "last_observed_normalized_reason": key[4], "log_reason_subtype": key[5], "count": count} for key, count in groups.items()],
    "subtype_counts": dict(subtypes), "excluded_post_snapshot_event_rows": after_snapshot,
    "ready_at_5m_expired_only": [{"bucket_start_utc": datetime.datetime.fromtimestamp(epoch, UTC).isoformat(), "bucket_start_kst": datetime.datetime.fromtimestamp(epoch, KST).isoformat(), "groups": [{"event_type": key[0], "artifact_clip_required": key[1], "last_observed_normalized_reason": key[2], "subtype": key[3], "count": count} for key, count in counts.items()]} for epoch, counts in sorted(buckets.items())],
    "limitations": ["Last observed persisted retry before ART task_updated_at is not the database lifecycle_v2_retry audit.", "task_type is absent; clip_required is an explicit event-media flag, not a recovered task type.", "No retry log is not by itself proof of never attempted; the separate attempt_count=0 flag comes from ART.", "The ART cohort contains non-materialized tasks only; successful-task ready_at buckets remain unavailable.", "The historical host override environment and physical mount chain remain outside this log correlation.", "This authorized historical log supplement does not block separately authorized stage 1 work."],
}
assert len(expired) == 2876
assert len(clip_expired) == 2866
assert len(coverage) == 1641
assert len(unattempted) == 1225
assert len(coverage) + len(unattempted) == len(clip_expired)
write("correlated_events.json", data)
write("summary.json", summary)
write("reason_samples.json", dict(examples))
print(json.dumps({"cohort": len(data["cohort_rows"]), "clip_required_expired": len(clip_expired), "edge_coverage": len(coverage), "never_attempted": len(unattempted), "same_source_hashes": True, "sample_groups": {key: len(value) for key, value in examples.items()}}))
