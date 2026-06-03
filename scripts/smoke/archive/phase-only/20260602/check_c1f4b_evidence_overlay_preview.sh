#!/usr/bin/env bash
# C1F.4b - Evidence overlay HTML preview smoke.
#
# Scope:
#   evidence bundle raw_clip.mov + annotations.jsonl -> static preview.html
#   with browser-side canvas overlay. No media re-encoding and no event/API path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GENERATOR="$PROJECT_ROOT/scripts/tools/generate_evidence_overlay_preview.py"
COMPOSE_FILE="$PROJECT_ROOT/infra/docker-compose.c1-official-replay-dev.yml"

ARTIFACT_DIR="${C1F4B_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1f4b}"
SUMMARY_FILE="$ARTIFACT_DIR/c1f4b_preview_summary.json"
mkdir -p "$ARTIFACT_DIR"

log() {
    echo "[C1F.4b] $(date '+%H:%M:%S') $*"
}

fail_result() {
    local result="$1"
    local reason="${2:-}"
    echo "RESULT=${result}"
    if [ -n "$reason" ]; then
        echo "REASON=${reason}"
    fi
    exit 1
}

find_latest_bundle() {
    python3 - <<'PY'
from pathlib import Path

root = Path("/data/video-analytics/media/evidence")
required = [
    "raw_clip.mov",
    "metadata.json",
    "sink_metadata.json",
    "annotations.jsonl",
    "summary.json",
]
if not root.is_dir():
    raise SystemExit(2)
candidates = []
for path in root.iterdir():
    if path.is_dir() and all((path / name).is_file() for name in required):
        candidates.append(path)
if not candidates:
    raise SystemExit(2)
print(max(candidates, key=lambda item: item.stat().st_mtime))
PY
}

BUNDLE_DIR="${C1F4B_BUNDLE_DIR:-}"
if [ -z "$BUNDLE_DIR" ]; then
    if ! BUNDLE_DIR="$(find_latest_bundle)"; then
        fail_result "FAIL_BUNDLE_NOT_FOUND" "no compatible evidence bundle found"
    fi
fi

RAW_CLIP="$BUNDLE_DIR/raw_clip.mov"
METADATA_JSON="$BUNDLE_DIR/metadata.json"
SINK_METADATA_JSON="$BUNDLE_DIR/sink_metadata.json"
ANNOTATIONS_JSONL="$BUNDLE_DIR/annotations.jsonl"
SUMMARY_JSON="$BUNDLE_DIR/summary.json"
PREVIEW_HTML="$BUNDLE_DIR/preview.html"

if [ ! -d "$BUNDLE_DIR" ]; then
    fail_result "FAIL_BUNDLE_NOT_FOUND" "$BUNDLE_DIR"
fi
if [ ! -s "$RAW_CLIP" ]; then
    fail_result "FAIL_RAW_CLIP_MISSING" "$RAW_CLIP"
fi
if [ ! -s "$ANNOTATIONS_JSONL" ]; then
    fail_result "FAIL_ANNOTATIONS_MISSING" "$ANNOTATIONS_JSONL"
fi
for required in "$METADATA_JSON" "$SINK_METADATA_JSON" "$SUMMARY_JSON"; do
    if [ ! -s "$required" ]; then
        fail_result "FAIL_BUNDLE_NOT_FOUND" "missing $required"
    fi
done

log "generating preview for bundle: $BUNDLE_DIR"
if ! python3 "$GENERATOR" --bundle-dir "$BUNDLE_DIR" >"$ARTIFACT_DIR/generator.out" 2>"$ARTIFACT_DIR/generator.err"; then
    cat "$ARTIFACT_DIR/generator.err" >&2 || true
    fail_result "FAIL_HTML_GENERATION" "$BUNDLE_DIR"
fi

if [ ! -s "$PREVIEW_HTML" ]; then
    fail_result "FAIL_HTML_GENERATION" "$PREVIEW_HTML"
