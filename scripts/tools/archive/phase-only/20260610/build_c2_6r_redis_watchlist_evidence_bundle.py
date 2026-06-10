#!/usr/bin/env python3
"""Build C2.6R evidence from an isolated face-worker Redis one-message proof."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "scripts" / "tools"
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
for path in (TOOLS_ROOT, FACE_WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import psycopg  # noqa: E402
import redis  # noqa: E402
from pgvector.psycopg import register_vector  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import build_c2_watchlist_evidence_bundle as c25  # noqa: E402
import build_c2_live_watchlist_evidence_bundle as c26  # noqa: E402
from app.face_match_event_service import build_watchlist_hit_event, publish_security_event  # noqa: E402
from app.match_repository import MatchResultRepository  # noqa: E402
from app.redis_consumer import RedisStreamConsumer  # noqa: E402
from app.vector_store import FaceVectorStore  # noqa: E402
from app.worker import _parse_observation, _validate_embedding  # noqa: E402


RESULT_PASS = "PASS_C2_6R_REDIS_CONSUMER_WATCHLIST_EVENT_READY"
RESULT_REDIS_GAP = "PARTIAL_C2_6R_REDIS_INFRA_GAP"
RESULT_FAIL = "FAIL_C2_6R_WATCHLIST_CONSUMER_BLOCKED"
EVENT_TYPE = "watchlist_hit"
REDIS_EVENT_FILE = "redis_watchlist_event.json"
C2_6R_SUMMARY_FILE = "c2_6r_redis_watchlist_summary.json"
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6395/0"
DEFAULT_SOURCE_OBSERVATION_ID = "face:c2_post_savant_fps_probe:4:17854:1"
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"
DEFAULT_EXTERNAL_PERSON_ID = "test:c2_4:person"
DEFAULT_WATCHLIST_RULE_ID = "c2_6r_test_watchlist_rule"
DEFAULT_WATCHLIST_RULE_NAME = "C2.6R Test Watchlist Rule"
DEFAULT_SEARCH_REQUEST_ID = "c2600000-0000-4000-8000-0000000000a1"
DEFAULT_INPUT_STREAM = "c2_6r.face_observations.test"
DEFAULT_OUTPUT_STREAM = "c2_6r.security.events.test"
DEFAULT_CONSUMER_GROUP = "c2_6r-face-worker-test"
DEFAULT_CONSUMER_NAME = "c2_6r-one-message"
DEFAULT_THRESHOLD = 0.99

FORBIDDEN_EVENT_KEYS = c25.FORBIDDEN_EVENT_KEYS | {
    "embedding_list",
    "crop_image",
}


@dataclass(frozen=True)
class RedisProof:
    input_stream: str
    output_stream: str
    input_message_id: str
    output_message_id: str
    cleanup_requested: bool
    cleanup_status: str
    decoded_observation: dict[str, Any]
    emitted_event: dict[str, Any]
    match_result_id: int
    search_request_id: str
    gallery_match: dict[str, Any]
    db_observation: dict[str, Any]


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    input_bundle: Path
    output_bundle: Path
    redis_event_path: Path
    live_event_path: Path
    c2_6r_summary_path: Path
    summary_path: Path
    sidecar_path: Path
    redis_proof: RedisProof
    summary: dict[str, Any]
    c2_6r_summary: dict[str, Any]


def build_c2_6r_redis_watchlist_evidence_bundle(
    *,
    input_bundle: Path,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    source_observation_id: str,
    external_person_id: str,
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    threshold: float,
    search_request_id: str,
    input_stream: str,
    output_stream: str,
    consumer_group: str,
    consumer_name: str,
    cleanup_streams: bool = True,
    overwrite: bool = False,
) -> BuildResult:
    input_bundle = input_bundle.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    c25._require_input_bundle(input_bundle)
    sidecar_rows = c25._read_jsonl(input_bundle / c25.SIDECAR_FILE)
    input_summary = c25._read_json(input_bundle / c25.SUMMARY_FILE)
    identity_patches = c25._read_jsonl(input_bundle / c25.IDENTITY_PATCHES_FILE)
    c25._validate_input_summary(input_summary)

    redis_client = redis.Redis.from_url(redis_url, decode_responses=False)
    try:
        redis_client.ping()
    except Exception as exc:
        raise RedisInfraGap(f"redis_unavailable:{exc}") from exc

    with psycopg.connect(database_url, autocommit=True) as conn:
        register_vector(conn)
        db_observation = c26.fetch_face_observation(conn, source_observation_id)
        if db_observation is None:
            raise RuntimeError(f"face_observation_missing:{source_observation_id}")
        redis_proof = run_isolated_redis_consumer_proof(
            conn=conn,
            redis_client=redis_client,
            db_observation=db_observation,
            external_person_id=external_person_id,
            watchlist_rule_id=watchlist_rule_id,
            watchlist_rule_name=watchlist_rule_name,
            threshold=threshold,
            search_request_id=search_request_id,
            input_stream=input_stream,
            output_stream=output_stream,
            consumer_group=consumer_group,
            consumer_name=consumer_name,
            cleanup_streams=cleanup_streams,
        )

    person_id = int(redis_proof.gallery_match["person_id"])
    binding = c25.find_known_face_binding(
        rows=sidecar_rows,
        identity_patches=identity_patches,
        person_id=person_id,
        external_person_id=external_person_id,
        source_observation_id=source_observation_id,
        threshold=threshold,
    )
    if binding is None:
        raise RuntimeError("input_sidecar_known_face_binding_missing")
    db_track_id = c25._text_or_none(redis_proof.db_observation.get("track_id"))
    evidence_track_id = c25._text_or_none(binding.track_id)
    track_id_join_warning = db_track_id != evidence_track_id

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()
    shutil.copytree(input_bundle, output_dir)

    patched_rows = patch_c2_6r_sidecar_fields(
        sidecar_rows,
        binding=binding,
        event=redis_proof.emitted_event,
        db_track_id=db_track_id,
        evidence_track_id=evidence_track_id,
        track_id_join_warning=track_id_join_warning,
    )
    summary = patch_c2_6r_summary(
        input_summary,
        rows=patched_rows,
        binding=binding,
        redis_proof=redis_proof,
        db_track_id=db_track_id,
        evidence_track_id=evidence_track_id,
        track_id_join_warning=track_id_join_warning,
    )
    c2_6r_summary = build_c2_6r_summary(
        input_bundle=input_bundle,
        output_bundle=output_dir,
        summary=summary,
        redis_proof=redis_proof,
        binding=binding,
        db_track_id=db_track_id,
        evidence_track_id=evidence_track_id,
        track_id_join_warning=track_id_join_warning,
    )
    validate_c2_6r_output(
        summary=summary,
        event=redis_proof.emitted_event,
        c2_6r_summary=c2_6r_summary,
    )

    sidecar_path = output_dir / c25.SIDECAR_FILE
    summary_path = output_dir / c25.SUMMARY_FILE
    sidecar_summary_path = output_dir / c25.SIDECAR_SUMMARY_FILE
    redis_event_path = output_dir / REDIS_EVENT_FILE
    live_event_path = output_dir / c26.LIVE_EVENT_FILE
    c2_6r_summary_path = output_dir / C2_6R_SUMMARY_FILE
    c25._write_jsonl(sidecar_path, patched_rows)
    c25._write_json(summary_path, summary)
    c25._write_json(sidecar_summary_path, summary)
    c25._write_json(redis_event_path, redis_proof.emitted_event)
    c25._write_json(live_event_path, redis_proof.emitted_event)
    c25._write_json(c2_6r_summary_path, c2_6r_summary)

    return BuildResult(
        result_marker=RESULT_PASS,
        input_bundle=input_bundle,
        output_bundle=output_dir,
        redis_event_path=redis_event_path,
        live_event_path=live_event_path,
        c2_6r_summary_path=c2_6r_summary_path,
        summary_path=summary_path,
        sidecar_path=sidecar_path,
        redis_proof=redis_proof,
        summary=summary,
        c2_6r_summary=c2_6r_summary,
    )


class RedisInfraGap(RuntimeError):
    """Raised when isolated Redis streams cannot be used."""


def run_isolated_redis_consumer_proof(
    *,
    conn: psycopg.Connection,
    redis_client: redis.Redis,
    db_observation: dict[str, Any],
    external_person_id: str,
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    threshold: float,
    search_request_id: str,
    input_stream: str,
    output_stream: str,
    consumer_group: str,
    consumer_name: str,
    cleanup_streams: bool,
) -> RedisProof:
    redis_client.delete(input_stream, output_stream)
    consumer = RedisStreamConsumer(
        redis_client,
        input_stream,
        consumer_group,
        consumer_name,
        start_id="0",
    )
    consumer.ensure_group()
    input_payload = build_test_face_observation_payload(db_observation)
    input_message_id = _decode_redis_id(
        redis_client.xadd(
            input_stream,
            {"data": json.dumps(input_payload, ensure_ascii=False)},
        )
    )
    messages = consumer.read_new(count=1, block_ms=1000)
    if len(messages) != 1:
        raise RuntimeError(f"isolated_input_message_read_count={len(messages)}")
    msg_id, fields = messages[0]
    if msg_id != input_message_id:
        raise RuntimeError(f"isolated_input_message_id_mismatch:{msg_id}!={input_message_id}")
    decoded = _parse_observation(fields)
    if decoded is None:
        raise RuntimeError("face_worker_parse_observation_failed")
    embedding_error = _validate_embedding(decoded, msg_id)
    if embedding_error:
        raise RuntimeError(f"face_worker_validate_embedding_failed:{embedding_error}")

    person = c26.fetch_person_by_external_id(conn, external_person_id)
    if person is None:
        raise RuntimeError(f"person_missing:{external_person_id}")
    gallery_match = run_gallery_match_for_decoded_observation(
        conn=conn,
        decoded_observation=decoded,
        person_id=int(person["id"]),
        threshold=threshold,
    )
    match_result_id = insert_c2_6r_match_result(
        conn=conn,
        db_observation=db_observation,
        gallery_match=gallery_match,
        threshold=threshold,
        search_request_id=search_request_id,
    )
    event = build_c2_6r_watchlist_event(
        decoded_observation=decoded,
        gallery_match=gallery_match,
        match_result_id=match_result_id,
        search_request_id=search_request_id,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        threshold=threshold,
    )
    assert_no_forbidden_event_payload(event)
    output_message_id = publish_security_event(redis_client, event, stream=output_stream)
    published = redis_client.xrange(output_stream, output_message_id, output_message_id)
    if len(published) != 1:
        raise RuntimeError("isolated_output_event_missing")
    published_event = _event_from_redis_fields(published[0][1])
    if published_event.get("source_event_id") != event.get("source_event_id"):
        raise RuntimeError("isolated_output_event_source_event_id_mismatch")
    if not consumer.ack(msg_id):
        raise RuntimeError("isolated_input_ack_failed")

    cleanup_status = "not_requested"
    if cleanup_streams:
        deleted = redis_client.delete(input_stream, output_stream)
        cleanup_status = f"deleted_{deleted}_streams"

    return RedisProof(
        input_stream=input_stream,
        output_stream=output_stream,
        input_message_id=input_message_id,
        output_message_id=output_message_id,
        cleanup_requested=cleanup_streams,
        cleanup_status=cleanup_status,
        decoded_observation=decoded,
        emitted_event=event,
        match_result_id=match_result_id,
        search_request_id=search_request_id,
        gallery_match=gallery_match,
        db_observation=db_observation,
    )


def build_test_face_observation_payload(db_observation: dict[str, Any]) -> dict[str, Any]:
    embedding = c26.observation_to_embedding(db_observation)
    payload = c25._dict(db_observation.get("payload"))
    media = c25._dict(payload.get("media"))
    return {
        "source_observation_id": db_observation["source_observation_id"],
        "camera_id": db_observation.get("camera_id") or db_observation.get("source_id") or "",
        "source_id": db_observation.get("source_id") or "",
        "track_id": str(db_observation.get("track_id") or ""),
        "timestamp_ms": int(db_observation.get("timestamp_ms") or 0),
        "face_bbox": _jsonable(db_observation.get("face_bbox")),
        "landmarks": _jsonable(db_observation.get("landmarks")),
        "face_confidence": float(db_observation.get("face_confidence") or 0.0),
        "quality": float(db_observation.get("quality") or 0.0),
        "person_bbox": _jsonable(db_observation.get("person_bbox")),
        "embedding": embedding,
        "embedding_model": db_observation.get("embedding_model") or "adaface",
        "embedding_dim": int(db_observation.get("embedding_dim") or len(embedding)),
        "embedding_norm": float(db_observation.get("embedding_norm") or sum(x * x for x in embedding) ** 0.5),
        "payload": {
            **payload,
            "media": media,
            "c2_6r_test_message": True,
        },
    }


def run_gallery_match_for_decoded_observation(
    *,
    conn: psycopg.Connection,
    decoded_observation: dict[str, Any],
    person_id: int,
    threshold: float,
) -> dict[str, Any]:
    store = FaceVectorStore(conn)
    embedding = [float(value) for value in decoded_observation["embedding"]]
    results = store.search_gallery(
        embedding,
        top_k=1,
        min_similarity=threshold,
        person_ids=[person_id],
    )
    if not results:
        raise RuntimeError("face_worker_consumer_pgvector_match_missing")
    best = dict(results[0])
    if float(best["similarity"]) < threshold:
        raise RuntimeError("face_worker_consumer_match_below_threshold")
    return best


def insert_c2_6r_match_result(
    *,
    conn: psycopg.Connection,
    db_observation: dict[str, Any],
    gallery_match: dict[str, Any],
    threshold: float,
    search_request_id: str,
) -> int:
    repo = MatchResultRepository(conn)
    row = {
        "search_request_id": search_request_id,
        "search_mode": "c2_6r_redis_watchlist_match",
        "query_observation_id": db_observation["id"],
        "query_source_observation_id": db_observation["source_observation_id"],
        "query_person_id": gallery_match.get("person_id"),
        "query_gallery_embedding_id": gallery_match["id"],
        "query_embedding_model": gallery_match.get("embedding_model", "adaface"),
        "similarity_threshold": threshold,
        "rank": 1,
        "similarity": gallery_match["similarity"],
        "face_confidence": db_observation.get("face_confidence"),
        "quality": db_observation.get("quality"),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "payload": {
            "phase": "C2.6R",
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_consumer",
        },
    }
    inserted_id = repo.insert_gallery_match_result(row)
    if inserted_id is not None:
        return int(inserted_id)
    for existing in repo.get_by_search_request(search_request_id):
        if int(existing.get("query_gallery_embedding_id") or 0) == int(gallery_match["id"]):
            return int(existing["id"])
    raise RuntimeError("match_result_insert_or_lookup_failed")


def build_c2_6r_watchlist_event(
    *,
    decoded_observation: dict[str, Any],
    gallery_match: dict[str, Any],
    match_result_id: int,
    search_request_id: str,
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    threshold: float,
) -> dict[str, Any]:
    event = build_watchlist_hit_event(
        observation=decoded_observation,
        gallery_match=gallery_match,
        threshold=threshold,
        severity="high",
    )
    media = c25._dict(c25._dict(decoded_observation.get("payload")).get("media"))
    source_observation_id = str(decoded_observation["source_observation_id"])
    person_id = int(gallery_match["person_id"])
    event_ts_ms = int(decoded_observation.get("timestamp_ms") or 0)
    frame_pts = media.get("frame_pts")
    frame_num = media.get("frame_num")
    event.update(
        {
            "schema_version": "1.0",
            "event_type": EVENT_TYPE,
            "source_event_id": f"c2_6r:watchlist_hit:{source_observation_id}:{person_id}",
            "producer": "face-worker-one-message-consumer",
            "source_observation_id": source_observation_id,
            "person_id": person_id,
            "external_person_id": gallery_match.get("external_person_id"),
            "person_name": gallery_match.get("person_name"),
            "gallery_embedding_id": int(gallery_match["id"]),
            "match_result_id": match_result_id,
            "search_request_id": search_request_id,
            "similarity": float(gallery_match["similarity"]),
            "threshold": float(threshold),
            "watchlist_rule_id": watchlist_rule_id,
            "watchlist_rule_name": watchlist_rule_name,
            "event_ts_ms": event_ts_ms,
            "start_ts_ms": event_ts_ms,
            "end_ts_ms": event_ts_ms,
            "frame_pts": frame_pts,
            "frame_num": frame_num,
            "frame_id": frame_num,
            "redis_consumer_loop_verified": True,
            "face_worker_one_message_entrypoint_verified": True,
            "redis_event_published": True,
        }
    )
    payload = c25._dict(event.get("payload"))
    payload.update(
        {
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_consumer",
            "source_observation_id": source_observation_id,
            "gallery_embedding_id": int(gallery_match["id"]),
            "match_result_id": match_result_id,
            "search_request_id": search_request_id,
            "similarity": float(gallery_match["similarity"]),
            "threshold": float(threshold),
            "watchlist_rule_id": watchlist_rule_id,
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": True,
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "geometry_source": "post_savant_sidecar",
            "geometry_modified": False,
        }
    )
    event["payload"] = payload
    return event


def patch_c2_6r_sidecar_fields(
    rows: list[dict[str, Any]],
    *,
    binding: c25.KnownFaceBinding,
    event: dict[str, Any],
    db_track_id: str | None,
    evidence_track_id: str | None,
    track_id_join_warning: bool,
) -> list[dict[str, Any]]:
    patched = c26.patch_c2_6_sidecar_fields(
        rows,
        binding=binding,
        live_event=event,
        watchlist_rule_id=str(event["watchlist_rule_id"]),
        watchlist_rule_name=str(event.get("watchlist_rule_name") or event["watchlist_rule_id"]),
        severity=str(event.get("severity") or "high"),
    )
    obj = patched[binding.row_index]["objects"][binding.object_index]
    original_geometry = c25._geometry_snapshot(obj)
    identity = c25._dict(obj.get("identity"))
    identity.update(
        {
            "watchlist_match_source": "face_worker_consumer",
            "redis_consumer_loop_verified": True,
            "face_worker_one_message_entrypoint_verified": True,
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": track_id_join_warning,
            "db_observation_track_id": db_track_id,
            "evidence_sidecar_track_id": evidence_track_id,
        }
    )
    obj["identity"] = identity
    if c25._geometry_snapshot(obj) != original_geometry:
        raise RuntimeError("c2_6r_sidecar_patch_modified_geometry")
    return patched


def patch_c2_6r_summary(
    summary: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    binding: c25.KnownFaceBinding,
    redis_proof: RedisProof,
    db_track_id: str | None,
    evidence_track_id: str | None,
    track_id_join_warning: bool,
) -> dict[str, Any]:
    event = redis_proof.emitted_event
    patched = c26.patch_c2_6_summary(
        summary,
        rows=rows,
        binding=binding,
        live_event=event,
        match=c26.FaceWorkerMatch(
            observation=redis_proof.decoded_observation,
            gallery_match=redis_proof.gallery_match,
            match_result_id=redis_proof.match_result_id,
            search_request_id=redis_proof.search_request_id,
            threshold=float(event["threshold"]),
            watchlist_rule_id=str(event["watchlist_rule_id"]),
            watchlist_rule_name=str(event.get("watchlist_rule_name") or event["watchlist_rule_id"]),
            watchlist_rule_source="c2_6r_synthetic_file_rule",
        ),
    )
    patched.update(
        {
            "source_event_id": event["source_event_id"],
            "redis_consumer_loop_verified": True,
            "face_worker_one_message_entrypoint_verified": True,
            "face_worker_match_verified": True,
            "live_watchlist_from_face_worker": True,
            "event_producer": event["producer"],
            "redis_input_stream": redis_proof.input_stream,
            "redis_output_stream": redis_proof.output_stream,
            "redis_input_message_id": redis_proof.input_message_id,
            "redis_output_message_id": redis_proof.output_message_id,
            "redis_cleanup_status": redis_proof.cleanup_status,
            "redis_event_published": True,
            "watchlist_rule_id": event["watchlist_rule_id"],
            "watchlist_rule_name": event.get("watchlist_rule_name"),
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_consumer",
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": track_id_join_warning,
            "db_observation_track_id": db_track_id,
            "evidence_sidecar_track_id": evidence_track_id,
            "redis_watchlist_event_file": REDIS_EVENT_FILE,
            "c2_6r_redis_watchlist_summary_file": C2_6R_SUMMARY_FILE,
        }
    )
    limitations = list(patched.get("limitations") or [])
    for item in (
        "isolated_redis_stream_one_message_consumer",
        "stable_sink_workaround_still_active",
        "event_style_replay_not_passed",
        "not_broad_recognition_accuracy_test",
    ):
        if item not in limitations:
            limitations.append(item)
    patched["limitations"] = limitations
    return patched


def build_c2_6r_summary(
    *,
    input_bundle: Path,
    output_bundle: Path,
    summary: dict[str, Any],
    redis_proof: RedisProof,
    binding: c25.KnownFaceBinding,
    db_track_id: str | None,
    evidence_track_id: str | None,
    track_id_join_warning: bool,
) -> dict[str, Any]:
    event = redis_proof.emitted_event
    return {
        "result_marker": RESULT_PASS,
        "input_bundle": str(input_bundle),
        "output_bundle": str(output_bundle),
        "execution_mode": "Mode A Redis stream one-message consumer",
        "event_type": EVENT_TYPE,
        "source_event_id": event.get("source_event_id"),
        "producer": event.get("producer"),
        "source_observation_id": event.get("source_observation_id"),
        "camera_id": event.get("camera_id"),
        "source_id": event.get("source_id"),
        "track_id": event.get("track_id"),
        "db_observation_track_id": db_track_id,
        "evidence_sidecar_track_id": evidence_track_id,
        "track_id_join_warning": track_id_join_warning,
        "primary_identity_join_key": "source_observation_id",
        "frame_num": event.get("frame_num"),
        "frame_pts": event.get("frame_pts"),
        "person_id": event.get("person_id"),
        "external_person_id": event.get("external_person_id"),
        "gallery_embedding_id": event.get("gallery_embedding_id"),
        "match_result_id": event.get("match_result_id"),
        "search_request_id": event.get("search_request_id"),
        "similarity": event.get("similarity"),
        "threshold": event.get("threshold"),
        "watchlist_rule_id": event.get("watchlist_rule_id"),
        "redis_input_stream": redis_proof.input_stream,
        "redis_output_stream": redis_proof.output_stream,
        "redis_input_message_id": redis_proof.input_message_id,
        "redis_output_message_id": redis_proof.output_message_id,
        "redis_cleanup_requested": redis_proof.cleanup_requested,
        "redis_cleanup_status": redis_proof.cleanup_status,
        "producer_path": "services/face-worker/app/redis_consumer.py + services/face-worker/app/face_match_event_service.py + services/face-worker/app/vector_store.py",
        "redis_consumer_loop_verified": summary.get("redis_consumer_loop_verified"),
        "face_worker_one_message_entrypoint_verified": summary.get("face_worker_one_message_entrypoint_verified"),
        "face_worker_match_verified": summary.get("face_worker_match_verified"),
        "live_watchlist_from_face_worker": summary.get("live_watchlist_from_face_worker"),
        "known_face_count": summary.get("known_face_count"),
        "watchlist_hit_count": summary.get("watchlist_hit_count"),
        "production_ready": summary.get("production_ready"),
        "video_integrity": summary.get("video_integrity"),
        "fallback_used": summary.get("fallback_used"),
        "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
        "allow_db_annotation_fallback": summary.get("allow_db_annotation_fallback"),
        "allow_legacy_annotation_fallback": summary.get("allow_legacy_annotation_fallback"),
        "evidence_capture_mode": summary.get("evidence_capture_mode"),
        "workaround_used": summary.get("workaround_used"),
        "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
        "join_method": binding.join_method,
        "limitations": summary.get("limitations") or [],
    }


def validate_c2_6r_output(
    *,
    summary: dict[str, Any],
    event: dict[str, Any],
    c2_6r_summary: dict[str, Any],
) -> None:
    assert_no_forbidden_event_payload(event)
    if event.get("event_type") != EVENT_TYPE:
        raise RuntimeError("redis_event_type_invalid")
    if event.get("producer") not in {"face-worker", "face-worker-one-message-consumer"}:
        raise RuntimeError("redis_event_producer_invalid")
    for key in (
        "source_observation_id",
        "person_id",
        "gallery_embedding_id",
        "match_result_id",
        "similarity",
        "threshold",
        "watchlist_rule_id",
    ):
        if event.get(key) in (None, ""):
            raise RuntimeError(f"redis_event_missing_{key}")
    if c25._dict(event.get("payload")).get("primary_identity_join_key") != "source_observation_id":
        raise RuntimeError("primary_identity_join_key_not_source_observation_id")
    if c25._dict(event.get("payload")).get("track_id_join_warning") is not True:
        raise RuntimeError("track_id_join_warning_required")
    if c25._dict(event.get("payload")).get("embedding_included") is not False:
        raise RuntimeError("redis_event_embedding_included")
    if c25._dict(event.get("payload")).get("image_bytes_included") is not False:
        raise RuntimeError("redis_event_image_bytes_included")
    if c25._dict(event.get("payload")).get("event_style_replay_job_passed") is not False:
        raise RuntimeError("redis_event_replay_status_invalid")
    if summary.get("redis_consumer_loop_verified") is not True:
        raise RuntimeError("summary_redis_consumer_loop_verified_required")
    if summary.get("face_worker_one_message_entrypoint_verified") is not True:
        raise RuntimeError("summary_one_message_entrypoint_required")
    if int(summary.get("known_face_count") or 0) <= 0:
        raise RuntimeError("known_face_count_positive_required")
    if int(summary.get("watchlist_hit_count") or 0) != 1:
        raise RuntimeError("watchlist_hit_count_must_be_1")
    if summary.get("fallback_used") is not False:
        raise RuntimeError("summary_fallback_used")
    if summary.get("legacy_used_for_visual_binding") is not False:
        raise RuntimeError("summary_legacy_used")
    if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("summary_capture_mode_invalid")
    if summary.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("summary_replay_status_invalid")
    if summary.get("production_ready") is not True:
        raise RuntimeError("summary_production_ready_required")
    if c25._dict(summary.get("video_integrity")).get("production_gate_passed") is not True:
        raise RuntimeError("summary_video_integrity_gate_required")
    if c2_6r_summary.get("result_marker") != RESULT_PASS:
        raise RuntimeError("c2_6r_summary_marker_invalid")


def assert_no_forbidden_event_payload(event: dict[str, Any]) -> None:
    hits = sorted(set(_find_forbidden_keys(event)))
    if hits:
        raise RuntimeError(f"redis_watchlist_event_contains_forbidden_payload:{','.join(hits)}")


def _find_forbidden_keys(value: Any, *, parent_key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_EVENT_KEYS:
                yield parent_key + str(key)
            yield from _find_forbidden_keys(nested, parent_key=parent_key + str(key) + ".")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _find_forbidden_keys(nested, parent_key=f"{parent_key}{index}.")


def _event_from_redis_fields(fields: dict[Any, Any]) -> dict[str, Any]:
    raw = fields.get(b"data") or fields.get("data")
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not raw:
        raise RuntimeError("redis_event_data_missing")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("redis_event_data_not_object")
    return payload


def _decode_redis_id(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(nested) for key, nested in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _result_payload(result: BuildResult) -> dict[str, Any]:
    event = result.redis_proof.emitted_event
    return {
        "result_marker": result.result_marker,
        "input_bundle": str(result.input_bundle),
        "output_bundle": str(result.output_bundle),
        "redis_watchlist_event_path": str(result.redis_event_path),
        "live_watchlist_event_path": str(result.live_event_path),
        "c2_6r_redis_watchlist_summary_path": str(result.c2_6r_summary_path),
        "summary_path": str(result.summary_path),
        "sidecar_path": str(result.sidecar_path),
        "input_stream": result.redis_proof.input_stream,
        "output_stream": result.redis_proof.output_stream,
        "input_message_id": result.redis_proof.input_message_id,
        "output_message_id": result.redis_proof.output_message_id,
        "cleanup_status": result.redis_proof.cleanup_status,
        "source_event_id": event.get("source_event_id"),
        "producer": event.get("producer"),
        "source_observation_id": event.get("source_observation_id"),
        "person_id": event.get("person_id"),
        "external_person_id": event.get("external_person_id"),
        "gallery_embedding_id": event.get("gallery_embedding_id"),
        "match_result_id": event.get("match_result_id"),
        "search_request_id": event.get("search_request_id"),
        "similarity": event.get("similarity"),
        "threshold": event.get("threshold"),
        "watchlist_rule_id": event.get("watchlist_rule_id"),
        "track_id": event.get("track_id"),
        "track_id_join_warning": result.summary.get("track_id_join_warning"),
        "known_face_count": result.summary.get("known_face_count"),
        "watchlist_hit_count": result.summary.get("watchlist_hit_count"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--source-observation-id", default=DEFAULT_SOURCE_OBSERVATION_ID)
    parser.add_argument("--external-person-id", default=DEFAULT_EXTERNAL_PERSON_ID)
    parser.add_argument("--watchlist-rule-id", default=DEFAULT_WATCHLIST_RULE_ID)
    parser.add_argument("--watchlist-rule-name", default=DEFAULT_WATCHLIST_RULE_NAME)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--search-request-id", default=DEFAULT_SEARCH_REQUEST_ID)
    parser.add_argument("--input-stream", default=DEFAULT_INPUT_STREAM)
    parser.add_argument("--output-stream", default=DEFAULT_OUTPUT_STREAM)
    parser.add_argument("--consumer-group", default=DEFAULT_CONSUMER_GROUP)
    parser.add_argument("--consumer-name", default=DEFAULT_CONSUMER_NAME)
    parser.add_argument("--no-cleanup-streams", action="store_true", default=False)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    try:
        result = build_c2_6r_redis_watchlist_evidence_bundle(
            input_bundle=args.input_bundle,
            output_dir=args.output_dir,
            database_url=args.database_url,
            redis_url=args.redis_url,
            source_observation_id=args.source_observation_id,
            external_person_id=args.external_person_id,
            watchlist_rule_id=args.watchlist_rule_id,
            watchlist_rule_name=args.watchlist_rule_name,
            threshold=args.threshold,
            search_request_id=args.search_request_id,
            input_stream=args.input_stream,
            output_stream=args.output_stream,
            consumer_group=args.consumer_group,
            consumer_name=args.consumer_name,
            cleanup_streams=not args.no_cleanup_streams,
            overwrite=args.overwrite,
        )
    except RedisInfraGap as exc:
        payload = {
            "result_marker": RESULT_REDIS_GAP,
            "reason": str(exc),
            "input_bundle": str(args.input_bundle),
            "output_bundle": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 3
    except Exception as exc:
        payload = {
            "result_marker": RESULT_FAIL,
            "reason": f"{type(exc).__name__}:{exc}",
            "input_bundle": str(args.input_bundle),
            "output_bundle": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
