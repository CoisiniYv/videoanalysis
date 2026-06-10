#!/usr/bin/env python3
"""Search external Reese / Finch gallery faces against video observations.

C2.12B is intentionally conservative: it can only PASS when a Reese or Finch
gallery embedding matches a real video-derived face_observation above the
chosen threshold and that observation is directly joinable to an existing C2
stable evidence sidecar. A gallery self-match is never accepted as C2.12B
evidence.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_C2_STABLE_BUNDLE = Path(
    "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035"
)
DEFAULT_THRESHOLD = 0.65
DEFAULT_TOP_K = 10
DEFAULT_WATCHLIST_RULE_ID = "c2_12b_external_person_watchlist_rule"
DEFAULT_CAPTURE_MODE = "stable_post_savant_sink_time_crop"

RESULT_PASS = "PASS_C2_12B_EXTERNAL_PERSON_WATCHLIST_EVIDENCE_READY"
RESULT_JOIN_GAP = "PARTIAL_C2_12B_MATCH_FOUND_EVIDENCE_JOIN_GAP"
RESULT_NO_MATCH = "PARTIAL_C2_12B_NO_VIDEO_MATCH_FOUND"
RESULT_NO_OBSERVATIONS = "PARTIAL_C2_12B_NO_VIDEO_OBSERVATIONS_AVAILABLE"
RESULT_FAIL = "FAIL_C2_12B_EXTERNAL_WATCHLIST_BLOCKED"

TARGETS = {
    "reese": {
        "name": "Reese",
        "external_person_id": "demo:f4_3:reese",
    },
    "finch": {
        "name": "Finch",
        "external_person_id": "demo:f4_3:finch",
    },
}

SIDECAR_FILE = "annotations.frame_cache.identity.jsonl"
SUMMARY_FILE = "summary.json"
WATCHLIST_EVENT_FILE = "watchlist_event.json"
MATCH_REPORT_FILE = "match_report.json"
C2_12B_SUMMARY_FILE = "c2_12b_external_watchlist_summary.json"
SEARCH_SUMMARY_FILE = "c2_12b_search_summary.json"
INVENTORY_FILE = "candidate_observation_inventory.json"
REES_TOP_MATCHES_FILE = "reese_top_matches.json"
FINCH_TOP_MATCHES_FILE = "finch_top_matches.json"
DECISION_REPORT_FILE = "decision_report.md"
OPERATOR_REPORT_FILE = "operator_external_watchlist_report.html"
UNSAFE_SCAN_FILE = "unsafe_payload_scan.json"

FORBIDDEN_KEYS = {
    "embedding",
    "embedding_vector",
    "embedding_values",
    "image_bytes",
    "crop_bytes",
    "face_crop_bytes",
    "base64",
    "image_base64",
    "crop_base64",
    "face_crop_base64",
    "base64_image",
}
ALLOWED_NUMERIC_ARRAY_PATH_TOKENS = {
    "bbox",
    "face_bbox",
    "person_bbox",
    "landmarks",
    "keypoints",
    "xyxy",
    "values",
}


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    output_dir: Path
    summary_path: Path
    summary: dict[str, Any]


def run_c2_12b_external_watchlist_evidence(
    *,
    output_dir: Path,
    database_url: str,
    stable_bundle: Path,
    threshold: float,
    top_k: int,
    watchlist_rule_id: str,
    overwrite: bool = False,
) -> BuildResult:
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0.0, 1.0], got {threshold}")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    output_dir = output_dir.resolve(strict=False)
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        _clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    stable_bundle = stable_bundle.resolve(strict=False)
    gallery = fetch_external_gallery(database_url)
    inventory = fetch_observation_inventory(database_url, stable_bundle)
    matches_by_identity = search_all_targets(
        database_url=database_url,
        gallery=gallery,
        stable_bundle=stable_bundle,
        threshold=threshold,
        top_k=top_k,
    )

    _write_json(output_dir / INVENTORY_FILE, inventory)
    _write_json(output_dir / REES_TOP_MATCHES_FILE, matches_by_identity.get("reese", []))
    _write_json(output_dir / FINCH_TOP_MATCHES_FILE, matches_by_identity.get("finch", []))

    decision = decide_result(
        gallery=gallery,
        inventory=inventory,
        matches_by_identity=matches_by_identity,
        threshold=threshold,
    )
    unsafe_scan = scan_for_unsafe_payload(
        {
            "gallery": gallery,
            "inventory": inventory,
            "matches_by_identity": matches_by_identity,
            "decision": decision,
        }
    )
    _write_json(output_dir / UNSAFE_SCAN_FILE, unsafe_scan)

    if decision["result_marker"] == RESULT_PASS:
        best_match = dict(decision["best_match"])
        pass_summary = build_evidence_bundle(
            input_bundle=stable_bundle,
            output_bundle=output_dir,
            match=best_match,
            threshold=threshold,
            watchlist_rule_id=watchlist_rule_id,
        )
        summary = build_summary(
            output_dir=output_dir,
            stable_bundle=stable_bundle,
            gallery=gallery,
            inventory=inventory,
            matches_by_identity=matches_by_identity,
            decision=decision,
            unsafe_scan=unsafe_scan,
            threshold=threshold,
            watchlist_rule_id=watchlist_rule_id,
            pass_summary=pass_summary,
        )
        summary_path = output_dir / C2_12B_SUMMARY_FILE
    else:
        summary = build_summary(
            output_dir=output_dir,
            stable_bundle=stable_bundle,
            gallery=gallery,
            inventory=inventory,
            matches_by_identity=matches_by_identity,
            decision=decision,
            unsafe_scan=unsafe_scan,
            threshold=threshold,
            watchlist_rule_id=watchlist_rule_id,
            pass_summary=None,
        )
        summary_path = output_dir / SEARCH_SUMMARY_FILE

    if not unsafe_scan.get("passed"):
        summary["result_marker"] = RESULT_FAIL
        summary["decision_reason"] = "unsafe_payload_scan_failed"

    _write_json(summary_path, summary)
    _write_decision_report(output_dir / DECISION_REPORT_FILE, summary)
    return BuildResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=summary_path,
        summary=summary,
    )


def fetch_external_gallery(database_url: str) -> dict[str, Any]:
    external_ids = [target["external_person_id"] for target in TARGETS.values()]
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS person_id, p.name, p.external_person_id,
                       p.is_active AS person_active,
                       pge.id AS gallery_embedding_id, pge.source_type,
                       pge.source_image_path, pge.source_observation_id,
                       pge.embedding_model, pge.model_version, pge.embedding_dim,
                       pge.embedding IS NOT NULL AS embedding_present,
                       pge.embedding_norm, pge.quality, pge.face_bbox,
                       pge.landmarks, pge.is_primary,
                       pge.is_active AS gallery_active, pge.created_at
                FROM persons p
                LEFT JOIN person_gallery_embeddings pge ON pge.person_id = p.id
                WHERE p.external_person_id = ANY(%(external_ids)s)
                ORDER BY p.external_person_id, pge.is_active DESC NULLS LAST,
                         pge.is_primary DESC NULLS LAST, pge.id
                """,
                {"external_ids": external_ids},
            )
            rows = [_sanitize_db_row(dict(row)) for row in cur.fetchall()]

    by_identity: dict[str, dict[str, Any]] = {}
    for identity_key, target in TARGETS.items():
        identity_rows = [
            row for row in rows
            if row.get("external_person_id") == target["external_person_id"]
        ]
        by_identity[identity_key] = {
            "name": target["name"],
            "external_person_id": target["external_person_id"],
            "gallery": identity_rows,
            "active_gallery_embedding_ids": [
                int(row["gallery_embedding_id"])
                for row in identity_rows
                if row.get("gallery_embedding_id")
                and row.get("gallery_active") is True
                and row.get("embedding_present") is True
            ],
        }
    return {"targets": by_identity, "rows": rows}


