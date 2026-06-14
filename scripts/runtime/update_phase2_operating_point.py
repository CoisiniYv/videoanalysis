#!/usr/bin/env python3
"""Update the dual-path T4 spec appendix from a passing Phase 2 report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PASS_TOKEN = "PASS_PHASE2_SINGLE_T4_30"
APPENDIX_HEADER = "## Appendix A — Measured Operating Point"


class AppendixUpdateError(RuntimeError):
    pass


def load_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppendixUpdateError(f"failed to read report {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise AppendixUpdateError("report must be a JSON object")
    return report


def validate_passing_report(report: dict[str, Any]) -> None:
    if report.get("passed") is not True or report.get("pass_token") != PASS_TOKEN:
        failed = [
            str(check.get("name"))
            for check in report.get("checks", [])
            if isinstance(check, dict) and check.get("ok") is False
        ]
        detail = ",".join(failed) if failed else "unknown"
        raise AppendixUpdateError(
            f"report is not a passing {PASS_TOKEN} report; failed_checks={detail}"
        )


def render_appendix(report: dict[str, Any], *, report_path: Path) -> str:
    validate_passing_report(report)
    operating_point = report.get("operating_point")
    if not isinstance(operating_point, dict):
        operating_point = {}
    return "\n".join(
        [
            f"T4 model / driver / CUDA: {_csv(operating_point.get('gpu_names'))}; driver/CUDA not recorded by runner",
            "pose engine batch / latency / throughput: not recorded by runner; fill from TensorRT engine measurement",
            "face engine batch / latency / throughput: not recorded by runner; fill from TensorRT engine measurement",
            "adaface engine batch / latency / throughput: not recorded by runner; fill from TensorRT engine measurement",
            (
                "chosen ANALYSIS_FPS: "
                f"target={_fmt(operating_point.get('target_fps'))}; "
                f"tolerance={_fmt(operating_point.get('fps_tolerance'))}; "
                f"observed_avg_min={_fmt(operating_point.get('per_source_effective_fps_avg_min'))}; "
                f"observed_avg_mean={_fmt(operating_point.get('per_source_effective_fps_avg_mean'))}; "
                f"observed_avg_max={_fmt(operating_point.get('per_source_effective_fps_avg_max'))}"
            ),
            "max_parallel_streams / batch_size / batched_push_timeout: not recorded by runner; fill from deployed env",
            (
                "per-shard NVDEC fps / GPU util / mem peak: "
                f"source_count={_fmt(operating_point.get('source_count'))}; "
                f"duration_s={_fmt(operating_point.get('duration_seconds'))}; "
                f"max_decoder_util_pct={_fmt(operating_point.get('max_decoder_util_pct'))}; "
                f"max_gpu_util_pct={_fmt(operating_point.get('max_gpu_util_pct'))}; "
                f"memory_peak_mib={_fmt(operating_point.get('memory_peak_mib'))}; "
                f"min_memory_headroom_pct={_fmt(operating_point.get('min_gpu_memory_headroom_pct'))}"
            ),
            (
                "Replay TTL / bitrate / working set / NVMe: "
                f"evidence_count_delta={_fmt(report.get('evidence_count_delta'))}; storage not recorded by runner"
            ),
            f"Phase 2 pressure report: {report_path}",
        ]
    )


def update_appendix_text(spec_text: str, appendix_body: str) -> str:
    header_index = spec_text.find(APPENDIX_HEADER)
    if header_index < 0:
        raise AppendixUpdateError(f"missing appendix header: {APPENDIX_HEADER}")
    fence_start = spec_text.find("```text", header_index)
    if fence_start < 0:
        raise AppendixUpdateError("missing Appendix A text fence")
    content_start = spec_text.find("\n", fence_start)
    if content_start < 0:
        raise AppendixUpdateError("malformed Appendix A text fence")
    content_start += 1
    fence_end = spec_text.find("```", content_start)
    if fence_end < 0:
        raise AppendixUpdateError("missing Appendix A closing fence")
    return spec_text[:content_start] + appendix_body.rstrip() + "\n" + spec_text[fence_end:]


def update_spec(*, report_path: Path, spec_path: Path, output_path: Path | None = None) -> str:
    report = load_report(report_path)
    appendix = render_appendix(report, report_path=report_path)
    updated = update_appendix_text(spec_path.read_text(encoding="utf-8"), appendix)
    target = output_path or spec_path
    target.write_text(updated, encoding="utf-8")
    return appendix


def _csv(value: Any) -> str:
    if isinstance(value, list) and value:
        return ", ".join(str(item) for item in value)
    if value:
        return str(value)
    return "not recorded"


def _fmt(value: Any) -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument(
        "--spec-path",
        type=Path,
        default=root / "specs/16_dual_path_30x2_t4_production_optimization.md",
    )
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = load_report(args.report_path)
        appendix = render_appendix(report, report_path=args.report_path)
        if args.dry_run:
            print(appendix)
            return 0
        update_spec(
            report_path=args.report_path,
            spec_path=args.spec_path,
            output_path=args.output_path,
        )
    except AppendixUpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("PASS_PHASE2_OPERATING_POINT_APPENDIX_UPDATED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
