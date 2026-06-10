#!/usr/bin/env python3
"""Build a C2.4 identity-patched evidence bundle.

This tool patches identity fields only.  Geometry remains the post-Savant
sidecar geometry from the input evidence bundle.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
if str(FACE_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(FACE_WORKER_ROOT))

import psycopg  # noqa: E402
import redis  # noqa: E402
from pgvector.psycopg import register_vector  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.gallery_repository import GalleryRepository  # noqa: E402
from app.match_repository import MatchResultRepository  # noqa: E402
from app.person_repository import PersonRepository  # noqa: E402
from app.repository import FaceObservationRepository  # noqa: E402
from app.vector_store import FaceVectorStore  # noqa: E402


SIDECAR_FILE = "annotations.frame_cache.identity.jsonl"
SUMMARY_FILE = "summary.json"
SIDECAR_SUMMARY_FILE = "summary.frame_cache.identity.json"
SINK_METADATA_FILE = "sink_metadata.json"
IDENTITY_PATCHES_FILE = "identity_patches.jsonl"
C2_4_SUMMARY_FILE = "c2_4_identity_binding_summary.json"
RESULT_PASS = "PASS_C2_4_IDENTITY_BINDING_EVIDENCE_PATCH_READY"
RESULT_JOIN_MISSING = "PARTIAL_C2_4_IDENTITY_JOIN_KEY_MISSING"
RESULT_FAIL = "FAIL_C2_4_IDENTITY_BINDING_BLOCKED"
DEFAULT_SEARCH_REQUEST_ID = "c2400000-0000-4000-8000-000000000001"
DEFAULT_EXTERNAL_PERSON_ID = "test:c2_4:person"
DEFAULT_PERSON_NAME = "C2.4 Test Person"


@dataclass(frozen=True)
class SidecarFace:
    frame_index: int
    frame_pts: int | None
    source_id: str | None
    sidecar_track_id: str | None
    object_id: str | None
    bbox_xyxy: list[float]
    confidence: float | None
    row_index: int
    object_index: int


@dataclass(frozen=True)
class RedisObservation:
    data: dict[str, Any]
    stream_id: str
    source_observation_id: str
    redis_source_id: str | None
    redis_track_id: str | None
    redis_frame_pts: int | None
    redis_timestamp_ms: int | None
    redis_frame_num: int | None
    bbox_cxcywh: list[float]
    face_confidence: float | None
    quality: float | None


@dataclass(frozen=True)
class JoinCandidate:
    face: SidecarFace
    observation: RedisObservation
    bbox_iou: float
    frame_pts_delta_ns: int | None
    timestamp_delta_ms: float | None


@dataclass(frozen=True)
class IdentityBinding:
    candidate: JoinCandidate
    observation_uuid: str
    person_id: int
    person_name: str
    external_person_id: str
    gallery_embedding_id: int
    match_result_id: int
    search_request_id: str
    similarity: float
    threshold: float


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    input_bundle: Path
    output_bundle: Path
    identity_patch_path: Path
    c2_4_summary_path: Path
    summary_path: Path
    sidecar_path: Path
    summary: dict[str, Any]
    c2_4_summary: dict[str, Any]
    patch: dict[str, Any]


def build_identity_patched_bundle(
    *,
    input_bundle: Path,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    redis_stream: str = "security.face_observations",
    redis_source_id: str = "c2_post_savant_fps_probe",
    external_person_id: str = DEFAULT_EXTERNAL_PERSON_ID,
    person_name: str = DEFAULT_PERSON_NAME,
    search_request_id: str = DEFAULT_SEARCH_REQUEST_ID,
    similarity_threshold: float = 0.99,
    join_iou_threshold: float = 0.995,
    join_pts_tolerance_ns: int = 2_000_000,
    prepare_db_schema: bool = False,
    reset_test_identity: bool = False,
    overwrite: bool = False,
) -> BuildResult:
    """Create an identity-patched copy of a C2 evidence bundle."""

    input_bundle = input_bundle.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    _require_input_bundle(input_bundle)
    _validate_uuid(search_request_id)
    if prepare_db_schema:
        prepare_identity_schema(database_url)

    sidecar_rows = _read_jsonl(input_bundle / SIDECAR_FILE)
    sidecar_faces = extract_sidecar_faces(sidecar_rows)
    if not sidecar_faces:
        raise RuntimeError("sidecar_face_objects_missing")

    redis_observations = load_redis_observations(
        redis_url=redis_url,
        stream=redis_stream,
        source_id=redis_source_id,
    )
    candidate = select_join_candidate(
        sidecar_faces=sidecar_faces,
        observations=redis_observations,
        iou_threshold=join_iou_threshold,
        pts_tolerance_ns=join_pts_tolerance_ns,
    )
    if candidate is None:
        raise JoinKeyMissing("no_unique_sidecar_to_face_observation_join")

    binding = create_identity_binding(
        database_url=database_url,
        candidate=candidate,
        external_person_id=external_person_id,
        person_name=person_name,
        search_request_id=search_request_id,
        similarity_threshold=similarity_threshold,
        reset_test_identity=reset_test_identity,
    )

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()
    shutil.copytree(input_bundle, output_dir)

    patched_rows, patch = patch_identity_rows(sidecar_rows, binding)
    summary = _read_json(input_bundle / SUMMARY_FILE)
    patched_summary = patch_summary(summary, patched_rows, binding, patch)
    c2_4_summary = build_c2_4_summary(
        input_bundle=input_bundle,
        output_bundle=output_dir,
        summary=patched_summary,
        binding=binding,
        patch=patch,
    )

    sidecar_path = output_dir / SIDECAR_FILE
    summary_path = output_dir / SUMMARY_FILE
    sidecar_summary_path = output_dir / SIDECAR_SUMMARY_FILE
    identity_patch_path = output_dir / IDENTITY_PATCHES_FILE
    c2_4_summary_path = output_dir / C2_4_SUMMARY_FILE
    _write_jsonl(sidecar_path, patched_rows)
    _write_json(summary_path, patched_summary)
    _write_json(sidecar_summary_path, patched_summary)
    _write_jsonl(identity_patch_path, [patch])
    _write_json(c2_4_summary_path, c2_4_summary)

    return BuildResult(
        result_marker=RESULT_PASS,
        input_bundle=input_bundle,
        output_bundle=output_dir,
        identity_patch_path=identity_patch_path,
        c2_4_summary_path=c2_4_summary_path,
        summary_path=summary_path,
        sidecar_path=sidecar_path,
        summary=patched_summary,
        c2_4_summary=c2_4_summary,
        patch=patch,
    )


class JoinKeyMissing(RuntimeError):
    """Raised when no reliable sidecar-to-observation join exists."""


def prepare_identity_schema(database_url: str) -> None:
    """Apply existing F3 identity migrations if the runtime DB lacks them."""

    migrations = [
        ROOT / "db/migrations/005_phase_f3_1_face_observations.sql",
        ROOT / "db/migrations/006_phase_f3_4_gallery_schema.sql",
        ROOT / "db/migrations/007_phase_f3_5_match_results_gallery_semantics.sql",
    ]
    with psycopg.connect(database_url, autocommit=True) as conn:
        for path in migrations:
            conn.execute(path.read_text(encoding="utf-8"))


def load_redis_observations(
    *,
    redis_url: str,
    stream: str,
    source_id: str,
) -> list[RedisObservation]:
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    output: list[RedisObservation] = []
    for stream_id, fields in client.xrange(stream):
        data = _parse_observation_payload(fields)
        if data.get("source_id") != source_id:
            continue
        bbox = _redis_bbox_cxcywh(data)
        sid = _text_or_none(data.get("source_observation_id"))
        if not sid or bbox is None:
            continue
        output.append(
            RedisObservation(
                data=data,
                stream_id=str(stream_id),
                source_observation_id=sid,
                redis_source_id=_text_or_none(data.get("source_id")),
                redis_track_id=_text_or_none(data.get("track_id")),
                redis_frame_pts=_redis_frame_pts(data),
                redis_timestamp_ms=_int_or_none(data.get("timestamp_ms")),
                redis_frame_num=_int_or_none(data.get("frame_num")),
                bbox_cxcywh=bbox,
                face_confidence=_float_or_none(data.get("face_confidence")),
                quality=_float_or_none(data.get("quality")),
            )
        )
    return output


def extract_sidecar_faces(rows: list[dict[str, Any]]) -> list[SidecarFace]:
    faces: list[SidecarFace] = []
    for row_index, row in enumerate(rows):
        objects = row.get("objects") if isinstance(row.get("objects"), list) else []
        for object_index, obj in enumerate(objects):
            if not isinstance(obj, dict):
                continue
            if _object_type(obj) not in {"face", "known_face"}:
                continue
            bbox = _sidecar_bbox_xyxy(obj)
            if bbox is None:
                continue
            faces.append(
                SidecarFace(
                    frame_index=int(row.get("frame_index") or row_index),
                    frame_pts=_int_or_none(row.get("frame_pts") or obj.get("frame_pts")),
                    source_id=_text_or_none(row.get("source_id") or obj.get("source_id")),
                    sidecar_track_id=_text_or_none(obj.get("track_id")),
                    object_id=_text_or_none(obj.get("object_id")),
                    bbox_xyxy=bbox,
                    confidence=_face_confidence(obj),
                    row_index=row_index,
                    object_index=object_index,
                )
            )
    return faces


def select_join_candidate(
    *,
    sidecar_faces: list[SidecarFace],
    observations: list[RedisObservation],
    iou_threshold: float,
    pts_tolerance_ns: int,
) -> JoinCandidate | None:
    unique: list[JoinCandidate] = []
    for face in sidecar_faces:
        candidates: list[JoinCandidate] = []
        for observation in observations:
            frame_pts_delta_ns = _frame_pts_delta(face.frame_pts, observation.redis_frame_pts)
            if frame_pts_delta_ns is not None and frame_pts_delta_ns > pts_tolerance_ns:
                continue
            if frame_pts_delta_ns is None:
                timestamp_delta_ms = _timestamp_delta_ms(face.frame_pts, observation.redis_timestamp_ms)
                if timestamp_delta_ms is None or timestamp_delta_ms > pts_tolerance_ns / 1_000_000:
                    continue
            else:
                timestamp_delta_ms = _timestamp_delta_ms(face.frame_pts, observation.redis_timestamp_ms)
            bbox_iou = _bbox_iou(face.bbox_xyxy, _cxcywh_to_xyxy(observation.bbox_cxcywh))
            if bbox_iou < iou_threshold:
                continue
            candidates.append(
                JoinCandidate(
                    face=face,
                    observation=observation,
                    bbox_iou=bbox_iou,
                    frame_pts_delta_ns=frame_pts_delta_ns,
                    timestamp_delta_ms=timestamp_delta_ms,
                )
            )
        if len(candidates) == 1:
            unique.append(candidates[0])
    if not unique:
        return None
    unique.sort(
        key=lambda item: (
            item.face.confidence if item.face.confidence is not None else -1.0,
            item.observation.quality if item.observation.quality is not None else -1.0,
            item.bbox_iou,
        ),
        reverse=True,
    )
    return unique[0]


def create_identity_binding(
    *,
    database_url: str,
    candidate: JoinCandidate,
    external_person_id: str,
    person_name: str,
    search_request_id: str,
    similarity_threshold: float,
    reset_test_identity: bool,
) -> IdentityBinding:
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        register_vector(conn)
        if reset_test_identity:
            _reset_test_identity(conn, external_person_id, search_request_id)
        repo = FaceObservationRepository(conn)
        repo.insert_observation(candidate.observation.data)
        observation = _fetch_observation(conn, candidate.observation.source_observation_id)
        if observation is None:
            raise RuntimeError("face_observation_materialization_failed")

        person_repo = PersonRepository(conn)
        person = person_repo.get_by_external_person_id(external_person_id)
        if person is None:
            person_id = person_repo.create_person(
                person_name,
                external_person_id=external_person_id,
                created_by="c2_4_identity_patch",
                updated_by="c2_4_identity_patch",
                payload={"phase": "C2.4", "purpose": "identity_binding_mvp"},
            )
        else:
            person_id = int(person["id"])
            person_name = str(person.get("name") or person_name)

        gallery_id = _ensure_gallery_embedding(
            conn=conn,
            observation=observation,
            person_id=person_id,
            source_observation_id=candidate.observation.source_observation_id,
        )
        match = _write_gallery_match(
            conn=conn,
            observation=observation,
            person_id=person_id,
            gallery_id=gallery_id,
            search_request_id=search_request_id,
            similarity_threshold=similarity_threshold,
        )
        if match["similarity"] < similarity_threshold:
            raise RuntimeError(
                f"gallery_match_below_threshold:{match['similarity']:.6f}<"
                f"{similarity_threshold:.6f}"
            )
        return IdentityBinding(
            candidate=candidate,
            observation_uuid=str(observation["id"]),
            person_id=person_id,
            person_name=person_name,
            external_person_id=external_person_id,
            gallery_embedding_id=gallery_id,
            match_result_id=int(match["match_result_id"]),
            search_request_id=search_request_id,
            similarity=float(match["similarity"]),
            threshold=similarity_threshold,
        )


def patch_identity_rows(
    rows: list[dict[str, Any]],
    binding: IdentityBinding,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    patched = json.loads(json.dumps(rows))
    face = binding.candidate.face
    target_obj = patched[face.row_index]["objects"][face.object_index]
    original_geometry = _geometry_snapshot(target_obj)
    patch = build_identity_patch(binding)
    target_obj["object_type"] = "known_face"
    target_obj["label"] = {
        **_dict(target_obj.get("label")),
        "kind": "known_face",
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "display_name": binding.person_name,
        "similarity": binding.similarity,
        "threshold": binding.threshold,
        "source_observation_id": binding.candidate.observation.source_observation_id,
        "match_result_id": binding.match_result_id,
        "gallery_embedding_id": binding.gallery_embedding_id,
    }
    target_obj["identity"] = {
        **_dict(target_obj.get("identity")),
        "status": "matched",
        "match_status": "above_threshold",
        "identity_binding_status": "matched",
        "source_observation_id": binding.candidate.observation.source_observation_id,
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "display_name": binding.person_name,
        "person_name": binding.person_name,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "search_request_id": binding.search_request_id,
        "similarity": binding.similarity,
        "threshold": binding.threshold,
        "rank": 1,
        "search_mode": "gallery_match",
        "identity_source": "match_results",
        "recognition_claim_allowed": True,
        "join_method": patch["join_method"],
        "visual_evidence_status": "identity_bound",
    }
    target_obj["style"] = {
        **_dict(target_obj.get("style")),
        "bbox_color": "#D50000",
        "label_color": "#D50000",
        "reason": "identity_match",
        "priority": 50,
    }
    if _geometry_snapshot(target_obj) != original_geometry:
        raise RuntimeError("identity_patch_modified_geometry")
    return patched, patch


def build_identity_patch(binding: IdentityBinding) -> dict[str, Any]:
    candidate = binding.candidate
    face = candidate.face
    observation = candidate.observation
    return {
        "schema_version": "1.0",
        "message_type": "identity_patch",
        "source_observation_id": observation.source_observation_id,
        "join_method": "frame_pts_bbox_iou_unique",
        "join_key": {
            "sidecar_source_id": face.source_id,
            "sidecar_frame_index": face.frame_index,
            "sidecar_frame_pts": face.frame_pts,
            "sidecar_track_id": face.sidecar_track_id,
            "sidecar_object_id": face.object_id,
            "sidecar_face_bbox_xyxy": face.bbox_xyxy,
            "redis_source_id": observation.redis_source_id,
            "redis_track_id": observation.redis_track_id,
            "redis_frame_pts": observation.redis_frame_pts,
            "redis_timestamp_ms": observation.redis_timestamp_ms,
            "redis_frame_num": observation.redis_frame_num,
            "redis_face_bbox_cxcywh": observation.bbox_cxcywh,
            "bbox_iou": candidate.bbox_iou,
            "frame_pts_delta_ns": candidate.frame_pts_delta_ns,
            "timestamp_delta_ms": candidate.timestamp_delta_ms,
            "track_id_used_for_join": False,
            "track_id_note": (
                "stable sink sidecar track ids are local to the cropped output; "
                "Redis source observation ids retain post-Savant person track ids"
            ),
        },
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "person_name": binding.person_name,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "search_request_id": binding.search_request_id,
        "similarity": binding.similarity,
        "threshold": binding.threshold,
        "search_mode": "gallery_match",
        "identity_source": "match_results",
        "identity_binding_status": "matched",
        "geometry_source": "post_savant_sidecar",
        "geometry_modified": False,
    }


def patch_summary(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    binding: IdentityBinding,
    patch: dict[str, Any],
) -> dict[str, Any]:
    output = json.loads(json.dumps(summary))
    counts = _count_objects(rows)
    output["evidence_topology"] = "post_savant"
    output["source_evidence_topology"] = summary.get("evidence_topology")
    output["evidence_capture_mode"] = "stable_post_savant_sink_time_crop"
    output["workaround_used"] = True
    output["event_style_replay_job_passed"] = False
    output["annotation_source"] = "sidecar"
    output["annotation_source_kind"] = "production_sidecar"
    output["fallback_used"] = False
    output["legacy_used_for_visual_binding"] = False
    output["allow_db_annotation_fallback"] = False
    output["allow_legacy_annotation_fallback"] = False
    output["identity_binding_connected"] = True
    output["identity_patch_source"] = "match_results"
    output["identity_patch_count"] = 1
    output["identity_patch_file"] = IDENTITY_PATCHES_FILE
    output["identity_binding_status"] = "matched"
    output["recognition_claim_allowed"] = counts["known_face"] > 0
    output["recognition_claim_scope"] = "identity_patched_known_face_objects_only"
    output["known_face_count"] = counts["known_face"]
    output["unknown_face_count"] = counts["face"]
    output["matched_objects"] = counts["known_face"]
    output["unknown_objects"] = counts["face"]
    output["object_counts"] = {
        "person": counts["person"],
        "face": counts["face"],
        "known_face": counts["known_face"],
    }
    output["person_objects_count"] = counts["person"]
    output["face_objects_count"] = counts["face"]
    output["known_face_objects_count"] = counts["known_face"]
    output["source_observation_id_count"] = counts["source_observation_id"]
    output["c2_4_identity_binding"] = {
        "result_marker": RESULT_PASS,
        "selected_frame_index": binding.candidate.face.frame_index,
        "selected_frame_pts": binding.candidate.face.frame_pts,
        "selected_track_id": binding.candidate.face.sidecar_track_id,
        "selected_object_id": binding.candidate.face.object_id,
        "source_observation_id": binding.candidate.observation.source_observation_id,
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "person_name": binding.person_name,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "search_request_id": binding.search_request_id,
        "similarity": binding.similarity,
        "threshold": binding.threshold,
        "join_method": patch["join_method"],
        "geometry_modified": False,
    }
    limitations = list(output.get("limitations") or [])
    for item in [
        "event_style_replay_not_production_ready_stable_sink_workaround",
        "identity_patch_applies_to_matched_known_face_objects_only",
    ]:
        if item not in limitations:
            limitations.append(item)
    output["limitations"] = limitations
    return output


def build_c2_4_summary(
    *,
    input_bundle: Path,
    output_bundle: Path,
    summary: dict[str, Any],
    binding: IdentityBinding,
    patch: dict[str, Any],
) -> dict[str, Any]:
    counts = _dict(summary.get("object_counts"))
    return {
        "result_marker": RESULT_PASS,
        "input_bundle": str(input_bundle),
        "output_bundle": str(output_bundle),
        "identity_patches_file": IDENTITY_PATCHES_FILE,
        "selected_frame": binding.candidate.face.frame_index,
        "selected_frame_pts": binding.candidate.face.frame_pts,
        "selected_track_id": binding.candidate.face.sidecar_track_id,
        "selected_object_id": binding.candidate.face.object_id,
        "source_observation_id": binding.candidate.observation.source_observation_id,
        "face_bbox": binding.candidate.face.bbox_xyxy,
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "person_name": binding.person_name,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "search_request_id": binding.search_request_id,
        "similarity": binding.similarity,
        "threshold": binding.threshold,
        "join_method": patch["join_method"],
        "join_key": patch["join_key"],
        "geometry_modified": False,
        "decoded_video_frame_count": summary.get("decoded_video_frame_count"),
        "metadata_frame_count": summary.get("original_metadata_frame_count"),
        "sidecar_frame_count": summary.get("sidecar_frame_count"),
        "known_face_count": counts.get("known_face"),
        "unknown_face_count": summary.get("unknown_face_count"),
        "person_count": counts.get("person"),
        "face_count": counts.get("face"),
        "production_ready": summary.get("production_ready"),
        "video_integrity": summary.get("video_integrity"),
        "fallback_used": summary.get("fallback_used"),
        "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
        "allow_db_annotation_fallback": summary.get("allow_db_annotation_fallback"),
        "allow_legacy_annotation_fallback": summary.get("allow_legacy_annotation_fallback"),
        "evidence_capture_mode": summary.get("evidence_capture_mode"),
        "workaround_used": summary.get("workaround_used"),
        "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
    }


def _ensure_gallery_embedding(
    *,
    conn: psycopg.Connection,
    observation: dict[str, Any],
    person_id: int,
    source_observation_id: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM person_gallery_embeddings
            WHERE person_id = %(person_id)s
              AND source_observation_id = %(source_observation_id)s
              AND is_active = true
            ORDER BY id
            LIMIT 1
            """,
            {"person_id": person_id, "source_observation_id": source_observation_id},
        )
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM person_gallery_embeddings
                WHERE person_id = %(person_id)s
                  AND is_primary = true
                  AND is_active = true
            )
            """,
            {"person_id": person_id},
        )
        row = cur.fetchone()
        has_primary = bool(row["exists"]) if row else False
    embedding = _embedding_list(observation["embedding"])
    gallery_repo = GalleryRepository(conn)
    return gallery_repo.add_embedding(
        person_id=person_id,
        embedding=embedding,
        source_type="c2_4_identity_patch_observation",
        source_observation_id=source_observation_id,
        face_bbox=observation.get("face_bbox"),
        landmarks=observation.get("landmarks"),
        quality=_float_or_none(observation.get("quality")),
        is_primary=not has_primary,
        payload={
            "phase": "C2.4",
            "identity_patch_source": "match_results",
            "deterministic_test_match": True,
        },
    )


def _write_gallery_match(
    *,
    conn: psycopg.Connection,
    observation: dict[str, Any],
    person_id: int,
    gallery_id: int,
    search_request_id: str,
    similarity_threshold: float,
) -> dict[str, Any]:
    embedding = _embedding_list(observation["embedding"])
    results = FaceVectorStore(conn).search_gallery(
        embedding,
        top_k=1,
        min_similarity=similarity_threshold,
        person_ids=[person_id],
    )
    if not results:
        raise RuntimeError("gallery_match_result_missing")
    result = results[0]
    if int(result["id"]) != gallery_id:
        raise RuntimeError("gallery_match_top1_gallery_id_mismatch")
    expires_at = datetime.now(timezone.utc) + timedelta(days=90)
    row_id = MatchResultRepository(conn).insert_gallery_match_result(
        {
            "search_request_id": search_request_id,
            "search_mode": "gallery_match",
            "query_observation_id": observation["id"],
            "query_source_observation_id": observation["source_observation_id"],
            "query_person_id": result["person_id"],
            "query_gallery_embedding_id": result["id"],
            "query_embedding_model": result.get("embedding_model", "adaface"),
            "similarity_threshold": similarity_threshold,
            "rank": 1,
            "similarity": result["similarity"],
            "face_confidence": observation.get("face_confidence"),
            "quality": observation.get("quality"),
            "expires_at": expires_at,
            "payload": {
                "phase": "C2.4",
                "identity_patch_source": "match_results",
                "deterministic_test_match": True,
            },
        }
    )
    if row_id is None:
        row_id = _fetch_match_result_id(conn, search_request_id, gallery_id)
    if row_id is None:
        raise RuntimeError("match_result_insert_failed")
    return {"match_result_id": int(row_id), "similarity": float(result["similarity"])}


def _reset_test_identity(
    conn: psycopg.Connection,
    external_person_id: str,
    search_request_id: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM match_results
            WHERE search_request_id = %(req)s
               OR query_person_id IN (
                    SELECT id FROM persons WHERE external_person_id = %(ext)s
               )
               OR query_gallery_embedding_id IN (
                    SELECT pge.id
                    FROM person_gallery_embeddings pge
                    JOIN persons p ON p.id = pge.person_id
                    WHERE p.external_person_id = %(ext)s
               )
            """,
            {"req": search_request_id, "ext": external_person_id},
        )
        cur.execute(
            """
            DELETE FROM person_gallery_embeddings
            WHERE person_id IN (
                SELECT id FROM persons WHERE external_person_id = %(ext)s
            )
            """,
            {"ext": external_person_id},
        )
        cur.execute(
            "DELETE FROM persons WHERE external_person_id = %(ext)s",
            {"ext": external_person_id},
        )


