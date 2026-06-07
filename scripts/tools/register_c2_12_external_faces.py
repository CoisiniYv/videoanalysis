#!/usr/bin/env python3
"""C2.12A external Reese / Finch face enrollment rebuild."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
if str(FACE_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(FACE_WORKER_ROOT))

from app.image_face_registration import (  # noqa: E402
    ERROR_MODEL_FILE_NOT_FOUND,
    ERROR_REAL_EMBEDDING_UNAVAILABLE,
    RegistrationRequest,
    register_external_image,
)
from app.vector_store import FaceVectorStore  # noqa: E402


INPUT_DIR = Path("/data/video-analytics/media/face-registration")
EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_FACE_DETECTOR_ONNX = Path("/data/video-analytics/models/yolov8_face/yolov8n-face.onnx")
DEFAULT_ADAFACE_ONNX = Path("/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx")
DEFAULT_PROVIDER_SPEC = "CUDAExecutionProvider,CPUExecutionProvider"

RESULT_PASS = "PASS_C2_12A_EXTERNAL_FACE_ENROLLMENT_READY"
RESULT_IMAGE_GAP = "PARTIAL_C2_12A_IMAGE_DISCOVERY_GAP"
RESULT_TOOL_GAP = "PARTIAL_C2_12A_EXTERNAL_EMBEDDING_TOOL_GAP"
RESULT_ONE_PERSON = "PARTIAL_C2_12A_ONE_PERSON_REGISTERED"
RESULT_FAIL = "FAIL_C2_12A_EXTERNAL_ENROLLMENT_BLOCKED"

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
TARGETS = {
    "reese": {
        "name": "Reese",
        "external_person_id": "demo:f4_3:reese",
        "patterns": ("reese", "john_reese", "john-reese", "johnreese", "demo:f4_3:reese"),
    },
    "finch": {
        "name": "Finch",
        "external_person_id": "demo:f4_3:finch",
        "patterns": ("finch", "harold_finch", "harold-finch", "haroldfinch", "demo:f4_3:finch"),
    },
}


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    output_dir: Path
    summary_path: Path
    summary: dict[str, Any]


def run_c2_12a_external_enrollment(
    *,
    input_dir: Path,
    output_dir: Path,
    database_url: str,
    face_detector_onnx: Path,
    adaface_onnx: Path,
    onnx_provider: str,
    quality_threshold: float,
    overwrite: bool = False,
) -> BuildResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"output directory already exists and is not empty: {output_dir}")

    inventory = discover_images(input_dir)
    _write_json(output_dir / "enrollment_inventory.json", inventory)

    results: list[dict[str, Any]] = []
    if _selected_identity_count(inventory) == 2:
        os.environ["DATABASE_URL"] = database_url
        for identity_key, target in TARGETS.items():
            selected = [item for item in inventory if item.get("identity_key") == identity_key and item["selected_for_registration"]]
            selected.sort(key=lambda item: item["path"])
            for index, item in enumerate(selected):
                result = register_or_reuse_image(
                    image_path=Path(item["path"]),
                    target=target,
                    database_url=database_url,
                    face_detector_onnx=face_detector_onnx,
                    adaface_onnx=adaface_onnx,
                    onnx_provider=onnx_provider,
                    quality_threshold=quality_threshold,
                    prefer_primary=index == 0,
                )
                result["identity_key"] = identity_key
                result["inventory_path"] = item["path"]
                results.append(result)
    _write_json(output_dir / "enrollment_results.json", {"results": results})

    db_persons = fetch_db_persons(database_url)
    db_gallery = fetch_db_gallery(database_url)
    _write_json(output_dir / "db_persons_after.json", db_persons)
    _write_json(output_dir / "db_gallery_after.json", db_gallery)

    self_check = run_gallery_self_check(database_url, db_gallery)
    unsafe_payload_scan = scan_db_payloads(db_persons, db_gallery)
    _write_json(output_dir / "gallery_self_check.json", self_check)
    _write_json(output_dir / "unsafe_payload_scan.json", unsafe_payload_scan)

    summary = build_summary(
        input_dir=input_dir,
        output_dir=output_dir,
        inventory=inventory,
        results=results,
        db_persons=db_persons,
        db_gallery=db_gallery,
        self_check=self_check,
        unsafe_payload_scan=unsafe_payload_scan,
        face_detector_onnx=face_detector_onnx,
        adaface_onnx=adaface_onnx,
        onnx_provider=onnx_provider,
    )
    _write_quality_report(output_dir / "enrollment_quality_report.md", summary)
    summary_path = output_dir / "c2_12a_external_enrollment_summary.json"
    _write_json(summary_path, summary)
    return BuildResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=summary_path,
        summary=summary,
    )


def discover_images(input_dir: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    if not input_dir.is_dir():
        return []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        identity_key, inferred_identity, reason = infer_identity(path)
        dimensions = read_image_dimensions(path)
        readable = dimensions is not None
        selected = bool(identity_key and readable)
        inventory.append(
            {
                "path": str(path),
                "file_size": path.stat().st_size,
                "extension": path.suffix.lower(),
                "image_dimensions": dimensions,
                "inferred_identity": inferred_identity,
                "identity_key": identity_key,
                "readable": readable,
                "selected_for_registration": selected,
                "reason": reason if selected else (reason or "identity_not_recognized_or_unreadable"),
            }
        )
    return inventory


def infer_identity(path: Path) -> tuple[str | None, str | None, str]:
    haystack = f"{path.parent.name} {path.stem} {path.name}".lower().replace(" ", "_")
    for identity_key, target in TARGETS.items():
        for pattern in target["patterns"]:
            normalized = pattern.lower().replace(":", "_").replace("-", "_")
            if normalized in haystack.replace(":", "_").replace("-", "_"):
                return identity_key, target["name"], f"matched_pattern:{pattern}"
    return None, None, "no_reese_finch_pattern"


def read_image_dimensions(path: Path) -> dict[str, int] | None:
    image = cv2.imread(str(path))
    if image is None:
        return None
    height, width = image.shape[:2]
    return {"width": int(width), "height": int(height)}


def register_or_reuse_image(
    *,
    image_path: Path,
    target: dict[str, Any],
    database_url: str,
    face_detector_onnx: Path,
    adaface_onnx: Path,
    onnx_provider: str,
    quality_threshold: float,
    prefer_primary: bool,
) -> dict[str, Any]:
    existing = find_existing_gallery_for_image(database_url, target["external_person_id"], image_path)
    if existing:
        return {
            "registration_status": "reused_existing_gallery",
            "status": "REGISTERED",
            "person_id": existing["person_id"],
            "external_person_id": target["external_person_id"],
            "name": existing["name"],
            "gallery_embedding_id": existing["gallery_embedding_id"],
            "embedding_model": existing["embedding_model"],
            "model_version": existing["model_version"],
            "embedding_dim": existing["embedding_dim"],
            "embedding_norm": existing["embedding_norm"],
            "quality": existing["quality"],
            "face_bbox": existing["face_bbox"],
            "landmarks": existing["landmarks"],
            "source_image_path": existing["source_image_path"],
            "real_embedding_used": _payload_bool(existing.get("payload"), "real_embedding_used", True),
            "dev_mock_used": _payload_bool(existing.get("payload"), "dev_mock_used", False),
            "fallback_used": _payload_bool(existing.get("payload"), "storage_fallback_used", False),
            "fake_embedding_used": False,
            "image_bytes_stored": False,
            "redis_image_bytes_used": False,
            "reused_existing": True,
        }

    has_primary = person_has_active_primary(database_url, target["external_person_id"])
    request = RegistrationRequest(
        image_path=str(image_path),
        external_person_id=target["external_person_id"],
        name=target["name"],
        person_id=None,
        description="C2.12A external face enrollment rebuild",
        source_type="manual_upload",
        is_primary=prefer_primary and not has_primary,
        quality_threshold=quality_threshold,
        allow_multiple_faces=False,
        keep_crop=False,
        created_by="c2_12a_external_enrollment",
        dev_mock_embedding_fixture=None,
        face_detector_onnx=str(face_detector_onnx),
        adaface_onnx=str(adaface_onnx),
        onnx_provider=onnx_provider,
    )
    result = register_external_image(request).to_dict()
    result["registration_status"] = "registered" if result.get("status") == "REGISTERED" else "rejected"
    result["fake_embedding_used"] = bool(result.get("dev_mock_used") or result.get("fallback_used"))
    result["image_bytes_stored"] = False
    result["redis_image_bytes_used"] = False
    result["reused_existing"] = False
    return result


def find_existing_gallery_for_image(
    database_url: str,
    external_person_id: str,
    image_path: Path,
) -> dict[str, Any] | None:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS person_id, p.name, p.external_person_id,
                       pge.id AS gallery_embedding_id, pge.embedding_model,
                       pge.model_version, pge.embedding_dim, pge.embedding_norm,
                       pge.quality, pge.face_bbox, pge.landmarks,
                       pge.source_image_path, pge.payload
                FROM persons p
                JOIN person_gallery_embeddings pge ON pge.person_id = p.id
                WHERE p.external_person_id = %(external_person_id)s
                  AND p.is_active = true
                  AND pge.is_active = true
                  AND pge.source_image_path = %(source_image_path)s
                ORDER BY pge.is_primary DESC, pge.id DESC
                LIMIT 1
                """,
                {
                    "external_person_id": external_person_id,
                    "source_image_path": str(image_path.resolve()),
                },
            )
            row = cur.fetchone()
    return dict(row) if row else None


