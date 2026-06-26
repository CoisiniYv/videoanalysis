"""Storage maintenance scanning, preview freezing, and guarded execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import re

from app.repositories.maintenance import MaintenanceRepository
from app.schemas.maintenance import (
    EvidenceDeletePreviewRequest,
    FaceMediaOrphansPreviewRequest,
    GalleryDeletePreviewRequest,
    PeopleDeletePreviewRequest,
)


SAFE_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
NO_AUTO_REGENERATE_MESSAGE = "删除后不会自动重新生成证据"
PREVIEW_TTL_MINUTES = 15
ACTIVE_TASK_STATUSES = {
    "pending",
    "claimed",
    "processing",
    "running",
    "in_progress",
}
STALE_PENDING_DELETABLE_MEDIA_STATUSES = {
    "ready",
    "generated_unverified",
    "generated_annotation_failed",
    "failed",
}
EVENT_CATEGORY_TYPES = {
    "identity": {"watchlist_hit", "live_search_hit"},
    "perimeter": {"intrusion", "wall_climb_suspicious"},
    "behavior": {"loitering", "running", "fall"},
    "crowd": {"crowd_gathering"},
}


class MaintenanceError(ValueError):
    """Raised for user-visible maintenance API failures."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MaintenanceSettings:
    media_root: Path
    evidence_root: Path
    face_upload_root: Path
    face_registration_root: Path
    artifacts_root: Path
    active_write_guard_seconds: int = 600
    max_items_per_run: int = 1000
    max_bytes_per_run: int = 100 * 1024 * 1024 * 1024
    confirm_secret: str = "storage-maintenance-local-confirm-secret"


