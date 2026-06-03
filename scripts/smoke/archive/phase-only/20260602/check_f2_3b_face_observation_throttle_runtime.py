#!/usr/bin/env python3
"""F2.3b — Redis face observation throttle runtime verification.

Reads recent entries from Redis Stream security.face_observations,
groups by reid_throttle_key, and verifies minimum timestamp delta
per key >= FACE_REID_MIN_INTERVAL_MS.

Usage:
    python scripts/smoke/check_f2_3b_face_observation_throttle_runtime.py \
        --stream security.face_observations \
        --count 1000 \
        --min-interval-ms 1000 \
        --redis-container c1-official-redis
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


def read_stream_entries(
    stream: str,
    count: int,
    redis_container: Optional[str] = None,
) -> List[Tuple[str, dict]]:
    """Read recent entries from Redis Stream via redis-cli XREVRANGE."""
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
        # Stream ID line
        stream_id = line
        i += 1
        fields = {}
        while i < len(lines):
            field_line = lines[i].strip()
            if not field_line:
                i += 1
                continue
            # Check if this looks like a stream ID (contains '-')
            # Stream IDs are like "1779853548947-0"
            if "-" in field_line and field_line.replace("-", "").isdigit():
                break
            # Field name
            field_name = field_line
            i += 1
            if i < len(lines):
                field_value = lines[i].strip()
                fields[field_name] = field_value
                i += 1
        if fields:
            entries.append((stream_id, fields))

    return entries


def parse_data_field(fields: dict) -> Optional[dict]:
    """Parse the 'data' JSON field from a Redis stream entry."""
    data_str = fields.get("data")
    if not data_str:
        return None
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        return None


def verify_throttle(
    entries: List[Tuple[str, dict]],
    min_interval_ms: int,
) -> Tuple[int, int, int, int, List[str]]:
    """Verify throttle compliance.

    Returns:
        (total_entries, unique_keys, min_delta_ms, violations_count, violation_details)
    """
    # Group by throttle_key
    key_timestamps: Dict[str, List[Tuple[int, str]]] = defaultdict(list)

    for stream_id, fields in entries:
        data = parse_data_field(fields)
        if data is None:
            continue

        throttle_key = data.get("reid_throttle_key", "")
        if not throttle_key:
            source_id = data.get("source_id", "?")
            track_id = data.get("track_id", "?")
            throttle_key = f"{source_id}:{track_id}"

        ts = data.get("timestamp_ms")
        if ts is None:
            continue

        key_timestamps[throttle_key].append((int(ts), stream_id))

    total_entries = sum(len(v) for v in key_timestamps.values())
    unique_keys = len(key_timestamps)

    global_min_delta = float("inf")
    violations = 0
    violation_details = []

    for key, ts_list in key_timestamps.items():
        # Sort by timestamp
        ts_list.sort(key=lambda x: x[0])
        for j in range(1, len(ts_list)):
            delta = ts_list[j][0] - ts_list[j - 1][0]
            if delta < global_min_delta:
                global_min_delta = delta
            if delta < min_interval_ms:
                violations += 1
                if len(violation_details) < 10:
                    violation_details.append(
                        f"VIOLATION key={key} "
                        f"ts1={ts_list[j-1][0]} ts2={ts_list[j][0]} "
                        f"delta={delta} "
                        f"ids={ts_list[j-1][1]},{ts_list[j][1]}"
                    )

    if global_min_delta == float("inf"):
        global_min_delta = 0

    return total_entries, unique_keys, int(global_min_delta), violations, violation_details


def main():
    parser = argparse.ArgumentParser(
        description="Verify Redis face observation throttle compliance",
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

    entries = read_stream_entries(
        args.stream, args.count, args.redis_container,
    )

    if not entries:
        print(f"entries=0 unique_keys=0 min_delta_ms=0 violations=0")
        print(f"FAIL: no entries found in stream {args.stream}")
        sys.exit(1)

    total, unique, min_delta, violations, details = verify_throttle(
        entries, args.min_interval_ms,
    )

    print(
        f"entries={total} unique_keys={unique} "
        f"min_delta_ms={min_delta} violations={violations}"
    )

    if violations > 0:
        for d in details:
            print(d)
        print(f"FAIL: throttle violations detected (min_interval={args.min_interval_ms}ms)")
        sys.exit(1)
    else:
        print(f"PASS: Redis face observation throttle verified")
        sys.exit(0)


if __name__ == "__main__":
    main()
