#!/usr/bin/env python3
"""Benchmark Qdrant face-gallery search with a synthetic temporary collection."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VECTOR_SIZE = 512


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://qdrant:6333"))
    parser.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    parser.add_argument("--prefer-grpc", dest="prefer_grpc", action="store_true", default=True)
    parser.add_argument("--no-prefer-grpc", dest="prefer_grpc", action="store_false")
    parser.add_argument("--collection", default="")
    parser.add_argument("--person-count", type=int, default=5000)
    parser.add_argument("--images-per-person", type=int, default=4)
    parser.add_argument("--queries", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--candidate-limit", type=int, default=20)
    parser.add_argument("--search-ef", type=int, default=128)
    parser.add_argument("--indexing-threshold-kb", type=int, default=1000)
    parser.add_argument("--full-scan-threshold-kb", type=int, default=1000)
    parser.add_argument("--default-segment-number", type=int, default=2)
    parser.add_argument("--hnsw-m", type=int, default=16)
    parser.add_argument("--hnsw-ef-construct", type=int, default=100)
    parser.add_argument("--target-sizes", default="2,20,200,all")
    parser.add_argument("--max-p95-ms", type=float, default=0.0)
    parser.add_argument("--max-p99-ms", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=20260629)
    parser.add_argument("--wait-index-s", type=float, default=60.0)
    parser.add_argument("--keep-collection", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = make_client(args)
    collection = args.collection or (
        "face_gallery_scale_bench_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    total_points = max(1, args.person_count) * max(1, args.images_per_person)
    started = time.perf_counter()
    try:
        recreate_collection(client, collection, args)
        upsert_points(client, collection, args, total_points)
        collection_status = wait_collection_ready(
            client,
            collection,
            expected_points=total_points,
            timeout_s=max(0.0, args.wait_index_s),
        )
        benchmark = run_benchmark(client, collection, args, total_points)
        result = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "collection": collection,
            "qdrant_url": args.qdrant_url,
            "prefer_grpc": args.prefer_grpc,
            "person_count": args.person_count,
            "images_per_person": args.images_per_person,
            "total_points": total_points,
            "queries_per_target_size": args.queries,
            "candidate_limit": args.candidate_limit,
            "search_ef": args.search_ef,
            "collection_tuning": {
                "indexing_threshold_kb": args.indexing_threshold_kb,
                "full_scan_threshold_kb": args.full_scan_threshold_kb,
                "default_segment_number": args.default_segment_number,
                "hnsw_m": args.hnsw_m,
                "hnsw_ef_construct": args.hnsw_ef_construct,
            },
            "target_sizes": parse_target_sizes(args.target_sizes),
            "collection_status": collection_status,
            "benchmark": benchmark,
            "acceptance": acceptance_summary(
                benchmark,
                max_p95_ms=args.max_p95_ms,
                max_p99_ms=args.max_p99_ms,
            ),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        print(text)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        if result["acceptance"]["status"] == "failed":
            return 2
    finally:
        if not args.keep_collection:
            try:
                client.delete_collection(collection_name=collection)
            except Exception:
                pass
    return 0


def make_client(args: argparse.Namespace) -> Any:
    try:
        from qdrant_client import QdrantClient
    except ModuleNotFoundError as exc:
        raise SystemExit("qdrant-client is not installed") from exc
    return QdrantClient(
        url=args.qdrant_url,
        api_key=args.qdrant_api_key or None,
        prefer_grpc=args.prefer_grpc,
        timeout=30.0,
    )


def qmodels() -> Any:
    from qdrant_client import models

    return models


def recreate_collection(client: Any, collection: str, args: argparse.Namespace) -> None:
    models = qmodels()
    try:
        if client.collection_exists(collection):
            client.delete_collection(collection_name=collection)
    except Exception:
        pass
    client.create_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(
            size=VECTOR_SIZE,
            distance=models.Distance.COSINE,
        ),
        hnsw_config=models.HnswConfigDiff(
            m=max(1, args.hnsw_m),
            ef_construct=max(1, args.hnsw_ef_construct),
            full_scan_threshold=max(1, args.full_scan_threshold_kb),
        ),
        optimizers_config=models.OptimizersConfigDiff(
            default_segment_number=max(0, args.default_segment_number),
            indexing_threshold=max(1, args.indexing_threshold_kb),
        ),
        on_disk_payload=True,
    )
    for field_name, schema in (
        ("person_id", models.PayloadSchemaType.INTEGER),
        ("external_person_id", models.PayloadSchemaType.KEYWORD),
        ("embedding_model", models.PayloadSchemaType.KEYWORD),
        ("is_active", models.PayloadSchemaType.BOOL),
        ("is_primary", models.PayloadSchemaType.BOOL),
    ):
        client.create_payload_index(
            collection_name=collection,
            field_name=field_name,
            field_schema=schema,
            wait=True,
        )


def upsert_points(
    client: Any,
    collection: str,
    args: argparse.Namespace,
    total_points: int,
) -> None:
    models = qmodels()
    batch_size = max(1, args.batch_size)
    images_per_person = max(1, args.images_per_person)
    points = []
    for point_id in range(1, total_points + 1):
        person_id = ((point_id - 1) // images_per_person) + 1
        image_index = (point_id - 1) % images_per_person
        points.append(
            models.PointStruct(
                id=point_id,
                vector=deterministic_vector(point_id),
                payload={
                    "gallery_embedding_id": point_id,
                    "person_id": person_id,
                    "person_name": f"person_{person_id:06d}",
                    "external_person_id": f"bench:{person_id:06d}",
                    "embedding_model": "adaface",
                    "is_active": True,
                    "is_primary": image_index == 0,
                    "quality": 0.8,
                },
            )
        )
        if len(points) >= batch_size:
            client.upsert(collection_name=collection, points=points, wait=True)
            points.clear()
    if points:
        client.upsert(collection_name=collection, points=points, wait=True)


def wait_collection_ready(
    client: Any,
    collection: str,
    *,
    expected_points: int,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    latest: Any = None
    while True:
        latest = client.get_collection(collection)
        points_count = int(getattr(latest, "points_count", 0) or 0)
        status = str(getattr(latest, "status", ""))
        if points_count >= expected_points and "green" in status.lower():
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return json.loads(latest.model_dump_json())


def run_benchmark(
    client: Any,
    collection: str,
    args: argparse.Namespace,
    total_points: int,
) -> dict[str, Any]:
    target_sizes = parse_target_sizes(args.target_sizes)
    rng = random.Random(args.seed)
    results: dict[str, Any] = {}
    for target_size in target_sizes:
        timings: list[float] = []
        result_counts: list[int] = []
        for _ in range(max(1, args.queries)):
            point_id = rng.randint(1, total_points)
            person_id = ((point_id - 1) // max(1, args.images_per_person)) + 1
            query_filter = build_filter(
                person_id=person_id,
                person_count=args.person_count,
                target_size=target_size,
            )
            query_vector = deterministic_vector(point_id)
            start = time.perf_counter()
            response = client.query_points(
                collection_name=collection,
                query=query_vector,
                limit=max(1, args.candidate_limit),
                query_filter=query_filter,
                with_payload=True,
                with_vectors=False,
                search_params=qmodels().SearchParams(hnsw_ef=max(1, args.search_ef)),
            )
            timings.append((time.perf_counter() - start) * 1000.0)
            result_counts.append(len(getattr(response, "points", response)))
        key = "all" if target_size is None else str(target_size)
        results[key] = {
            "target_count": target_size if target_size is not None else "all",
            "query_latency_ms": percentile_summary(timings),
            "result_count": percentile_summary([float(value) for value in result_counts]),
        }
    return results


def build_filter(
    *,
    person_id: int,
    person_count: int,
    target_size: int | None,
) -> Any:
    models = qmodels()
    must = [
        models.FieldCondition(key="is_active", match=models.MatchValue(value=True)),
        models.FieldCondition(key="embedding_model", match=models.MatchValue(value="adaface")),
    ]
    if target_size is not None:
        size = max(1, min(int(target_size), max(1, person_count)))
        start = max(1, person_id - (size // 2))
        end = min(person_count, start + size - 1)
        start = max(1, end - size + 1)
        must.append(
            models.FieldCondition(
                key="person_id",
                match=models.MatchAny(any=list(range(start, end + 1))),
            )
        )
    return models.Filter(must=must)


def deterministic_vector(point_id: int) -> list[float]:
    state = (point_id * 2654435761) & 0xFFFFFFFF
    values: list[float] = []
    total_sq = 0.0
    for _ in range(VECTOR_SIZE):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        value = (state / 2147483648.0) - 1.0
        values.append(value)
        total_sq += value * value
    norm = math.sqrt(total_sq) or 1.0
    return [value / norm for value in values]


def parse_target_sizes(value: str) -> list[int | None]:
    out: list[int | None] = []
    for item in value.split(","):
        token = item.strip().lower()
        if not token:
            continue
        if token in {"all", "*", "none"}:
            out.append(None)
        else:
            out.append(max(1, int(token)))
    return out or [None]


def percentile_summary(values: list[float]) -> dict[str, float | int | str]:
    if not values:
        return {"status": "not_enough_data"}
    ordered = sorted(values)
    return {
        "status": "measured",
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(percentile(ordered, 0.95), 3),
        "p99": round(percentile(ordered, 0.99), 3),
        "max": round(ordered[-1], 3),
    }


def acceptance_summary(
    benchmark: dict[str, Any],
    *,
    max_p95_ms: float,
    max_p99_ms: float,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for target_name, item in benchmark.items():
        latency = item.get("query_latency_ms") or {}
        if latency.get("status") != "measured":
            continue
        p95 = float(latency.get("p95") or 0.0)
        p99 = float(latency.get("p99") or 0.0)
        if max_p95_ms > 0 and p95 > max_p95_ms:
            failures.append(
                {
                    "target": target_name,
                    "metric": "p95",
                    "actual_ms": p95,
                    "limit_ms": max_p95_ms,
                }
            )
        if max_p99_ms > 0 and p99 > max_p99_ms:
            failures.append(
                {
                    "target": target_name,
                    "metric": "p99",
                    "actual_ms": p99,
                    "limit_ms": max_p99_ms,
                }
            )
    return {
        "status": "failed" if failures else "passed",
        "max_p95_ms": max_p95_ms or None,
        "max_p99_ms": max_p99_ms or None,
        "failures": failures,
    }


def percentile(ordered: list[float], fraction: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
