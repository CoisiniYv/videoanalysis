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
    parser.add_argument("--requested-start-pts", type=int, default=None)
    parser.add_argument("--requested-end-pts", type=int, default=None)
    parser.add_argument("--event-frame-pts", type=int, default=None)
    parser.add_argument("--time-domain-crop-applied", action="store_true", default=False)
    parser.add_argument("--crop-video-to-time-window", action="store_true", default=False)
    parser.add_argument("--video-integrity-required", action="store_true", default=False)
    parser.add_argument("--evidence-capture-mode", default=None)
    parser.add_argument("--event-style-replay-job-passed", action="store_true", default=None)
    parser.add_argument("--event-style-replay-job-failed", action="store_false", dest="event_style_replay_job_passed")
    parser.add_argument("--replay-event-flow-status", default=None)
    parser.add_argument("--workaround-used", action="store_true", default=None)
    parser.add_argument("--workaround-not-used", action="store_false", dest="workaround_used")
    parser.add_argument("--workaround-reason", default=None)
    parser.add_argument("--replay-stop-strategy", default=None)
    args = parser.parse_args(argv)

    replay_timing_metadata = {}
    if args.requested_start_pts is not None:
        replay_timing_metadata["requested_start_pts"] = args.requested_start_pts
    if args.requested_end_pts is not None:
        replay_timing_metadata["requested_end_pts"] = args.requested_end_pts
    if args.requested_start_pts is not None and args.requested_end_pts is not None:
        replay_timing_metadata["requested_duration_s"] = (
            args.requested_end_pts - args.requested_start_pts
        ) / 1_000_000_000.0
    if args.replay_stop_strategy is not None:
        replay_timing_metadata["replay_stop_strategy"] = args.replay_stop_strategy

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
        requested_start_pts=args.requested_start_pts,
        requested_end_pts=args.requested_end_pts,
        event_frame_pts=args.event_frame_pts,
        time_domain_crop_applied=args.time_domain_crop_applied,
        crop_video_to_time_window=args.crop_video_to_time_window,
        video_integrity_required=args.video_integrity_required,
        evidence_capture_mode=args.evidence_capture_mode,
        event_style_replay_job_passed=args.event_style_replay_job_passed,
        replay_event_flow_status=args.replay_event_flow_status,
        workaround_used=args.workaround_used,
        workaround_reason=args.workaround_reason,
        replay_timing_metadata=replay_timing_metadata,
    )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
