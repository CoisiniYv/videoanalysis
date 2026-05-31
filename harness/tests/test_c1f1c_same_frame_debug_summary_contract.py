"""C1F.1c — Same-frame pose + face debug summary export contract tests.

Read-only checks for the debug probe implementation, schema, output path,
environment gate, and runtime boundaries.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MODULE_YAML = REPO / "modules" / "savant_security" / "module.yml"
PYFUNC_FILE = (
    REPO
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "same_frame_detection_debug.py"
)
COMPOSE_C1E = REPO / "infra" / "docker-compose.c1-official-replay-dev.yml"
AUDIT_DOC = REPO / "docs" / "c1f1c_same_frame_debug_summary_export.md"
C1F1A_DOC = REPO / "docs" / "c1f1a_dual_primary_current_state_audit.md"
OUTPUT_DIR = Path("/data/video-analytics/artifacts/c1f1")
OUTPUT_FILE = OUTPUT_DIR / "same_frame_pose_face_summary.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_module_yaml() -> dict:
    with open(MODULE_YAML) as f:
        return yaml.safe_load(f)


def _get_pipeline_elements(module: dict) -> list[dict]:
    pipeline = module.get("pipeline", {})
    if isinstance(pipeline, dict):
        return pipeline.get("elements", [])
    if isinstance(pipeline, list):
        return pipeline
    return []


def _find_element_by_name(module: dict, name: str) -> dict | None:
    elements = _get_pipeline_elements(module)
    for e in elements:
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def _element_index(module: dict, name: str) -> int:
    elements = _get_pipeline_elements(module)
    for i, e in enumerate(elements):
        if isinstance(e, dict) and e.get("name") == name:
            return i
    return -1


# ---------------------------------------------------------------------------
# 1. Pyfunc file exists and compiles
# ---------------------------------------------------------------------------

class TestPyfuncExists:
    def test_file_exists(self):
        assert PYFUNC_FILE.exists(), f"Pyfunc not found: {PYFUNC_FILE}"

    def test_syntax_valid(self):
        source = PYFUNC_FILE.read_text()
        import ast
        ast.parse(source)  # will raise SyntaxError if invalid


# ---------------------------------------------------------------------------
# 2. Module.yml registration
# ---------------------------------------------------------------------------

class TestModuleRegistration:
    def test_element_registered(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "same_frame_detection_debug")
        assert elem is not None, "same_frame_detection_debug not in module.yml"

    def test_element_type_is_pyfunc(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "same_frame_detection_debug")
        assert elem.get("element") == "pyfunc"

    def test_module_path_correct(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "same_frame_detection_debug")
        assert elem.get("module") == "custom.pyfuncs.same_frame_detection_debug"

    def test_class_name_correct(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "same_frame_detection_debug")
        assert elem.get("class_name") == "SameFrameDetectionDebugPyFunc"

    def test_after_face_person_associator(self):
        module = _load_module_yaml()
        idx_debug = _element_index(module, "same_frame_detection_debug")
        idx_assoc = _element_index(module, "face_person_associator")
        assert idx_debug > idx_assoc, (
            f"Debug probe must be after associator: debug={idx_debug}, assoc={idx_assoc}"
        )

    def test_after_adaface(self):
        module = _load_module_yaml()
        idx_debug = _element_index(module, "same_frame_detection_debug")
        idx_adaface = _element_index(module, "adaface")
        assert idx_debug > idx_adaface, (
            f"Debug probe must be after adaface: debug={idx_debug}, adaface={idx_adaface}"
        )

    def test_output_dir_kwarg(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "same_frame_detection_debug")
        kwargs = elem.get("kwargs", {})
        assert kwargs.get("output_dir") == "/data/video-analytics/artifacts/c1f1"


# ---------------------------------------------------------------------------
# 3. Environment gate
# ---------------------------------------------------------------------------

class TestEnvironmentGate:
    def test_gate_uses_c1f1_flag(self):
        source = PYFUNC_FILE.read_text()
        assert "C1F1_SAME_FRAME_DEBUG_ENABLED" in source

    def test_gate_default_disabled(self):
        source = PYFUNC_FILE.read_text()
        # Default should be False (not enabled unless env is set)
        assert "default: bool = False" in source or "default=False" in source

    def test_truthy_set_correct(self):
        source = PYFUNC_FILE.read_text()
        assert '"1"' in source and '"true"' in source


# ---------------------------------------------------------------------------
# 4. Output path and format
# ---------------------------------------------------------------------------

class TestOutputPathAndFormat:
    def test_default_output_dir(self):
        source = PYFUNC_FILE.read_text()
        assert "/data/video-analytics/artifacts/c1f1" in source

    def test_default_output_file(self):
        source = PYFUNC_FILE.read_text()
        assert "same_frame_pose_face_summary.jsonl" in source

    def test_creates_dir_if_missing(self):
        source = PYFUNC_FILE.read_text()
        assert "mkdir" in source and "parents=True" in source

    def test_output_format_jsonl(self):
        source = PYFUNC_FILE.read_text()
        # JSONL = one JSON per line
        assert "json.dumps" in source
        assert '\\n' in source or "write" in source


# ---------------------------------------------------------------------------
# 5. Schema fields
# ---------------------------------------------------------------------------

class TestSchemaFields:
    def test_source_id_in_output(self):
        source = PYFUNC_FILE.read_text()
        assert '"source_id"' in source

    def test_frame_uuid_in_output(self):
        source = PYFUNC_FILE.read_text()
        assert '"frame_uuid"' in source

    def test_frame_num_in_output(self):
        source = PYFUNC_FILE.read_text()
        assert '"frame_num"' in source

    def test_frame_pts_in_output(self):
        source = PYFUNC_FILE.read_text()
        assert '"frame_pts"' in source

    def test_timestamp_ms_in_output(self):
        source = PYFUNC_FILE.read_text()
        assert '"timestamp_ms"' in source

    def test_pose_section(self):
        source = PYFUNC_FILE.read_text()
        assert '"pose"' in source
        assert '"person_count"' in source
        assert '"persons"' in source

    def test_face_section(self):
        source = PYFUNC_FILE.read_text()
        assert '"face"' in source
        assert '"face_count"' in source
        assert '"faces"' in source

    def test_association_section(self):
        source = PYFUNC_FILE.read_text()
        assert '"association"' in source
        assert '"matched_face_person_pairs"' in source
        assert '"unmatched_face_count"' in source
        assert '"reason_if_no_match"' in source

    def test_person_fields(self):
        source = PYFUNC_FILE.read_text()
        assert '"track_id"' in source
        assert '"bbox"' in source
        assert '"confidence"' in source
        assert '"keypoints_count"' in source
        assert '"visible_keypoint_count"' in source
        assert '"mean_keypoint_confidence"' in source

    def test_face_fields(self):
        source = PYFUNC_FILE.read_text()
        assert '"landmarks_count"' in source


# ---------------------------------------------------------------------------
# 6. Handles no-face and no-person frames
# ---------------------------------------------------------------------------

class TestNullHandling:
    def test_no_face_reason(self):
        source = PYFUNC_FILE.read_text()
        assert "no_faces_detected" in source

    def test_no_person_reason(self):
        source = PYFUNC_FILE.read_text()
        assert "no_persons_detected" in source

    def test_no_both_reason(self):
        source = PYFUNC_FILE.read_text()
        assert "no_persons_and_no_faces" in source

    def test_no_match_reason(self):
        source = PYFUNC_FILE.read_text()
        assert "no_geometric_match_found" in source

    def test_reason_set_when_no_pairs(self):
        source = PYFUNC_FILE.read_text()
        # The logic should set reason when matched_pairs is empty
        assert "not matched_pairs" in source or "len(matched_pairs) == 0" in source or "not matched" in source


# ---------------------------------------------------------------------------
# 7. Uses frame_anchor_metadata
# ---------------------------------------------------------------------------

class TestUsesFrameAnchorMetadata:
    def test_imports_anchor_metadata(self):
        source = PYFUNC_FILE.read_text()
        assert "frame_anchor_metadata" in source
        assert "extract_frame_anchor_metadata" in source


# ---------------------------------------------------------------------------
# 8. Uses face_person_associator namespace
# ---------------------------------------------------------------------------

class TestUsesAssociationNamespace:
    def test_reads_associator_namespace(self):
        source = PYFUNC_FILE.read_text()
        assert "face_person_associator" in source

    def test_reads_person_track_id(self):
        source = PYFUNC_FILE.read_text()
        assert "person_track_id" in source

    def test_reads_association_score(self):
        source = PYFUNC_FILE.read_text()
        assert "association_score" in source

    def test_reads_association_method(self):
        source = PYFUNC_FILE.read_text()
        assert "association_method" in source


# ---------------------------------------------------------------------------
# 9. Runtime boundaries — forbidden paths
# ---------------------------------------------------------------------------

class TestForbiddenPaths:
    """Check that the pyfunc does NOT import or call forbidden APIs.

    These tests strip the docstring first to avoid matching 'Does NOT'
    declarations in comments.
    """

    def _code_only(self) -> str:
        """Return source without docstrings."""
        import ast
        source = PYFUNC_FILE.read_text()
        tree = ast.parse(source)
        # Remove docstring nodes
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, (ast.Constant, ast.Str))):
                    node.body = node.body[1:]
        return ast.unparse(tree).lower()

    def test_no_image_write_api(self):
        code = self._code_only()
        for bad in ["cv2.imwrite", "pil.image", "save_image"]:
            assert bad not in code, f"Forbidden image API: {bad}"

    def test_no_base64_encoding(self):
        code = self._code_only()
        assert "base64" not in code

    def test_no_redis_publish(self):
        code = self._code_only()
        # Should not have redis xadd or similar
        assert "xadd" not in code
        assert "redis" not in code

    def test_no_rtsp_reference(self):
        code = self._code_only()
        assert "rtsp" not in code


# ---------------------------------------------------------------------------
# 10. Bbox conversion
# ---------------------------------------------------------------------------

class TestBboxConversion:
    def test_xyxy_conversion_exists(self):
        source = PYFUNC_FILE.read_text()
        assert "_to_xyxy" in source or "xyxy" in source

    def test_bbox_in_xyxy_format(self):
        source = PYFUNC_FILE.read_text()
        # The output should be [x1, y1, x2, y2]
        assert "xc - w / 2" in source or "x1, y1, x2, y2" in source


# ---------------------------------------------------------------------------
# 11. Doc exists
# ---------------------------------------------------------------------------

class TestDocExists:
    def test_doc_present(self):
        assert AUDIT_DOC.exists(), f"Doc not found: {AUDIT_DOC}"

    def test_doc_has_pass_status(self):
        content = AUDIT_DOC.read_text()
        assert "PASS_DEBUG_EXPORT_READY" in content


# ---------------------------------------------------------------------------
# 12. C1F.1a doc still present (regression)
# ---------------------------------------------------------------------------

class TestC1F1aRegression:
    def test_c1f1a_doc_present(self):
        assert C1F1A_DOC.exists()

    def test_c1f1a_has_pass_config_ready(self):
        content = C1F1A_DOC.read_text()
        assert "PASS_CONFIG_READY" in content
