#!/usr/bin/env bash
# C1F.4c - Unified file-based evidence viewer smoke.
#
# Scope:
#   Build and run the standalone evidence-viewer service, verify read-only
#   bundle APIs, and validate overlay contracts without touching the evidence
#   production pipeline.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/infra/docker-compose.c1-official-replay-dev.yml"
SERVICE="${C1F4C_SERVICE:-evidence-viewer}"
CONTAINER="${C1F4C_CONTAINER:-c1-official-evidence-viewer}"
API_BASE="${C1F4C_API_BASE:-http://localhost:8090}"
ARTIFACT_DIR="${C1F4C_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1f4c}"
SUMMARY_FILE="$ARTIFACT_DIR/c1f4c_evidence_viewer_summary.json"

mkdir -p "$ARTIFACT_DIR"

log() {
    echo "[C1F.4c] $(date '+%H:%M:%S') $*"
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

require_file() {
    local path="$1"
    if [ ! -f "$path" ]; then
        fail_result "FAIL_SERVICE_CONTRACT" "missing $path"
    fi
}

require_file "$PROJECT_ROOT/services/evidence-viewer/Dockerfile"
require_file "$PROJECT_ROOT/services/evidence-viewer/requirements.txt"
require_file "$PROJECT_ROOT/services/evidence-viewer/app/main.py"
require_file "$PROJECT_ROOT/services/evidence-viewer/app/evidence_index.py"
require_file "$PROJECT_ROOT/services/evidence-viewer/app/static/index.html"
require_file "$PROJECT_ROOT/services/evidence-viewer/app/static/app.js"
require_file "$PROJECT_ROOT/services/evidence-viewer/app/static/style.css"

log "running local contract probes"
PYTHONPATH="$PROJECT_ROOT/services/evidence-viewer" python3 - <<'PY'
import json
import tempfile
from pathlib import Path

from app.evidence_index import (
    EvidencePathError,
    annotation_time_seconds,
    discover_raw_clip,
    first_sink_frame_pts,
    object_label,
    object_style,
    parse_jsonl_records,
    parse_json_or_jsonl_records,
    safe_bundle_dir,
)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    bundle = root / "event-1"
    bundle.mkdir()
    (bundle / "raw_clip.mp4").write_bytes(b"video")
    (bundle / "metadata.json").write_text(
        json.dumps({"media": {"raw_clip_path": "/evidence/event-1/raw_clip.mp4"}}),
        encoding="utf-8",
    )
    assert discover_raw_clip(bundle).name == "raw_clip.mp4"
    assert safe_bundle_dir(root, "event-1") == bundle.resolve(strict=False)
    for bad in ("../etc/passwd", "..%2Fetc%2Fpasswd", "/tmp/x", "event/child"):
        try:
            safe_bundle_dir(root, bad)
        except EvidencePathError:
            pass
        else:
            raise AssertionError(f"path traversal accepted: {bad}")

    jsonl = bundle / "annotations.jsonl"
    jsonl.write_text('{"frame_pts": 1100000000, "objects": []}\nnot-json\n', encoding="utf-8")
    records, warnings = parse_jsonl_records(jsonl)
    assert len(records) == 1 and warnings

    sink = bundle / "sink_metadata.json"
    sink.write_text('{"pts": 1000000000, "width": 1920, "height": 1080}\n', encoding="utf-8")
    sink_records, sink_warnings = parse_json_or_jsonl_records(sink)
    assert first_sink_frame_pts(sink_records) == 1000000000
    assert not sink_warnings
    assert annotation_time_seconds(records[0], 1000000000) == (0.1, "frame_pts")

    unknown = {
        "track_id": "t1",
        "identity": {"status": "unknown", "similarity": None},
        "style": {"bbox_color": "#D50000", "reason": "alert_hit", "priority": 100},
    }
    label = object_label(unknown, {"timestamp_ms": 123})
    assert "sim 0.00" not in label and "0.sim" not in label and "score 0.00" not in label
    style, style_warnings = object_style(unknown)
    assert style["bbox_color"] != "#D50000"
    assert "unknown_style_overridden_from_event_alert" in style_warnings
PY

log "validating compose config"
docker compose -f "$COMPOSE_FILE" config >/tmp/c1f4c-compose-config.yml
grep -q "^  evidence-viewer:" /tmp/c1f4c-compose-config.yml || fail_result "FAIL_COMPOSE_CONFIG" "evidence-viewer service missing"
grep -q "container_name: c1-official-evidence-viewer" /tmp/c1f4c-compose-config.yml || fail_result "FAIL_COMPOSE_CONFIG" "container name missing"
EVIDENCE_VIEWER_SECTION="$(awk '/^  evidence-viewer:/{flag=1;next}/^  [A-Za-z0-9_.-]+:/{flag=0}flag' /tmp/c1f4c-compose-config.yml)"
grep -q "source: /data/video-analytics/media/evidence" <<<"$EVIDENCE_VIEWER_SECTION" || fail_result "FAIL_COMPOSE_CONFIG" "evidence source mount missing"
grep -q "target: /evidence" <<<"$EVIDENCE_VIEWER_SECTION" || fail_result "FAIL_COMPOSE_CONFIG" "evidence target mount missing"
grep -q "read_only: true" <<<"$EVIDENCE_VIEWER_SECTION" || fail_result "FAIL_COMPOSE_CONFIG" "read-only evidence mount missing"
if grep -Eiq 'DATABASE_URL|POSTGRES|REDIS_URL|redis|postgres|nvidia|privileged' <<<"$EVIDENCE_VIEWER_SECTION"; then
    fail_result "FAIL_COMPOSE_CONFIG" "forbidden dependency/env in evidence-viewer service"
fi

log "building and starting $SERVICE"
docker compose -f "$COMPOSE_FILE" up -d --build "$SERVICE" >/tmp/c1f4c-compose-up.log

log "waiting for /health"
for _ in $(seq 1 60); do
    if curl --noproxy '*' -fsS "$API_BASE/health" >"$ARTIFACT_DIR/health.json" 2>/tmp/c1f4c-health.err; then
        break
    fi
    sleep 1
done
curl --noproxy '*' -fsS "$API_BASE/health" >"$ARTIFACT_DIR/health.json" \
    || fail_result "FAIL_HEALTH" "$(cat /tmp/c1f4c-health.err 2>/dev/null || true)"

curl --noproxy '*' -fsS "$API_BASE/" >"$ARTIFACT_DIR/index.html" \
    || fail_result "FAIL_VIEWER_HTML" "GET / failed"
grep -q "Evidence Viewer" "$ARTIFACT_DIR/index.html" || fail_result "FAIL_VIEWER_HTML" "title missing"
grep -q "canvas" "$ARTIFACT_DIR/index.html" || fail_result "FAIL_VIEWER_HTML" "canvas missing"

curl --noproxy '*' -fsS "$API_BASE/static/app.js" >"$ARTIFACT_DIR/app.js" \
    || fail_result "FAIL_VIEWER_HTML" "GET /static/app.js failed"
grep -q "annotationTimeSeconds" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_TIME_ALIGNMENT" "alignment function missing"
grep -q "frame_pts" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_TIME_ALIGNMENT" "frame_pts logic missing"
grep -q "time_offset_ms_fallback" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_TIME_ALIGNMENT" "fallback marker missing"
grep -q "normalizeBbox" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_BBOX_LOGIC" "bbox normalization missing"
grep -q "cxcywh" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_BBOX_LOGIC" "cxcywh missing"
grep -q "xyxy" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_BBOX_LOGIC" "xyxy missing"
grep -q "xywh" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_BBOX_LOGIC" "xywh missing"
grep -q "Unknown face" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_UNKNOWN_LABEL" "unknown label missing"
if grep -q "sim 0.00" "$ARTIFACT_DIR/app.js" || grep -q "0.sim" "$ARTIFACT_DIR/app.js" || grep -q "score 0.00" "$ARTIFACT_DIR/app.js"; then
    fail_result "FAIL_UNKNOWN_LABEL" "unknown zero-sim label found"
fi
grep -q "unknown_style_overridden_from_event_alert" "$ARTIFACT_DIR/app.js" || fail_result "FAIL_UNKNOWN_STYLE" "unknown red override missing"
if grep -Eqi 'https?://|cdn' "$ARTIFACT_DIR/index.html" "$ARTIFACT_DIR/app.js" "$PROJECT_ROOT/services/evidence-viewer/app/static/style.css"; then
    fail_result "FAIL_VIEWER_HTML" "external network resource reference found"
fi

curl --noproxy '*' -fsS "$API_BASE/api/bundles" >"$ARTIFACT_DIR/bundles.json" \
    || fail_result "FAIL_BUNDLES_API" "GET /api/bundles failed"

EVENT_ID="$(python3 - "$ARTIFACT_DIR/bundles.json" <<'PY'
import json
import sys

data = json.loads(open(sys.argv[1], encoding="utf-8").read())
bundles = data.get("bundles") or []
print(bundles[0].get("event_id", "") if bundles else "")
PY
)"