def load_settings() -> MaintenanceSettings:
    media_root = Path(os.getenv("MEDIA_ROOT", "/data/video-analytics/media"))
    return MaintenanceSettings(
        media_root=media_root,
        evidence_root=media_root / "evidence",
        face_upload_root=Path(os.getenv("FACE_UPLOAD_ROOT", str(media_root / "face_uploads"))),
        face_registration_root=Path(
            os.getenv("FACE_REGISTRATION_ROOT", str(media_root / "face_registration"))
        ),
        artifacts_root=Path(
            os.getenv("STORAGE_MAINTENANCE_ARTIFACTS_ROOT", "/data/video-analytics/artifacts/maintenance")
        ),
        active_write_guard_seconds=_int_env("STORAGE_MAINTENANCE_ACTIVE_WRITE_GUARD_SECONDS", 600),
        max_items_per_run=_int_env("STORAGE_MAINTENANCE_MAX_ITEMS_PER_RUN", 1000),
        max_bytes_per_run=_int_env("STORAGE_MAINTENANCE_MAX_BYTES_PER_RUN", 100 * 1024 * 1024 * 1024),
        confirm_secret=os.getenv("STORAGE_MAINTENANCE_CONFIRM_SECRET")
        or os.getenv("STORAGE_MAINTENANCE_OPERATOR_TOKEN")
        or "storage-maintenance-local-confirm-secret",
    )


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000.0
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            raw = float(text)
        except ValueError:
            raw = None
        if raw is not None:
            if raw > 10_000_000_000:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _as_aware(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


def validate_event_id_segment(event_id: str) -> None:
    if not event_id or event_id in {".", ".."}:
        raise MaintenanceError("invalid event_id")
    if "/" in event_id or "\\" in event_id:
        raise MaintenanceError("event_id must be a single path segment")
    if not SAFE_EVENT_ID_RE.fullmatch(event_id):
        raise MaintenanceError("event_id contains unsafe characters")


def safe_bundle_dir(evidence_root: Path, event_id: str) -> Path:
    validate_event_id_segment(event_id)
    root = evidence_root.resolve(strict=False)
    candidate = (root / event_id).resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise MaintenanceError("event_id resolves outside evidence root")
    return candidate


def _safe_relative_path(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not _is_relative_to(resolved_path, resolved_root):
        raise MaintenanceError("path resolves outside allowed root")
    return str(resolved_path.relative_to(resolved_root))


def _dir_size_and_mtime(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file() or path.is_symlink():
        stat = path.lstat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    total = 0
    latest = int(path.lstat().st_mtime_ns)
    for child in path.rglob("*"):
        try:
            stat = child.lstat()
        except OSError:
            continue
        latest = max(latest, int(stat.st_mtime_ns))
        if child.is_file() or child.is_symlink():
            total += int(stat.st_size)
    return total, latest


def _path_size(path: Path) -> int:
    size, _mtime = _dir_size_and_mtime(path)
    return size


def _metadata_sha256(bundle_dir: Path) -> str | None:
    path = bundle_dir / "metadata.json"
    if not path.is_file():
        return None
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _event_time_from_bundle(bundle_dir: Path, db_record: dict[str, Any] | None) -> tuple[datetime | None, str]:
    metadata = _load_json(bundle_dir / "metadata.json")
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    for key in ("alarm_machine_time", "created_at", "start_ts", "event_time", "timestamp", "event_ts_ms"):
        parsed = _parse_datetime(event.get(key))
        if parsed:
            return parsed, f"metadata.event.{key}"
    for key in ("alarm_machine_time", "event_created_at"):
        parsed = _parse_datetime(metadata.get(key))
        if parsed:
            return parsed, f"metadata.{key}"
        parsed = _parse_datetime(media.get(key))
        if parsed:
            return parsed, f"metadata.media.{key}"
    if db_record:
        for key in ("created_at", "start_ts", "event_ts_ms"):
            parsed = _parse_datetime(db_record.get(key))
            if parsed:
                return parsed, f"db.{key}"
    summary = _load_json(bundle_dir / "summary.json")
    for key in ("alarm_machine_time", "event_created_at", "created_at", "start_ts", "event_time", "timestamp", "event_ts_ms"):
        parsed = _parse_datetime(summary.get(key))
        if parsed:
            return parsed, f"summary.{key}"
    try:
        return datetime.fromtimestamp(bundle_dir.stat().st_mtime, tz=timezone.utc), "mtime_fallback"
    except OSError:
        return None, "unavailable"


def _metadata_value(bundle_dir: Path, key: str) -> str | None:
    metadata = _load_json(bundle_dir / "metadata.json")
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    status = metadata.get("status") if isinstance(metadata.get("status"), dict) else {}
    summary = _load_json(bundle_dir / "summary.json")
    if key == "clip_status":
        value = status.get("clip_status") or summary.get("clip_status")
    elif key == "visual_evidence_status":
        value = status.get("visual_evidence_status") or summary.get("visual_evidence_status")
    else:
        value = event.get(key) or summary.get(key)
    return str(value) if value is not None else None


def _has_raw_clip(bundle_dir: Path) -> bool:
    return any(path.is_file() and path.stat().st_size > 0 for path in bundle_dir.glob("raw_clip.*"))


def _candidate_hash(items: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "target_type": item.get("target_type"),
            "target_id": str(item.get("target_id") or ""),
            "relative_path": item.get("relative_path"),
            "resolved_path": item.get("resolved_path"),
            "size_bytes": int(item.get("size_bytes") or 0),
            "mtime_ns": int(item.get("mtime_ns") or 0),
            "content_fingerprint": item.get("content_fingerprint"),
            "db_event_id": str(item.get("db_event_id") or ""),
            "db_task_id": str(item.get("db_task_id") or ""),
            "media_status_at_preview": item.get("media_status_at_preview"),
            "eligibility_status": item.get("eligibility_status"),
        }
        for item in items
    ]
    normalized.sort(key=lambda row: (row["target_type"] or "", row["target_id"], row["relative_path"] or ""))
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_hash_from_rows(rows: list[dict[str, Any]]) -> str:
    return _candidate_hash(rows)


def _confirm_token(secret: str, preview_id: str, candidate_hash: str, expires_at: datetime) -> str:
    payload = f"{preview_id}|{candidate_hash}|{expires_at.isoformat()}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


class StorageMaintenanceService:
    def __init__(
        self,
        repo: MaintenanceRepository,
        settings: MaintenanceSettings | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or load_settings()

    def storage_summary(self) -> dict[str, Any]:
        media_root = self.settings.media_root
        try:
            stat = os.statvfs(media_root)
            total = int(stat.f_frsize * stat.f_blocks)
            free = int(stat.f_frsize * stat.f_bavail)
            used = max(0, total - free)
            free_percent = round((free / total) * 100, 2) if total else 0
        except OSError:
            total = used = free = free_percent = 0

        evidence_root = self.settings.evidence_root
        bundles = list(self._iter_bundle_dirs())
        evidence_bytes = sum(_path_size(path) for path in bundles)
        event_times: list[datetime] = []
        for path in bundles:
            event_time, _source = _event_time_from_bundle(path, None)
            if event_time:
                event_times.append(event_time)

        trash_root = media_root / ".trash"
        debug_roots = {
            "replay_sink_output": media_root / "replay-sink-output",
            "debug": media_root / "debug",
            "midterm_snapshots": media_root / "midterm-snapshots",
        }
        debug_sizes = {name: _path_size(path) for name, path in debug_roots.items()}
        face_upload_bytes = _path_size(self.settings.face_upload_root)
        face_registration_bytes = _path_size(self.settings.face_registration_root)
        referenced_count, orphan_count = self._face_reference_counts()
        known_bytes = (
            evidence_bytes
            + _path_size(trash_root)
            + sum(debug_sizes.values())
            + face_upload_bytes
            + face_registration_bytes
        )
        root_tree_bytes = _path_size(media_root)

        return {
            "media_root": {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "free_percent": free_percent,
            },
            "evidence": {
                "bundle_count": len(bundles),
                "total_bytes": evidence_bytes,
                "root": str(evidence_root),
                "layout": "flat_bundle",
                "oldest_event_time": min(event_times).isoformat() if event_times else None,
                "newest_event_time": max(event_times).isoformat() if event_times else None,
            },
            "face_media": {
                "upload_bytes": face_upload_bytes,
                "registration_bytes": face_registration_bytes,
                "referenced_file_count": referenced_count,
                "orphan_file_count": orphan_count,
            },
            "trash": {
                "bytes": _path_size(trash_root),
                "item_count": sum(1 for _ in trash_root.rglob("*")) if trash_root.exists() else 0,
            },
            "debug_sinks": {
                "total_bytes": sum(debug_sizes.values()),
                "roots": debug_sizes,
            },
            "other_media": {
                "bytes": max(0, root_tree_bytes - known_bytes),
            },
            "contract": {
                "entrypoint": "8090",
                "evidence_layout": "flat_bundle",
                "execute_default_enabled": False,
                "execute_enabled": _bool_env("STORAGE_MAINTENANCE_EXECUTE_ENABLED", False),
                "no_auto_regenerate_message": NO_AUTO_REGENERATE_MESSAGE,
            },
        }

    def create_evidence_delete_preview(self, request: EvidenceDeletePreviewRequest) -> dict[str, Any]:
        items = self._evidence_candidates(request)
        if len(items) > request.max_items:
            raise MaintenanceError("too_many_candidates", 413)
        if len(items) > self.settings.max_items_per_run:
            raise MaintenanceError("too_many_candidates", 413)
        estimated_bytes = sum(
            int(item.get("size_bytes") or 0)
            for item in items
            if item.get("eligibility_status") == "eligible"
        )
        if estimated_bytes > self.settings.max_bytes_per_run:
            raise MaintenanceError("too_many_bytes", 413)
        return self._create_preview_job(
            job_type="evidence_delete",
            target_type="evidence",
            delete_mode=request.delete_mode,
            requested_by=request.operator,
            reason=request.reason,
            request_payload=request.model_dump(mode="json"),
            items=items,
        )

    def create_people_delete_preview(self, request: PeopleDeletePreviewRequest) -> dict[str, Any]:
        created_from = _as_aware(request.created_from)
        created_to = _as_aware(request.created_to)
        if request.older_than_days is not None and created_to is None:
            created_to = _utcnow() - timedelta(days=request.older_than_days)
        has_selector = bool(request.person_ids or request.external_person_ids or created_from or created_to)
        rows = []
        if has_selector:
            rows = self.repo.find_people_for_delete(
                person_ids=request.person_ids,
                external_person_ids=request.external_person_ids,
                created_from=created_from,
                created_to=created_to,
            )
        items: list[dict[str, Any]] = []
        person_ids = [int(row["id"]) for row in rows]
        gallery_rows = self.repo.list_gallery_for_people(person_ids) if request.include_gallery else []
        for row in rows:
            person_id = str(row["id"])
            eligible = bool(row.get("is_active", True))
            items.append(
                {
                    "target_type": "person",
                    "target_id": person_id,
                    "status": "planned" if eligible else "skipped",
                    "absolute_path": None,
                    "relative_path": None,
                    "resolved_path": None,
                    "size_bytes": 0,
                    "mtime_ns": 0,
                    "content_fingerprint": f"person:{person_id}:{row.get('updated_at')}",
                    "db_event_id": None,
                    "db_task_id": None,
                    "media_status_at_preview": None,
                    "eligibility_status": "eligible" if eligible else "skipped",
                    "skip_reason": None if eligible else "person_inactive",
                    "item_payload": {
                        "name": row.get("name"),
                        "external_person_id": row.get("external_person_id"),
                        "is_active_at_preview": row.get("is_active"),
                        "created_at": _iso(row.get("created_at")),
                        "operation": "soft_delete",
                    },
                }
            )
        for row in gallery_rows:
            gallery_id = str(row["id"])
            eligible = bool(row.get("is_active", True))
            items.append(self._gallery_preview_item(row, eligible=eligible, skip_reason=None if eligible else "gallery_inactive"))
        return self._create_preview_job(
            job_type="people_delete",
            target_type="people",
            delete_mode="soft_delete",
            requested_by=request.operator,
            reason=request.reason,
            request_payload=request.model_dump(mode="json"),
            items=items,
        )

    def create_gallery_delete_preview(self, request: GalleryDeletePreviewRequest) -> dict[str, Any]:
        created_from = _as_aware(request.created_from)
        created_to = _as_aware(request.created_to)
        if request.older_than_days is not None and created_to is None:
            created_to = _utcnow() - timedelta(days=request.older_than_days)
        has_selector = bool(request.gallery_embedding_ids or request.person_ids or created_from or created_to)
        rows = []
        if has_selector:
            rows = self.repo.find_gallery_for_delete(
                gallery_embedding_ids=request.gallery_embedding_ids,
                person_ids=request.person_ids,
                created_from=created_from,
                created_to=created_to,
                only_inactive=request.only_inactive,
            )
        items = [
            self._gallery_preview_item(
                row,
                eligible=bool(row.get("is_active", True)),
                skip_reason=None if row.get("is_active", True) else "gallery_inactive",
            )
            for row in rows
        ]
        return self._create_preview_job(
            job_type="gallery_delete",
            target_type="gallery",
            delete_mode="soft_delete",
            requested_by=request.operator,
            reason=request.reason,
            request_payload=request.model_dump(mode="json"),
            items=items,
        )

    def create_face_media_orphans_preview(self, request: FaceMediaOrphansPreviewRequest) -> dict[str, Any]:
        referenced = self._referenced_face_paths()
        roots = [self.settings.face_upload_root, self.settings.face_registration_root]
        cutoff = None
        if request.older_than_days is not None:
            cutoff = _utcnow() - timedelta(days=request.older_than_days)
        items: list[dict[str, Any]] = []
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() and not path.is_symlink():
                    continue
                try:
                    resolved = path.resolve(strict=False)
                    root_resolved = root.resolve(strict=False)
                    if not _is_relative_to(resolved, root_resolved):
                        raise MaintenanceError("face media path resolves outside allowed root")
                    size, mtime_ns = _dir_size_and_mtime(path)
                    mtime = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)
                    relative = _safe_relative_path(root, path)
                except (OSError, MaintenanceError) as exc:
                    items.append(
                        self._face_media_item(
                            path=path,
                            root=root,
                            status="skipped",
                            eligibility_status="skipped",
                            skip_reason=f"path_unsafe:{exc}",
                            size_bytes=0,
                            mtime_ns=0,
                            referenced_rows=[],
                        )
                    )
                    continue
                key = str(resolved)
                referenced_rows = referenced.get(key, [])
                eligible = not referenced_rows
                skip_reason = None
                if referenced_rows:
                    skip_reason = "still_referenced"
                elif cutoff and mtime > cutoff:
                    eligible = False
                    skip_reason = "inside_retention_window"
                if len(items) >= request.max_items:
                    raise MaintenanceError("too_many_candidates", 413)
                items.append(
                    self._face_media_item(
                        path=path,
                        root=root,
                        status="planned" if eligible else "skipped",
                        eligibility_status="eligible" if eligible else "skipped",
                        skip_reason=skip_reason,
                        size_bytes=size,
                        mtime_ns=mtime_ns,
                        referenced_rows=referenced_rows,
                        relative_path=relative,
                    )
                )
        return self._create_preview_job(
            job_type="face_media_orphan_cleanup",
            target_type="face_media",
            delete_mode=request.delete_mode,
            requested_by=request.operator,
            reason=request.reason,
            request_payload=request.model_dump(mode="json"),
            items=items,
        )

    def execute_job(
        self,
        *,
        preview_id: str,
        confirm_token: str,
        delete_mode: str | None,
        reason: str,
        operator: str,
        requested_candidate_hash: str | None = None,
    ) -> dict[str, Any]:
        job = self.repo.get_job(preview_id)
        if not job:
            raise MaintenanceError("preview not found", 404)
        candidate_hash = job.get("candidate_hash")
        preview_expires_at = _parse_datetime(job.get("preview_expires_at"))
        if not candidate_hash:
            raise MaintenanceError("preview missing candidate_hash", 409)
        if preview_expires_at is None:
            raise MaintenanceError("preview missing preview_expires_at", 409)
        if _utcnow() > preview_expires_at:
            self.repo.update_job(preview_id, status="expired", error_message="preview_expired")
            raise MaintenanceError("preview expired", 409)
        if requested_candidate_hash and requested_candidate_hash != candidate_hash:
            raise MaintenanceError("candidate_hash mismatch", 409)
        expected_token = _confirm_token(self.settings.confirm_secret, preview_id, candidate_hash, preview_expires_at)
        if not hmac.compare_digest(confirm_token, expected_token):
            raise MaintenanceError("confirm_token mismatch", 403)

        items = self.repo.list_items(preview_id)
        current_hash = candidate_hash_from_rows(items)
        if current_hash != candidate_hash:
            raise MaintenanceError("candidate_hash mismatch", 409)

        job_type = str(job.get("job_type") or "")
        self.repo.update_job(preview_id, status="running", started=True)
        results: list[dict[str, Any]] = []
        if job_type == "evidence_delete":
            request_payload = _json_dict(job.get("request_payload"))
            results = self._execute_evidence_items(
                job_id=preview_id,
                items=items,
                delete_mode=delete_mode or job.get("delete_mode") or "trash",
                reason=reason,
                operator=operator,
                allow_stale_pending_tasks=bool(request_payload.get("allow_stale_pending_tasks")),
            )
        elif job_type == "people_delete":
            results = self._execute_people_items(preview_id, items, reason=reason, operator=operator)
        elif job_type == "gallery_delete":
            results = self._execute_gallery_items(preview_id, items, reason=reason, operator=operator)
        elif job_type == "face_media_orphan_cleanup":
            results = self._execute_face_media_items(
                job_id=preview_id,
                items=items,
                delete_mode=delete_mode or job.get("delete_mode") or "trash",
                reason=reason,
                operator=operator,
            )
        else:
            raise MaintenanceError(f"unsupported job_type:{job_type}", 400)

        counts = _status_counts(results)
        result_payload = self._result_payload(job_type=job_type, delete_mode=delete_mode, items=results)
        final_status = "completed" if not any(key in counts for key in ("failed_fs", "failed_db", "compensation_required")) else "completed_with_attention"
        self.repo.update_job(preview_id, status=final_status, result_payload=result_payload, finished=True)
        return {
            "job_id": preview_id,
            "status": final_status,
            "status_counts": counts,
            "summary": result_payload,
            "no_auto_regenerate_message": NO_AUTO_REGENERATE_MESSAGE,
        }

    def job_detail(self, job_id: str, *, limit: int = 50, offset: int = 0, status: str | None = None) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if not job:
            raise MaintenanceError("job not found", 404)
        rows = self.repo.list_items(job_id, limit=limit, offset=offset, status=status)
        total = self.repo.count_items(job_id, status=status)
        counts = self.repo.item_status_counts(job_id)
        failed = [
            self._item_response(row)
            for row in self.repo.list_items(job_id, limit=20, status="failed_fs")
            + self.repo.list_items(job_id, limit=20, status="failed_db")
        ]
        compensation = [
            self._item_response(row)
            for row in self.repo.list_items(job_id, limit=20, status="compensation_required")
        ]
        return {
            "job": {
                "job_id": str(job.get("id")),
                "job_type": job.get("job_type"),
                "target_type": job.get("target_type"),
                "status": job.get("status"),
                "delete_mode": job.get("delete_mode"),
                "requested_by": job.get("requested_by"),
                "reason": job.get("reason"),
                "created_at": _iso(job.get("created_at")),
                "started_at": _iso(job.get("started_at")),
                "finished_at": _iso(job.get("finished_at")),
                "preview_expires_at": _iso(job.get("preview_expires_at")),
                "candidate_hash": job.get("candidate_hash"),
                "summary": _json_dict(job.get("result_payload")) or _json_dict(job.get("preview_payload")),
                "status_counts": counts,
                "no_auto_regenerate_message": NO_AUTO_REGENERATE_MESSAGE,
            },
            "items": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "records": [self._item_response(row) for row in rows],
            },
            "attention_items": {
                "failed": failed,
                "compensation_required": compensation,
            },
            "no_auto_regenerate_message": NO_AUTO_REGENERATE_MESSAGE,
        }

    def _iter_bundle_dirs(self) -> list[Path]:
        root = self.settings.evidence_root
        if not root.is_dir():
            return []
        bundle_dirs: list[Path] = []
        for candidate in sorted(root.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir():
                continue
            if candidate.name == "events":
                continue
            try:
                safe_bundle_dir(root, candidate.name)
            except MaintenanceError:
                continue
            bundle_dirs.append(candidate)
        return bundle_dirs

    def _evidence_candidates(self, request: EvidenceDeletePreviewRequest) -> list[dict[str, Any]]:
        event_ids = [value.strip() for value in request.event_ids if value and value.strip()]
        bundles: list[tuple[str, Path, str | None]] = []
        if event_ids:
            for event_id in event_ids:
                if event_id == "events":
                    bundles.append((event_id, self.settings.evidence_root / event_id, "reserved_legacy_events_root"))
                    continue
                try:
                    bundle = safe_bundle_dir(self.settings.evidence_root, event_id)
                except MaintenanceError as exc:
                    bundles.append((event_id, self.settings.evidence_root / event_id, str(exc)))
                    continue
                bundles.append((event_id, bundle, None))
        else:
            bundles = [(path.name, path, None) for path in self._iter_bundle_dirs()]

        time_from = _as_aware(request.time_from)
        time_to = _as_aware(request.time_to)
        has_time_filter = bool(time_from or time_to)
        if request.older_than_days is not None and time_to is None:
            time_to = _utcnow() - timedelta(days=request.older_than_days)
            has_time_filter = True

        items: list[dict[str, Any]] = []
        for event_id, bundle, path_error in bundles:
            if path_error:
                items.append(self._evidence_item(event_id, bundle, status="skipped", skip_reason=f"path_unsafe:{path_error}"))
                continue
            db_record = self.repo.get_evidence_record(event_id)
            if not bundle.is_dir():
                items.append(
                    self._evidence_item(
                        event_id,
                        bundle,
                        status="skipped",
                        skip_reason="bundle_missing",
                        db_record=db_record,
                    )
                )
                continue
            event_time, time_source = _event_time_from_bundle(bundle, db_record)
            if has_time_filter and (event_time is None or time_source in {"mtime_fallback", "unavailable"}):
                items.append(
                    self._evidence_item(
                        event_id,
                        bundle,
                        status="skipped",
                        skip_reason="missing_event_time_for_range",
                        db_record=db_record,
                        item_payload={
                            "event_time": _iso(event_time),
                            "time_source": time_source,
                            "event_type": _metadata_value(bundle, "event_type"),
                            "camera_id": _metadata_value(bundle, "camera_id"),
                            "source_id": _metadata_value(bundle, "source_id"),
                            "db_record_missing": db_record is None,
                            "no_auto_regenerate": True,
                        },
                    )
                )
                continue
            if time_from and event_time and event_time < time_from:
                continue
            if time_to and event_time and event_time > time_to:
                continue
            if request.event_type and request.event_type != _metadata_value(bundle, "event_type"):
                continue
            if request.event_category:
                event_type = _metadata_value(bundle, "event_type") or ""
                allowed = EVENT_CATEGORY_TYPES.get(request.event_category)
                if allowed and event_type not in allowed:
                    continue
            if request.camera_id and request.camera_id != _metadata_value(bundle, "camera_id"):
                continue
            if request.source_id and request.source_id != _metadata_value(bundle, "source_id"):
                continue
            if request.clip_status and request.clip_status != _metadata_value(bundle, "clip_status"):
                continue
            if request.visual_evidence_status and request.visual_evidence_status != _metadata_value(bundle, "visual_evidence_status"):
                continue
            if request.has_raw_clip is not None and _has_raw_clip(bundle) != request.has_raw_clip:
                continue

            skip_reason = self._evidence_skip_reason(
                bundle,
                db_record,
                allow_stale_pending_tasks=request.allow_stale_pending_tasks,
            )
            item = self._evidence_item(
                event_id,
                bundle,
                status="planned" if skip_reason is None else "skipped",
                skip_reason=skip_reason,
                db_record=db_record,
                item_payload={
                    "event_time": _iso(event_time),
                    "time_source": time_source,
                    "event_type": _metadata_value(bundle, "event_type"),
                    "camera_id": _metadata_value(bundle, "camera_id"),
                    "source_id": _metadata_value(bundle, "source_id"),
                    "evidence_task_status_before_delete": db_record.get("evidence_task_status") if db_record else None,
                    "db_record_missing": db_record is None,
                    "no_auto_regenerate": True,
                },
            )
            items.append(item)
        return items

    def _evidence_skip_reason(
        self,
        bundle: Path,
        db_record: dict[str, Any] | None,
        *,
        allow_stale_pending_tasks: bool = False,
    ) -> str | None:
        if (bundle / ".lock").exists():
            return "active_write_guard"
        if self.settings.active_write_guard_seconds > 0:
            try:
                mtime = datetime.fromtimestamp(bundle.stat().st_mtime, tz=timezone.utc)
            except OSError:
                return "stat_failed"
            if _utcnow() - mtime < timedelta(seconds=self.settings.active_write_guard_seconds):
                return "active_write_guard"
        if self._active_task_blocks_delete(db_record, allow_stale_pending_tasks=allow_stale_pending_tasks):
            return "task_pending"
        media_status = str(db_record.get("media_status") or "").lower() if db_record else ""
        if media_status in {"media_deleted", "media_expired"}:
            return "already_media_deleted"
        return None

    def _active_task_blocks_delete(
        self,
        db_record: dict[str, Any] | None,
        *,
        allow_stale_pending_tasks: bool,
    ) -> bool:
        task_status = str(db_record.get("evidence_task_status") or "").lower() if db_record else ""
        if task_status not in ACTIVE_TASK_STATUSES:
            return False
        media_status = str(db_record.get("media_status") or "").lower() if db_record else ""
        return not (allow_stale_pending_tasks and media_status in STALE_PENDING_DELETABLE_MEDIA_STATUSES)

    def _evidence_item(
        self,
        event_id: str,
        bundle: Path,
        *,
        status: str,
        skip_reason: str | None,
        db_record: dict[str, Any] | None = None,
        item_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        eligibility = "eligible" if skip_reason is None else "skipped"
        try:
            resolved = bundle.resolve(strict=False)
            relative = _safe_relative_path(self.settings.evidence_root, bundle)
            size, mtime_ns = _dir_size_and_mtime(bundle)
        except (MaintenanceError, OSError) as exc:
            resolved = bundle
            relative = event_id
            size = 0
            mtime_ns = 0
            status = "skipped"
            eligibility = "skipped"
            skip_reason = f"path_unsafe:{exc}"
        metadata_hash = _metadata_sha256(bundle)
        fingerprint = f"{size}:{mtime_ns}:{relative}"
        if metadata_hash:
            fingerprint = f"{fingerprint}:metadata_sha256={metadata_hash}"
        return {
            "target_type": "evidence",
            "target_id": event_id,
            "status": status,
            "absolute_path": str(bundle),
            "relative_path": relative,
            "resolved_path": str(resolved),
            "size_bytes": size,
            "mtime_ns": mtime_ns,
            "content_fingerprint": fingerprint,
            "db_event_id": str(db_record.get("event_id")) if db_record and db_record.get("event_id") else None,
            "db_task_id": str(db_record.get("task_id")) if db_record and db_record.get("task_id") else None,
            "media_status_at_preview": db_record.get("media_status") if db_record else None,
            "eligibility_status": eligibility,
            "skip_reason": skip_reason,
            "item_payload": item_payload or {"db_record_missing": db_record is None, "no_auto_regenerate": True},
        }

    def _gallery_preview_item(self, row: dict[str, Any], *, eligible: bool, skip_reason: str | None) -> dict[str, Any]:
        payload = _json_dict(row.get("payload"))
        return {
            "target_type": "gallery",
            "target_id": str(row["id"]),
            "status": "planned" if eligible else "skipped",
            "absolute_path": None,
            "relative_path": None,
            "resolved_path": None,
            "size_bytes": 0,
            "mtime_ns": 0,
            "content_fingerprint": f"gallery:{row['id']}:{row.get('updated_at')}",
            "db_event_id": None,
            "db_task_id": None,
            "media_status_at_preview": None,
            "eligibility_status": "eligible" if eligible else "skipped",
            "skip_reason": skip_reason,
            "item_payload": {
                "person_id": row.get("person_id"),
                "source_image_path": row.get("source_image_path"),
                "registered_crop_path": payload.get("registered_crop_path"),
                "is_active_at_preview": row.get("is_active"),
                "is_primary_at_preview": row.get("is_primary"),
                "operation": "soft_delete",
            },
        }

    def _face_media_item(
        self,
        *,
        path: Path,
        root: Path,
        status: str,
        eligibility_status: str,
        skip_reason: str | None,
        size_bytes: int,
        mtime_ns: int,
        referenced_rows: list[dict[str, Any]],
        relative_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            resolved = path.resolve(strict=False)
            rel = relative_path if relative_path is not None else _safe_relative_path(root, path)
        except MaintenanceError:
            resolved = path
            rel = path.name
        return {
            "target_type": "face_media",
            "target_id": f"{root.name}:{rel}",
            "status": status,
            "absolute_path": str(path),
            "relative_path": rel,
            "resolved_path": str(resolved),
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
            "content_fingerprint": f"{size_bytes}:{mtime_ns}:{rel}",
            "db_event_id": None,
            "db_task_id": None,
            "media_status_at_preview": None,
            "eligibility_status": eligibility_status,
            "skip_reason": skip_reason,
            "item_payload": {
                "root": str(root),
                "referenced_rows": referenced_rows,
                "is_symlink": path.is_symlink(),
            },
        }

    def _create_preview_job(
        self,
        *,
        job_type: str,
        target_type: str,
        delete_mode: str | None,
        requested_by: str | None,
        reason: str | None,
        request_payload: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        expires_at = _utcnow() + timedelta(minutes=PREVIEW_TTL_MINUTES)
        candidate_hash = _candidate_hash(items)
        preview_payload = self._preview_payload(items)
        job = self.repo.create_job(
            job_type=job_type,
            target_type=target_type,
            status="preview",
            delete_mode=delete_mode,
            requested_by=requested_by,
            reason=reason,
            request_payload=_json_safe(request_payload),
            preview_payload=preview_payload,
            candidate_hash=candidate_hash,
            preview_expires_at=expires_at,
        )
        job_id = str(job["id"])
        self.repo.add_items(job_id, items)
        confirm_token = _confirm_token(self.settings.confirm_secret, job_id, candidate_hash, expires_at)
        return {
            "preview_id": job_id,
            "confirm_token": confirm_token,
            "candidate_hash": candidate_hash,
            "preview_expires_at": expires_at.isoformat(),
            "delete_mode": delete_mode,
            **preview_payload,
            "no_auto_regenerate_message": NO_AUTO_REGENERATE_MESSAGE,
        }

    def _preview_payload(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        eligible = [item for item in items if item.get("eligibility_status") == "eligible"]
        skipped = [item for item in items if item.get("eligibility_status") != "eligible"]
        event_times = [
            _parse_datetime(_json_dict(item.get("item_payload")).get("event_time"))
            for item in items
        ]
        event_times = [value for value in event_times if value is not None]
        groups: dict[str, dict[str, int]] = {"event_type": {}, "camera_id": {}}
        for item in items:
            payload = _json_dict(item.get("item_payload"))
            for key in ("event_type", "camera_id"):
                value = payload.get(key)
                if value:
                    groups[key][str(value)] = groups[key].get(str(value), 0) + 1
        return {
            "candidate_count": len(items),
            "deletable_count": len(eligible),
            "skipped_count": len(skipped),
            "estimated_bytes": sum(int(item.get("size_bytes") or 0) for item in eligible),
            "oldest_event_time": min(event_times).isoformat() if event_times else None,
            "newest_event_time": max(event_times).isoformat() if event_times else None,
            "groups": groups,
            "skipped": [
                {"target_type": item.get("target_type"), "target_id": item.get("target_id"), "reason": item.get("skip_reason")}
                for item in skipped[:100]
            ],
        }

    def _execute_evidence_items(
        self,
        *,
        job_id: str,
        items: list[dict[str, Any]],
        delete_mode: str,
        reason: str,
        operator: str,
        allow_stale_pending_tasks: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in items:
            item_result = dict(item)
            if item.get("status") == "completed":
                item_result["status"] = "already_completed"
                results.append(item_result)
                continue
            if item.get("eligibility_status") != "eligible":
                self.repo.update_item(int(item["id"]), status="skipped", skip_reason=item.get("skip_reason") or "not_eligible")
                item_result["status"] = "skipped"
                results.append(item_result)
                continue
            validation_error = self._validate_evidence_item(item)
            db_record = self.repo.get_evidence_record(str(item["target_id"]))
            if validation_error:
                if validation_error == "source_missing" and db_record and db_record.get("media_status") in {"media_deleted", "media_expired"}:
                    self.repo.update_item(int(item["id"]), status="completed", skip_reason="source_missing_already_marked", completed=True)
                    item_result["status"] = "completed"
                    results.append(item_result)
                    continue
                status = "compensation_required" if validation_error == "source_missing" else "skipped"
                self.repo.update_item(int(item["id"]), status=status, skip_reason=validation_error)
                item_result["status"] = status
                item_result["skip_reason"] = validation_error
                results.append(item_result)
                continue
            if self._active_task_blocks_delete(
                db_record,
                allow_stale_pending_tasks=allow_stale_pending_tasks,
            ):
                self.repo.update_item(int(item["id"]), status="skipped", skip_reason="db_state_changed")
                item_result["status"] = "skipped"
                item_result["skip_reason"] = "db_state_changed"
                results.append(item_result)
                continue

            bundle = Path(str(item["resolved_path"]))
            try:
                bytes_deleted, trash_path = self._delete_path(
                    source=bundle,
                    job_id=job_id,
                    target_id=str(item["target_id"]),
                    target_type="evidence",
                    delete_mode=delete_mode,
                )
                if db_record and db_record.get("event_id"):
                    self.repo.update_event_media_deleted(
                        event_id=str(db_record["event_id"]),
                        job_id=job_id,
                        operator=operator,
                        reason=reason,
                        media_status="media_deleted",
                    )
                    self.repo.mark_evidence_tasks_deleted_metadata(
                        event_id=str(db_record["event_id"]),
                        job_id=job_id,
                    )
                payload = _json_dict(item.get("item_payload"))
                payload.update(
                    {
                        "deleted_bytes": bytes_deleted,
                        "delete_mode": delete_mode,
                        "trash_path": trash_path,
                        "db_record_missing": db_record is None,
                        "evidence_task_delete_marker": f"deleted_by_storage_maintenance:{job_id}",
                        "no_auto_regenerate": True,
                    }
                )
                self.repo.update_item(
                    int(item["id"]),
                    status="completed",
                    trash_path=trash_path,
                    item_payload=payload,
                    completed=True,
                )
                item_result.update({"status": "completed", "deleted_bytes": bytes_deleted, "trash_path": trash_path})
                self._write_audit_line(job_id, item_result, action=delete_mode)
            except OSError as exc:
                self.repo.update_item(int(item["id"]), status="failed_fs", error_message=str(exc))
                item_result.update({"status": "failed_fs", "error_message": str(exc)})
            except Exception as exc:
                self.repo.update_item(int(item["id"]), status="failed_db", error_message=str(exc))
                item_result.update({"status": "failed_db", "error_message": str(exc)})
            results.append(item_result)
        return results

    def _execute_people_items(self, job_id: str, items: list[dict[str, Any]], *, reason: str, operator: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in items:
            item_result = dict(item)
            if item.get("eligibility_status") != "eligible":
                self.repo.update_item(int(item["id"]), status="skipped", skip_reason=item.get("skip_reason") or "not_eligible")
                item_result["status"] = "skipped"
            elif item.get("target_type") == "person":
                person = self.repo.get_person(int(item["target_id"]))
                if not person:
                    self.repo.update_item(int(item["id"]), status="compensation_required", skip_reason="person_missing")
                    item_result["status"] = "compensation_required"
                elif not person.get("is_active", True):
                    self.repo.update_item(int(item["id"]), status="completed", skip_reason="already_inactive", completed=True)
                    item_result["status"] = "completed"
                else:
                    self.repo.soft_delete_person(person_id=int(item["target_id"]), operator=operator, reason=reason)
                    self.repo.update_item(int(item["id"]), status="completed", completed=True)
                    item_result["status"] = "completed"
            elif item.get("target_type") == "gallery":
                item_result = self._execute_gallery_item(item, reason=reason, operator=operator)
            results.append(item_result)
            self._write_audit_line(job_id, item_result, action="soft_delete")
        return results

    def _execute_gallery_items(self, job_id: str, items: list[dict[str, Any]], *, reason: str, operator: str) -> list[dict[str, Any]]:
        results = [self._execute_gallery_item(item, reason=reason, operator=operator) for item in items]
        for item in results:
            self._write_audit_line(job_id, item, action="soft_delete")
        return results

    def _execute_gallery_item(self, item: dict[str, Any], *, reason: str, operator: str) -> dict[str, Any]:
        item_result = dict(item)
        if item.get("eligibility_status") != "eligible":
            self.repo.update_item(int(item["id"]), status="skipped", skip_reason=item.get("skip_reason") or "not_eligible")
            item_result["status"] = "skipped"
            return item_result
        gallery = self.repo.get_gallery(int(item["target_id"]))
        if not gallery:
            self.repo.update_item(int(item["id"]), status="compensation_required", skip_reason="gallery_missing")
            item_result["status"] = "compensation_required"
        elif not gallery.get("is_active", True):
            self.repo.update_item(int(item["id"]), status="completed", skip_reason="already_inactive", completed=True)
            item_result["status"] = "completed"
        else:
            self.repo.soft_delete_gallery(gallery_id=int(item["target_id"]), operator=operator, reason=reason)
            self.repo.update_item(int(item["id"]), status="completed", completed=True)
            item_result["status"] = "completed"
        return item_result

    def _execute_face_media_items(
        self,
        *,
        job_id: str,
        items: list[dict[str, Any]],
        delete_mode: str,
        reason: str,
        operator: str,
    ) -> list[dict[str, Any]]:
        referenced = self._referenced_face_paths()
        results: list[dict[str, Any]] = []
        del reason, operator
        for item in items:
            item_result = dict(item)
            if item.get("eligibility_status") != "eligible":
                self.repo.update_item(int(item["id"]), status="skipped", skip_reason=item.get("skip_reason") or "not_eligible")
                item_result["status"] = "skipped"
                results.append(item_result)
                continue
            path = Path(str(item["absolute_path"]))
            resolved = path.resolve(strict=False)
            if str(resolved) in referenced:
                self.repo.update_item(int(item["id"]), status="skipped", skip_reason="still_referenced")
                item_result["status"] = "skipped"
                item_result["skip_reason"] = "still_referenced"
                results.append(item_result)
                continue
            validation_error = self._validate_file_item(item, allowed_roots=[self.settings.face_upload_root, self.settings.face_registration_root])
            if validation_error:
                self.repo.update_item(int(item["id"]), status="skipped", skip_reason=validation_error)
                item_result["status"] = "skipped"
                item_result["skip_reason"] = validation_error
                results.append(item_result)
                continue
            try:
                bytes_deleted, trash_path = self._delete_path(
                    source=path,
                    job_id=job_id,
                    target_id=Path(str(item["relative_path"])).name,
                    target_type="face",
                    delete_mode=delete_mode,
                )
                payload = _json_dict(item.get("item_payload"))
                payload.update({"deleted_bytes": bytes_deleted, "delete_mode": delete_mode, "trash_path": trash_path})
                self.repo.update_item(
                    int(item["id"]),
                    status="completed",
                    trash_path=trash_path,
                    item_payload=payload,
                    completed=True,
                )
                item_result.update({"status": "completed", "deleted_bytes": bytes_deleted, "trash_path": trash_path})
                self._write_audit_line(job_id, item_result, action=delete_mode)
            except OSError as exc:
                self.repo.update_item(int(item["id"]), status="failed_fs", error_message=str(exc))
                item_result.update({"status": "failed_fs", "error_message": str(exc)})
            results.append(item_result)
        return results

    def _validate_evidence_item(self, item: dict[str, Any]) -> str | None:
        target_id = str(item.get("target_id") or "")
        try:
            bundle = safe_bundle_dir(self.settings.evidence_root, target_id)
        except MaintenanceError:
            return "path_changed"
        expected_resolved = str(item.get("resolved_path") or "")
        if str(bundle.resolve(strict=False)) != expected_resolved:
            return "path_changed"
        if not bundle.exists():
            return "source_missing"
        if (bundle / ".lock").exists():
            return "active_write_guard"
        if self.settings.active_write_guard_seconds > 0:
            try:
                mtime = datetime.fromtimestamp(bundle.stat().st_mtime, tz=timezone.utc)
            except OSError:
                return "stat_failed"
            if _utcnow() - mtime < timedelta(seconds=self.settings.active_write_guard_seconds):
                return "active_write_guard"
        current_size, current_mtime = _dir_size_and_mtime(bundle)
        if current_size != int(item.get("size_bytes") or 0) or current_mtime != int(item.get("mtime_ns") or 0):
            return "stale_preview_item"
        return None

    def _validate_file_item(self, item: dict[str, Any], *, allowed_roots: list[Path]) -> str | None:
        path = Path(str(item.get("absolute_path") or ""))
        if not path.exists():
            return "source_missing"
        resolved = path.resolve(strict=False)
        if not any(_is_relative_to(resolved, root.resolve(strict=False)) for root in allowed_roots):
            return "path_changed"
        size, mtime_ns = _dir_size_and_mtime(path)
        if size != int(item.get("size_bytes") or 0) or mtime_ns != int(item.get("mtime_ns") or 0):
            return "stale_preview_item"
        return None

    def _delete_path(
        self,
        *,
        source: Path,
        job_id: str,
        target_id: str,
        target_type: str,
        delete_mode: str,
    ) -> tuple[int, str | None]:
        size = _path_size(source)
        if delete_mode == "trash":
            trash_root = self.settings.media_root / ".trash" / target_type / job_id
            trash_root.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9_.:-]+", "_", target_id).strip("._") or uuid.uuid4().hex
            target = trash_root / safe_name
            if target.exists():
                target = trash_root / f"{safe_name}-{uuid.uuid4().hex[:8]}"
            shutil.move(str(source), str(target))
            return size, str(target)
        if source.is_dir() and not source.is_symlink():
            shutil.rmtree(source)
        else:
            source.unlink()
        return size, None

    def _referenced_face_paths(self) -> dict[str, list[dict[str, Any]]]:
        references: dict[str, list[dict[str, Any]]] = {}
        for row in self.repo.gallery_image_references():
            payload = _json_dict(row.get("payload"))
            for kind, raw_path in (
                ("source_image_path", row.get("source_image_path")),
                ("registered_crop_path", payload.get("registered_crop_path")),
            ):
                if not raw_path:
                    continue
                path = Path(str(raw_path))
                try:
                    resolved = path.resolve(strict=False)
                except OSError:
                    continue
                references.setdefault(str(resolved), []).append(
                    {
                        "gallery_embedding_id": row.get("id"),
                        "person_id": row.get("person_id"),
                        "is_active": row.get("is_active"),
                        "path_kind": kind,
                    }
                )
        return references

    def _face_reference_counts(self) -> tuple[int, int]:
        referenced = self._referenced_face_paths()
        roots = [self.settings.face_upload_root, self.settings.face_registration_root]
        files: list[str] = []
        for root in roots:
            if root.exists():
                for path in root.rglob("*"):
                    if path.is_file() or path.is_symlink():
                        try:
                            files.append(str(path.resolve(strict=False)))
                        except OSError:
                            pass
        referenced_files = {path for path in files if path in referenced}
        orphan_files = {path for path in files if path not in referenced}
        return len(referenced_files), len(orphan_files)

    def _result_payload(self, *, job_type: str, delete_mode: str | None, items: list[dict[str, Any]]) -> dict[str, Any]:
        completed = [item for item in items if item.get("status") in {"completed", "already_completed"}]
        skipped = [item for item in items if item.get("status") == "skipped"]
        failed = [item for item in items if str(item.get("status") or "").startswith("failed")]
        compensation = [item for item in items if item.get("status") == "compensation_required"]
        deleted_bytes = sum(int(item.get("deleted_bytes") or 0) for item in completed)
        trash_bytes_added = deleted_bytes if delete_mode == "trash" else 0
        freed_bytes = deleted_bytes if delete_mode == "permanent" else 0
        return {
            "job_type": job_type,
            "candidate_count": len(items),
            "completed_count": len(completed),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "compensation_required_count": len(compensation),
            "estimated_bytes": sum(int(item.get("size_bytes") or 0) for item in items),
            "deleted_bytes": deleted_bytes,
            "freed_bytes": freed_bytes,
            "trash_bytes_added": trash_bytes_added,
            "no_auto_regenerate_message": NO_AUTO_REGENERATE_MESSAGE,
        }

    def _item_response(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "item_id": row.get("id"),
            "target_type": row.get("target_type"),
            "target_id": row.get("target_id"),
            "status": row.get("status"),
            "size_bytes": row.get("size_bytes"),
            "skip_reason": row.get("skip_reason"),
            "error_message": row.get("error_message"),
            "trash_path": row.get("trash_path"),
            "completed_at": _iso(row.get("completed_at")),
            "eligibility_status": row.get("eligibility_status"),
        }

    def _write_audit_line(self, job_id: str, item: dict[str, Any], *, action: str) -> None:
        try:
            self.settings.artifacts_root.mkdir(parents=True, exist_ok=True)
            record = {
                "job_id": job_id,
                "target_type": item.get("target_type"),
                "target_id": item.get("target_id"),
                "action": action,
                "bytes": int(item.get("deleted_bytes") or item.get("size_bytes") or 0),
                "status": item.get("status"),
                "reason": item.get("skip_reason") or item.get("error_message"),
                "no_auto_regenerate": True,
            }
            with (self.settings.artifacts_root / f"{job_id}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            return