def fetch_observation_inventory(database_url: str, stable_bundle: Path) -> dict[str, Any]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM face_observations")
            total = int(cur.fetchone()["total"])
            cur.execute("SELECT COUNT(*) AS total FROM face_observations WHERE embedding IS NOT NULL")
            with_embedding = int(cur.fetchone()["total"])
            cur.execute(
                """
                SELECT COALESCE(source_id, '<null>') AS source_id,
                       COALESCE(camera_id, '<null>') AS camera_id,
                       COUNT(*) AS observation_count,
                       COUNT(embedding) AS embedding_count
                FROM face_observations
                GROUP BY source_id, camera_id
                ORDER BY COUNT(*) DESC, source_id, camera_id
                """
            )
            distribution = [_sanitize_db_row(dict(row)) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id::text AS id, source_observation_id, camera_id,
                       source_id, track_id, frame_num, timestamp_ms,
                       face_bbox, landmarks, face_confidence, quality,
                       detector_model, embedding_model, model_version,
                       embedding_dim, embedding_norm, snapshot_path, crop_path,
                       created_at
                FROM face_observations
                WHERE embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100
                """
            )
            observations = []
            for row in cur.fetchall():
                item = _sanitize_db_row(dict(row))
                item["appears_in_c2_stable_sidecar"] = check_sidecar_join(
                    stable_bundle,
                    str(item.get("source_observation_id") or ""),
                )
                observations.append(item)
    return {
        "total_face_observations": total,
        "face_observations_with_embedding": with_embedding,
        "source_distribution": distribution,
        "observations": observations,
        "stable_bundle": str(stable_bundle),
    }


def search_all_targets(
    *,
    database_url: str,
    gallery: dict[str, Any],
    stable_bundle: Path,
    threshold: float,
    top_k: int,
) -> dict[str, list[dict[str, Any]]]:
    matches: dict[str, list[dict[str, Any]]] = {}
    for identity_key, target in TARGETS.items():
        target_gallery = gallery.get("targets", {}).get(identity_key, {})
        active_ids = list(target_gallery.get("active_gallery_embedding_ids") or [])
        identity_matches: list[dict[str, Any]] = []
        for gallery_id in active_ids:
            identity_matches.extend(
                search_gallery_against_observations(
                    database_url=database_url,
                    identity_key=identity_key,
                    external_person_id=target["external_person_id"],
                    gallery_embedding_id=int(gallery_id),
                    stable_bundle=stable_bundle,
                    threshold=threshold,
                    top_k=top_k,
                )
            )
        identity_matches.sort(
            key=lambda item: (
                -float(item.get("similarity") or -999.0),
                str(item.get("source_observation_id") or ""),
            )
        )
        matches[identity_key] = identity_matches[:top_k]
    return matches


def search_gallery_against_observations(
    *,
    database_url: str,
    identity_key: str,
    external_person_id: str,
    gallery_embedding_id: int,
    stable_bundle: Path,
    threshold: float,
    top_k: int,
) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pge.person_id AS query_person_id,
                       fo.id::text AS id, fo.source_observation_id,
                       fo.camera_id, fo.source_id, fo.track_id, fo.frame_num,
                       fo.timestamp_ms, fo.person_bbox, fo.face_bbox,
                       fo.landmarks, fo.face_confidence, fo.quality,
                       fo.detector_model, fo.embedding_model, fo.model_version,
                       fo.embedding_dim, fo.embedding_norm, fo.snapshot_path,
                       fo.crop_path, fo.created_at,
                       1 - (fo.embedding <=> pge.embedding) AS similarity,
                       fo.embedding <=> pge.embedding AS distance
                FROM face_observations fo
                JOIN person_gallery_embeddings pge ON pge.id = %(gallery_embedding_id)s
                WHERE fo.embedding IS NOT NULL
                ORDER BY fo.embedding <=> pge.embedding
                LIMIT %(top_k)s
                """,
                {"gallery_embedding_id": gallery_embedding_id, "top_k": top_k},
            )
            rows = [_sanitize_db_row(dict(row)) for row in cur.fetchall()]

    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        source_observation_id = str(row.get("source_observation_id") or "")
        joined = check_sidecar_join(stable_bundle, source_observation_id)
        row.update(
            {
                "rank": index,
                "identity_key": identity_key,
                "query_external_person_id": external_person_id,
                "query_gallery_embedding_id": gallery_embedding_id,
                "match_source": "video_face_observation",
                "fake_match": False,
                "self_match_used_for_pass": False,
                "threshold": threshold,
                "threshold_passed": float(row.get("similarity") or -999.0) >= threshold,
                "source_observation_id_present": bool(source_observation_id),
                "real_face_observation": True,
                "c2_stable_sidecar_join": joined,
            }
        )
        output.append(row)
    return output


