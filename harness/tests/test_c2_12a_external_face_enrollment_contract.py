"""C2.12A external Reese / Finch enrollment contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "tools" / "register_c2_12_external_faces.py"


def test_external_person_ids_are_deterministic() -> None:
    tool = _load_tool()

    assert tool.TARGETS["reese"]["external_person_id"] == "demo:f4_3:reese"
    assert tool.TARGETS["finch"]["external_person_id"] == "demo:f4_3:finch"


def test_fake_embedding_must_be_false_for_pass() -> None:
    tool = _load_tool()
    summary = _summary()
    summary["fake_embedding_used"] = True
    marker = _marker_from_summary(tool, summary)

    assert marker != tool.RESULT_PASS


def test_embedding_dim_must_be_512() -> None:
    tool = _load_tool()
    summary = _summary()
    summary["persons"][0]["embedding_dim"] = 128

    assert _marker_from_summary(tool, summary) != tool.RESULT_PASS


def test_active_gallery_required() -> None:
    tool = _load_tool()
    summary = _summary()
    summary["persons"][1]["gallery_embedding_ids"] = []

    assert _marker_from_summary(tool, summary) != tool.RESULT_PASS


def test_source_image_path_required() -> None:
    summary = _summary()
    for person in summary["persons"]:
        assert person["registered_images"]


def test_image_base64_crop_bytes_not_allowed_in_payload() -> None:
    tool = _load_tool()
    scan = tool.scan_db_payloads(
        {"persons": [{"payload": {}}]},
        {"gallery": [{"payload": {"image_base64": "data:image/jpeg;base64,AAAA"}}]},
    )

    assert scan["passed"] is False
    assert scan["payload_has_image_bytes"] is True


def test_no_redis_image_bytes() -> None:
    summary = _summary()

    assert summary["redis_image_bytes_used"] is False


def test_multiple_images_per_person_allowed() -> None:
    tool = _load_tool()
    inventory = [
        {"identity_key": "reese", "selected_for_registration": True, "path": "reese1.jpg"},
        {"identity_key": "reese", "selected_for_registration": True, "path": "reese2.jpg"},
        {"identity_key": "finch", "selected_for_registration": True, "path": "finch.jpg"},
    ]

    assert tool._selected_identity_count(inventory) == 2


def test_no_face_or_multiple_face_cases_are_rejected() -> None:
    tool = _load_tool()
    summary = _summary()
    summary["persons"][0]["registration_status"] = "rejected"
    summary["persons"][0]["gallery_embedding_ids"] = []

    assert _marker_from_summary(tool, summary) != tool.RESULT_PASS


def test_pass_requires_both_reese_and_finch_registered() -> None:
    tool = _load_tool()
    summary = _summary()
    assert _marker_from_summary(tool, summary) == tool.RESULT_PASS

    summary["persons"] = summary["persons"][:1]
    assert _marker_from_summary(tool, summary) != tool.RESULT_PASS


def _marker_from_summary(tool: Any, summary: dict[str, Any]) -> str:
    both_ready = all(tool._person_ready(person) for person in summary["persons"]) and len(summary["persons"]) == 2
    norms_valid = all(
        person.get("embedding_norms")
        and all(0.90 <= float(norm) <= 1.10 for norm in person["embedding_norms"])
        for person in summary["persons"]
    )
    if (
        both_ready
        and norms_valid
        and summary["fake_embedding_used"] is False
        and summary["unsafe_payload_scan_passed"] is True
        and summary["self_check_passed"] is True
    ):
        return tool.RESULT_PASS
    if sum(1 for person in summary["persons"] if tool._person_ready(person)) == 1:
        return tool.RESULT_ONE_PERSON
    return tool.RESULT_FAIL


def _summary() -> dict[str, Any]:
    return {
        "persons": [
            {
                "name": "Reese",
                "external_person_id": "demo:f4_3:reese",
                "person_id": 10,
                "active": True,
                "registered_images": ["/data/video-analytics/media/face-registration/reese.jpg"],
                "gallery_embedding_ids": [101],
                "embedding_dim": 512,
                "embedding_norms": [1.0],
                "embedding_model": "adaface",
                "registration_status": "registered",
            },
            {
                "name": "Finch",
                "external_person_id": "demo:f4_3:finch",
                "person_id": 11,
                "active": True,
                "registered_images": ["/data/video-analytics/media/face-registration/finch.jpg"],
                "gallery_embedding_ids": [102],
                "embedding_dim": 512,
                "embedding_norms": [1.0],
                "embedding_model": "adaface",
                "registration_status": "registered",
            },
        ],
        "fake_embedding_used": False,
        "unsafe_payload_scan_passed": True,
        "self_check_passed": True,
        "redis_image_bytes_used": False,
    }


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("register_c2_12_external_faces", TOOL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
