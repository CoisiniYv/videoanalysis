#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${C2_14B_COMPOSE_FILE:-$ROOT/infra/docker-compose.c2-post-savant-replay-poc.yml}"
FPS_OVERRIDE="${C2_14B_FPS_OVERRIDE:-/tmp/c2-fps-only-probe.override.yml}"
SOURCE_ID="${C2_14B_SOURCE_ID:-c2_post_savant_fps_probe}"
RING_ROOT="${C2_14B_RING_ROOT:-/data/video-analytics/media/rtsp-ring}"
CHUNK_SIZE="${C2_14B_CHUNK_SIZE:-120}"
RUNTIME_SECONDS="${C2_14B_RUNTIME_SECONDS:-150}"
MAX_RUNTIME_SECONDS="${C2_14B_MAX_RUNTIME_SECONDS:-300}"
TTL_SECONDS="${MEDIA_RING_TTL_SECONDS:-600}"
MAX_BYTES="${MEDIA_RING_MAX_BYTES_PER_SOURCE:-5368709120}"
MIN_KEEP_SECONDS="${MEDIA_RING_MIN_KEEP_SECONDS:-120}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
REDIS_URL="${C2_14B_REDIS_URL:-redis://127.0.0.1:6395/0}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
OUT_DIR="${C2_14B_OUTPUT_DIR:-/data/video-analytics/media/evidence/c2_14b_rtsp_segment_writer_${STAMP}}"
DIR_LOCATION="${C2_14B_DIR_LOCATION:-/media/rtsp-ring/%source_id/segments/${STAMP}_%chunk_idx}"

mkdir -p "$OUT_DIR"

RUNTIME_OVERRIDE="$OUT_DIR/c2_14b_runtime.override.yml"
cat >"$RUNTIME_OVERRIDE" <<YAML
services:
  replay-service:
    volumes:
      - $ROOT/modules/savant_replay/config.c2_14_ring_pass_through.json:/opt/etc/config.json:ro
  video-file-sink:
    environment:
      SOURCE_ID: $SOURCE_ID
      DIR_LOCATION: $DIR_LOCATION
      CHUNK_SIZE: "$CHUNK_SIZE"
      METADATA_JSON_FORMAT: native
YAML

COMPOSE_ARGS=(-f "$COMPOSE_FILE")
if [[ -f "$FPS_OVERRIDE" ]]; then
  COMPOSE_ARGS+=(-f "$FPS_OVERRIDE")
fi
COMPOSE_ARGS+=(-f "$RUNTIME_OVERRIDE")

docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate replay-service video-file-sink >/dev/null

SUMMARY_JSON="$OUT_DIR/decision_summary.json"
python "$ROOT/scripts/tools/run_c2_14b_rtsp_segment_writer_smoke.py" \
  --source-id "$SOURCE_ID" \
  --ring-root "$RING_ROOT" \
  --output-dir "$OUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$REDIS_URL" \
  --runtime-seconds "$RUNTIME_SECONDS" \
  --max-runtime-seconds "$MAX_RUNTIME_SECONDS" \
  --chunk-size "$CHUNK_SIZE" \
  --expected-dir-location "$DIR_LOCATION" \
  --ttl-seconds "$TTL_SECONDS" \
  --max-bytes "$MAX_BYTES" \
  --min-keep-seconds "$MIN_KEEP_SECONDS" \
  >"$OUT_DIR/tool_output.json"

python - "$SUMMARY_JSON" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
summary = json.loads(summary_path.read_text())
ring = summary.get("ring_output") or {}
retention = summary.get("retention") or {}
event = summary.get("selected_event") or {}

print(f"input_type={summary.get('input_type')}")
print(f"source_id={summary.get('source_id')}")
print(f"ring_root={summary.get('ring_root')}")
print(f"chunk_size={summary.get('chunk_size')}")
print(f"segments_written={ring.get('segment_count')}")
print(f"segments_indexed={ring.get('indexed_segment_count')}")
print("retention_status=bounded_dry_run_safe" if retention.get("deleted_count") == 0 and retention.get("unsafe_deletion_target_count") == 0 else "retention_status=unsafe_or_unknown")
print(f"retention_delete_candidates={retention.get('delete_candidate_count')}")
print(f"event_selected={bool(event)}")
print(f"event_type={event.get('event_type')}")
print(f"evidence_bundle_path={summary.get('evidence_bundle_path')}")
print(f"video_integrity_status={summary.get('video_integrity_status')}")
print(f"overall_marker={summary.get('result_marker')}")
print(f"output_dir={summary_path.parent}")

if str(summary.get("result_marker") or "").startswith("FAIL_"):
    sys.exit(2)
PY
