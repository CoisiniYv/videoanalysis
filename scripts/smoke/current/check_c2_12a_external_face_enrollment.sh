#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_12A_EVIDENCE_ROOT="${C2_12A_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_12A_RUN_ID="${C2_12A_RUN_ID:-c2_12a_external_enrollment_$(date +%Y%m%dT%H%M%S)}"
C2_12A_OUTPUT_DIR="${C2_12A_OUTPUT_DIR:-$C2_12A_EVIDENCE_ROOT/$C2_12A_RUN_ID}"
C2_12A_INPUT_DIR="${C2_12A_INPUT_DIR:-/data/video-analytics/media/face-registration}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
C2_12A_FACE_DETECTOR_ONNX="${C2_12A_FACE_DETECTOR_ONNX:-/data/video-analytics/models/yolov8_face/yolov8n-face.onnx}"
C2_12A_ADAFACE_ONNX="${C2_12A_ADAFACE_ONNX:-/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx}"

python "$ROOT_DIR/scripts/tools/register_c2_12_external_faces.py" \
  --input-dir "$C2_12A_INPUT_DIR" \
  --output-dir "$C2_12A_OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --face-detector-onnx "$C2_12A_FACE_DETECTOR_ONNX" \
  --adaface-onnx "$C2_12A_ADAFACE_ONNX" \
  --overwrite

python - "$C2_12A_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_path = output_dir / "c2_12a_external_enrollment_summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
errors = []

if summary.get("result_marker") != "PASS_C2_12A_EXTERNAL_FACE_ENROLLMENT_READY":
    errors.append(f"result_marker:{summary.get('result_marker')}")
if summary.get("fake_embedding_used") is not False:
    errors.append("fake_embedding_used")
if summary.get("image_bytes_stored") is not False:
    errors.append("image_bytes_stored")
if summary.get("redis_image_bytes_used") is not False:
    errors.append("redis_image_bytes_used")
if summary.get("self_check_passed") is not True:
    errors.append("self_check")

persons = {item.get("external_person_id"): item for item in summary.get("persons", [])}
for external_id in ("demo:f4_3:reese", "demo:f4_3:finch"):
    person = persons.get(external_id)
    if not person:
        errors.append(f"missing_person:{external_id}")
        continue
    if person.get("active") is not True:
        errors.append(f"inactive_person:{external_id}")
    if not person.get("gallery_embedding_ids"):
        errors.append(f"missing_gallery:{external_id}")
    if person.get("embedding_dim") != 512:
        errors.append(f"embedding_dim:{external_id}")
    norms = person.get("embedding_norms") or []
    if not norms or any(not (0.90 <= float(norm) <= 1.10) for norm in norms):
        errors.append(f"embedding_norm:{external_id}")
    if person.get("embedding_model") != "adaface":
        errors.append(f"embedding_model:{external_id}")

result = {
    "result_marker": "PASS_C2_12A_EXTERNAL_FACE_ENROLLMENT_READY" if not errors else "FAIL_C2_12A_EXTERNAL_ENROLLMENT_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "summary": str(summary_path),
    "persons": summary.get("persons"),
    "self_check_passed": summary.get("self_check_passed"),
    "fake_embedding_used": summary.get("fake_embedding_used"),
    "image_bytes_stored": summary.get("image_bytes_stored"),
    "redis_image_bytes_used": summary.get("redis_image_bytes_used"),
}
print(json.dumps(result, indent=2, default=str, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