TRAVERSAL_STATUS="$(curl --noproxy '*' --path-as-is -sS -o /tmp/c1f4c-traversal.out -w '%{http_code}' "$API_BASE/api/bundles/../etc/passwd" || true)"
if [ "$TRAVERSAL_STATUS" = "200" ]; then
    fail_result "FAIL_PATH_TRAVERSAL" "plain traversal returned 200"
fi
ENCODED_TRAVERSAL_STATUS="$(curl --noproxy '*' --path-as-is -sS -o /tmp/c1f4c-encoded-traversal.out -w '%{http_code}' "$API_BASE/api/bundles/..%2Fetc%2Fpasswd" || true)"
if [ "$ENCODED_TRAVERSAL_STATUS" = "200" ]; then
    fail_result "FAIL_PATH_TRAVERSAL" "encoded traversal returned 200"
fi

if [ -z "$EVENT_ID" ]; then
    python3 - "$SUMMARY_FILE" "$CONTAINER" "$API_BASE" <<'PY'
import json
import sys
summary = {
    "result": "PASS_CONTRACT_ONLY_REAL_BUNDLE_NOT_VERIFIED",
    "container": sys.argv[2],
    "api_base": sys.argv[3],
    "real_bundle_verified": False,
}
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(summary, indent=2))
PY
    echo "RESULT=PASS_CONTRACT_ONLY_REAL_BUNDLE_NOT_VERIFIED"
    echo "service=$SERVICE"
    echo "container=$CONTAINER"
    echo "api_base=$API_BASE"
    exit 0