def _fetch_observation(conn: psycopg.Connection, source_observation_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_observation_id, camera_id, source_id, track_id,
                   timestamp_ms, frame_num, face_bbox, landmarks,
                   face_confidence, quality, embedding
            FROM face_observations
            WHERE source_observation_id = %(sid)s
            """,
            {"sid": source_observation_id},
        )
        return cur.fetchone()


def _fetch_match_result_id(
    conn: psycopg.Connection,
    search_request_id: str,
    gallery_id: int,
) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM match_results
            WHERE search_request_id = %(req)s
              AND query_gallery_embedding_id = %(gallery_id)s
            """,
            {"req": search_request_id, "gallery_id": gallery_id},
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def _parse_observation_payload(fields: dict[str, Any]) -> dict[str, Any]:
    raw = fields.get("data") or fields.get("payload")
    if isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    return dict(fields)


def _redis_bbox_cxcywh(data: dict[str, Any]) -> list[float] | None:
    face_bbox = data.get("face_bbox")
    if not isinstance(face_bbox, dict):
        return None
    fmt = str(face_bbox.get("format") or "").lower()
    if fmt != "cxcywh":
        return None
    values = face_bbox.get("values") or face_bbox.get("cxcywh")
    if not isinstance(values, list) or len(values) < 4:
        return None
    parsed = [_float_or_none(value) for value in values[:4]]
    if any(value is None for value in parsed):
        return None
    return [float(value) for value in parsed if value is not None]


def _redis_frame_pts(data: dict[str, Any]) -> int | None:
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    return _int_or_none(media.get("frame_pts") or data.get("frame_pts"))


def _sidecar_bbox_xyxy(obj: dict[str, Any]) -> list[float] | None:
    bbox = obj.get("bbox")
    if not isinstance(bbox, dict):
        return None
    values = bbox.get("xyxy")
    if not isinstance(values, list) or len(values) < 4:
        return None
    parsed = [_float_or_none(value) for value in values[:4]]
    if any(value is None for value in parsed):
        return None
    return [float(value) for value in parsed if value is not None]


def _face_confidence(obj: dict[str, Any]) -> float | None:
    bbox = _dict(obj.get("bbox"))
    detection = _dict(obj.get("detection"))
    return _float_or_none(bbox.get("confidence") or detection.get("confidence"))


def _object_type(obj: dict[str, Any]) -> str:
    direct = _text_or_none(obj.get("object_type"))
    if direct:
        return direct
    label = _dict(obj.get("label"))
    kind = _text_or_none(label.get("kind"))
    if kind == "known_face":
        return "known_face"
    if kind in {"unknown_face", "face"}:
        return "face"
    return "unknown"


def _count_objects(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"person": 0, "face": 0, "known_face": 0, "source_observation_id": 0}
    for row in rows:
        for obj in row.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            object_type = _object_type(obj)
            if object_type in {"person", "face", "known_face"}:
                counts[object_type] += 1
            identity = _dict(obj.get("identity"))
            if identity.get("source_observation_id"):
                counts["source_observation_id"] += 1
    return counts


def _geometry_snapshot(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "bbox": json.loads(json.dumps(obj.get("bbox"))),
        "landmarks": json.loads(json.dumps(obj.get("landmarks"))),
        "pose": json.loads(json.dumps(obj.get("pose"))),
        "track_id": obj.get("track_id"),
        "object_id": obj.get("object_id"),
    }


def _frame_pts_delta(sidecar_pts: int | None, redis_pts: int | None) -> int | None:
    if sidecar_pts is None or redis_pts is None:
        return None
    return abs(int(sidecar_pts) - int(redis_pts))


def _timestamp_delta_ms(sidecar_pts: int | None, redis_timestamp_ms: int | None) -> float | None:
    if sidecar_pts is None or redis_timestamp_ms is None:
        return None
    return abs(float(sidecar_pts) / 1_000_000.0 - float(redis_timestamp_ms))


def _cxcywh_to_xyxy(values: list[float]) -> list[float]:
    cx, cy, width, height = values[:4]
    return [cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0]


def _bbox_iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a[:4]
    bx1, by1, bx2, by2 = b[:4]
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def _embedding_list(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def _require_input_bundle(bundle: Path) -> None:
    if not bundle.is_dir():
        raise FileNotFoundError(f"input bundle missing: {bundle}")
    for name in (SIDECAR_FILE, SUMMARY_FILE, SINK_METADATA_FILE):
        path = bundle / name
        if not path.is_file():
            raise FileNotFoundError(f"required bundle file missing: {path}")
    if not any((bundle / name).is_file() for name in ("raw_clip.mov", "raw_clip.mp4")):
        raise FileNotFoundError(f"raw_clip.mov/raw_clip.mp4 missing: {bundle}")


def _validate_uuid(value: str) -> None:
    uuid.UUID(value)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"expected object JSONL row at {path}:{line_number}")
        rows.append(payload)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _result_payload(result: BuildResult) -> dict[str, Any]:
    c2 = result.c2_4_summary
    return {
        "result_marker": result.result_marker,
        "input_bundle": str(result.input_bundle),
        "output_bundle": str(result.output_bundle),
        "summary_path": str(result.summary_path),
        "sidecar_path": str(result.sidecar_path),
        "identity_patches_path": str(result.identity_patch_path),
        "c2_4_identity_binding_summary_path": str(result.c2_4_summary_path),
        "selected_frame": c2.get("selected_frame"),
        "selected_track_id": c2.get("selected_track_id"),
        "source_observation_id": c2.get("source_observation_id"),
        "person_id": c2.get("person_id"),
        "external_person_id": c2.get("external_person_id"),
        "gallery_embedding_id": c2.get("gallery_embedding_id"),
        "match_result_id": c2.get("match_result_id"),
        "similarity": c2.get("similarity"),
        "threshold": c2.get("threshold"),
        "join_method": c2.get("join_method"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6395/0")
    parser.add_argument("--redis-stream", default="security.face_observations")
    parser.add_argument("--redis-source-id", default="c2_post_savant_fps_probe")
    parser.add_argument("--external-person-id", default=DEFAULT_EXTERNAL_PERSON_ID)
    parser.add_argument("--person-name", default=DEFAULT_PERSON_NAME)
    parser.add_argument("--search-request-id", default=DEFAULT_SEARCH_REQUEST_ID)
    parser.add_argument("--similarity-threshold", type=float, default=0.99)
    parser.add_argument("--join-iou-threshold", type=float, default=0.995)
    parser.add_argument("--join-pts-tolerance-ns", type=int, default=2_000_000)
    parser.add_argument("--prepare-db-schema", action="store_true", default=False)
    parser.add_argument("--reset-test-identity", action="store_true", default=False)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    database_url = args.database_url
    if database_url is None:
        import os

        database_url = os.environ.get("DATABASE_URL", "postgresql://video:video@127.0.0.1:5432/video_analytics")

    try:
        result = build_identity_patched_bundle(
            input_bundle=args.input_bundle,
            output_dir=args.output_dir,
            database_url=database_url,
            redis_url=args.redis_url,
            redis_stream=args.redis_stream,
            redis_source_id=args.redis_source_id,
            external_person_id=args.external_person_id,
            person_name=args.person_name,
            search_request_id=args.search_request_id,
            similarity_threshold=args.similarity_threshold,
            join_iou_threshold=args.join_iou_threshold,
            join_pts_tolerance_ns=args.join_pts_tolerance_ns,
            prepare_db_schema=args.prepare_db_schema,
            reset_test_identity=args.reset_test_identity,
            overwrite=args.overwrite,
        )
    except JoinKeyMissing as exc:
        payload = {
            "result_marker": RESULT_JOIN_MISSING,
            "reason": str(exc),
            "input_bundle": str(args.input_bundle),
            "output_bundle": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
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