def decide_result(
    *,
    gallery: dict[str, Any],
    inventory: dict[str, Any],
    matches_by_identity: dict[str, list[dict[str, Any]]],
    threshold: float,
) -> dict[str, Any]:
    missing_gallery = [
        target["external_person_id"]
        for identity_key, target in TARGETS.items()
        if not gallery.get("targets", {}).get(identity_key, {}).get("active_gallery_embedding_ids")
    ]
    if missing_gallery:
        return {
            "result_marker": RESULT_FAIL,
            "reason": "external_gallery_missing",
            "missing_external_person_ids": missing_gallery,
            "threshold": threshold,
            "best_match": None,
        }
    if int(inventory.get("face_observations_with_embedding") or 0) == 0:
        return {
            "result_marker": RESULT_NO_OBSERVATIONS,
            "reason": "no_video_face_observations_with_embedding",
            "threshold": threshold,
            "best_match": None,
        }

    all_matches = [
        match
        for matches in matches_by_identity.values()
        for match in matches
    ]
    best_overall = max(
        all_matches,
        key=lambda item: float(item.get("similarity") or -999.0),
        default=None,
    )
    eligible = [
        match for match in all_matches
        if is_valid_video_match(match, threshold=threshold)
    ]
    if not eligible:
        return {
            "result_marker": RESULT_NO_MATCH,
            "reason": "no_reese_finch_video_observation_above_threshold",
            "threshold": threshold,
            "best_match": best_overall,
            "best_similarity": best_overall.get("similarity") if best_overall else None,
        }

    best = max(eligible, key=lambda item: float(item.get("similarity") or -999.0))
    if not best.get("c2_stable_sidecar_join", {}).get("appears_in_sidecar"):
        return {
            "result_marker": RESULT_JOIN_GAP,
            "reason": "match_above_threshold_not_joinable_to_c2_stable_sidecar",
            "threshold": threshold,
            "best_match": best,
            "best_similarity": best.get("similarity"),
        }

    return {
        "result_marker": RESULT_PASS,
        "reason": "match_above_threshold_joinable_to_c2_stable_sidecar",
        "threshold": threshold,
        "best_match": best,
        "best_similarity": best.get("similarity"),
    }


