#!/usr/bin/env bash
set -euo pipefail

REPORT_PATH="${R3_3A2C_REPORT_PATH:-docs/r3_3a2c_replay_uuid_domain_report.md}"

echo "=== R3.3A2c Replay/Cache UUID Domain Smoke ==="
echo "report_path=${REPORT_PATH}"
echo "scope=uuid_domain_only"
echo "replay_api_calls=NO"
echo "clip_generation=NO"
echo "visual_alignment=NO"
echo "production_clip_worker=NO"
echo "production_media_worker=NO"
echo "production_video_file_sink=NO"
echo "db_migration=NO"
echo "performance_test=NO"

if [[ ! -f "${REPORT_PATH}" ]]; then
  echo "FAIL: report not found: ${REPORT_PATH}"
  exit 1
fi

python3 - <<'PY' "${REPORT_PATH}"
from __future__ import annotations

import re
import sys
from pathlib import Path


report_path = Path(sys.argv[1])
text = report_path.read_text(encoding="utf-8")

match = re.search(r"Conclusion:\s*`([^`]+)`", text)
if not match:
    raise SystemExit("FAIL: missing Conclusion field")

conclusion = match.group(1)
allowed = {'same_domain', 'different_domain', 'blocked'}
if conclusion not in allowed:
    raise SystemExit(f"FAIL: invalid conclusion={conclusion!r}")

required = [
    "UUID-independent",
    "Correlation Key Method",
    "UUID Generation Point",
    "clip_generated = false",
    "visual_alignment_performed = false",
    "No production clip-worker",
    "No production media-worker",
    "No production Video File Sink deployment",
    "No DB migration",
    "No performance test",
]
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit("FAIL: missing report fields: " + ", ".join(missing))

if conclusion == "blocked" and "PASS was not claimed" not in text:
    raise SystemExit("FAIL: blocked report must explicitly avoid PASS claim")

print(f"conclusion={conclusion}")
print("replay_cache_metadata_compared=false")
print("clip_generated=false")
print("visual_alignment_performed=false")
print("PASS: R3.3A2c report-only UUID domain smoke")
PY
