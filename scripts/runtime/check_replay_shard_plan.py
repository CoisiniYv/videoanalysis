#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("infra/config/replay-shards.midterm.json")


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def source_to_shard(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for shard in config.get("shards") or []:
        shard_id = str(shard.get("shard_id") or "")
        api_url = str(shard.get("replay_api_url") or "")
        sink_url = str(shard.get("replay_job_sink_url") or "")
        in_stream = str(shard.get("in_stream_endpoint") or "")
        if not shard_id or not api_url or not sink_url or not in_stream:
            raise ValueError(f"incomplete replay shard config: {shard!r}")
        for source_id in shard.get("source_ids") or []:
            source_text = str(source_id or "")
            if not source_text:
                raise ValueError(f"empty source_id in shard {shard_id}")
            if source_text in mapping:
                raise ValueError(
                    f"source_id {source_text} assigned to both "
                    f"{mapping[source_text]['shard_id']} and {shard_id}"
                )
            mapping[source_text] = {
                "shard_id": shard_id,
                "replay_api_url": api_url,
                "replay_job_sink_url": sink_url,
                "in_stream_endpoint": in_stream,
            }
    return mapping


def check_plan(config: dict[str, Any], *, expected_sources: int, max_imbalance: int) -> dict[str, Any]:
    mapping = source_to_shard(config)
    counts = Counter(item["shard_id"] for item in mapping.values())
    if len(mapping) != expected_sources:
        raise ValueError(f"expected {expected_sources} source_ids, got {len(mapping)}")
    if len(counts) < 2:
        raise ValueError("expected at least two replay shards")
    imbalance = max(counts.values()) - min(counts.values())
    if imbalance > max_imbalance:
        raise ValueError(f"shard imbalance {imbalance} exceeds {max_imbalance}: {dict(counts)}")
    missing_routes = [
        source_id
        for source_id, route in mapping.items()
        if not route["replay_api_url"] or not route["replay_job_sink_url"]
    ]
    if missing_routes:
        raise ValueError(f"missing clip-worker route for source_ids: {missing_routes}")
    return {
        "status": "PASS_REPLAY_SHARD_PLAN",
        "source_count": len(mapping),
        "shard_counts": dict(sorted(counts.items())),
        "max_imbalance": imbalance,
        "sample_routes": {
            source_id: mapping[source_id]
            for source_id in sorted(mapping)[:2] + sorted(mapping)[-2:]
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the Replay shard plan without requiring camera streams.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--expected-sources", type=int, default=60)
    parser.add_argument("--max-imbalance", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        summary = check_plan(
            load_config(args.config),
            expected_sources=args.expected_sources,
            max_imbalance=args.max_imbalance,
        )
    except Exception as exc:
        print(json.dumps({"status": "FAIL_REPLAY_SHARD_PLAN", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