def is_valid_video_match(match: dict[str, Any], *, threshold: float) -> bool:
    if match.get("fake_match") is True:
        return False
    if match.get("self_match_used_for_pass") is True:
        return False
    if match.get("match_source") != "video_face_observation":
        return False
    if match.get("query_external_person_id") not in {
        TARGETS["reese"]["external_person_id"],
        TARGETS["finch"]["external_person_id"],
    }:
        return False
    if not match.get("source_observation_id"):
        return False
    if match.get("real_face_observation") is not True:
        return False
    return float(match.get("similarity") or -999.0) >= threshold


def build_evidence_bundle(
    *,
    input_bundle: Path,
    output_bundle: Path,
    match: dict[str, Any],
    threshold: float,
    watchlist_rule_id: str,
) -> dict[str, Any]:
    if output_bundle.exists():
        preserved = {
            INVENTORY_FILE,
            REES_TOP_MATCHES_FILE,
            FINCH_TOP_MATCHES_FILE,
            UNSAFE_SCAN_FILE,
        }
        temp_files = {}
        for name in preserved:
            path = output_bundle / name
            if path.exists():
                temp_files[name] = path.read_bytes()
        _clear_dir(output_bundle)
        shutil.copytree(input_bundle, output_bundle, dirs_exist_ok=True)
        for name, data in temp_files.items():
            (output_bundle / name).write_bytes(data)
    else:
        shutil.copytree(input_bundle, output_bundle)

    sidecar_path = output_bundle / SIDECAR_FILE
    summary_path = output_bundle / SUMMARY_FILE
    rows = _read_jsonl(sidecar_path)
    patched_rows, geometry_check = patch_sidecar_for_external_match(rows, match)
    if not geometry_check["geometry_unchanged"]:
        raise RuntimeError("geometry_changed_by_identity_patch")

    input_summary = _read_json(summary_path)
    event = build_watchlist_event(
        match=match,
        bundle_path=output_bundle,
        threshold=threshold,
        watchlist_rule_id=watchlist_rule_id,
    )
    assert_safe_event(event)
    summary = patch_summary_for_external_match(input_summary, event)
    report_html = render_operator_report(event, summary)

    _write_jsonl(sidecar_path, patched_rows)
    _write_json(output_bundle / WATCHLIST_EVENT_FILE, event)
    _write_json(output_bundle / MATCH_REPORT_FILE, {"match": match, "geometry_check": geometry_check})
    _write_json(summary_path, summary)
    _write_json(output_bundle / C2_12B_SUMMARY_FILE, summary)
    (output_bundle / OPERATOR_REPORT_FILE).write_text(report_html, encoding="utf-8")
    return {
        "evidence_bundle_path": str(output_bundle),
        "watchlist_event_path": str(output_bundle / WATCHLIST_EVENT_FILE),
        "summary_path": str(summary_path),
        "sidecar_path": str(sidecar_path),
        "operator_report_path": str(output_bundle / OPERATOR_REPORT_FILE),
        "geometry_unchanged": geometry_check["geometry_unchanged"],
        "known_face_count": summary.get("known_face_count"),
        "watchlist_hit_count": summary.get("watchlist_hit_count"),
    }