def person_has_active_primary(database_url: str, external_person_id: str) -> bool:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bool_or(pge.is_active AND pge.is_primary) AS has_primary
                FROM persons p
                LEFT JOIN person_gallery_embeddings pge ON pge.person_id = p.id
                WHERE p.external_person_id = %(external_person_id)s
                """,
                {"external_person_id": external_person_id},
            )
            row = cur.fetchone()
    return bool(row and row["has_primary"])


def fetch_db_persons(database_url: str) -> dict[str, Any]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id AS person_id, name, external_person_id, is_active,
                       created_at, updated_at, payload
                FROM persons
                WHERE external_person_id = ANY(%(external_ids)s)
                ORDER BY external_person_id
                """,
                {"external_ids": [target["external_person_id"] for target in TARGETS.values()]},
            )
            rows = [dict(row) for row in cur.fetchall()]
    return {"persons": rows}


def fetch_db_gallery(database_url: str) -> dict[str, Any]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS person_id, p.name, p.external_person_id, p.is_active AS person_active,
                       pge.id AS gallery_embedding_id, pge.source_type,
                       pge.source_image_path, pge.source_observation_id,
                       pge.embedding_model, pge.model_version, pge.embedding_dim,
                       pge.embedding IS NOT NULL AS embedding_present,
                       pge.embedding_norm, pge.quality, pge.face_bbox, pge.landmarks,
                       pge.is_primary, pge.is_active AS gallery_active,
                       pge.payload, pge.created_at, pge.updated_at
                FROM persons p
                LEFT JOIN person_gallery_embeddings pge ON pge.person_id = p.id
                WHERE p.external_person_id = ANY(%(external_ids)s)
                ORDER BY p.external_person_id, pge.is_active DESC NULLS LAST,
                         pge.is_primary DESC NULLS LAST, pge.id
                """,
                {"external_ids": [target["external_person_id"] for target in TARGETS.values()]},
            )
            rows = [dict(row) for row in cur.fetchall()]
    return {"gallery": rows}


def run_gallery_self_check(database_url: str, db_gallery: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    active_ids = [
        int(row["gallery_embedding_id"])
        for row in db_gallery.get("gallery", [])
        if row.get("gallery_embedding_id") and row.get("gallery_active") is True
    ]
    if not active_ids:
        return {"self_check_passed": False, "checks": checks, "reason": "no_active_gallery_embeddings"}

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        store = FaceVectorStore(conn)
        with conn.cursor() as cur:
            for gallery_id in active_ids:
                cur.execute(
                    """
                    SELECT p.external_person_id, p.name, pge.id AS gallery_embedding_id,
                           pge.embedding
                    FROM person_gallery_embeddings pge
                    JOIN persons p ON p.id = pge.person_id
                    WHERE pge.id = %(gallery_id)s
                    """,
                    {"gallery_id": gallery_id},
                )
                row = cur.fetchone()
                if row is None or row["embedding"] is None:
                    checks.append({"gallery_embedding_id": gallery_id, "passed": False, "reason": "embedding_missing"})
                    continue
                embedding = row["embedding"]
                if hasattr(embedding, "tolist"):
                    embedding_list = [float(x) for x in embedding.tolist()]
                else:
                    embedding_list = [float(x) for x in embedding]
                results = store.search_gallery(embedding_list, top_k=5, min_similarity=0.0)
                top = results[0] if results else None
                checks.append(
                    {
                        "gallery_embedding_id": gallery_id,
                        "query_external_person_id": row["external_person_id"],
                        "top_gallery_embedding_id": top.get("id") if top else None,
                        "top_person_id": top.get("person_id") if top else None,
                        "top_external_person_id": top.get("external_person_id") if top else None,
                        "top_similarity": top.get("similarity") if top else None,
                        "passed": bool(
                            top
                            and top.get("external_person_id") == row["external_person_id"]
                            and float(top.get("similarity") or 0.0) >= 0.99
                        ),
                    }
                )
    target_pass = {
        target["external_person_id"]: any(
            check.get("query_external_person_id") == target["external_person_id"] and check.get("passed") is True
            for check in checks
        )
        for target in TARGETS.values()
    }
    return {"self_check_passed": all(target_pass.values()), "target_pass": target_pass, "checks": checks}


def scan_db_payloads(db_persons: dict[str, Any], db_gallery: dict[str, Any]) -> dict[str, Any]:
    hits: list[str] = []
    for group_name, rows in (("persons", db_persons.get("persons", [])), ("gallery", db_gallery.get("gallery", []))):
        for index, row in enumerate(rows):
            for field_name in ("payload",):
                hits.extend(_find_unsafe(row.get(field_name), f"{group_name}.{index}.{field_name}"))
    hits = sorted(set(hits))
    return {
        "passed": not hits,
        "payload_has_embedding": any("embedding" in hit for hit in hits),
        "payload_has_image_bytes": any(token in hit for hit in hits for token in ("image_bytes", "base64", "crop_bytes")),
        "forbidden_key_paths": hits,
    }


def build_summary(
    *,
    input_dir: Path,
    output_dir: Path,
    inventory: list[dict[str, Any]],
    results: list[dict[str, Any]],
    db_persons: dict[str, Any],
    db_gallery: dict[str, Any],
    self_check: dict[str, Any],
    unsafe_payload_scan: dict[str, Any],
    face_detector_onnx: Path,
    adaface_onnx: Path,
    onnx_provider: str,
) -> dict[str, Any]:
    person_summaries = []
    for identity_key, target in TARGETS.items():
        person_rows = [
            row for row in db_persons.get("persons", [])
            if row.get("external_person_id") == target["external_person_id"]
        ]
        gallery_rows = [
            row for row in db_gallery.get("gallery", [])
            if row.get("external_person_id") == target["external_person_id"]
            and row.get("gallery_active") is True
        ]
        result_rows = [row for row in results if row.get("identity_key") == identity_key]
        person_summaries.append(
            {
                "name": target["name"],
                "external_person_id": target["external_person_id"],
                "person_id": person_rows[0].get("person_id") if person_rows else None,
                "active": person_rows[0].get("is_active") if person_rows else False,
                "registered_images": sorted({row.get("source_image_path") or row.get("inventory_path") for row in result_rows if row.get("source_image_path") or row.get("inventory_path")}),
                "gallery_embedding_ids": [row.get("gallery_embedding_id") for row in gallery_rows if row.get("gallery_embedding_id")],
                "embedding_dim": 512 if gallery_rows and all(row.get("embedding_dim") == 512 for row in gallery_rows) else None,
                "embedding_norms": [row.get("embedding_norm") for row in gallery_rows if row.get("embedding_norm") is not None],
                "embedding_model": gallery_rows[0].get("embedding_model") if gallery_rows else None,
                "model_version": gallery_rows[0].get("model_version") if gallery_rows else None,
                "quality": [row.get("quality") for row in gallery_rows if row.get("quality") is not None],
                "face_bbox": [row.get("face_bbox") for row in gallery_rows if row.get("face_bbox") is not None],
                "landmarks": [row.get("landmarks") for row in gallery_rows if row.get("landmarks") is not None],
                "registration_status": _person_registration_status(person_rows, gallery_rows, result_rows),
            }
        )

    fake_embedding_used = any(row.get("fake_embedding_used") is True or row.get("dev_mock_used") is True for row in results)
    image_gap = _selected_identity_count(inventory) < 2
    tool_gap = any(row.get("error_code") in {ERROR_REAL_EMBEDDING_UNAVAILABLE, ERROR_MODEL_FILE_NOT_FOUND} for row in results)
    one_person_registered = sum(1 for person in person_summaries if _person_ready(person)) == 1
    both_ready = all(_person_ready(person) for person in person_summaries)
    norms_valid = all(
        all(0.90 <= float(norm) <= 1.10 for norm in person.get("embedding_norms", []))
        and bool(person.get("embedding_norms"))
        for person in person_summaries
    )
    unsafe_ok = unsafe_payload_scan.get("passed") is True
    self_check_ok = self_check.get("self_check_passed") is True

    if image_gap:
        marker = RESULT_IMAGE_GAP
    elif tool_gap:
        marker = RESULT_TOOL_GAP
    elif both_ready and norms_valid and not fake_embedding_used and unsafe_ok and self_check_ok:
        marker = RESULT_PASS
    elif one_person_registered:
        marker = RESULT_ONE_PERSON
    else:
        marker = RESULT_FAIL

    return {
        "result_marker": marker,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "face_detector_onnx": str(face_detector_onnx),
        "adaface_onnx": str(adaface_onnx),
        "onnx_provider": onnx_provider,
        "persons": person_summaries,
        "db_write_performed": bool(results),
        "fake_embedding_used": fake_embedding_used,
        "image_bytes_stored": False,
        "redis_image_bytes_used": False,
        "unsafe_payload_scan_passed": unsafe_ok,
        "self_check_passed": self_check_ok,
        "self_check": self_check,
        "inventory_count": len(inventory),
        "selected_image_count": sum(1 for row in inventory if row.get("selected_for_registration")),
        "watchlist_rules_table_created": False,
        "watchlist_rules_required": False,
        "event_style_replay_job_passed": False,
        "limitations": [
            "not_new_video_watchlist_test",
            "not_broad_accuracy_test",
            "watchlist_rules_table_still_absent",
            "c2_12b_needed_for_new_video_evidence",
            "event_style_replay_not_passed",
        ],
    }


def _person_registration_status(
    person_rows: list[dict[str, Any]],
    gallery_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
) -> str:
    if person_rows and gallery_rows:
        if any(row.get("registration_status") == "registered" for row in result_rows):
            return "registered"
        if any(row.get("registration_status") == "reused_existing_gallery" for row in result_rows):
            return "reused_existing_gallery"
        return "registered"
    if result_rows:
        return str(result_rows[-1].get("registration_status") or result_rows[-1].get("status") or "failed")
    return "missing"


def _person_ready(person: dict[str, Any]) -> bool:
    return bool(
        person.get("person_id")
        and person.get("active") is True
        and person.get("gallery_embedding_ids")
        and person.get("embedding_dim") == 512
        and person.get("embedding_model") == "adaface"
    )


def _selected_identity_count(inventory: list[dict[str, Any]]) -> int:
    return len({row.get("identity_key") for row in inventory if row.get("selected_for_registration")})


def _payload_bool(payload: Any, key: str, default: bool) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return default
    if isinstance(payload, dict) and key in payload:
        value = payload[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "true"
    return default


def _find_unsafe(value: Any, path: str) -> list[str]:
    hits: list[str] = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            lowered = value.lower()
            if "data:image" in lowered or ";base64," in lowered:
                hits.append(path)
            return hits
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            next_path = f"{path}.{key}"
            if lowered in {"embedding", "embedding_vector", "embedding_values"} and nested not in (None, "", False, [], {}):
                hits.append(next_path)
            if lowered in {"image_bytes", "crop_bytes", "face_crop_bytes", "base64", "image_base64", "crop_base64"} and nested not in (None, "", False, [], {}):
                hits.append(next_path)
            hits.extend(_find_unsafe(nested, next_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            hits.extend(_find_unsafe(nested, f"{path}.{index}"))
    return hits


def _write_quality_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# C2.12A External Face Enrollment Quality Report",
        "",
        f"Result marker: `{summary['result_marker']}`",
        f"Input dir: `{summary['input_dir']}`",
        f"Face detector ONNX: `{summary['face_detector_onnx']}`",
        f"AdaFace ONNX: `{summary['adaface_onnx']}`",
        "",
        "## Persons",
    ]
    for person in summary["persons"]:
        lines.extend(
            [
                "",
                f"### {person['name']}",
                f"- external_person_id: `{person['external_person_id']}`",
                f"- person_id: `{person.get('person_id')}`",
                f"- status: `{person.get('registration_status')}`",
                f"- gallery_embedding_ids: `{person.get('gallery_embedding_ids')}`",
                f"- embedding_model: `{person.get('embedding_model')}`",
                f"- embedding_norms: `{person.get('embedding_norms')}`",
                f"- quality: `{person.get('quality')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Safety",
            f"- fake_embedding_used: `{summary['fake_embedding_used']}`",
            f"- image_bytes_stored: `{summary['image_bytes_stored']}`",
            f"- redis_image_bytes_used: `{summary['redis_image_bytes_used']}`",
            f"- unsafe_payload_scan_passed: `{summary['unsafe_payload_scan_passed']}`",
            "",
            "## Limitations",
        ]
    )
    lines.extend(f"- `{item}`" for item in summary["limitations"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=f"c2_12a_external_enrollment_{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--face-detector-onnx", type=Path, default=DEFAULT_FACE_DETECTOR_ONNX)
    parser.add_argument("--adaface-onnx", type=Path, default=DEFAULT_ADAFACE_ONNX)
    parser.add_argument("--onnx-provider", default=DEFAULT_PROVIDER_SPEC)
    parser.add_argument("--quality-threshold", type=float, default=0.65)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or args.evidence_root / args.run_id
    result = run_c2_12a_external_enrollment(
        input_dir=args.input_dir,
        output_dir=output_dir,
        database_url=args.database_url,
        face_detector_onnx=args.face_detector_onnx,
        adaface_onnx=args.adaface_onnx,
        onnx_provider=args.onnx_provider,
        quality_threshold=args.quality_threshold,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": result.result_marker,
                "output_dir": str(result.output_dir),
                "summary": str(result.summary_path),
                "persons": result.summary.get("persons"),
                "self_check_passed": result.summary.get("self_check_passed"),
                "fake_embedding_used": result.summary.get("fake_embedding_used"),
                "image_bytes_stored": result.summary.get("image_bytes_stored"),
                "redis_image_bytes_used": result.summary.get("redis_image_bytes_used"),
            },
            indent=2,
            default=str,
            sort_keys=True,
        )
    )
    return 0 if result.result_marker in {RESULT_PASS, RESULT_ONE_PERSON, RESULT_TOOL_GAP, RESULT_IMAGE_GAP} else 2


if __name__ == "__main__":
    raise SystemExit(main())
