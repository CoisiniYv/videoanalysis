#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.c2-post-savant-replay-poc.yml"
ENV_FILE="$ROOT_DIR/infra/env/c2-post-savant-replay-poc.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

VIEWER_URL="${C2_POC_VIEWER_URL:-http://127.0.0.1:8090}"
VIEWER_EVIDENCE_ROOT="${C2_POC_VIEWER_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
SOURCE_BUNDLE="${1:-}"
if [[ -z "$SOURCE_BUNDLE" ]]; then
  SOURCE_BUNDLE="$(
    find "${C2_POC_SINK_ROOT:-/data/video-analytics/media/c2-post-savant-replay-poc}" \
      -name annotations.frame_cache.identity.jsonl \
      -type f \
      -printf '%T@ %h\n' 2>/dev/null \
    | sort -nr \
    | awk 'NR==1 {sub(/^[^ ]+ /, ""); print}'
  )"
fi
if [[ -z "$SOURCE_BUNDLE" || ! -d "$SOURCE_BUNDLE" ]]; then
  echo "BLOCKED_C2_1B_VIEWER_CANNOT_FIND_C2_BUNDLE source_bundle_missing" >&2
  exit 1
fi

RUN_ID="c2_1_viewer_runtime_$(date +%Y%m%dT%H%M%S)"
RUNTIME_BUNDLE="$VIEWER_EVIDENCE_ROOT/$RUN_ID"
REPORT_PATH="/tmp/${RUN_ID}.json"

compose() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

mkdir -p "$RUNTIME_BUNDLE"
if [[ -f "$SOURCE_BUNDLE/video.mov" ]]; then
  cp "$SOURCE_BUNDLE/video.mov" "$RUNTIME_BUNDLE/raw_clip.mov"
elif [[ -f "$SOURCE_BUNDLE/raw_clip.mov" ]]; then
  cp "$SOURCE_BUNDLE/raw_clip.mov" "$RUNTIME_BUNDLE/raw_clip.mov"
else
  echo "BLOCKED_C2_1B_VIEWER_CANNOT_FIND_C2_BUNDLE video_missing" >&2
  exit 1
fi
cp "$SOURCE_BUNDLE/annotations.frame_cache.identity.jsonl" "$RUNTIME_BUNDLE/annotations.frame_cache.identity.jsonl"
cp "$SOURCE_BUNDLE/summary.frame_cache.identity.json" "$RUNTIME_BUNDLE/summary.frame_cache.identity.json"
if [[ -f "$SOURCE_BUNDLE/metadata.json" ]]; then
  cp "$SOURCE_BUNDLE/metadata.json" "$RUNTIME_BUNDLE/sink_metadata.json"
fi

python - "$RUNTIME_BUNDLE" "$RUN_ID" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
event_id = sys.argv[2]
metadata = {
    "event": {
        "event_id": event_id,
        "event_type": "intrusion",
        "source_id": "c2_post_savant_replay_poc",
        "camera_id": "c2_post_savant_replay_poc",
    },
    "media": {"raw_clip_name": "raw_clip.mov"},
    "status": {"clip_status": "generated"},
}
summary = {
    "event_id": event_id,
    "event_type": "intrusion",
    "source_id": "c2_post_savant_replay_poc",
    "camera_id": "c2_post_savant_replay_poc",
}
(bundle / "metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
(bundle / "summary.json").write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
PY

compose up -d evidence-viewer >/tmp/"${RUN_ID}-compose-up.log"

deadline=$((SECONDS + 60))
until curl --noproxy '*' -fsS "$VIEWER_URL/health" >/tmp/"${RUN_ID}-health.json"; do
  if (( SECONDS >= deadline )); then
    echo "BLOCKED_C2_1B_VIEWER_SERVICE_FAILED health_unavailable" >&2
    exit 1
  fi
  sleep 2
done

AUTO_RESPONSE_PATH="/tmp/${RUN_ID}-annotations-auto.json"
LEGACY_RESPONSE_PATH="/tmp/${RUN_ID}-annotations-legacy.json"
curl --noproxy '*' -fsS "$VIEWER_URL/api/bundles/$RUN_ID/annotations?source=auto" >"$AUTO_RESPONSE_PATH"
legacy_applicable="false"
if [[ -f "$RUNTIME_BUNDLE/annotations.jsonl" ]]; then
  curl --noproxy '*' -fsS "$VIEWER_URL/api/bundles/$RUN_ID/annotations?source=legacy" >"$LEGACY_RESPONSE_PATH"
  legacy_applicable="true"
fi

python - "$AUTO_RESPONSE_PATH" "$LEGACY_RESPONSE_PATH" "$legacy_applicable" "$RUN_ID" "$RUNTIME_BUNDLE" "$REPORT_PATH" <<'PY'
import json
import sys
from pathlib import Path

auto = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
legacy = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if sys.argv[3] == "true" else None
event_id = sys.argv[4]
bundle = sys.argv[5]
report_path = Path(sys.argv[6])
summary = json.loads((Path(bundle) / "summary.frame_cache.identity.json").read_text(encoding="utf-8"))
report = {
    "event_id": event_id,
    "bundle_path": bundle,
    "annotation_source": auto.get("annotation_source"),
    "annotation_source_kind": auto.get("annotation_source_kind"),
    "production_ready": auto.get("production_ready"),
    "fallback_used": auto.get("fallback_used"),
    "legacy_used_for_visual_binding": auto.get("legacy_used_for_visual_binding"),
    "visual_evidence_status": auto.get("visual_evidence_status"),
    "annotation_count": auto.get("count"),
    "frame_count": summary.get("frame_count"),
    "object_counts": summary.get("object_counts"),
    "explicit_legacy": {
        "applicable": legacy is not None,
        "annotation_source_kind": legacy.get("annotation_source_kind") if legacy else None,
    },
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if auto.get("annotation_source") != "sidecar":
    print("BLOCKED_C2_1B_VIEWER_AUTO_NOT_USING_SIDECAR", json.dumps(report, sort_keys=True), file=sys.stderr)
    sys.exit(2)
if auto.get("annotation_source_kind") != "production_sidecar":
    print("BLOCKED_C2_1B_VIEWER_AUTO_NOT_USING_SIDECAR", json.dumps(report, sort_keys=True), file=sys.stderr)
    sys.exit(2)
if auto.get("fallback_used") is not False:
    print("BLOCKED_C2_1B_VIEWER_AUTO_NOT_USING_SIDECAR", json.dumps(report, sort_keys=True), file=sys.stderr)
    sys.exit(2)
if auto.get("legacy_used_for_visual_binding") is not False:
    print("BLOCKED_C2_1B_VIEWER_AUTO_NOT_USING_SIDECAR", json.dumps(report, sort_keys=True), file=sys.stderr)
    sys.exit(2)
if auto.get("production_ready") is not True:
    print("BLOCKED_C2_1B_VIEWER_AUTO_NOT_USING_SIDECAR", json.dumps(report, sort_keys=True), file=sys.stderr)
    sys.exit(2)
print(json.dumps(report, indent=2, sort_keys=True))
PY