fi

log "verifying real bundle $EVENT_ID"
ENCODED_EVENT_ID="$(python3 - "$EVENT_ID" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
)"

curl --noproxy '*' -fsS "$API_BASE/api/bundles/$ENCODED_EVENT_ID" >"$ARTIFACT_DIR/manifest.json" \
    || fail_result "FAIL_BUNDLE_API" "bundle manifest failed"
curl --noproxy '*' -fsS "$API_BASE/api/bundles/$ENCODED_EVENT_ID/annotations" >"$ARTIFACT_DIR/annotations.json" \
    || fail_result "FAIL_ANNOTATIONS_API" "annotations API failed"
curl --noproxy '*' -fsS "$API_BASE/api/bundles/$ENCODED_EVENT_ID/sink-metadata" >"$ARTIFACT_DIR/sink_metadata.json" \
    || fail_result "FAIL_SINK_METADATA_API" "sink metadata API failed"
RAW_STATUS="$(curl --noproxy '*' -fsS -o /tmp/c1f4c-raw-clip.bin -w '%{http_code}' "$API_BASE/api/bundles/$ENCODED_EVENT_ID/media/raw_clip" || true)"
if [ "$RAW_STATUS" != "200" ] || [ ! -s /tmp/c1f4c-raw-clip.bin ]; then
    fail_result "FAIL_RAW_CLIP_API" "raw clip status=$RAW_STATUS"
fi

python3 - "$SUMMARY_FILE" "$ARTIFACT_DIR" "$CONTAINER" "$API_BASE" "$EVENT_ID" <<'PY'
import json
import sys
from pathlib import Path

summary_file = Path(sys.argv[1])
artifact_dir = Path(sys.argv[2])
container = sys.argv[3]
api_base = sys.argv[4]
event_id = sys.argv[5]

manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
annotations = json.loads((artifact_dir / "annotations.json").read_text(encoding="utf-8"))
sink = json.loads((artifact_dir / "sink_metadata.json").read_text(encoding="utf-8"))
records = annotations.get("records") or []
objects = [obj for row in records for obj in row.get("objects", [])]
if not records:
    raise SystemExit("no parsed annotation records")
if not sink.get("records"):
    raise SystemExit("no parsed sink metadata records")
if not any(obj.get("bbox") for obj in objects):
    raise SystemExit("no bbox in parsed annotations")

metadata = manifest.get("metadata") or {}
summary = manifest.get("summary") or {}
clip_validation = (metadata.get("media") or {}).get("clip_validation") or {}
result = {
    "result": "PASS_C1F4C_UNIFIED_FILE_BASED_EVIDENCE_VIEWER",
    "container": container,
    "api_base": api_base,
    "event_id": event_id,
    "raw_clip_name": manifest.get("raw_clip_name"),
    "annotation_records": len(records),
    "sink_metadata_records": sink.get("count"),
    "face_objects": len(objects),
    "matched_objects": summary.get("matched_objects"),
    "unknown_objects": summary.get("unknown_objects"),
    "clip_status": (metadata.get("status") or {}).get("clip_status"),
    "decode_warning_count": clip_validation.get("decode_error_count", 0),
    "frontend_overlay_required": (metadata.get("annotations") or {}).get("frontend_overlay_required"),
    "real_bundle_verified": True,
}
summary_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
PY

echo "RESULT=PASS_C1F4C_UNIFIED_FILE_BASED_EVIDENCE_VIEWER"
python3 - "$SUMMARY_FILE" <<'PY'
import json
import sys
data = json.loads(open(sys.argv[1], encoding="utf-8").read())
for key, value in data.items():
    print(f"{key}={value}")
PY
