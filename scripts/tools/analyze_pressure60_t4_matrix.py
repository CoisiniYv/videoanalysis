#!/usr/bin/env python3
"""Compare pressure60 T4 stage/output/CPU/timeout experiment artifacts."""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path
from statistics import mean
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_artifact(path: Path) -> dict[str, Any]:
    report = read_json(path / "report.json")
    config = report.get("config") or read_json(path / "run_config.json")
    diagnostics = report.get("diagnostics") or {}
    sample = diagnostics.get("sample_summary") or read_json(path / "sample_summary.json")
    db_summary = report.get("db_summary_before_cleanup") or read_json(
        path / "db_summary_before_cleanup.json"
    )
    rows = [row for row in sample.get("samples") or [] if isinstance(row, dict)]
    fps_values = [
        float(row["avg_effective_fps_10s"])
        for row in rows[1:]
        if row.get("avg_effective_fps_10s") is not None
    ]
    steady_fps = round(mean(fps_values), 4) if fps_values else None
    try:
        target_fps = float(Fraction(str(config.get("fps") or "0")))
    except (ValueError, ZeroDivisionError):
        target_fps = 0.0
    return {
        "run_id": report.get("run_id") or path.name,
        "artifact_dir": str(path),
        "status": report.get("status") or "missing_report",
        "failure_reasons": report.get("failure_reasons") or [],
        "stage": config.get("savant_ablation_stage") or "unknown",
        "output_mode": config.get("savant_output_mode") or "unknown",
        "cpu_profile": config.get("cpu_isolation_profile") or "none",
        "cuda_mps": bool(config.get("cuda_mps")),
        "adaface_classifier_async": bool(config.get("adaface_classifier_async")),
        "face_secondary_track_id": bool(config.get("face_secondary_track_id")),
        "adaface_input_queue": bool(config.get("adaface_input_queue")),
        "adaface_crop_resize": bool(config.get("adaface_crop_resize")),
        "adaface_pre_gate": bool(config.get("adaface_pre_gate")),
        "batch_timeout_us": int(config.get("batched_push_timeout") or 0),
        "stream_count": int(config.get("stream_count") or 0),
        "fps": config.get("fps"),
        "steady_effective_fps_mean": steady_fps,
        "steady_target_ratio": (
            round(steady_fps / target_fps, 4)
            if steady_fps is not None and target_fps > 0
            else None
        ),
        "last_effective_fps": (
            rows[-1].get("avg_effective_fps_10s") if rows else None
        ),
        "forwarded_target_ratio": sample.get("final_forwarded_target_ratio"),
        "max_queue_depth": sample.get("max_queue_depth"),
        "queue_full_samples": sample.get("queue_full_samples"),
        "send_failures_delta": sample.get("max_savant_send_failures_delta"),
        "max_savant_cpu_percent": sample.get("max_savant_cpu_percent"),
        "max_worker_cpu_percent": sample.get("max_worker_cpu_percent") or {},
        "stage_metrics": sample.get("final_savant_stage_metrics") or {},
        "events": int(db_summary.get("events") or 0),
        "tasks": int(db_summary.get("tasks") or 0),
        "bundles": int(db_summary.get("bundles") or 0),
        "playable_bundles": int(db_summary.get("playable_bundles") or 0),
    }