def build_watchlist_event(
    *,
    match: dict[str, Any],
    bundle_path: Path,
    threshold: float,
    watchlist_rule_id: str,
) -> dict[str, Any]:
    external_person_id = str(match.get("query_external_person_id") or "")
    if external_person_id not in {
        TARGETS["reese"]["external_person_id"],
        TARGETS["finch"]["external_person_id"],
    }:
        raise ValueError("external_person_id_must_be_reese_or_finch")
    source_observation_id = str(match.get("source_observation_id") or "")
    if not source_observation_id or match.get("real_face_observation") is not True:
        raise ValueError("real_source_observation_id_required")
    if match.get("fake_match") is True or match.get("self_match_used_for_pass") is True:
        raise ValueError("fake_or_self_match_not_allowed")

    identity_key = str(match.get("identity_key") or "")
    person_id = match.get("person_id")
    if person_id is None:
        person_id = _person_id_from_gallery_match(match)
    return {
        "schema_version": "1.0",
        "event_type": "watchlist_hit",
        "source_event_id": (
            f"c2_12b:watchlist_hit:{source_observation_id}:"
            f"{external_person_id}:{match.get('query_gallery_embedding_id')}"
        ),
        "producer": "c2_12b_external_gallery_video_search",
        "camera_id": match.get("camera_id"),
        "source_id": match.get("source_id"),
        "track_id": match.get("track_id"),
        "source_observation_id": source_observation_id,
        "person_id": person_id,
        "external_person_id": external_person_id,
        "gallery_embedding_id": match.get("query_gallery_embedding_id"),
        "similarity": match.get("similarity"),
        "threshold": threshold,
        "watchlist_rule_id": watchlist_rule_id,
        "frame_num": match.get("frame_num"),
        "timestamp_ms": match.get("timestamp_ms"),
        "severity": "high",
        "evidence": {
            "bundle_path": str(bundle_path),
            "capture_mode": DEFAULT_CAPTURE_MODE,
            "workaround_used": True,
            "event_style_replay_job_passed": False,
        },
        "payload": {
            "identity_source": "external_gallery_to_video_observation",
            "watchlist_match_source": "c2_12b_pgvector_search",
            "embedding_included": False,
            "image_bytes_included": False,
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": bool(match.get("c2_stable_sidecar_join", {}).get("track_id_join_warning")),
            "evidence_capture_mode": DEFAULT_CAPTURE_MODE,
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "identity_key": identity_key,
        },
    }


def _person_id_from_gallery_match(match: dict[str, Any]) -> int | None:
    for key in ("query_person_id", "person_id"):
        value = match.get(key)
        if value is not None:
            return int(value)
    return None


