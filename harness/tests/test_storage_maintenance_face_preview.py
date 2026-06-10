from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.schemas.maintenance import FaceMediaOrphansPreviewRequest  # noqa: E402
from app.services.storage_maintenance import MaintenanceSettings, StorageMaintenanceService  # noqa: E402

from harness.tests.test_storage_maintenance_evidence_preview import FakeRepo  # noqa: E402


def test_face_media_orphan_preview_skips_any_db_reference(tmp_path: Path) -> None:
    media = tmp_path / "media"
    upload = media / "face_uploads"
    registration = media / "face_registration"
    upload.mkdir(parents=True)
    registration.mkdir(parents=True)
    referenced = upload / "referenced.jpg"
    orphan = registration / "orphan.jpg"
    referenced.write_bytes(b"ref")
    orphan.write_bytes(b"orphan")

    class Repo(FakeRepo):
        def gallery_image_references(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": 1,
                    "person_id": 10,
                    "source_image_path": str(referenced),
                    "is_active": False,
                    "payload": {},
                }
            ]

    settings = MaintenanceSettings(
        media_root=media,
        evidence_root=media / "evidence",
        face_upload_root=upload,
        face_registration_root=registration,
        artifacts_root=tmp_path / "artifacts",
        active_write_guard_seconds=0,
        confirm_secret="test-secret",
    )
    repo = Repo()
    service = StorageMaintenanceService(repo, settings)
    preview = service.create_face_media_orphans_preview(
        FaceMediaOrphansPreviewRequest(older_than_days=0)
    )

    rows = repo.items[preview["preview_id"]]
    by_name = {Path(row["absolute_path"]).name: row for row in rows}
    assert by_name["referenced.jpg"]["skip_reason"] == "still_referenced"
    assert by_name["orphan.jpg"]["eligibility_status"] == "eligible"
