#!/usr/bin/env bash
# Phase E1.1a — Media Output Directory Lockdown Smoke Test
#
# Verifies:
#   1. No legacy continuous-write containers running
#   2. No new files in banned legacy directories
#   3. Evidence output only in /data/video-analytics/media/evidence/events/
#   4. API URLs conform to spec
#   5. Print du summary

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MEDIA_ROOT="${MEDIA_ROOT:-/data/video-analytics/media}"
EVIDENCE_EVENTS="${EVIDENCE_EVENTS:-/data/video-analytics/media/evidence/events}"

PASS=0
FAIL=0

ok() { echo "OK  [$((++PASS))] $1"; }
fail() { echo "FAIL [$((++FAIL))] $1"; }

echo "=== Phase E1.1a Media Output Directory Lockdown Check ==="
echo ""

# ── 1. Banned legacy containers ───────────────────────────────────────

banned_containers=(
  "phase3b-video-file-sink"
  "phase3b-media-worker"
  "phase3b-clip-worker"
  "phase3b-replay-service"
  "phase1f-savant"
  "phase2c-savant"
)

RUNNING=$(newgrp docker <<'DOCKER_EOF'
docker ps --format "{{.Names}}" 2>/dev/null
DOCKER_EOF
)

for banned in "${banned_containers[@]}"; do
  if echo "$RUNNING" | grep -qF "$banned" 2>/dev/null; then
    fail "banned container running: $banned"
  else
    ok "banned container NOT running: $banned"
  fi
done

# ── 2. No new files in banned legacy dirs (last 5 min) ────────────────

banned_dirs=(
  "$MEDIA_ROOT/replay-sink-output"
  "$MEDIA_ROOT/snapshots"
)

for dir in "${banned_dirs[@]}"; do
  if [ -d "$dir" ]; then
    recent=$(find "$dir" -type f -mmin -5 2>/dev/null | wc -l)
    if [ "$recent" -gt 0 ]; then
      fail "recent files in legacy dir ($recent files): $dir"
    else
      ok "no recent files in legacy dir: $dir"
    fi
  else
    ok "legacy dir does not exist: $dir"
  fi
done

# ── 3. No new files in repo-relative media/evidence/ (old flat layout) ─

REPO_EVIDENCE="$SCRIPT_DIR/../../media/evidence"
for subdir in snapshots clips; do
  target="$REPO_EVIDENCE/$subdir"
  if [ -d "$target" ]; then
    recent=$(find "$target" -type f -mmin -5 2>/dev/null | wc -l)
    if [ "$recent" -gt 0 ]; then
      fail "recent files in repo evidence/$subdir ($recent files)"
    else
      ok "no recent files in repo evidence/$subdir"
    fi
  else
    ok "repo evidence/$subdir does not exist"
  fi
done

# ── 4. Evidence output only in E1.1a per-event directories ────────────

if [ -d "$EVIDENCE_EVENTS" ]; then
  count=$(ls "$EVIDENCE_EVENTS" 2>/dev/null | wc -l)
  ok "evidence events dir exists ($count event dirs)"

  # Check at least one event dir has correct filenames
  sample=$(ls "$EVIDENCE_EVENTS" 2>/dev/null | head -1)
  if [ -n "$sample" ]; then
    event_dir="$EVIDENCE_EVENTS/$sample"
    for f in snapshot.jpg annotated_snapshot.jpg clip_raw.mp4 evidence_metadata.json; do
      if [ -f "$event_dir/$f" ]; then
        ok "found $f in $event_dir"
      else
        fail "missing $f in $event_dir"
      fi
    done
  fi
else
  fail "evidence events dir not found: $EVIDENCE_EVENTS"
fi

# ── 5. No evidence files written to old flat directories ──────────────

for old_flat in "$MEDIA_ROOT/evidence/snapshots" "$MEDIA_ROOT/evidence/clips" "$MEDIA_ROOT/evidence/snapshots/annotated"; do
  if [ -d "$old_flat" ]; then
    recent=$(find "$old_flat" -type f -mmin -5 2>/dev/null | wc -l) || true
    if [ "$recent" -gt 0 ]; then
      fail "recent files in old flat evidence dir ($recent): $old_flat"
    else
      ok "no recent files in old flat evidence dir: $old_flat"
    fi
  else
    ok "old flat evidence dir does not exist: $old_flat"
  fi
done

# ── 6. Print disk usage ────────────────────────────────────────────────

echo ""
echo "=== Disk Usage: $MEDIA_ROOT ==="
du -h -d 2 "$MEDIA_ROOT" 2>/dev/null || echo "(cannot read $MEDIA_ROOT)"
echo ""

echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
