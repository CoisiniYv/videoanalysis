#!/usr/bin/env bash
# List all artifact runs under VIDEO_ANALYTICS_ARTIFACT_ROOT.
#
# Usage:
#   bash scripts/maintenance/list_artifacts.sh
#   bash scripts/maintenance/list_artifacts.sh --phase d1-rtsp-15min
#   bash scripts/maintenance/list_artifacts.sh --json

set -euo pipefail

ARTIFACT_ROOT="${VIDEO_ANALYTICS_ARTIFACT_ROOT:-/data/video-analytics/artifacts}"
RUNS_DIR="${ARTIFACT_ROOT}/runs"
PHASE_FILTER=""
JSON_OUTPUT="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase)
      PHASE_FILTER="$2"
      shift 2
      ;;
    --json)
      JSON_OUTPUT="yes"
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$RUNS_DIR" ]]; then
  echo "No runs directory found at: $RUNS_DIR" >&2
  exit 0
fi

list_phase() {
  local phase_dir="$1"
  local phase_name
  phase_name="$(basename "$phase_dir")"

  for run_dir in "$phase_dir"/*/; do
    [[ -d "$run_dir" ]] || continue
    local run_id
    run_id="$(basename "$run_dir")"
    local file_count
    file_count="$(find "$run_dir" -type f | wc -l)"
    local total_size
    total_size="$(du -sh "$run_dir" 2>/dev/null | cut -f1)"
    local created_at=""
    if [[ -f "$run_dir/manifest.json" ]]; then
      created_at="$(python3 -c "
import json, sys
try:
    data = json.loads(open(sys.argv[1]).read())
    print(data.get('created_at', ''))
except Exception:
    print('')
" "$run_dir/manifest.json" 2>/dev/null || true)"
    fi
    local mtime
    mtime="$(stat -c '%Y' "$run_dir" 2>/dev/null || echo 0)"
    local mtime_human
    mtime_human="$(date -d "@$mtime" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown')"

    if [[ "$JSON_OUTPUT" == "yes" ]]; then
      printf '{"phase":"%s","run_id":"%s","files":%s,"size":"%s","created_at":"%s","mtime":"%s","path":"%s"}\n' \
        "$phase_name" "$run_id" "$file_count" "$total_size" "$created_at" "$mtime_human" "$run_dir"
    else
      printf "%-25s %-50s %5s files  %8s  %s  %s\n" \
        "$phase_name" "$run_id" "$file_count" "$total_size" "$mtime_human" "$created_at"
    fi
  done
}

if [[ "$JSON_OUTPUT" == "yes" ]]; then
  echo "["
  first="yes"
  for phase_dir in "$RUNS_DIR"/*/; do
    [[ -d "$phase_dir" ]] || continue
    phase_name="$(basename "$phase_dir")"
    [[ -n "$PHASE_FILTER" && "$phase_name" != "$PHASE_FILTER" ]] && continue
    while IFS= read -r line; do
      if [[ "$first" == "yes" ]]; then
        first="no"
      else
        echo ","
      fi
      printf "  %s" "$line"
    done < <(list_phase "$phase_dir")
  done
  echo ""
  echo "]"
else
  printf "%-25s %-50s %10s  %8s  %19s  %s\n" \
    "PHASE" "RUN_ID" "FILES" "SIZE" "MODIFIED" "CREATED_AT"
  printf "%s\n" "$(printf '%.0s-' {1..130})"
  for phase_dir in "$RUNS_DIR"/*/; do
    [[ -d "$phase_dir" ]] || continue
    phase_name="$(basename "$phase_dir")"
    [[ -n "$PHASE_FILTER" && "$phase_name" != "$PHASE_FILTER" ]] && continue
    list_phase "$phase_dir"
  done
fi
