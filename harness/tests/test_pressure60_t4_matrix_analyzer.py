from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "tools" / "analyze_pressure60_t4_matrix.py"


def load_module():
    spec = importlib.util.spec_from_file_location("pressure60_t4_matrix_analyzer", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_artifact(
    root: Path,
    run_id: str,
    *,
    stage: str,
    ratio: float,
    timeout_us: int = 40000,
    queue_full_samples: int = 0,
    steady_fps: float = 3.9,
) -> Path:
    artifact = root / run_id
    artifact.mkdir()
    report = {
        "run_id": run_id,
        "status": "passed",
        "failure_reasons": [],
        "config": {
            "savant_ablation_stage": stage,
            "savant_output_mode": "metadata-only",
            "cpu_isolation_profile": "none",
            "cuda_mps": False,
            "adaface_classifier_async": False,
            "face_secondary_track_id": False,
            "adaface_input_queue": False,
            "adaface_crop_resize": False,
            "adaface_pre_gate": False,
            "adaface_decoupled": False,
            "batched_push_timeout": timeout_us,
            "stream_count": 60,
            "fps": "4/1",
        },
        "diagnostics": {
            "sample_summary": {
                "samples": [
                    {"avg_effective_fps_10s": 1.0},
                    {"avg_effective_fps_10s": steady_fps},
                    {"avg_effective_fps_10s": steady_fps},
                ],
                "final_forwarded_target_ratio": ratio,
                "max_queue_depth": 12,
                "queue_full_samples": queue_full_samples,
                "max_savant_send_failures_delta": 0,
                "max_savant_cpu_percent": 444.0,
                "max_worker_cpu_percent": {"media_worker": 120.0},
                "final_savant_stage_metrics": {
                    "yolo26_pose": {
                        "duration_mean_ms": 7.5,
                        "inference_compute_mean_ms": 7.0,
                        "duration_p95_upper_ms": 10.0,
                        "batch_occupancy": {"4": 9},
                        "batch_full_ratio": 0.98,
                    }
                },
            }
        },
        "db_summary_before_cleanup": {
            "events": 10,
            "tasks": 9,
            "bundles": 8,
            "playable_bundles": 7,
        },
    }
    (artifact / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return artifact


def test_summarize_artifact_reads_pressure_report_contract(tmp_path: Path) -> None:
    module = load_module()
    artifact = write_artifact(
        tmp_path,
        "pressure60_matrix_ab01_pose",
        stage="pose-only",
        ratio=0.97,
    )

    row = module.summarize_artifact(artifact)

    assert row["steady_effective_fps_mean"] == 3.9
    assert row["steady_target_ratio"] == 0.975
    assert row["cuda_mps"] is False
    assert row["adaface_classifier_async"] is False
    assert row["face_secondary_track_id"] is False
    assert row["adaface_input_queue"] is False
    assert row["adaface_crop_resize"] is False
    assert row["adaface_pre_gate"] is False
    assert row["adaface_decoupled"] is False
    assert row["forwarded_target_ratio"] == 0.97
    assert row["stage_metrics"]["yolo26_pose"]["batch_full_ratio"] == 0.98
    assert row["events"] == 10
    assert row["playable_bundles"] == 7


def test_diagnosis_finds_ablation_drop_and_best_timeout(tmp_path: Path) -> None:
    module = load_module()
    stages = [
        ("ab01_pose", "pose-only", 0.98),
        ("ab02_tracker", "pose-tracker-rules", 0.97),
        ("ab03_face", "pose-face", 0.84),
        ("ab04_adaface", "pose-face-adaface", 0.82),
    ]
    rows = [
        module.summarize_artifact(
            write_artifact(
                tmp_path,
                f"pressure60_matrix_{case_id}",
                stage=stage,
                ratio=ratio,
                steady_fps=ratio * 4,
            )
        )
        for case_id, stage, ratio in stages
    ]
    rows.extend(
        [
            module.summarize_artifact(
                write_artifact(
                    tmp_path,
                    "pressure60_matrix_bt01_10ms",
                    stage="full-exporter",
                    ratio=1.01,
                    timeout_us=10000,
                    steady_fps=1.01 * 4,
                )
            ),
            module.summarize_artifact(
                write_artifact(
                    tmp_path,
                    "pressure60_matrix_bt02_20ms",
                    stage="full-exporter",
                    ratio=1.02,
                    timeout_us=20000,
                    steady_fps=1.02 * 4,
                )
            ),
        ]
    )

    result = module.diagnosis(rows)

    assert result["best_batch_timeout_us"] == 10000
    assert result["batch_timeout_selection"] == (
        "lowest_timeout_meeting_throughput_and_batch_fullness"
    )
    assert result["first_material_ablation_drop"] == {
        "from_stage": "pose-tracker-rules",
        "to_stage": "pose-face",
        "ratio_delta": -0.13,
    }
    assert result["nvinfer_dominant_proven"] is True
    assert result["nvinfer_measured_share"] == 0.9333
    assert result["int8_or_batch8_recommendation"] == "eligible_for_controlled_experiment"


def test_diagnosis_does_not_claim_nvinfer_without_stage_timing() -> None:
    module = load_module()
    rows = [
        {
            "run_id": "pressure60_matrix_ab01_pose",
            "stage": "pose-only",
            "forwarded_target_ratio": 0.8,
            "stage_metrics": {},
        }
    ]

    result = module.diagnosis(rows)

    assert result["nvinfer_measured_share"] is None
    assert result["nvinfer_dominant_proven"] is False
    assert result["int8_or_batch8_recommendation"] == (
        "pending_non_nvinfer_or_insufficient_stage_proof"
    )


def test_diagnosis_subtracts_nvinfer_postprocessing_time() -> None:
    module = load_module()
    rows = [
        {
            "run_id": "pressure60_matrix_ab01_pose",
            "stage": "pose-only",
            "steady_target_ratio": 0.8,
            "stage_metrics": {
                "yolo26_pose": {"duration_mean_ms": 10.0},
                "yolo26_pose_postproc": {"duration_mean_ms": 9.0},
                "behavior_rules": {"duration_mean_ms": 1.0},
            },
        }
    ]

    result = module.diagnosis(rows)

    assert result["nvinfer_compute_metrics_available"] is False
    assert result["nvinfer_compute_mean_ms_sum"] == 0.0
    assert result["nvinfer_non_postproc_element_ms"] == 1.0
    assert result["nvinfer_postproc_ms"] == 9.0
    assert result["nvinfer_measured_share"] is None
    assert result["nvinfer_dominant_proven"] is False


def test_markdown_contains_run_and_diagnosis() -> None:
    module = load_module()
    summary = {
        "runs": [
            {
                "run_id": "pressure60_matrix_ab01_pose",
                "stage": "pose-only",
                "output_mode": "copy",
                "cpu_profile": "none",
                "cuda_mps": True,
                "adaface_classifier_async": True,
                "face_secondary_track_id": True,
                "adaface_input_queue": True,
                "adaface_crop_resize": True,
                "adaface_pre_gate": True,
                "adaface_decoupled": True,
                "batch_timeout_us": 40000,
                "steady_effective_fps_mean": 3.9,
                "steady_target_ratio": 0.975,
                "queue_full_samples": 0,
                "send_failures_delta": 0,
                "playable_bundles": 0,
            }
        ],
        "diagnosis": {"nvinfer_dominant_proven": False},
    }

    rendered = module.markdown(summary)

    assert "pressure60_matrix_ab01_pose" in rendered
    assert "| True |" in rendered
    assert '"nvinfer_dominant_proven": false' in rendered