def diagnosis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def throughput_ratio(row: dict[str, Any]) -> float:
        value = row.get("steady_target_ratio")
        if value is None:
            value = row.get("forwarded_target_ratio")
        return float(value or 0)

    best_timeout = None
    timeout_rows = [row for row in rows if "bt0" in row["run_id"]]
    timeout_selection = "no_timeout_rows"
    if timeout_rows:
        eligible_timeout_rows = []
        for row in timeout_rows:
            nvinfer_full_ratios = [
                float((metrics or {}).get("batch_full_ratio") or 0.0)
                for stage, metrics in (row.get("stage_metrics") or {}).items()
                if stage in {"yolo26_pose", "yolov8_face", "adaface"}
            ]
            if (
                throughput_ratio(row) >= 0.99
                and int(row.get("queue_full_samples") or 0) == 0
                and int(row.get("send_failures_delta") or 0) == 0
                and nvinfer_full_ratios
                and min(nvinfer_full_ratios) >= 0.95
            ):
                eligible_timeout_rows.append(row)
        if eligible_timeout_rows:
            best_timeout = min(
                eligible_timeout_rows,
                key=lambda row: int(row.get("batch_timeout_us") or 0),
            )["batch_timeout_us"]
            timeout_selection = "lowest_timeout_meeting_throughput_and_batch_fullness"
        else:
            best_timeout = max(
                timeout_rows,
                key=lambda row: (
                    throughput_ratio(row),
                    -int(row.get("queue_full_samples") or 0),
                ),
            )["batch_timeout_us"]
            timeout_selection = "fallback_highest_throughput"

    ablation_rows = [row for row in rows if "_ab0" in row["run_id"]]
    first_material_drop = None
    bottleneck_row = None
    previous = None
    for row in ablation_rows:
        ratio = throughput_ratio(row)
        if bottleneck_row is None and ratio < 0.95:
            bottleneck_row = row
        if previous is not None and previous[1] - ratio >= 0.05:
            first_material_drop = {
                "from_stage": previous[0],
                "to_stage": row["stage"],
                "ratio_delta": round(ratio - previous[1], 4),
            }
            bottleneck_row = row
            break
        previous = (row["stage"], ratio)

    nvinfer_names = {"yolo26_pose", "yolov8_face", "adaface"}
    measured_total_ms = 0.0
    nvinfer_non_postproc_ms = 0.0
    nvinfer_compute_ms = 0.0
    nvinfer_compute_samples = 0
    nvinfer_postproc_ms = 0.0
    if bottleneck_row:
        stage_metrics = bottleneck_row.get("stage_metrics") or {}
        for stage, metrics in stage_metrics.items():
            if stage.endswith("_postproc"):
                continue
            duration_ms = float((metrics or {}).get("duration_mean_ms") or 0.0)
            measured_total_ms += duration_ms
            if stage in nvinfer_names:
                postproc_ms = float(
                    (stage_metrics.get(f"{stage}_postproc") or {}).get(
                        "duration_mean_ms"
                    )
                    or 0.0
                )
                nvinfer_postproc_ms += postproc_ms
                nvinfer_non_postproc_ms += max(0.0, duration_ms - postproc_ms)
                compute_ms = (metrics or {}).get("inference_compute_mean_ms")
                if compute_ms is not None:
                    nvinfer_compute_ms += float(compute_ms)
                    nvinfer_compute_samples += 1
    nvinfer_share = (
        round(nvinfer_compute_ms / measured_total_ms, 4)
        if measured_total_ms > 0 and nvinfer_compute_samples > 0
        else None
    )
    nvinfer_dominant = bool(
        bottleneck_row
        and throughput_ratio(bottleneck_row) < 0.95
        and nvinfer_share is not None
        and nvinfer_share >= 0.5
    )

    return {
        "best_batch_timeout_us": best_timeout,
        "batch_timeout_selection": timeout_selection,
        "first_material_ablation_drop": first_material_drop,
        "bottleneck_run_id": bottleneck_row.get("run_id") if bottleneck_row else None,
        "nvinfer_measured_share": nvinfer_share,
        "nvinfer_compute_metrics_available": nvinfer_compute_samples > 0,
        "nvinfer_compute_mean_ms_sum": round(nvinfer_compute_ms, 3),
        "nvinfer_non_postproc_element_ms": round(nvinfer_non_postproc_ms, 3),
        "nvinfer_postproc_ms": round(nvinfer_postproc_ms, 3),
        "nvinfer_dominant_proven": nvinfer_dominant,
        "int8_or_batch8_recommendation": (
            "eligible_for_controlled_experiment"
            if nvinfer_dominant
            else "pending_non_nvinfer_or_insufficient_stage_proof"
        ),
    }


def markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Pressure60 T4 analysis matrix",
        "",
        "| run | stage | output | cpu | MPS | AdaFace async | face track ID | AdaFace queue | bbox crop | pre-gate | timeout us | effective fps | steady/target | queue full | send failures | bundles |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["runs"]:
        lines.append(
            "| {run_id} | {stage} | {output_mode} | {cpu_profile} | {cuda_mps} | {adaface_classifier_async} | {face_secondary_track_id} | {adaface_input_queue} | {adaface_crop_resize} | {adaface_pre_gate} | {batch_timeout_us} | "
            "{steady_effective_fps_mean} | {steady_target_ratio} | {queue_full_samples} | "
            "{send_failures_delta} | {playable_bundles} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Diagnosis",
            "",
            "```json",
            json.dumps(summary["diagnosis"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("--matrix-id", required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args()

    paths = sorted(args.artifact_root.glob(f"pressure60_{args.matrix_id}_*"))
    rows = [summarize_artifact(path) for path in paths if path.is_dir()]
    summary = {
        "matrix_id": args.matrix_id,
        "artifact_root": str(args.artifact_root),
        "run_count": len(rows),
        "runs": rows,
        "diagnosis": diagnosis(rows),
    }
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.write_text(output + "\n", encoding="utf-8")
    if args.output_md:
        args.output_md.write_text(markdown(summary), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
