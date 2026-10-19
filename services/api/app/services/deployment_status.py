"""Read-only deployment identity and update-channel status for 8090."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = Path("/data/video-analytics/media/evidence/.deployment-manifest.json")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    compose = manifest.get("compose") if isinstance(manifest.get("compose"), dict) else {}
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    git = manifest.get("git") if isinstance(manifest.get("git"), dict) else {}
    return {
        "schema_version": manifest.get("schema_version"),
        "captured_at": manifest.get("captured_at"),
        "release_id": manifest.get("release_id") or str(git.get("commit") or "")[:12],
        "git_commit": git.get("commit"),
        "git_branch": git.get("branch"),
        "git_dirty": bool(git.get("dirty")),
        "compose_config_sha256": compose.get("config_sha256"),
        "compose_profiles": compose.get("profiles") or [],
        "images": compose.get("image_records") or [],
        "running_image_ids": runtime.get("running_image_ids"),
        "migration_set_sha256": artifacts.get("migration_set_sha256"),
        "model_set_sha256": artifacts.get("model_set_sha256"),
        "docker_server_version": runtime.get("docker_server_version"),
        "compose_version": runtime.get("compose_version"),
        "gpu_summary": runtime.get("gpu_summary"),
    }


def _manifest_identity(summary: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(summary.get(key) or "")
        for key in (
            "release_id",
            "git_commit",
            "compose_config_sha256",
            "migration_set_sha256",
            "model_set_sha256",
        )
    )


def build_deployment_status() -> dict[str, Any]:
    manifest_path = Path(os.getenv("DEPLOYMENT_MANIFEST_PATH", str(DEFAULT_MANIFEST)))
    manifest = _load_json(manifest_path)
    update_manifest_raw = os.getenv("DEPLOYMENT_UPDATE_MANIFEST_PATH", "").strip()
    update_manifest_path = Path(update_manifest_raw) if update_manifest_raw else None
    update_manifest = _load_json(update_manifest_path) if update_manifest_path else None

    if manifest is None:
        return {
            "available": False,
            "state": "manifest_missing",
            "manifest_path": str(manifest_path),
            "current": None,
            "update": {
                "channel": os.getenv("DEPLOYMENT_UPDATE_CHANNEL", ""),
                "configured": bool(update_manifest_path),
                "available": None,
                "reason": "部署清单尚未生成",
            },
        }

    current = _safe_manifest_summary(manifest)
    update = {
        "channel": os.getenv("DEPLOYMENT_UPDATE_CHANNEL", ""),
        "configured": bool(update_manifest_path),
        "available": None,
        "reason": "更新清单未配置",
        "manifest_path": str(update_manifest_path) if update_manifest_path else None,
    }
    if update_manifest is not None:
        candidate = _safe_manifest_summary(update_manifest)
        candidate_identity = _manifest_identity(candidate)
        update["available"] = bool(candidate_identity[0] and candidate_identity != _manifest_identity(current))
        update["reason"] = "检测到候选版本差异" if update["available"] else "当前版本已是候选版本"
        update["candidate"] = candidate

    return {
        "available": True,
        "state": "dirty" if current.get("git_dirty") else "current",
        "manifest_path": str(manifest_path),
        "current": current,
        "update": update,
    }

