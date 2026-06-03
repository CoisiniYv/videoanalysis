#!/usr/bin/env python3
"""F2.4 — Redis face observation contract runtime verification.

Reads recent entries from Redis Stream security.face_observations and
validates the F2.4 contract: camera_id/source_id mapping, 3-part
throttle key, one-to-one per frame, timestamp normalization, schema
completeness, and no image bytes.

Usage:
    python scripts/smoke/check_f2_4_face_observation_contract_runtime.py \
        --stream security.face_observations \
        --count 1000 \
        --min-interval-ms 1000 \
        --redis-container c1-official-redis
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple


# ── Redis helpers ────────────────────────────────────────────────────────────

def read_stream_entries(
    stream: str,
    count: int,
    redis_container: Optional[str] = None,
) -> List[Tuple[str, dict, dict]]:
    """Read recent entries from Redis Stream via redis-cli XREVRANGE.

    Returns list of (stream_id, flat_fields, parsed_data_dict).
    """
    cmd = ["docker", "exec"]
    if redis_container:
        cmd.append(redis_container)
    cmd.extend(["redis-cli", "XREVRANGE", stream, "+", "-", "COUNT", str(count)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR: redis-cli failed: {result.stderr}", file=sys.stderr)
        return []

    entries = []
    lines = result.stdout.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        stream_id = line
        i += 1
        fields = {}
        while i < len(lines):
            field_line = lines[i].strip()
            if not field_line:
                i += 1
                continue
            if "-" in field_line and field_line.replace("-", "").isdigit():
                break
            field_name = field_line
            i += 1
            if i < len(lines):
                field_value = lines[i].strip()
                fields[field_name] = field_value
                i += 1
        if fields:
            data = {}
            data_str = fields.get("data")
            if data_str:
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    pass
            entries.append((stream_id, fields, data))
    return entries


# ── Contract checks ──────────────────────────────────────────────────────────

def _read_ts(data: dict) -> Optional[int]:
    ts = data.get("timestamp_ms")
    if ts is None:
        return None
    return int(ts)


def check_contract(
    entries: List[Tuple[str, dict, dict]],
) -> dict:
    """Run all F2.4 contract checks. Returns a result dict."""
    out: dict = {
        "entries": len(entries),
        "unique_keys": 0,
        "camera_config_resolved_true": 0,
        "camera_config_resolved_false": 0,
        "camera_config_resolved_missing": 0,
        "camera_id_equals_source_id": 0,
        "camera_id_differs_source_id": 0,
        "min_delta_ms": None,
        "throttle_violations": 0,
        "one_to_one_violations": 0,
        "schema_violations": 0,
        "schema_violation_details": [],  # type: List[str]
        "pass": True,
    }

    if not entries:
        out["pass"] = False
        print("FAIL: no entries in stream")
        return out

    # ── Per-entry schema checks ──
    flat_keys: Set[str] = set()
    for _, fields, data in entries:
        flat_keys.update(fields.keys())

        violations = []

        # Check 1: camera_id exists
        if not data.get("camera_id"):
            violations.append("missing camera_id")

        # Check 2: source_id exists
        if not data.get("source_id"):
            violations.append("missing source_id")

        # Check 3: camera_config_resolved
        payload = data.get("payload", {})
        if isinstance(payload, dict) and "camera_config_resolved" in payload:
            if payload["camera_config_resolved"]:
                out["camera_config_resolved_true"] += 1
            else:
                out["camera_config_resolved_false"] += 1
        else:
            out["camera_config_resolved_missing"] += 1

        # Check 4: camera_id vs source_id
        cam_id = data.get("camera_id", "")
        src_id = data.get("source_id", "")
        if cam_id and src_id:
            if cam_id == src_id:
                out["camera_id_equals_source_id"] += 1
            else:
                out["camera_id_differs_source_id"] += 1

        # Check 5: reid_throttle_key >= 3 parts
        throttle_key = data.get("reid_throttle_key", "")
        if throttle_key:
            parts = throttle_key.split(":")
            if len(parts) < 3:
                violations.append(
                    f"reid_throttle_key bad format: {throttle_key} ({len(parts)} parts)"
                )

        # Check 6: track_id exists and > 0
        track_id_raw = data.get("track_id")
        if track_id_raw is None:
            violations.append("missing track_id")
        else:
            try:
                track_id = int(track_id_raw)
                if track_id <= 0:
                    violations.append(f"track_id <= 0: {track_id}")
            except (ValueError, TypeError):
                violations.append(f"track_id not int: {track_id_raw}")

        # Check 7: timestamp_ms is int
        ts = data.get("timestamp_ms")
        if ts is None:
            violations.append("missing timestamp_ms")
        elif not isinstance(ts, int):
            violations.append(f"timestamp_ms not int: {type(ts).__name__}")

        # Check 10: source_observation_id format
        obs_id = data.get("source_observation_id", "")
        if obs_id:
            if not obs_id.startswith("face:"):
                violations.append(f"source_observation_id missing face: prefix: {obs_id}")

        # Check 11: embedding_dim == 512
        emb_dim = data.get("embedding_dim")
        if emb_dim is None:
            violations.append("missing embedding_dim")
        elif emb_dim != 512:
            violations.append(f"embedding_dim != 512: {emb_dim}")

        # Check 12: embedding length 512
        emb = data.get("embedding")
        if emb is None:
            violations.append("missing embedding")
        elif not isinstance(emb, list):
            violations.append(f"embedding not list: {type(emb).__name__}")
        elif len(emb) != 512:
            violations.append(f"embedding length != 512: {len(emb)}")

        # Check 13: landmarks length 10
        lms = data.get("landmarks")
        if lms is None:
            violations.append("missing landmarks")
        elif not isinstance(lms, list):
            violations.append(f"landmarks not list: {type(lms).__name__}")
        elif len(lms) != 10:
            violations.append(f"landmarks length != 10: {len(lms)}")

        # Check 14: face_bbox length 4
        fbbox = data.get("face_bbox")
        if fbbox is None:
            violations.append("missing face_bbox")
        elif not isinstance(fbbox, list):
            violations.append(f"face_bbox not list: {type(fbbox).__name__}")
        elif len(fbbox) != 4:
            violations.append(f"face_bbox length != 4: {len(fbbox)}")

        # Check 15: no image bytes
        json_str = json.dumps(data)
        for forbidden in ["image_bytes", "frame_bytes", "crop_bytes", "base64", "jpeg"]:
            if forbidden in json_str:
                violations.append(f"forbidden field in payload: {forbidden}")

        if violations:
            out["schema_violations"] += 1
            if len(out["schema_violation_details"]) < 20:
                oid = data.get("source_observation_id", "?")
                out["schema_violation_details"].append(f"{oid}: " + "; ".join(violations))

    # ── Check 3b: report camera_config_resolved status ──
    if out["camera_config_resolved_missing"] > 0:
        print(
            f"NOTE: camera_config_resolved missing from payload in "
            f"{out['camera_config_resolved_missing']}/{out['entries']} entries. "
            f"Need to redeploy with updated exporter code."
        )

    # ── Check 4b: camera_id vs source_id report ──
    if out["camera_id_differs_source_id"] > 0:
        print(
            f"  camera_id differs from source_id in "
            f"{out['camera_id_differs_source_id']}/{out['entries']} entries"
        )
    if out["camera_id_equals_source_id"] > 0:
        print(
            f"  camera_id == source_id in "
            f"{out['camera_id_equals_source_id']}/{out['entries']} entries "
            f"(same in test config, not proof of fallback)"
        )

    # ── Check 8/9: throttle key timestamps monotonic and delta >= threshold ──
    key_ts: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
    for stream_id, _, data in entries:
        throttle_key = data.get("reid_throttle_key", "")
        if not throttle_key:
            continue
        ts = _read_ts(data)
        if ts is None:
            continue
        obs_id = data.get("source_observation_id", stream_id)
        key_ts[throttle_key].append((ts, stream_id, obs_id))

    out["unique_keys"] = len(key_ts)
    global_min_delta = float("inf")
    throttle_violations = 0

    # Sort each key's entries by redis stream ID (reverse chronological from XREVRANGE)
    # Actually, XREVRANGE returns newest-first. Reverse for chronological order.
    for key, ts_list in key_ts.items():
        ts_list.reverse()  # chronological order
        for j in range(1, len(ts_list)):
            delta = ts_list[j][0] - ts_list[j - 1][0]
            if delta < global_min_delta:
                global_min_delta = delta
            if delta < 1000:  # min_interval_ms
                throttle_violations += 1

    out["min_delta_ms"] = int(global_min_delta) if global_min_delta != float("inf") else None
    out["throttle_violations"] = throttle_violations

    # ── Check 16: one-to-one — same frame/timestamp, same person track ──
    # Group by (timestamp_ms, track_id) — should be exactly 1 per group
    frame_track_groups: Dict[Tuple[int, str], List[str]] = defaultdict(list)
    for _, _, data in entries:
        ts = _read_ts(data)
        track_id = str(data.get("track_id", ""))
        if ts is None or not track_id:
            continue
        obs_id = data.get("source_observation_id", "?")
        frame_track_groups[(ts, track_id)].append(obs_id)

    one_to_one_violations = 0
    for (ts, tid), obs_ids in frame_track_groups.items():
        if len(obs_ids) > 1:
            one_to_one_violations += 1
            if one_to_one_violations <= 5:
                print(
                    f"  one-to-one VIOLATION: ts={ts} track={tid} "
                    f"count={len(obs_ids)} ids={obs_ids}"
                )

    out["one_to_one_violations"] = one_to_one_violations

    # ── Final verdict ──
    failures = []
    if out["entries"] == 0:
        failures.append("no entries")
    if out["schema_violations"] > 0:
        failures.append(f"schema_violations={out['schema_violations']}")
    if throttle_violations > 0:
        failures.append(f"throttle_violations={throttle_violations}")
    if one_to_one_violations > 0:
        failures.append(f"one_to_one_violations={one_to_one_violations}")

    out["pass"] = len(failures) == 0
    if failures:
        out["failure_reasons"] = failures

    return out


def _fmt(val) -> str:
    return str(val) if val is not None else "N/A"


def main():
    parser = argparse.ArgumentParser(
        description="Verify F2.4 face observation contract compliance",
    )
    parser.add_argument(
        "--stream", default="security.face_observations",
        help="Redis stream name",
    )
    parser.add_argument(
        "--count", type=int, default=1000,
        help="Number of recent entries to read",
    )
    parser.add_argument(
        "--min-interval-ms", type=int, default=1000,
        help="Minimum allowed interval between same-key observations (ms)",
    )
    parser.add_argument(
        "--redis-container", default="c1-official-redis",
        help="Docker container name for Redis",
    )
    args = parser.parse_args()

    entries = read_stream_entries(args.stream, args.count, args.redis_container)

    if not entries:
        print("entries=0 unique_keys=0")
        print("FAIL: no entries found in stream")
        sys.exit(1)

    result = check_contract(entries)

    # ── Output ──
    print(f"entries={result['entries']}")
    print(f"unique_keys={result['unique_keys']}")
    print(f"camera_config_resolved_true={result['camera_config_resolved_true']}")
    print(f"camera_config_resolved_false={result['camera_config_resolved_false']}")
    print(f"camera_config_resolved_missing={result['camera_config_resolved_missing']}")
    print(f"min_delta_ms={_fmt(result['min_delta_ms'])}")
    print(f"throttle_violations={result['throttle_violations']}")
    print(f"one_to_one_violations={result['one_to_one_violations']}")
    print(f"schema_violations={result['schema_violations']}")

    if result["schema_violation_details"]:
        for d in result["schema_violation_details"][:10]:
            print(f"  schema: {d}")

    if result["pass"]:
        print("PASS")
        sys.exit(0)
    else:
        reasons = result.get("failure_reasons", ["unknown"])
        print(f"FAIL: {'; '.join(reasons)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
