#!/usr/bin/env bash
# Clean up old artifact runs under VIDEO_ANALYTICS_ARTIFACT_ROOT.
#
# DEFAULT: dry-run mode. Must pass --confirm to actually delete.
#
# Usage:
#   bash scripts/maintenance/cleanup_artifacts.sh                         # dry-run, all categories
#   bash scripts/maintenance/cleanup_artifacts.sh --category midterm      # dry-run, one category
#   bash scripts/maintenance/cleanup_artifacts.sh --older-than-days 7     # dry-run, older than 7 days
#   bash scripts/maintenance/cleanup_artifacts.sh --keep-latest 3         # dry-run, keep latest 3 per category
#   bash scripts/maintenance/cleanup_artifacts.sh --confirm               # ACTUALLY delete

set -euo pipefail

ARTIFACT_ROOT="${VIDEO_ANALYTICS_ARTIFACT_ROOT:-/data/video-analytics/artifacts}"
RUNS_DIR="${ARTIFACT_ROOT}/runs"
LATEST_DIR="${ARTIFACT_ROOT}/latest"

CATEGORY_FILTER=""
OLDER_THAN_DAYS=""
KEEP_LATEST=""
DRY_RUN="yes"
CONFIRM="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --category)
      CATEGORY_FILTER="$2"
      shift 2
      ;;
    --older-than-days)
      OLDER_THAN_DAYS="$2"
      shift 2
      ;;
    --keep-latest)
      KEEP_LATEST="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="yes"
      CONFIRM="no"
      shift
      ;;
    --confirm)
      DRY_RUN="no"
      CONFIRM="yes"
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--category <category>] [--older-than-days <N>] [--keep-latest <N>] [--dry-run] [--confirm]" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$RUNS_DIR" ]]; then
  echo "No runs directory found at: $RUNS_DIR"
  exit 0
fi

if [[ "$DRY_RUN" == "yes" ]]; then
  echo "=== DRY RUN MODE (no files will be deleted) ==="
  echo "Pass --confirm to actually delete."
  echo ""
fi

TOTAL_CLEANED=0
TOTAL_FREED=""

cleanup_category() {
  local category_dir="$1"
  local category_name
  category_name="$(basename "$category_dir")"

  local -a run_dirs=()
  for run_dir in "$category_dir"/*/; do
    [[ -d "$run_dir" ]] || continue
    run_dirs+=("$run_dir")
  done

  if [[ ${#run_dirs[@]} -eq 0 ]]; then
    return
  fi

  # Sort by mtime (oldest first)
  local -a sorted_dirs=()
  while IFS= read -r d; do
    sorted_dirs+=("$d")
  done < <(for d in "${run_dirs[@]}"; do
    echo "$(stat -c '%Y' "$d" 2>/dev/null || echo 0) $d"
  done | sort -n | awk '{print $2}')

  local -a to_clean=()

  # Filter by age
  if [[ -n "$OLDER_THAN_DAYS" ]]; then
    local cutoff_ts
    cutoff_ts="$(date -d "-${OLDER_THAN_DAYS} days" +%s 2>/dev/null || echo 0)"
    for run_dir in "${sorted_dirs[@]}"; do
      local mtime
      mtime="$(stat -c '%Y' "$run_dir" 2>/dev/null || echo 0)"
      if [[ "$mtime" -lt "$cutoff_ts" ]]; then
        to_clean+=("$run_dir")
      fi
    done
  else
    to_clean=("${sorted_dirs[@]}")
  fi

  # Apply keep-latest
  if [[ -n "$KEEP_LATEST" && "$KEEP_LATEST" -gt 0 ]]; then
    local total=${#to_clean[@]}
    if [[ "$total" -le "$KEEP_LATEST" ]]; then
      return
    fi
    local remove_count=$((total - KEEP_LATEST))
    to_clean=("${to_clean[@]:0:$remove_count}")
  fi

  if [[ ${#to_clean[@]} -eq 0 ]]; then
    return
  fi

  for run_dir in "${to_clean[@]}"; do
    local run_id
    run_id="$(basename "$run_dir")"
    local size
    size="$(du -sh "$run_dir" 2>/dev/null | cut -f1)"
    local mtime_human
    mtime_human="$(date -d "@$(stat -c '%Y' "$run_dir" 2>/dev/null || echo 0)" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown')"

    if [[ "$DRY_RUN" == "yes" ]]; then
      echo "  [DRY-RUN] Would remove: $category_name/$run_id ($size, $mtime_human)"
    else
      echo "  Removing: $category_name/$run_id ($size, $mtime_human)"
      rm -rf "$run_dir"
    fi
    TOTAL_CLEANED=$((TOTAL_CLEANED + 1))
  done
}

for category_dir in "$RUNS_DIR"/*/; do
  [[ -d "$category_dir" ]] || continue
  category_name="$(basename "$category_dir")"
  [[ -n "$CATEGORY_FILTER" && "$category_name" != "$CATEGORY_FILTER" ]] && continue
  cleanup_category "$category_dir"
done

echo ""
if [[ "$TOTAL_CLEANED" -eq 0 ]]; then
  echo "No runs matched the cleanup criteria."
else
  if [[ "$DRY_RUN" == "yes" ]]; then
    echo "Would clean up $TOTAL_CLEANED run(s). Pass --confirm to actually delete."
  else
    echo "Cleaned up $TOTAL_CLEANED run(s)."
  fi
fi
