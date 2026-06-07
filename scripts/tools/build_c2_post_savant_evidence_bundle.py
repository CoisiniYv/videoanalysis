#!/usr/bin/env python3
"""Build a C2 production evidence bundle from post-Savant sink output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
if str(MEDIA_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))

from app.post_savant_evidence_bundle import build_post_savant_evidence_bundle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package post-Savant video-file-sink output as a C2 evidence bundle."
    )
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--copy-video", action="store_true", default=False)
    parser.add_argument("--trim-sidecar-to-video", action="store_true", default=False)
    parser.add_argument("--overwrite", action="store_true", default=False)
    parser.add_argument("--max-fps", default=None)
    parser.add_argument("--min-fps", default=None)
    parser.add_argument("--fps-gating-applied", action="store_true", default=None)
    parser.add_argument("--fps-gating-not-applied", action="store_false", dest="fps_gating_applied")
    parser.add_argument("--source-input-fps-estimate", type=float, default=None)
    args = parser.parse_args(argv)

    result = build_post_savant_evidence_bundle(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        copy_video=args.copy_video,
        trim_sidecar_to_video=args.trim_sidecar_to_video,
        overwrite=args.overwrite,
        max_fps=args.max_fps,
        min_fps=args.min_fps,
        fps_gating_applied=args.fps_gating_applied,
        source_input_fps_estimate=args.source_input_fps_estimate,
    )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
