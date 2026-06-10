from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "services" / "face-worker" / "register_face_image.py"
DEFAULT_TEST_IMAGE = Path(
    "/tmp/Savant-upstream/samples/face_reid/assets/gallery/kevin_hart_01.jpeg"
)
PREFIX = "demo:f4_2c:"


@pytest.fixture(scope="module")
def f42c_env() -> dict[str, str]:
    database_url = os.environ.get("DATABASE_URL")
    yolo = os.environ.get("YOLOV8_FACE_ONNX")
    adaface = os.environ.get("ADAFACE_ONNX")
    image = Path(os.environ.get("F4_2_TEST_IMAGE", str(DEFAULT_TEST_IMAGE)))

    missing = []
    if not database_url:
        missing.append("DATABASE_URL")
    if not yolo or not Path(yolo).is_file():
        missing.append("YOLOV8_FACE_ONNX")
    if not adaface or not Path(adaface).is_file():
        missing.append("ADAFACE_ONNX")
    if not image.is_file():
        missing.append("F4_2_TEST_IMAGE")
    if missing:
        pytest.skip("F4.2c integration skipped; missing " + ", ".join(missing))

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["YOLOV8_FACE_ONNX"] = yolo
    env["ADAFACE_ONNX"] = adaface
    env["F4_2_TEST_IMAGE"] = str(image)

    _cleanup_prefix(database_url)
    return env


def _cleanup_prefix(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM person_gallery_embeddings pge
                USING persons p
                WHERE pge.person_id = p.id
                  AND p.external_person_id LIKE %(prefix)s
                """,
                {"prefix": f"{PREFIX}%"},
            )
            cur.execute(
                """
                DELETE FROM persons
                WHERE external_person_id LIKE %(prefix)s
                """,
                {"prefix": f"{PREFIX}%"},
            )


def _run_registration(
    env: dict[str, str],
    external_person_id: str,
    *,
    is_primary: bool = False,
    image: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    cmd = [
        sys.executable,
        str(CLI),
        "--image",
        image or env["F4_2_TEST_IMAGE"],
        "--external-person-id",
        external_person_id,
        "--name",
        external_person_id,
        "--output-json",
    ]
    if is_primary:
        cmd.append("--is-primary")
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not emit JSON. rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        ) from exc
    return proc.returncode, payload


def _fetch_person(database_url: str, external_person_id: str) -> dict | None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, external_person_id, name
                FROM persons
                WHERE external_person_id = %(external_person_id)s
                """,
                {"external_person_id": external_person_id},
            )
            return cur.fetchone()


def _fetch_galleries(database_url: str, person_id: int) -> list[dict]:
    with psycopg.connect(database_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, person_id, is_primary, is_active, source_type,
                       source_observation_id, embedding_model, embedding_dim,
                       embedding_norm, payload
                FROM person_gallery_embeddings
                WHERE person_id = %(person_id)s
                ORDER BY id
                """,
                {"person_id": person_id},
            )
            return list(cur.fetchall())


def test_duplicate_external_person_id_reuses_person(f42c_env: dict[str, str]) -> None:
    external_id = f"{PREFIX}duplicate_person"
    rc1, first = _run_registration(f42c_env, external_id)
    rc2, second = _run_registration(f42c_env, external_id)

    assert rc1 == 0
    assert rc2 == 0
    assert first["status"] == "REGISTERED"
    assert second["status"] == "REGISTERED"
    assert first["person_id"] == second["person_id"]
    assert first["person_reused"] is False
    assert second["person_reused"] is True

    person = _fetch_person(f42c_env["DATABASE_URL"], external_id)
    assert person is not None
    galleries = _fetch_galleries(f42c_env["DATABASE_URL"], int(person["id"]))
    assert len([g for g in galleries if g["is_active"]]) == 2


def test_primary_registration_replaces_existing_primary(f42c_env: dict[str, str]) -> None:
    external_id = f"{PREFIX}primary_person"
    rc1, first = _run_registration(f42c_env, external_id, is_primary=True)
    rc2, second = _run_registration(f42c_env, external_id, is_primary=True)

    assert rc1 == 0
    assert rc2 == 0
    assert first["gallery_embedding_id"] != second["gallery_embedding_id"]

    galleries = _fetch_galleries(f42c_env["DATABASE_URL"], int(second["person_id"]))
    active_primaries = [
        g for g in galleries if g["is_active"] and g["is_primary"]
    ]
    assert len(active_primaries) == 1
    assert int(active_primaries[0]["id"]) == int(second["gallery_embedding_id"])

    old = next(g for g in galleries if int(g["id"]) == int(first["gallery_embedding_id"]))
    assert old["is_active"] is True
    assert old["is_primary"] is False


def test_non_primary_multi_gallery_does_not_disturb_primary(
    f42c_env: dict[str, str],
) -> None:
    external_id = f"{PREFIX}non_primary_person"
    rc1, primary = _run_registration(f42c_env, external_id, is_primary=True)
    rc2, non_primary_1 = _run_registration(f42c_env, external_id)
    rc3, non_primary_2 = _run_registration(f42c_env, external_id)

    assert rc1 == rc2 == rc3 == 0
    galleries = _fetch_galleries(f42c_env["DATABASE_URL"], int(primary["person_id"]))
    assert len([g for g in galleries if g["is_active"]]) == 3
    active_primaries = [
        g for g in galleries if g["is_active"] and g["is_primary"]
    ]
    assert len(active_primaries) == 1
    assert int(active_primaries[0]["id"]) == int(primary["gallery_embedding_id"])
    assert non_primary_1["is_primary"] is False
    assert non_primary_2["is_primary"] is False


def test_video_input_is_rejected_without_db_write(f42c_env: dict[str, str]) -> None:
    external_id = f"{PREFIX}bad_video"
    rc, payload = _run_registration(
        f42c_env,
        external_id,
        image=str(ROOT / "testVideo" / "allface.mp4"),
    )

    assert rc == 1
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "IMAGE_FILE_TYPE_UNSUPPORTED"
    assert _fetch_person(f42c_env["DATABASE_URL"], external_id) is None


def test_missing_model_returns_clear_error_without_db_write(
    f42c_env: dict[str, str],
) -> None:
    external_id = f"{PREFIX}missing_model"
    rc, payload = _run_registration(
        f42c_env,
        external_id,
        extra_args=[
            "--face-detector-onnx",
            "/tmp/missing_yolov8_face.onnx",
            "--adaface-onnx",
            "/tmp/missing_adaface.onnx",
        ],
    )

    assert rc == 1
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "MODEL_FILE_NOT_FOUND"
    assert _fetch_person(f42c_env["DATABASE_URL"], external_id) is None


def test_low_quality_failure_does_not_write_gallery(f42c_env: dict[str, str]) -> None:
    external_id = f"{PREFIX}low_quality"
    rc, payload = _run_registration(
        f42c_env,
        external_id,
        extra_args=["--quality-threshold", "1.1"],
    )

    assert rc == 1
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "QUALITY_TOO_LOW"
    assert _fetch_person(f42c_env["DATABASE_URL"], external_id) is None
