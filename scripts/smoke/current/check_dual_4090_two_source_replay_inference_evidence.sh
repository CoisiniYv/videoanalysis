#!/usr/bin/env bash
set -euo pipefail

PASS_TOKEN="PASS_DUAL_4090_TWO_SOURCE_REPLAY_INFERENCE_EVIDENCE"

SOURCE_A_ID="${SOURCE_A_ID:-primary_rtsp}"
SOURCE_B_ID="${SOURCE_B_ID:-source_00000000-0000-4000-8000-781078565686}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
EVIDENCE_MIN_MTIME_EPOCH="${EVIDENCE_MIN_MTIME_EPOCH:-}"
FORWARDER_A_METRICS_URL="${FORWARDER_A_METRICS_URL:-http://127.0.0.1:18182/metrics}"
FORWARDER_B_METRICS_URL="${FORWARDER_B_METRICS_URL:-http://127.0.0.1:18183/metrics}"
SAVANT_A_METRICS_URL="${SAVANT_A_METRICS_URL:-http://127.0.0.1:18180/metrics}"
SAVANT_B_METRICS_URL="${SAVANT_B_METRICS_URL:-http://127.0.0.1:18181/metrics}"

required_containers=(
  video-analytics-midterm-replay-a
  video-analytics-midterm-replay-b
  video-analytics-midterm-analysis-forwarder-a
  video-analytics-midterm-analysis-forwarder-b
  video-analytics-midterm-savant-a
  video-analytics-midterm-savant-b
  video-analytics-midterm-video-file-sink-a
  video-analytics-midterm-video-file-sink-b
)

fail() {
  echo "FAIL_DUAL_4090_TWO_SOURCE_REPLAY_INFERENCE_EVIDENCE reason=$*" >&2
  exit 1
}

require_container_running() {
  local name="$1"
  local status
  status="$(docker inspect "$name" --format '{{.State.Status}}' 2>/dev/null || true)"
  [[ "$status" == "running" ]] || fail "container_not_running container=$name status=${status:-missing}"
}

metric_value() {
  local url="$1"
  local metric="$2"
  local source_id="$3"
  curl --noproxy '*' -fsS "$url" \
    | awk -v metric="$metric" -v source="$source_id" '
        $1 ~ "^" metric "\\{" && $1 ~ "source_id=\"" source "\"" {print $2; found=1}
        END {if (!found) print ""}
      '
}

require_positive_metric() {
  local url="$1"
  local metric="$2"
  local source_id="$3"
  local value
  value="$(metric_value "$url" "$metric" "$source_id")"
  [[ -n "$value" ]] || fail "metric_missing url=$url metric=$metric source_id=$source_id"
  awk -v value="$value" 'BEGIN {exit !(value > 0)}' \
    || fail "metric_not_positive url=$url metric=$metric source_id=$source_id value=$value"
}

latest_ready_evidence_id() {
  local source_id="$1"
  local find_args=("$EVIDENCE_ROOT" -maxdepth 2 -name summary.json)
  if [[ -n "$EVIDENCE_MIN_MTIME_EPOCH" ]]; then
    find_args+=(-newermt "@$EVIDENCE_MIN_MTIME_EPOCH")
  fi
  find "${find_args[@]}" -printf '%T@ %p\n' \
    | sort -nr \
    | while read -r _ path; do
        jq -er --arg source_id "$source_id" '
          select(.source_id == $source_id)
          | select(.production_ready == true)
          | select(.visual_evidence_status == "verified")
          | .event_id
        ' "$path" 2>/dev/null || true
      done \
    | head -1
}

for container in "${required_containers[@]}"; do
  require_container_running "$container"
done

curl --noproxy '*' -fsS "$SAVANT_A_METRICS_URL" >/dev/null \
  || fail "savant_a_metrics_unreachable url=$SAVANT_A_METRICS_URL"
curl --noproxy '*' -fsS "$SAVANT_B_METRICS_URL" >/dev/null \
  || fail "savant_b_metrics_unreachable url=$SAVANT_B_METRICS_URL"

require_positive_metric "$FORWARDER_A_METRICS_URL" "va_forwarder_frames_seen_total" "$SOURCE_A_ID"
require_positive_metric "$FORWARDER_B_METRICS_URL" "va_forwarder_frames_seen_total" "$SOURCE_B_ID"
require_positive_metric "$FORWARDER_A_METRICS_URL" "va_forwarder_frames_forwarded_total" "$SOURCE_A_ID"
require_positive_metric "$FORWARDER_B_METRICS_URL" "va_forwarder_frames_forwarded_total" "$SOURCE_B_ID"
require_positive_metric "$SAVANT_A_METRICS_URL" "va_savant_frame_annotations_exported_total" "$SOURCE_A_ID"
require_positive_metric "$SAVANT_B_METRICS_URL" "va_savant_frame_annotations_exported_total" "$SOURCE_B_ID"

event_a="$(latest_ready_evidence_id "$SOURCE_A_ID")"
event_b="$(latest_ready_evidence_id "$SOURCE_B_ID")"

[[ -n "$event_a" ]] || fail "missing_ready_verified_evidence source_id=$SOURCE_A_ID"
[[ -n "$event_b" ]] || fail "missing_ready_verified_evidence source_id=$SOURCE_B_ID"
[[ "$event_a" != "$event_b" ]] || fail "same_evidence_id_for_both_sources event_id=$event_a"

echo "$PASS_TOKEN source_a=$SOURCE_A_ID evidence_a=$event_a source_b=$SOURCE_B_ID evidence_b=$event_b"