fi

grep -qi "<video" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "video tag missing"
grep -q "raw_clip.mov" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "raw_clip reference missing"
grep -q "annotations.jsonl" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "annotations reference missing"
grep -q "sink_metadata.json" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "sink metadata reference missing"
grep -qi "<canvas" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "canvas missing"
grep -q "cxcywh" "$PREVIEW_HTML" || fail_result "FAIL_BBOX_LOGIC_MISSING" "cxcywh conversion missing"
grep -q "bbox_color" "$PREVIEW_HTML" || fail_result "FAIL_BBOX_LOGIC_MISSING" "style color logic missing"
grep -q "firstVideoFramePts" "$PREVIEW_HTML" || fail_result "FAIL_TIME_ALIGNMENT_LOGIC_MISSING" "first frame pts missing"
grep -q "targetPts" "$PREVIEW_HTML" || fail_result "FAIL_TIME_ALIGNMENT_LOGIC_MISSING" "target pts missing"
grep -q "NS_PER_SECOND" "$PREVIEW_HTML" || fail_result "FAIL_TIME_ALIGNMENT_LOGIC_MISSING" "ns/sec conversion missing"
grep -q "time_offset_ms_fallback" "$PREVIEW_HTML" || fail_result "FAIL_TIME_ALIGNMENT_LOGIC_MISSING" "fallback marker missing"
grep -q "showUnknown" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "unknown filter missing"
grep -q "showMatched" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "matched filter missing"
grep -q "showLandmarks" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "landmark toggle missing"
grep -q "showLabels" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "label toggle missing"
grep -q "activeObjects" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "active object count missing"
grep -q "generated_corrupt" "$PREVIEW_HTML" || fail_result "FAIL_HTML_GENERATION" "decode warning display missing"

if grep -qi "annotated_clip" "$PREVIEW_HTML"; then
    fail_result "FAIL_HTML_GENERATION" "preview references annotated clip"
fi
if grep -qi "ffmpeg" "$PREVIEW_HTML"; then
    fail_result "FAIL_HTML_GENERATION" "preview references ffmpeg"
fi
if grep -qi "rtsp://" "$PREVIEW_HTML"; then
    fail_result "FAIL_HTML_GENERATION" "preview references RTSP URL"
fi
if grep -Eqi "https?://|cdn" "$PREVIEW_HTML"; then
    fail_result "FAIL_HTML_GENERATION" "preview references external network resource"
fi

log "validating bundle JSON and annotations"
if ! python3 - "$BUNDLE_DIR" "$SUMMARY_FILE" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
out_file = Path(sys.argv[2])

metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
summary = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))

sink_records = []
with (bundle / "sink_metadata.json").open("r", encoding="utf-8") as fh:
    for raw in fh:
        raw = raw.strip()
        if raw:
            sink_records.append(json.loads(raw))
if not sink_records:
    raise SystemExit("no sink metadata records")
first = next((row for row in sink_records if row.get("pts") is not None), None)
if first is None:
    raise SystemExit("no sink metadata pts")
pts_values = [int(row["pts"]) for row in sink_records if row.get("pts") is not None]
duration_values = [
    int(row.get("duration") or 0)
    for row in sink_records
    if row.get("duration") is not None
]
duration_from_sink = None
if pts_values:
    duration_from_sink = (
        max(pts_values) - int(first["pts"]) + (duration_values[-1] if duration_values else 0)
    ) / 1_000_000_000