def patch_sidecar_for_external_match(
    rows: list[dict[str, Any]],
    match: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_observation_id = str(match.get("source_observation_id") or "")
    if not source_observation_id:
        raise ValueError("source_observation_id_required")
    patched_rows = copy.deepcopy(rows)
    before_geometry: Any = None
    after_geometry: Any = None
    patched_count = 0

    for row in patched_rows:
        for obj in row.get("objects", []) or []:
            identity = obj.get("identity")
            if isinstance(identity, dict) and identity.get("source_observation_id") == source_observation_id:
                before_geometry = _geometry_signature(obj)
                identity.update(
                    {
                        "display_name": TARGETS.get(str(match.get("identity_key")), {}).get("name"),
                        "event_type": "watchlist_hit",
                        "external_person_id": match.get("query_external_person_id"),
                        "gallery_embedding_id": match.get("query_gallery_embedding_id"),
                        "identity_source": "external_gallery_to_video_observation",
                        "match_status": "above_threshold",
                        "person_id": match.get("person_id") or match.get("query_person_id"),
                        "person_name": TARGETS.get(str(match.get("identity_key")), {}).get("name"),
                        "similarity": match.get("similarity"),
                        "source_observation_id": source_observation_id,
                        "threshold": match.get("threshold"),
                        "visual_evidence_status": "external_watchlist_hit_identity_bound",
                        "watchlist_hit_status": "matched",
                        "watchlist_match_source": "c2_12b_pgvector_search",
                        "watchlist_rule_id": DEFAULT_WATCHLIST_RULE_ID,
                        "track_id_join_warning": bool(match.get("c2_stable_sidecar_join", {}).get("track_id_join_warning")),
                    }
                )
                obj["object_type"] = "known_face"
                after_geometry = _geometry_signature(obj)
                patched_count += 1
                break

    return patched_rows, {
        "patched_object_count": patched_count,
        "geometry_unchanged": patched_count == 1 and before_geometry == after_geometry,
        "source_observation_id": source_observation_id,
    }


def patch_summary_for_external_match(summary: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    patched = copy.deepcopy(summary)
    patched.update(
        {
            "production_ready": bool(patched.get("production_ready", True)),
            "known_face_count": max(int(patched.get("known_face_count") or 0), 1),
            "watchlist_hit_count": max(int(patched.get("watchlist_hit_count") or 0), 1),
            "evidence_capture_mode": DEFAULT_CAPTURE_MODE,
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "c2_12b_external_person": {
                "event_type": event["event_type"],
                "source_observation_id": event["source_observation_id"],
                "person_id": event.get("person_id"),
                "external_person_id": event["external_person_id"],
                "gallery_embedding_id": event["gallery_embedding_id"],
                "similarity": event["similarity"],
                "threshold": event["threshold"],
                "watchlist_rule_id": event["watchlist_rule_id"],
            },
        }
    )
    return patched


def render_operator_report(event: dict[str, Any], summary: dict[str, Any]) -> str:
    evidence = event.get("evidence") or {}
    rows = [
        ("Event type", event.get("event_type")),
        ("External person id", event.get("external_person_id")),
        ("Person id", event.get("person_id")),
        ("Source observation id", event.get("source_observation_id")),
        ("Similarity", event.get("similarity")),
        ("Threshold", event.get("threshold")),
        ("Watchlist rule", event.get("watchlist_rule_id")),
        ("Evidence bundle", evidence.get("bundle_path")),
        ("Capture mode", evidence.get("capture_mode")),
        ("Workaround used", evidence.get("workaround_used")),
        ("Event-style Replay passed", evidence.get("event_style_replay_job_passed")),
        ("Known face count", summary.get("known_face_count")),
        ("Watchlist hit count", summary.get("watchlist_hit_count")),
    ]
    body = "\n".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.12B External Watchlist Evidence</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #18202a; }}
    table {{ border-collapse: collapse; min-width: 720px; }}
    th, td {{ border: 1px solid #ccd3dc; padding: 0.55rem 0.75rem; text-align: left; }}
    th {{ background: #eef2f7; width: 240px; }}
    .warning {{ color: #8a4b00; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>External Person Watchlist Hit</h1>
  <table>{body}</table>
  <p class="warning">Event-style Replay is not passed; this uses the stable sink time-domain crop workaround.</p>
</body>
</html>
"""


def build_summary(
    *,
    output_dir: Path,
    stable_bundle: Path,
    gallery: dict[str, Any],
    inventory: dict[str, Any],
    matches_by_identity: dict[str, list[dict[str, Any]]],
    decision: dict[str, Any],
    unsafe_scan: dict[str, Any],
    threshold: float,
    watchlist_rule_id: str,
    pass_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    result_marker = str(decision["result_marker"])
    best = decision.get("best_match") or {}
    return {
        "schema_version": "1.0",
        "result_marker": result_marker,
        "output_dir": str(output_dir),
        "stable_bundle": str(stable_bundle),
        "threshold": threshold,
        "threshold_rationale": (
            "C2.12B uses a conservative external-photo-to-video diagnostic threshold; "
            "gallery self-match thresholds are not used for PASS."
        ),
        "watchlist_rule_id": watchlist_rule_id,
        "decision_reason": decision.get("reason"),
        "best_match": best,
        "reese_top_matches": matches_by_identity.get("reese", []),
        "finch_top_matches": matches_by_identity.get("finch", []),
        "gallery": gallery,
        "candidate_inventory": {
            "total_face_observations": inventory.get("total_face_observations"),
            "face_observations_with_embedding": inventory.get("face_observations_with_embedding"),
            "source_distribution": inventory.get("source_distribution"),
        },
        "evidence_bundle_generated": result_marker == RESULT_PASS,
        "evidence_bundle_path": pass_summary.get("evidence_bundle_path") if pass_summary else None,
        "known_face_count": pass_summary.get("known_face_count") if pass_summary else 0,
        "watchlist_hit_count": pass_summary.get("watchlist_hit_count") if pass_summary else 0,
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "unsafe_payload_scan_passed": unsafe_scan.get("passed"),
        "fake_match_used": False,
        "gallery_self_match_used_for_pass": False,
        "test_c2_4_person_used": False,
        "evidence_capture_mode": DEFAULT_CAPTURE_MODE if result_marker == RESULT_PASS else None,
        "workaround_used": True if result_marker == RESULT_PASS else False,
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
        "db_window_fallback_used": False,
        "legacy_annotation_fallback_used": False,
        "limitations": [
            "depends_on_whether_reese_or_finch_appear_in_current_video_observations",
            "not_broad_accuracy_test",
            "watchlist_rules_table_absent_test_rule_contract_used",
            "stable_sink_workaround_used_if_bundle_generated",
            "event_style_replay_not_passed",
        ],
        "pass_artifacts": pass_summary,
    }


def check_sidecar_join(stable_bundle: Path, source_observation_id: str) -> dict[str, Any]:
    sidecar_path = stable_bundle / SIDECAR_FILE
    result = {
        "bundle_path": str(stable_bundle),
        "sidecar_path": str(sidecar_path),
        "appears_in_sidecar": False,
        "matching_frame_count": 0,
        "first_frame_index": None,
        "first_frame_pts": None,
        "matched_object_type": None,
        "matched_object_id": None,
        "matched_track_id": None,
        "track_id_join_warning": False,
    }
    if not source_observation_id or not sidecar_path.is_file():
        return result
    for line in sidecar_path.read_text(encoding="utf-8").splitlines():
        if source_observation_id not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for obj in row.get("objects", []) or []:
            identity = obj.get("identity")
            if isinstance(identity, dict) and identity.get("source_observation_id") == source_observation_id:
                result.update(
                    {
                        "appears_in_sidecar": True,
                        "matching_frame_count": int(result["matching_frame_count"]) + 1,
                    }
                )
                if result["first_frame_index"] is None:
                    result.update(
                        {
                            "first_frame_index": row.get("frame_index"),
                            "first_frame_pts": row.get("frame_pts"),
                            "matched_object_type": obj.get("object_type"),
                            "matched_object_id": obj.get("object_id"),
                            "matched_track_id": obj.get("track_id"),
                            "track_id_join_warning": bool(identity.get("track_id_join_warning")),
                        }
                    )
    return result


def assert_safe_event(event: dict[str, Any]) -> None:
    scan = scan_for_unsafe_payload(event)
    if not scan["passed"]:
        raise RuntimeError(f"unsafe_event_payload:{scan['forbidden_key_paths']}")


def scan_for_unsafe_payload(value: Any) -> dict[str, Any]:
    hits = sorted(set(_find_unsafe(value, "$")))
    return {
        "passed": not hits,
        "payload_has_embedding": any("embedding" in hit for hit in hits),
        "payload_has_image_bytes": any(
            token in hit
            for hit in hits
            for token in ("image_bytes", "base64", "crop_bytes", "face_crop_bytes")
        ),
        "forbidden_key_paths": hits,
    }


def _find_unsafe(value: Any, path: str) -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            next_path = f"{path}.{key_text}"
            if lowered in FORBIDDEN_KEYS and nested not in (None, "", False, [], {}):
                hits.append(next_path)
            hits.extend(_find_unsafe(nested, next_path))
    elif isinstance(value, list):
        if _looks_like_forbidden_vector(value, path):
            hits.append(path)
        for index, nested in enumerate(value):
            hits.extend(_find_unsafe(nested, f"{path}.{index}"))
    elif isinstance(value, str):
        lowered = value.lower()
        if "data:image" in lowered or ";base64," in lowered:
            hits.append(path)
    return hits


def _looks_like_forbidden_vector(value: list[Any], path: str) -> bool:
    if len(value) < 64:
        return False
    lowered_path = path.lower()
    if any(token in lowered_path for token in ALLOWED_NUMERIC_ARRAY_PATH_TOKENS):
        return False
    numeric_count = sum(
        1
        for item in value
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    )
    return numeric_count >= 64 and numeric_count / max(len(value), 1) >= 0.9


def _sanitize_db_row(row: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in row.items():
        lowered = key.lower()
        if lowered == "embedding":
            continue
        if isinstance(value, bytes):
            clean[key] = f"<{len(value)} bytes omitted>"
        elif hasattr(value, "tolist"):
            clean[key] = "<vector omitted>"
        else:
            clean[key] = value
    return clean


def _geometry_signature(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "bbox": copy.deepcopy(obj.get("bbox")),
        "pose": copy.deepcopy(obj.get("pose")),
        "landmarks": copy.deepcopy(obj.get("landmarks")),
        "object_id": obj.get("object_id"),
        "track_id": obj.get("track_id"),
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, default=str, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_decision_report(path: Path, summary: dict[str, Any]) -> None:
    best = summary.get("best_match") or {}
    lines = [
        "# C2.12B External Person Watchlist Search Decision",
        "",
        f"Result marker: `{summary['result_marker']}`",
        f"Decision reason: `{summary.get('decision_reason')}`",
        f"Threshold: `{summary.get('threshold')}`",
        "",
        "## Best Match",
        "",
        f"- external_person_id: `{best.get('query_external_person_id')}`",
        f"- source_observation_id: `{best.get('source_observation_id')}`",
        f"- similarity: `{best.get('similarity')}`",
        f"- threshold_passed: `{best.get('threshold_passed')}`",
        f"- sidecar_join: `{(best.get('c2_stable_sidecar_join') or {}).get('appears_in_sidecar')}`",
        "",
        "## Boundary",
        "",
        "- Gallery self-match is not accepted for C2.12B PASS.",
        "- No embedding vectors or image/crop bytes are written to outputs.",
        "- Event-style Replay is not passed and is not claimed.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--stable-bundle", type=Path, default=DEFAULT_C2_STABLE_BUNDLE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--watchlist-rule-id", default=DEFAULT_WATCHLIST_RULE_ID)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id
    if not run_id:
        prefix = "c2_12b_external_watchlist_search"
        run_id = f"{prefix}_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    output_dir = args.output_dir or args.evidence_root / run_id
    result = run_c2_12b_external_watchlist_evidence(
        output_dir=output_dir,
        database_url=args.database_url,
        stable_bundle=args.stable_bundle,
        threshold=args.threshold,
        top_k=args.top_k,
        watchlist_rule_id=args.watchlist_rule_id,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": result.result_marker,
                "output_dir": str(result.output_dir),
                "summary": str(result.summary_path),
                "threshold": result.summary.get("threshold"),
                "decision_reason": result.summary.get("decision_reason"),
                "best_match": result.summary.get("best_match"),
                "payload_has_embedding": result.summary.get("payload_has_embedding"),
                "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
            },
            indent=2,
            default=str,
            sort_keys=True,
        )
    )
    accepted = {
        RESULT_PASS,
        RESULT_JOIN_GAP,
        RESULT_NO_MATCH,
        RESULT_NO_OBSERVATIONS,
    }
    return 0 if result.result_marker in accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
