"""Phase H1 — Artifact Output Policy contract tests.

Validates that all smoke scripts and exporters respect the centralized
artifact output policy defined in docs/artifact_output_policy.md.

These are STATIC checks — no 15-minute RTSP run required.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _contains_pattern(text: str, pattern: str) -> bool:
    return pattern in text


def _lacks_pattern(text: str, pattern: str) -> bool:
    return pattern not in text


# ---------------------------------------------------------------------------
# 1. D1 smoke must not write to repo/manual-inspection
# ---------------------------------------------------------------------------


class TestD1SmokeOutputPath:
    """D1 smoke must use VIDEO_ANALYTICS_ARTIFACT_ROOT, not repo/manual-inspection."""

    def test_d1_smoke_uses_artifact_root(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        assert "VIDEO_ANALYTICS_ARTIFACT_ROOT" in smoke, (
            "D1 smoke must reference VIDEO_ANALYTICS_ARTIFACT_ROOT"
        )
        assert "/data/video-analytics/artifacts" in smoke, (
            "D1 smoke must default to /data/video-analytics/artifacts"
        )

    def test_d1_smoke_no_manual_inspection_output_dir(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        # OUTPUT_DIR must not point to manual-inspection
        lines = smoke.splitlines()
        for line in lines:
            if line.startswith("OUTPUT_DIR="):
                assert "manual-inspection" not in line, (
                    f"D1 smoke OUTPUT_DIR must not use manual-inspection: {line}"
                )

    def test_d1_smoke_generates_run_id(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        assert "RUN_ID=" in smoke
        assert "RUN_TIMESTAMP=" in smoke or "date" in smoke

    def test_d1_smoke_outputs_manifest_path(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        assert "manifest_path=" in smoke, (
            "D1 smoke must output manifest_path in its report"
        )

    def test_d1_smoke_outputs_artifact_root(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        assert "artifact_root=" in smoke, (
            "D1 smoke must output artifact_root in its report"
        )

    def test_d1_smoke_outputs_run_id(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        assert "run_id=" in smoke, (
            "D1 smoke must output run_id in its report"
        )


# ---------------------------------------------------------------------------
# 2. P1c smoke must use VIDEO_ANALYTICS_ARTIFACT_ROOT
# ---------------------------------------------------------------------------


class TestP1cSmokeOutputPath:
    """P1c smoke must use VIDEO_ANALYTICS_ARTIFACT_ROOT for manifest."""

    def test_p1c_smoke_uses_artifact_root(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh")
        assert "VIDEO_ANALYTICS_ARTIFACT_ROOT" in smoke, (
            "P1c smoke must reference VIDEO_ANALYTICS_ARTIFACT_ROOT"
        )

    def test_p1c_smoke_generates_manifest(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh")
        assert "manifest.json" in smoke, (
            "P1c smoke must generate manifest.json"
        )

    def test_p1c_smoke_outputs_manifest_path(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh")
        assert "manifest_path=" in smoke, (
            "P1c smoke must output manifest_path in its report"
        )

    def test_p1c_smoke_outputs_artifact_root(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh")
        assert "artifact_root=" in smoke, (
            "P1c smoke must output artifact_root in its report"
        )


# ---------------------------------------------------------------------------
# 3. D1 exporter must use artifact root
# ---------------------------------------------------------------------------


class TestD1ExporterOutputPath:
    """D1 exporter must use VIDEO_ANALYTICS_ARTIFACT_ROOT."""

    def test_exporter_uses_artifact_root(self):
        exporter = _read(REPO_ROOT / "scripts/debug/export_d1_detection_report.py")
        assert "VIDEO_ANALYTICS_ARTIFACT_ROOT" in exporter, (
            "D1 exporter must reference VIDEO_ANALYTICS_ARTIFACT_ROOT"
        )
        assert "/data/video-analytics/artifacts" in exporter, (
            "D1 exporter must default to /data/video-analytics/artifacts"
        )

    def test_exporter_no_manual_inspection_default(self):
        exporter = _read(REPO_ROOT / "scripts/debug/export_d1_detection_report.py")
        assert "manual-inspection" not in exporter.split("DEFAULT_OUTPUT_DIR")[1].split("\n")[0], (
            "D1 exporter DEFAULT_OUTPUT_DIR must not use manual-inspection"
        )


# ---------------------------------------------------------------------------
# 4. manifest.json schema
# ---------------------------------------------------------------------------


class TestManifestSchema:
    """manifest.json schema must have required fields."""

    REQUIRED_FIELDS = {"schema_version", "phase", "run_id", "created_at", "input", "runtime", "artifacts", "git"}

    def test_d1_smoke_generates_valid_manifest_schema(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        for field in self.REQUIRED_FIELDS:
            assert f'"{field}"' in smoke, (
                f"D1 smoke manifest must include field '{field}'"
            )

    def test_p1c_smoke_generates_valid_manifest_schema(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh")
        for field in self.REQUIRED_FIELDS:
            assert f'"{field}"' in smoke, (
                f"P1c smoke manifest must include field '{field}'"
            )


# ---------------------------------------------------------------------------
# 5. Latest pointer
# ---------------------------------------------------------------------------


class TestLatestPointer:
    """Smoke scripts must create latest symlinks."""

    def test_d1_smoke_creates_latest_pointer(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_d1_rtsp_15min_detection_report.sh")
        assert "LATEST_DIR" in smoke
        assert "ln -s" in smoke, "D1 smoke must create symlink for latest pointer"

    def test_p1c_smoke_creates_latest_pointer(self):
        smoke = _read(REPO_ROOT / "scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh")
        assert "LATEST_DIR" in smoke
        assert "ln -s" in smoke, "P1c smoke must create symlink for latest pointer"


# ---------------------------------------------------------------------------
# 6. .gitignore enforcement
# ---------------------------------------------------------------------------


class TestGitignorePolicy:
    """.gitignore must prevent runtime artifacts from being committed."""

    def test_gitignore_ignores_artifacts(self):
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert "artifacts/" in gitignore, ".gitignore must include artifacts/"

    def test_gitignore_ignores_manual_inspection(self):
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert "manual-inspection/" in gitignore

    def test_gitignore_ignores_oodd(self):
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert "oodd/" in gitignore, ".gitignore must include oodd/"

    def test_gitignore_ignores_media_files(self):
        gitignore = _read(REPO_ROOT / ".gitignore")
        for ext in ("*.mov", "*.mp4", "*.webm", "*.jpg", "*.png"):
            assert ext in gitignore, f".gitignore must include {ext}"

    def test_gitignore_ignores_docker_deploy_package(self):
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert "docker-deploy-package.zip" in gitignore, (
            ".gitignore must include docker-deploy-package.zip"
        )

    def test_gitignore_ignores_zip(self):
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert "*.zip" in gitignore or "docker-deploy-package.zip" in gitignore


# ---------------------------------------------------------------------------
# 7. Repo root pollution
# ---------------------------------------------------------------------------


class TestRepoRootPollution:
    """Repo root must not contain runtime output directories."""

    def test_no_oodd_directory(self):
        oodd = REPO_ROOT / "oodd"
        assert not oodd.exists(), (
            "oodd/ must not exist in repo root"
        )

    def test_no_tmp_visual_results(self):
        tmp_vr = REPO_ROOT / "tmp" / "visual_results"
        assert not tmp_vr.exists(), (
            "tmp/visual_results/ must not exist in repo root"
        )

    def test_manual_inspection_is_gitignored(self):
        """manual-inspection/ may exist locally but must be gitignored."""
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert "manual-inspection/" in gitignore


# ---------------------------------------------------------------------------
# 8. Cleanup script defaults to dry-run
# ---------------------------------------------------------------------------


class TestCleanupScriptPolicy:
    """cleanup_artifacts.sh must default to dry-run."""

    def test_cleanup_defaults_to_dry_run(self):
        cleanup = _read(REPO_ROOT / "scripts/maintenance/cleanup_artifacts.sh")
        # The default DRY_RUN should be "yes"
        assert 'DRY_RUN="yes"' in cleanup, (
            "cleanup_artifacts.sh must default to DRY_RUN=yes"
        )

    def test_cleanup_requires_confirm_to_delete(self):
        cleanup = _read(REPO_ROOT / "scripts/maintenance/cleanup_artifacts.sh")
        assert "--confirm" in cleanup, (
            "cleanup_artifacts.sh must require --confirm to actually delete"
        )

    def test_cleanup_supports_phase_filter(self):
        cleanup = _read(REPO_ROOT / "scripts/maintenance/cleanup_artifacts.sh")
        assert "--phase" in cleanup

    def test_cleanup_supports_older_than_days(self):
        cleanup = _read(REPO_ROOT / "scripts/maintenance/cleanup_artifacts.sh")
        assert "--older-than-days" in cleanup

    def test_cleanup_supports_keep_latest(self):
        cleanup = _read(REPO_ROOT / "scripts/maintenance/cleanup_artifacts.sh")
        assert "--keep-latest" in cleanup


# ---------------------------------------------------------------------------
# 9. Maintenance scripts exist and are executable
# ---------------------------------------------------------------------------


class TestMaintenanceScriptsExist:
    """Maintenance scripts must exist."""

    def test_list_artifacts_exists(self):
        assert (REPO_ROOT / "scripts/maintenance/list_artifacts.sh").is_file()

    def test_cleanup_artifacts_exists(self):
        assert (REPO_ROOT / "scripts/maintenance/cleanup_artifacts.sh").is_file()


# ---------------------------------------------------------------------------
# 10. Policy document exists
# ---------------------------------------------------------------------------


class TestPolicyDocument:
    """Artifact output policy document must exist."""

    def test_policy_doc_exists(self):
        assert (REPO_ROOT / "docs/artifact_output_policy.md").is_file()

    def test_policy_doc_defines_artifact_root(self):
        doc = _read(REPO_ROOT / "docs/artifact_output_policy.md")
        assert "VIDEO_ANALYTICS_ARTIFACT_ROOT" in doc

    def test_policy_doc_defines_manifest_schema(self):
        doc = _read(REPO_ROOT / "docs/artifact_output_policy.md")
        assert "manifest.json" in doc
        assert "schema_version" in doc

    def test_policy_doc_defines_run_id_format(self):
        doc = _read(REPO_ROOT / "docs/artifact_output_policy.md")
        assert "run_id" in doc.lower() or "Run ID" in doc