lines = []
with (bundle / "annotations.jsonl").open("r", encoding="utf-8") as fh:
    for lineno, raw in enumerate(fh, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid jsonl line {lineno}: {exc}") from exc
if not lines:
    raise SystemExit("annotations.jsonl has no lines")

objects = [obj for line in lines for obj in line.get("objects", [])]
bboxes = [obj.get("bbox") for obj in objects if obj.get("bbox")]
if not bboxes:
    raise SystemExit("no bbox in annotations")
colors = [
    obj.get("style", {}).get("bbox_color")
    for obj in objects
    if obj.get("style", {}).get("bbox_color")
]
if not colors:
    raise SystemExit("no style.bbox_color in annotations")
frame_pts_count = sum(1 for line in lines if line.get("frame_pts") is not None)
fallback_used = frame_pts_count < len(lines)

if summary.get("embedding_leaked") is not False:
    raise SystemExit("summary embedding_leaked is not false")
if summary.get("image_bytes_leaked") is not False:
    raise SystemExit("summary image_bytes_leaked is not false")

validation = metadata.get("media", {}).get("clip_validation", {})
preview = {
    "result": "PASS_OVERLAY_PREVIEW_READY",
    "bundle_dir": str(bundle),
    "raw_clip": str(bundle / "raw_clip.mov"),
    "metadata_json": str(bundle / "metadata.json"),
    "sink_metadata_json": str(bundle / "sink_metadata.json"),
    "annotations_jsonl": str(bundle / "annotations.jsonl"),
    "summary_json": str(bundle / "summary.json"),
    "preview_html": str(bundle / "preview.html"),
    "width": first.get("width"),
    "height": first.get("height"),
    "framerate": first.get("framerate"),
    "first_frame_pts": first.get("pts"),
    "duration_from_sink_metadata": duration_from_sink,
    "raw_clip_duration": metadata.get("media", {}).get("raw_clip_duration"),
    "clip_status": metadata.get("status", {}).get("clip_status"),
    "decode_warning_count": validation.get("decode_error_count", 0),
    "annotation_lines": summary.get("annotation_lines", len(lines)),
    "face_objects": summary.get("face_objects", len(objects)),
    "matched_objects": summary.get("matched_objects", 0),
    "unknown_objects": summary.get("unknown_objects", 0),
    "colors_used": summary.get("colors_used", sorted(set(colors))),
    "bbox_format": sorted({bbox.get("format", "") for bbox in bboxes}),
    "time_alignment_strategy": "frame_pts_against_first_sink_metadata_pts",
    "time_offset_fallback_used": fallback_used,
    "embedding_leaked": summary.get("embedding_leaked"),
    "image_bytes_leaked": summary.get("image_bytes_leaked"),
}
out_file.write_text(json.dumps(preview, indent=2), encoding="utf-8")
PY
then
    fail_result "FAIL_INVALID_JSONL" "bundle annotation validation failed"
fi

echo "RESULT=PASS_OVERLAY_PREVIEW_READY"
python3 - "$SUMMARY_FILE" <<'PY'
import json
import sys

data = json.loads(open(sys.argv[1], encoding="utf-8").read())
for key in (
    "bundle_dir",
    "raw_clip",
    "metadata_json",
    "sink_metadata_json",
    "annotations_jsonl",
    "summary_json",
    "preview_html",
    "width",
    "height",
    "framerate",
    "first_frame_pts",
    "duration_from_sink_metadata",
    "raw_clip_duration",
    "clip_status",
    "decode_warning_count",
    "annotation_lines",
    "face_objects",
    "matched_objects",
    "unknown_objects",
    "colors_used",
    "bbox_format",
    "time_alignment_strategy",
    "time_offset_fallback_used",
):
    value = data.get(key, "")
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    print(f"{key}={value}")
print("video_tag=YES")
print("canvas_overlay=YES")
print("controls=YES")
print("landmarks_toggle=YES")
print("label_toggle=YES")
print("unknown_filter=YES")
print("matched_filter=YES")
print("validation_warning_displayed=YES")
print("annotated_clip_generated=NO")
print("ffmpeg_used=NO")
print("second_rtsp=NO")
print("external_cdn=NO")
print("production_frontend=NO")
print("api_frontend_implemented=NO")
PY
