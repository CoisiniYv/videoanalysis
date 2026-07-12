#!/usr/bin/env python3
"""Run a midterm 60-source RTSP pressure test and retain evidence samples.

The script is intentionally operational rather than a unit-test harness.  It
uses PostgreSQL as the camera source of truth, applies the 8090 runtime control
APIs, samples runtime/GPU/DB state, then cleans temporary pressure data while
keeping a bounded set of playable evidence bundles for operator review.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
import yaml
from psycopg.rows import dict_row
from redis import Redis

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.evidence_lifecycle import (
    ACTIVE_COMPATIBILITY_TASK_STATUSES,
    ACTIVE_MATERIALIZATION_STATUSES,
    TERMINAL_MATERIALIZATION_STATUSES,
)

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from parse_video_file_sink_pressure import (  # noqa: E402
    aggregate_video_file_sink_metrics,
    parse_video_file_sink_log,
)


DEFAULT_DB_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6396/0"
DEFAULT_API_BASE = "http://127.0.0.1:8090/api/v1"
DEFAULT_COMPOSE_FILE = "infra/docker-compose.midterm.yml"
DEFAULT_ENV_FILE = "infra/env/midterm.env"
DEFAULT_RTSP_URI = "rtsp://192.168.1.105:8554/live/1080movie"
DEFAULT_ARTIFACT_ROOT = Path("/data/video-analytics/artifacts")
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_REPLAY_EPOCH_ROOT = Path("/data/video-analytics/media/replay-sink-output/midterm/epochs")
DEFAULT_MODULE_CONFIG_PATH = Path("modules/savant_security/config/cameras.midterm.yml")
SAVANT_MODULE_PATH = Path("modules/savant_security/module.yml")
DEFAULT_PRESSURE_FPS = "8/1"
DEFAULT_PRESSURE_DURATION_S = 600
DEFAULT_PRESSURE_DRAIN_S = 120
ROLLING_CACHE_FULL_RATE_MIN_RATIO = 0.90
SOURCE_CONTROLLER = Path("scripts/runtime/camera_source_controller.py")
SOURCE_ADAPTER_SITECUSTOMIZE = (
    REPO_ROOT / "scripts/runtime/source_adapter_overlay/sitecustomize.py"
)
DUAL_SHARD_PROFILE = "dual-4090-two-source"
CUDA_MPS_CONTAINER = "video-analytics-midterm-cuda-mps-pressure"
CUDA_MPS_IMAGE_DEFAULT = "ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1"
CUDA_MPS_RUNTIME_ROOT = Path("/tmp/video-analytics-mps-pressure")
DUAL_SHARD_SERVICES = [
    "savant-a",
    "savant-b",
    "analysis-forwarder-a",
    "analysis-forwarder-b",
    "replay-raw-fanout-a",
    "replay-raw-fanout-b",
    "video-file-sink-a",
    "video-file-sink-b",
    "video-file-sink-c",
    "video-file-sink-d",
    "video-file-sink-e",
    "video-file-sink-f",
    "video-file-sink-g",
    "video-file-sink-h",
    "replay-a",
    "replay-b",
    "replay-c",
    "replay-d",
    "replay-e",
    "replay-f",
    "replay-g",
    "replay-h",
]
DUAL_SHARD_SINGLE_SERVICES = ["source-adapter", "analysis-forwarder", "savant-security"]
DUAL_SHARD_FORWARDER_METRICS = {
    "replay-a": "http://127.0.0.1:18182/metrics",
    "replay-b": "http://127.0.0.1:18183/metrics",
}
DUAL_SHARD_FORWARDER_HEALTH = {
    "replay-a": "http://127.0.0.1:18182/healthz",
    "replay-b": "http://127.0.0.1:18183/healthz",
}
DUAL_SHARD_RAW_FORWARDER_METRICS = {
    "replay-a": "http://127.0.0.1:18185/metrics",
    "replay-b": "http://127.0.0.1:18186/metrics",
}
DUAL_SHARD_SAVANT_METRICS = {
    "replay-a": "http://127.0.0.1:18180/metrics",
    "replay-b": "http://127.0.0.1:18181/metrics",
}
ADAFACE_CENTRAL_METRICS_URL = "http://127.0.0.1:18187/metrics"
ADAFACE_SHARDED_CENTRAL_METRICS = {
    "replay-a": "http://127.0.0.1:18187/metrics",
    "replay-b": "http://127.0.0.1:18190/metrics",
}
ADAFACE_FORWARDER_METRICS = {
    "replay-a": "http://127.0.0.1:18188/metrics",
    "replay-b": "http://127.0.0.1:18189/metrics",
}
ADAFACE_DECOUPLED_SERVICES = [
    "adaface-forwarder-a",
    "adaface-forwarder-b",
    "savant-adaface-central",
]
ADAFACE_ROI_WORKER_SERVICE = "adaface-roi-worker"
ADAFACE_ROI_WORKER_CONTAINER = "video-analytics-midterm-adaface-roi-worker"
ADAFACE_ROI_METRICS_URL = "http://127.0.0.1:18187/metrics"
PROM_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
SAVANT_COUNTER_METRICS = {
    "va_savant_frames_seen_total",
    "va_savant_frame_annotations_exported_total",
    "va_savant_pose_stage_frames_total",
    "va_savant_pose_frames_with_person_total",
    "va_savant_pose_objects_total",
    "va_savant_face_stage_frames_total",
    "va_savant_face_frames_with_face_total",
    "va_savant_face_objects_total",
    "va_savant_adaface_embeddings_total",
    "va_savant_person_observations_exported_total",
    "va_savant_face_observations_exported_total",
}
SAVANT_GAUGE_METRICS = {
    "va_savant_effective_fps",
    "va_savant_last_frame_age_seconds",
}
ACTIVE_MATERIALIZATION_STATES = set(ACTIVE_MATERIALIZATION_STATUSES)
ACTIVE_TASK_STATES = set(ACTIVE_COMPATIBILITY_TASK_STATUSES)
TERMINAL_EVIDENCE_STATES = set(TERMINAL_MATERIALIZATION_STATUSES)
SECURITY_STREAMS = [
    "security.events",
    "security.face_observations",
    "security.person_observations",
    "security.frame_annotations",
    "security.record_requests",
    "security.alerts",
]
DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION = 6
DOWNSTREAM_OBSERVABILITY_REQUIRED_SECTIONS = (
    "redis",
    "postgresql",
    "qdrant",
    "event_worker",
    "face_worker",
    "media_worker",
    "evidence_types",
    "phase_latency_ms",
    "replay_admission",
    "replay_topology",
    "video_file_sink",
    "evidence_8090",
)
PRESSURE_OBSERVED_WINDOW_SLACK_S = 60.0
EVIDENCE_WINDOW_DURATION_TOLERANCE_S = 1.25
PRESSURE_COOLDOWN_SECONDS = 30
PRESSURE_COOLDOWN_GRACE_MS = 1000
PRESSURE_COOLDOWN_EVENT_TYPES = ("intrusion", "watchlist_hit")
WORKER_CONTAINER_NAMES = {
    "event_worker": "video-analytics-midterm-event-worker",
    "face_worker": "video-analytics-midterm-face-worker",
    "media_worker": "video-analytics-midterm-media-worker",
    "clip_worker": "video-analytics-midterm-clip-worker",
}
WORKER_COMPOSE_SERVICES = {
    "video-analytics-midterm-event-worker": "event-worker",
    "video-analytics-midterm-face-worker": "face-worker",
    "video-analytics-midterm-media-worker": "media-worker",
    "video-analytics-midterm-clip-worker": "clip-worker",
}


def remove_prefix(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


VIDEO_FILE_SINK_CONTAINERS = {
    "video-file-sink": "video-analytics-midterm-video-file-sink",
    "video-file-sink-a": "video-analytics-midterm-video-file-sink-a",
    "video-file-sink-b": "video-analytics-midterm-video-file-sink-b",
    "video-file-sink-c": "video-analytics-midterm-video-file-sink-c",
    "video-file-sink-d": "video-analytics-midterm-video-file-sink-d",
    "video-file-sink-e": "video-analytics-midterm-video-file-sink-e",
    "video-file-sink-f": "video-analytics-midterm-video-file-sink-f",
    "video-file-sink-g": "video-analytics-midterm-video-file-sink-g",
    "video-file-sink-h": "video-analytics-midterm-video-file-sink-h",
}
REPLAY_TOPOLOGY_CONTAINERS = {
    "replay-service": "video-analytics-midterm-replay-service",
    "replay-a": "video-analytics-midterm-replay-a",
    "replay-b": "video-analytics-midterm-replay-b",
    "replay-c": "video-analytics-midterm-replay-c",
    "replay-d": "video-analytics-midterm-replay-d",
    "replay-e": "video-analytics-midterm-replay-e",
    "replay-f": "video-analytics-midterm-replay-f",
    "replay-g": "video-analytics-midterm-replay-g",
    "replay-h": "video-analytics-midterm-replay-h",
    **VIDEO_FILE_SINK_CONTAINERS,
}
VIDEO_FILE_SINK_LOG_PATHS = {
    "video-file-sink": "video_file_sink_logs_since_start.txt",
    "video-file-sink-a": "video_file_sink_a_logs_since_start.txt",
    "video-file-sink-b": "video_file_sink_b_logs_since_start.txt",
    "video-file-sink-c": "video_file_sink_c_logs_since_start.txt",
    "video-file-sink-d": "video_file_sink_d_logs_since_start.txt",
    "video-file-sink-e": "video_file_sink_e_logs_since_start.txt",
    "video-file-sink-f": "video_file_sink_f_logs_since_start.txt",
    "video-file-sink-g": "video_file_sink_g_logs_since_start.txt",
    "video-file-sink-h": "video_file_sink_h_logs_since_start.txt",
}
EVIDENCE_SHARD_IDS = (
    "replay-a",
    "replay-b",
    "replay-c",
    "replay-d",
    "replay-e",
    "replay-f",
    "replay-g",
    "replay-h",
)
EVIDENCE_SHARD_BRANCH = {
    "replay-a": "a",
    "replay-b": "b",
    "replay-c": "a",
    "replay-d": "b",
    "replay-e": "a",
    "replay-f": "b",
    "replay-g": "a",
    "replay-h": "b",
}
SAVANT_ABLATION_STAGES = (
    "pose-only",
    "pose-tracker-rules",
    "pose-face",
    "pose-face-adaface",
    "full-exporter",
    "full-evidence",
)
SAVANT_ABLATION_ELEMENTS = {
    "pose-only": {"yolo26_pose", "savant_perf_metrics"},
    "pose-tracker-rules": {
        "yolo26_pose", "tracker", "behavior_rules", "savant_perf_metrics",
    },
    "pose-face": {
        "yolo26_pose", "tracker", "behavior_rules", "yolov8_face",
        "face_person_associator", "savant_perf_metrics",
    },
    "pose-face-adaface": {
        "yolo26_pose", "tracker", "behavior_rules", "yolov8_face",
        "face_person_associator", "adaface", "face_reid_gate",
        "savant_perf_metrics",
    },
    "full-exporter": {
        "yolo26_pose", "tracker", "behavior_rules", "yolov8_face",
        "face_person_associator", "face_roi_exporter", "adaface", "face_reid_gate",
        "face_observation_exporter", "frame_annotation_exporter",
        "savant_perf_metrics",
    },
    "full-evidence": None,
}
CPU_ISOLATION_PROFILES = {
    "none": {},
    "t4-16cpu": {
        "savant-a": "0-2,8-10",
        "savant-b": "3-5,11-13",
        "analysis-forwarder-a": "6,14",
        "analysis-forwarder-b": "6,14",
        "workers": "7,15",
    },
    "t4-16cpu-evidence": {
        "savant-a": "0-2,8-10",
        "savant-b": "3-5,11-13",
        "analysis-forwarder-a": "6,14",
        "analysis-forwarder-b": "6,14",
        "workers": "15",
        "worker-media-worker": "7",
        "worker-face-worker": "6,14",
        "drain-workers": "0-15",
    },
    "local-24cpu": {
        "savant-a": "0,2,4,6,8,10",
        "savant-b": "12-17",
        "analysis-forwarder-a": "18-19",
        "analysis-forwarder-b": "18-19",
        "workers": "20-23",
    },
}


@dataclass(frozen=True)
class PressureConfig:
    run_id: str
    stream_count: int
    rtsp_uri: str
    fps: str
    min_fps: str
    batch_size: int
    pose_batch_size: int
    face_detector_batch_size: int
    face_embedding_batch_size: int
    face_infer_interval: int
    face_embedding_infer_interval: int
    max_parallel_streams: int
    batched_push_timeout: int
    duration_s: int
    sample_interval_s: int
    drain_s: int
    guard_wait_s: int
    keep_evidence: int
    evidence_group_size: int
    evidence_policy_groups: tuple[tuple[int, int], ...]
    pressure_materialization_event_type_quotas: str
    pressure_disable_evidence_admission: bool
    pressure_algorithm_cooldown_s: int
    artifact_dir: Path
    db_url: str
    redis_url: str
    api_base: str
    compose_file: str
    env_file: str
    evidence_root: Path
    replay_epoch_root: Path
    force_runtime_restart: bool
    no_quiesce_before_guard: bool
    rtsp_republish_output_base: str
    rtsp_republish_input_uri: str
    rtsp_republish_mode: str
    rtsp_republish_local_server: bool
    rtsp_republish_local_server_image: str
    rtsp_republish_local_server_network: str
    rtsp_republish_local_server_gateway: str
    rtsp_republish_local_server_host_port: int
    rtsp_republish_warmup_s: int
    rtsp_republish_readiness_timeout_s: int
    rtsp_republish_readiness_parallelism: int
    rtsp_republish_readiness_restart_attempts: int
    rtsp_republish_input_offset_s: float
    rtsp_republish_input_loop: bool
    rtsp_republish_h264_repeat_headers: bool
    max_send_failures: int
    max_exited_sources: int
    max_validate_seq_iq: int
    forwarder_null_sink: bool
    dual_shard_same_gpu: bool
    dual_shard_api: bool
    dual_shard_gpu: str
    dual_shard_source_mode: str
    evidence_shard_count: int
    rolling_cache_evidence: bool
    rolling_cache_enable_coverage_merge: bool
    cleanup: bool
    clear_existing_evidence: bool = False
    discard_pressure_results: bool = False
    rolling_cache_prefill_s: int = 0
    rolling_cache_postfill_s: int = 0
    pressure_source_visibility_timeout_s: int = 180
    pressure_source_visibility_poll_s: int = 5
    pressure_source_visibility_stable_samples: int = 2
    pressure_source_visibility_restart_attempts: int = 1
    pressure_source_ffmpeg_timeout_ms: int = 60000
    pressure_source_ffmpeg_init_timeout_ms: int = 60000
    pressure_source_start_stagger_s: float = 0.5
    savant_ablation_stage: str = "full-evidence"
    savant_output_mode: str = "copy"
    cpu_isolation_profile: str = "none"
    cuda_mps: bool = False
    mps_savant_active_thread_percentage: int = 0
    mps_adaface_active_thread_percentage: int = 0
    adaface_classifier_async: bool = False
    face_secondary_track_id: bool = False
    adaface_input_queue: bool = False
    adaface_crop_resize: bool = False
    adaface_pre_gate: bool = False
    adaface_decoupled: bool = False
    max_adaface_forwarder_send_failure_ratio: float = 0.005
    adaface_decoupled_sharded: bool = False
    adaface_roi_redis: bool = False
    adaface_roi_batch_timeout_ms: int = 10
    media_worker_materialization_max_active: int = 4
    media_worker_rolling_remux_workers: int = 1
    preserve_warmup_results: bool = False
    pressure_sampling_start_event_ts_ms: int = 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a full midterm 60-source pressure test through 8090 runtime control."
    )
    parser.add_argument("--fps", default=DEFAULT_PRESSURE_FPS, help="Target pressure FPS, e.g. 8/1.")
    parser.add_argument("--min-fps", default="1/1")
    parser.add_argument("--streams", type=int, default=60)
    parser.add_argument("--duration-s", type=int, default=DEFAULT_PRESSURE_DURATION_S)
    parser.add_argument("--sample-interval-s", type=int, default=30)
    parser.add_argument("--drain-s", type=int, default=DEFAULT_PRESSURE_DRAIN_S)
    parser.add_argument(
        "--guard-wait-s",
        type=int,
        default=1200,
        help="Seconds to wait for existing evidence tasks after quiescing current sources.",
    )
    parser.add_argument(
        "--keep-evidence",
        type=int,
        default=-1,
        help=(
            "Number of playable evidence clips to retain in the report; use -1 "
            "for all playable evidence. Defaults to -1 for high-density acceptance."
        ),
    )
    parser.add_argument(
        "--evidence-group-size",
        type=int,
        default=10,
        help="Number of pressure cameras per evidence length group.",
    )
    parser.add_argument(
        "--evidence-policy-groups",
        default="2:2,3:3,5:5,8:8,10:10,15:15",
        help=(
            "Comma-separated pre:post seconds for pressure evidence groups. "
            "With --streams=60 and --evidence-group-size=10 this creates 6 groups."
        ),
    )
    parser.add_argument(
        "--pressure-materialization-event-type-quotas",
        default="auto",
        help=(
            "Temporary clip-worker materialization quotas for retained-evidence "
            "pressure runs. Use 'auto' to derive a 20%% headroom quota from "
            "--keep-evidence, or empty/off to disable."
        ),
    )
    parser.add_argument(
        "--pressure-disable-evidence-admission",
        action="store_true",
        help=(
            "Temporarily disable event-worker evidence admission/cooldown limits "
            "for pressure runs that need every recordable event to attempt materialization."
        ),
    )
    parser.add_argument(
        "--pressure-algorithm-cooldown-s",
        type=int,
        default=PRESSURE_COOLDOWN_SECONDS,
        help=(
            "Per source+algorithm cooldown for pressure cameras. Use 60 to cap "
            "a 60-camera, 2-algorithm, 600s run near 1200 events."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument(
        "--pose-batch-size",
        type=int,
        default=None,
        help="YOLO26-pose nvinfer batch size. Defaults to --batch-size.",
    )
    parser.add_argument(
        "--face-detector-batch-size",
        type=int,
        default=None,
        help="YOLOv8-face detector batch size. Defaults to --batch-size.",
    )
    parser.add_argument(
        "--face-embedding-batch-size",
        type=int,
        default=16,
        help="AdaFace embedding batch size.",
    )
    parser.add_argument(
        "--face-infer-interval",
        type=int,
        default=None,
        help=(
            "YOLOv8-Face nvinfer interval. Defaults to a value derived from "
            "--fps so face detector inference is about 1 FPS."
        ),
    )
    parser.add_argument(
        "--face-embedding-infer-interval",
        type=int,
        default=None,
        help=(
            "AdaFace nvinfer interval. Defaults to --face-infer-interval, "
            "so embedding inference is also about 1 FPS."
        ),
    )
    parser.add_argument("--max-parallel-streams", type=int, default=64)
    parser.add_argument("--batched-push-timeout", type=int, default=40000)
    parser.add_argument(
        "--media-worker-materialization-max-active",
        type=int,
        default=4,
        help=(
            "Shared end-to-end media-worker materialization WIP limit. "
            "Phase 6 compares this value with all lane worker counts fixed."
        ),
    )
    parser.add_argument(
        "--media-worker-rolling-remux-workers",
        type=int,
        default=1,
        help=(
            "Rolling-cache remux lane worker count. Keep at 1 for the Phase 6 "
            "max_active matrix; vary only in a separately labeled remux-lane experiment."
        ),
    )
    parser.add_argument(
        "--savant-ablation-stage",
        choices=SAVANT_ABLATION_STAGES,
        default="full-evidence",
        help="Cumulative Savant pipeline stage used for bottleneck isolation.",
    )
    parser.add_argument(
        "--savant-output-mode",
        choices=("copy", "metadata-only"),
        default="copy",
        help="Emit pass-through frame content or metadata only from Savant.",
    )
    parser.add_argument(
        "--cpu-isolation-profile",
        choices=tuple(CPU_ISOLATION_PROFILES),
        default="none",
        help="Temporary cpuset layout for inference and downstream worker isolation.",
    )
    parser.add_argument(
        "--cuda-mps",
        action="store_true",
        help=(
            "Run both same-GPU Savant branches through a temporary CUDA MPS "
            "control/server container. The helper is removed during restore."
        ),
    )
    parser.add_argument(
        "--mps-savant-active-thread-percentage",
        type=int,
        default=0,
        help="CUDA MPS active-thread percentage for each Savant branch (0 disables).",
    )
    parser.add_argument(
        "--mps-adaface-active-thread-percentage",
        type=int,
        default=0,
        help="CUDA MPS active-thread percentage for the ROI AdaFace worker (0 disables).",
    )
    parser.add_argument(
        "--adaface-classifier-async",
        action="store_true",
        help=(
            "Enable DeepStream secondary classifier async mode for the AdaFace "
            "pressure module. Diagnostic-only until embedding integrity passes."
        ),
    )
    parser.add_argument(
        "--face-secondary-track-id",
        action="store_true",
        help=(
            "Propagate associated person track IDs onto face objects so "
            "DeepStream secondary reinference caching can identify them."
        ),
    )
    parser.add_argument(
        "--adaface-input-queue",
        action="store_true",
        help=(
            "Insert a bounded non-leaky GStreamer queue immediately before "
            "AdaFace to isolate upstream face detection scheduling."
        ),
    )
    parser.add_argument(
        "--adaface-crop-resize",
        action="store_true",
        help=(
            "Replace landmark alignment with bbox crop+resize for a diagnostic "
            "canary. Do not use as the production face-quality default."
        ),
    )
    parser.add_argument(
        "--adaface-pre-gate",
        action="store_true",
        help=(
            "Create cadence-throttled face candidate clones before AdaFace so "
            "only downstream-export-eligible faces consume embedding inference."
        ),
    )
    parser.add_argument(
        "--adaface-decoupled",
        action="store_true",
        help=(
            "Keep AdaFace off the dual-YOLO critical path and feed one central "
            "AdaFace Savant module through bounded drop-capable forwarders."
        ),
    )
    parser.add_argument(
        "--adaface-decoupled-sharded",
        action="store_true",
        help=(
            "Run one decoupled AdaFace sidecar per 30-source YOLO shard. "
            "Diagnostic until T4 throughput and bounded-loss gates pass."
        ),
    )
    parser.add_argument(
        "--adaface-roi-redis",
        action="store_true",
        help=(
            "Disable inline/full-H264 AdaFace and export only aligned 112x112 "
            "face crops to a bounded Redis Stream consumed by the batch16 "
            "adaface-roi-worker. Requires --dual-shard-same-gpu."
        ),
    )
    parser.add_argument(
        "--adaface-roi-batch-timeout-ms",
        type=int,
        default=10,
        help="Maximum ROI aggregation wait for the batch16 AdaFace worker.",
    )
    parser.add_argument("--rtsp-uri", default=DEFAULT_RTSP_URI)
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL", DEFAULT_DB_URL))
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--compose-file", default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--replay-epoch-root", type=Path, default=DEFAULT_REPLAY_EPOCH_ROOT)
    parser.add_argument("--force-runtime-restart", action="store_true")
    parser.add_argument(
        "--no-quiesce-before-guard",
        action="store_true",
        help="Do not disable existing cameras before waiting for evidence guard clearance.",
    )
    parser.add_argument(
        "--rtsp-republish-output-base",
        default="",
        help=(
            "Optional RTSP output base for host ffmpeg republishers. "
            "Supports {run_id}, {index}, and {source_id}; when no placeholder is present, "
            "/{source_id} is appended. Example: rtsp://127.0.0.1:8554/pressure/{run_id}"
        ),
    )
    parser.add_argument(
        "--rtsp-republish-input-uri",
        default="",
        help="Input URI for RTSP republishers. Defaults to --rtsp-uri.",
    )
    parser.add_argument(
        "--rtsp-republish-mode",
        choices=("copy", "transcode"),
        default="copy",
        help="Use copy first to avoid extra CPU; transcode only if the RTSP server/source needs it.",
    )
    parser.add_argument(
        "--rtsp-republish-warmup-s",
        type=int,
        default=10,
        help="Seconds to let ffmpeg republishers warm up before 8090 runtime restart.",
    )
    parser.add_argument(
        "--rtsp-republish-input-offset-s",
        type=float,
        default=0.0,
        help=(
            "Deterministic input offset for host ffmpeg republishers. "
            "Use with a fixed local file or VOD URI to make pressure runs comparable."
        ),
    )
    parser.add_argument(
        "--rtsp-republish-input-loop",
        action="store_true",
        help=(
            "Loop the republisher input indefinitely. Intended for deterministic "
            "fixed-file pressure runs whose source file may be shorter than the run."
        ),
    )
    parser.add_argument(
        "--rtsp-republish-h264-repeat-headers",
        action="store_true",
        help=(
            "For fixed H.264 copy inputs, convert to Annex B and inject codec "
            "extradata at keyframes so Savant parsers do not depend on RTSP SDP "
            "parameter-set propagation."
        ),
    )
    parser.add_argument(
        "--rtsp-republish-local-server",
        action="store_true",
        help=(
            "Start a run-scoped MediaMTX container and publish pressure paths "
            "through the current Compose network gateway instead of a shared "
            "external RTSP service."
        ),
    )
    parser.add_argument(
        "--rtsp-republish-local-server-image",
        default="bluenviron/mediamtx:1.11.3",
        help="Pinned MediaMTX image used by the run-scoped RTSP server.",
    )
    parser.add_argument(
        "--rtsp-republish-local-server-network",
        default="video-analytics-midterm_default",
        help="Docker network whose gateway is reachable by pressure adapters.",
    )
    parser.add_argument(
        "--rtsp-republish-local-server-host-port",
        type=int,
        default=18554,
        help="Host RTSP port mapped to the run-scoped MediaMTX container.",
    )
    parser.add_argument(
        "--rtsp-republish-readiness-timeout-s",
        type=int,
        default=120,
        help=(
            "Seconds to require every republished RTSP path to be readable by "
            "ffprobe before source adapter containers are started."
        ),
    )
    parser.add_argument(
        "--rtsp-republish-readiness-parallelism",
        type=int,
        default=4,
        help="Maximum concurrent RTSP readability probes.",
    )
    parser.add_argument(
        "--rtsp-republish-readiness-restart-attempts",
        type=int,
        default=1,
        help=(
            "Targeted restart attempts for a publisher process whose RTSP path "
            "remains unreadable even though the process is alive."
        ),
    )
    parser.add_argument(
        "--rolling-cache-prefill-s",
        type=int,
        default=0,
        help=(
            "Seconds to let pressure sources fill rolling-cache segments before "
            "the measured window starts. Pressure DB rows from the warmup are "
            "deleted before sampling so pre-roll evidence windows are possible."
        ),
    )
    parser.add_argument(
        "--preserve-warmup-results",
        action="store_true",
        help=(
            "Retain prefill/visibility event and evidence rows. Formal pressure "
            "queries are fenced by the measured-window start timestamp."
        ),
    )
    parser.add_argument(
        "--rolling-cache-postfill-s",
        type=int,
        default=0,
        help=(
            "Seconds to keep pressure sources running after the measured window "
            "so tail events can accumulate post-roll rolling-cache coverage. "
            "Postfill events are retained by default for operator review."
        ),
    )
    parser.add_argument(
        "--clear-existing-evidence",
        action="store_true",
        help=(
            "Destructive evidence reset: delete all existing events and their "
            "database/filesystem evidence before the run. Face/person observations, "
            "registered people, and trajectory images are always preserved."
        ),
    )
    parser.add_argument(
        "--discard-pressure-results",
        action="store_true",
        help=(
            "Destructive isolation option: delete this run's non-retained events, "
            "observations, and evidence during cleanup. Disabled by default; normal "
            "cleanup only stops temporary runtime sources and preserves all visual results."
        ),
    )
    parser.add_argument(
        "--pressure-source-visibility-timeout-s",
        type=int,
        default=180,
        help=(
            "Seconds to wait for every pressure source to be visible in "
            "forwarder/Savant metrics before starting the measured window."
        ),
    )
    parser.add_argument(
        "--pressure-source-visibility-poll-s",
        type=int,
        default=5,
        help="Polling interval for the pressure source visibility barrier.",
    )
    parser.add_argument(
        "--pressure-source-visibility-stable-samples",
        type=int,
        default=2,
        help="Consecutive all-visible samples required before sampling starts.",
    )
    parser.add_argument(
        "--pressure-source-visibility-restart-attempts",
        type=int,
        default=1,
        help=(
            "Number of restart attempts for pressure source adapters still "
            "missing from runtime metrics during the visibility barrier."
        ),
    )
    parser.add_argument(
        "--pressure-source-ffmpeg-timeout-ms",
        type=int,
        default=60000,
        help=(
            "FFMPEG_TIMEOUT_MS for pressure source adapters. 60 parallel RTSP "
            "pulls can exceed the default 20s source startup budget."
        ),
    )
    parser.add_argument(
        "--pressure-source-ffmpeg-init-timeout-ms",
        type=int,
        default=60000,
        help=(
            "ffmpeg_input constructor timeout for pressure source adapters. "
            "Savant 0.6.0 otherwise keeps its independent 10s init default."
        ),
    )
    parser.add_argument(
        "--pressure-source-start-stagger-s",
        type=float,
        default=0.5,
        help=(
            "Seconds to wait between pressure source adapter starts. This avoids "
            "a 60-way RTSP pull startup burst against the local republisher."
        ),
    )
    parser.add_argument(
        "--max-send-failures",
        type=int,
        default=0,
        help="Maximum allowed analysis-forwarder Savant send failures.",
    )
    parser.add_argument(
        "--max-adaface-forwarder-send-failure-ratio",
        type=float,
        default=0.005,
        help=(
            "Maximum failed/attempted send ratio for the bounded, asynchronous "
            "central AdaFace forwarders. The primary forwarder remains governed "
            "by --max-send-failures."
        ),
    )
    parser.add_argument(
        "--max-exited-sources",
        type=int,
        default=0,
        help="Maximum allowed pressure source containers not running during diagnostics.",
    )
    parser.add_argument(
        "--max-validate-seq-iq",
        type=int,
        default=0,
        help="Maximum allowed validate_seq_iq log lines in Savant logs.",
    )
    parser.add_argument(
        "--forwarder-null-sink",
        action="store_true",
        help=(
            "Run the pressure sources through Replay and analysis-forwarder, "
            "but count forwarded frames in a null sink instead of sending to Savant."
        ),
    )
    parser.add_argument(
        "--dual-shard-same-gpu",
        action="store_true",
        help=(
            "Run pressure sources through replay-a/b, analysis-forwarder-a/b, "
            "and savant-a/b, with both Savant branches pinned to one GPU."
        ),
    )
    parser.add_argument(
        "--dual-shard-api",
        action="store_true",
        help="Apply the dual-shard topology through the 8090 runtime topology API.",
    )
    parser.add_argument(
        "--dual-shard-gpu",
        default="0",
        help="Physical GPU id used by both Savant branches in --dual-shard-same-gpu mode.",
    )
    parser.add_argument(
        "--dual-shard-source-mode",
        choices=("balanced", "all-a", "all-b"),
        default="balanced",
        help=(
            "Source placement for dual-shard pressure runs. Use all-a/all-b for "
            "single-shard capacity probes before changing shard count."
        ),
    )
    parser.add_argument(
        "--evidence-shard-count",
        type=int,
        choices=(2, 4, 8),
        default=4,
        help=(
            "Replay/video-file-sink evidence shard count for dual-shard pressure "
            "runs. The inference topology remains dual, but evidence export can "
            "be split across four or eight Replay/sink outlets."
        ),
    )
    parser.add_argument(
        "--rolling-cache-evidence",
        action="store_true",
        help=(
            "Canary mode: suppress per-event Replay record requests and let "
            "media-worker materialize evidence from rolling cache segments."
        ),
    )
    parser.add_argument(
        "--rolling-cache-enable-coverage-merge",
        action="store_true",
        help=(
            "Enable same-source event coverage/alias merge during rolling-cache "
            "pressure canaries."
        ),
    )
    parser.add_argument("--no-cleanup", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.dual_shard_same_gpu and args.forwarder_null_sink:
        raise SystemExit("--dual-shard-same-gpu cannot be combined with --forwarder-null-sink")
    if args.dual_shard_api and not args.dual_shard_same_gpu:
        raise SystemExit("--dual-shard-api requires --dual-shard-same-gpu")
    if args.cuda_mps and not args.dual_shard_same_gpu:
        raise SystemExit("--cuda-mps requires --dual-shard-same-gpu")
    for name, value in (
        ("--mps-savant-active-thread-percentage", args.mps_savant_active_thread_percentage),
        ("--mps-adaface-active-thread-percentage", args.mps_adaface_active_thread_percentage),
    ):
        if not 0 <= int(value) <= 100:
            raise SystemExit(f"{name} must be between 0 and 100")
    if args.adaface_decoupled_sharded and not args.adaface_decoupled:
        raise SystemExit(
            "--adaface-decoupled-sharded requires --adaface-decoupled"
        )
    if args.adaface_roi_redis and not args.dual_shard_same_gpu:
        raise SystemExit("--adaface-roi-redis requires --dual-shard-same-gpu")
    if args.adaface_roi_redis and args.adaface_decoupled:
        raise SystemExit(
            "--adaface-roi-redis cannot be combined with --adaface-decoupled"
        )
    if args.adaface_roi_redis and args.savant_ablation_stage not in {
        "full-exporter",
        "full-evidence",
    }:
        raise SystemExit(
            "--adaface-roi-redis requires full-exporter or full-evidence stage"
        )
    if not 0.0 <= args.max_adaface_forwarder_send_failure_ratio <= 1.0:
        raise SystemExit(
            "--max-adaface-forwarder-send-failure-ratio must be between 0 and 1"
        )
    if args.media_worker_materialization_max_active < 0:
        raise SystemExit(
            "--media-worker-materialization-max-active must be non-negative"
        )
    if args.media_worker_rolling_remux_workers < 1:
        raise SystemExit("--media-worker-rolling-remux-workers must be positive")
    if (
        args.rolling_cache_evidence
        and args.media_worker_materialization_max_active < 2
    ):
        raise SystemExit(
            "--rolling-cache-evidence requires "
            "--media-worker-materialization-max-active >= 2"
        )
    if (
        args.rolling_cache_evidence
        and args.media_worker_rolling_remux_workers
        > args.media_worker_materialization_max_active
    ):
        raise SystemExit(
            "--media-worker-rolling-remux-workers cannot exceed "
            "--media-worker-materialization-max-active"
        )
    if args.dual_shard_same_gpu and args.keep_evidence > 0 and not args.dual_shard_api:
        raise SystemExit(
            "--dual-shard-same-gpu with evidence retention requires "
            "--dual-shard-api so clip-worker can be wired to the topology "
            "replay shard plan."
        )
    run_id = args.run_id or _default_run_id(
        args.fps,
        forwarder_null_sink=args.forwarder_null_sink,
        dual_shard_same_gpu=args.dual_shard_same_gpu,
        dual_shard_api=args.dual_shard_api,
    )
    face_infer_interval = (
        args.face_infer_interval
        if args.face_infer_interval is not None
        else infer_interval_for_target_fps(args.fps, target_fps=Fraction(1, 1))
    )
    artifact_dir = (args.artifact_root / run_id).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    local_rtsp_gateway = (
        docker_network_gateway(args.rtsp_republish_local_server_network)
        if args.rtsp_republish_local_server
        else ""
    )
    rtsp_republish_output_base = args.rtsp_republish_output_base
    if args.rtsp_republish_local_server:
        rtsp_republish_output_base = (
            f"rtsp://{local_rtsp_gateway}:"
            f"{max(1, int(args.rtsp_republish_local_server_host_port))}"
            "/pressure/{run_id}/{source_id}"
        )
    cfg = PressureConfig(
        run_id=run_id,
        stream_count=args.streams,
        rtsp_uri=args.rtsp_uri,
        fps=args.fps,
        min_fps=args.min_fps,
        batch_size=args.batch_size,
        pose_batch_size=args.pose_batch_size or args.batch_size,
        face_detector_batch_size=args.face_detector_batch_size or args.batch_size,
        face_embedding_batch_size=args.face_embedding_batch_size,
        face_infer_interval=face_infer_interval,
        face_embedding_infer_interval=(
            args.face_embedding_infer_interval
            if args.face_embedding_infer_interval is not None
            else face_infer_interval
        ),
        max_parallel_streams=args.max_parallel_streams,
        batched_push_timeout=args.batched_push_timeout,
        duration_s=args.duration_s,
        sample_interval_s=args.sample_interval_s,
        drain_s=args.drain_s,
        guard_wait_s=args.guard_wait_s,
        keep_evidence=args.keep_evidence,
        evidence_group_size=args.evidence_group_size,
        evidence_policy_groups=parse_evidence_policy_groups(
            args.evidence_policy_groups
        ),
        pressure_materialization_event_type_quotas=(
            pressure_materialization_event_type_quotas(
                args.pressure_materialization_event_type_quotas,
                keep_evidence=args.keep_evidence,
            )
        ),
        pressure_disable_evidence_admission=args.pressure_disable_evidence_admission,
        pressure_algorithm_cooldown_s=max(
            1,
            int(args.pressure_algorithm_cooldown_s or PRESSURE_COOLDOWN_SECONDS),
        ),
        artifact_dir=artifact_dir,
        db_url=args.db_url,
        redis_url=args.redis_url,
        api_base=args.api_base.rstrip("/"),
        compose_file=args.compose_file,
        env_file=args.env_file,
        evidence_root=args.evidence_root,
        replay_epoch_root=args.replay_epoch_root,
        force_runtime_restart=args.force_runtime_restart,
        no_quiesce_before_guard=args.no_quiesce_before_guard,
        rtsp_republish_output_base=rtsp_republish_output_base,
        rtsp_republish_input_uri=args.rtsp_republish_input_uri or args.rtsp_uri,
        rtsp_republish_mode=args.rtsp_republish_mode,
        rtsp_republish_local_server=bool(args.rtsp_republish_local_server),
        rtsp_republish_local_server_image=str(
            args.rtsp_republish_local_server_image
        ),
        rtsp_republish_local_server_network=str(
            args.rtsp_republish_local_server_network
        ),
        rtsp_republish_local_server_gateway=local_rtsp_gateway,
        rtsp_republish_local_server_host_port=max(
            1,
            int(args.rtsp_republish_local_server_host_port),
        ),
        rtsp_republish_warmup_s=args.rtsp_republish_warmup_s,
        rtsp_republish_readiness_timeout_s=max(
            0,
            int(args.rtsp_republish_readiness_timeout_s or 0),
        ),
        rtsp_republish_readiness_parallelism=max(
            1,
            int(args.rtsp_republish_readiness_parallelism or 1),
        ),
        rtsp_republish_readiness_restart_attempts=max(
            0,
            int(args.rtsp_republish_readiness_restart_attempts or 0),
        ),
        rtsp_republish_input_offset_s=max(0.0, float(args.rtsp_republish_input_offset_s or 0.0)),
        rtsp_republish_input_loop=bool(args.rtsp_republish_input_loop),
        rtsp_republish_h264_repeat_headers=bool(
            args.rtsp_republish_h264_repeat_headers
        ),
        rolling_cache_prefill_s=max(0, int(args.rolling_cache_prefill_s or 0)),
        rolling_cache_postfill_s=max(0, int(args.rolling_cache_postfill_s or 0)),
        max_send_failures=args.max_send_failures,
        max_exited_sources=args.max_exited_sources,
        max_validate_seq_iq=args.max_validate_seq_iq,
        forwarder_null_sink=args.forwarder_null_sink,
        dual_shard_same_gpu=args.dual_shard_same_gpu,
        dual_shard_api=args.dual_shard_api,
        dual_shard_gpu=str(args.dual_shard_gpu),
        dual_shard_source_mode=args.dual_shard_source_mode,
        evidence_shard_count=args.evidence_shard_count,
        rolling_cache_evidence=bool(args.rolling_cache_evidence),
        rolling_cache_enable_coverage_merge=bool(
            args.rolling_cache_enable_coverage_merge
        ),
        cleanup=not args.no_cleanup,
        clear_existing_evidence=bool(args.clear_existing_evidence),
        discard_pressure_results=bool(args.discard_pressure_results),
        pressure_source_visibility_timeout_s=max(
            0,
            int(args.pressure_source_visibility_timeout_s or 0),
        ),
        pressure_source_visibility_poll_s=max(
            1,
            int(args.pressure_source_visibility_poll_s or 1),
        ),
        pressure_source_visibility_stable_samples=max(
            1,
            int(args.pressure_source_visibility_stable_samples or 1),
        ),
        pressure_source_visibility_restart_attempts=max(
            0,
            int(args.pressure_source_visibility_restart_attempts or 0),
        ),
        pressure_source_ffmpeg_timeout_ms=max(
            1000,
            int(args.pressure_source_ffmpeg_timeout_ms or 60000),
        ),
        pressure_source_ffmpeg_init_timeout_ms=max(
            1000,
            int(args.pressure_source_ffmpeg_init_timeout_ms or 60000),
        ),
        pressure_source_start_stagger_s=max(
            0.0,
            float(args.pressure_source_start_stagger_s or 0.0),
        ),
        savant_ablation_stage=args.savant_ablation_stage,
        savant_output_mode=args.savant_output_mode,
        cpu_isolation_profile=args.cpu_isolation_profile,
        cuda_mps=bool(args.cuda_mps),
        mps_savant_active_thread_percentage=int(
            args.mps_savant_active_thread_percentage
        ),
        mps_adaface_active_thread_percentage=int(
            args.mps_adaface_active_thread_percentage
        ),
        adaface_classifier_async=bool(args.adaface_classifier_async),
        face_secondary_track_id=bool(args.face_secondary_track_id),
        adaface_input_queue=bool(args.adaface_input_queue),
        adaface_crop_resize=bool(args.adaface_crop_resize),
        adaface_pre_gate=bool(args.adaface_pre_gate),
        adaface_decoupled=bool(args.adaface_decoupled),
        max_adaface_forwarder_send_failure_ratio=float(
            args.max_adaface_forwarder_send_failure_ratio
        ),
        adaface_decoupled_sharded=bool(args.adaface_decoupled_sharded),
        adaface_roi_redis=bool(args.adaface_roi_redis),
        adaface_roi_batch_timeout_ms=max(
            1, int(args.adaface_roi_batch_timeout_ms)
        ),
        media_worker_materialization_max_active=int(
            args.media_worker_materialization_max_active
        ),
        media_worker_rolling_remux_workers=int(
            args.media_worker_rolling_remux_workers
        ),
        preserve_warmup_results=bool(args.preserve_warmup_results),
    )
    report: dict[str, Any] = {
        "run_id": cfg.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": _jsonable_config(cfg),
        "artifact_dir": str(cfg.artifact_dir),
        "steps": [],
    }
    write_json(cfg.artifact_dir / "run_config.json", report["config"])
    conn = psycopg.connect(cfg.db_url, row_factory=dict_row, autocommit=True)
    redis_client = Redis.from_url(cfg.redis_url, decode_responses=False)
    original_perf: dict[str, Any] | None = None
    original_topology: dict[str, Any] | None = None
    original_clip_worker_replay_shards: dict[str, str] | None = None
    original_event_worker_evidence_env: dict[str, str] | None = None
    original_media_worker_rolling_env: dict[str, str] | None = None
    original_rolling_cache_sink_states: dict[str, dict[str, Any]] | None = None
    original_worker_cpu_isolation: dict[str, Any] | None = None
    original_module_config_exists = DEFAULT_MODULE_CONFIG_PATH.exists()
    original_module_config_text = (
        DEFAULT_MODULE_CONFIG_PATH.read_text(encoding="utf-8")
        if original_module_config_exists
        else None
    )
    original_cameras: list[dict[str, Any]] = []
    runtime_epoch_root: str | None = None
    rtsp_republishers: list[subprocess.Popen] = []
    rtsp_republish_local_server: dict[str, Any] | None = None
    pressure_started_monotonic: float | None = None
    pressure_sources_path: Path | None = None
    started_at = datetime.now(timezone.utc)
    try:
        original_perf = api_json(cfg.api_base, "GET", "/runtime/performance-config")["data"][
            "saved_config"
        ]
        original_topology = api_json(cfg.api_base, "GET", "/runtime/topology-config")["data"][
            "saved_config"
        ]
        original_clip_worker_replay_shards = clip_worker_replay_shard_env_snapshot()
        original_event_worker_evidence_env = event_worker_evidence_env_snapshot()
        original_media_worker_rolling_env = media_worker_rolling_cache_env_snapshot()
        original_rolling_cache_sink_states = rolling_cache_sink_state_snapshot()
        if cfg.cpu_isolation_profile != "none":
            original_worker_cpu_isolation = apply_worker_cpu_isolation(cfg)
            report["cpu_isolation_apply"] = original_worker_cpu_isolation
        original_cameras = fetch_cameras(conn)
        write_json(cfg.artifact_dir / "cameras_before.json", original_cameras)
        write_json(cfg.artifact_dir / "performance_before.json", original_perf)
        write_json(cfg.artifact_dir / "topology_before.json", original_topology)

        active = prepare_evidence_guard(conn, cfg, report)
        if active["blocking_count"] and not cfg.force_runtime_restart:
            report["status"] = "blocked_active_evidence"
            report["active_evidence"] = active
            write_json(cfg.artifact_dir / "report.json", report)
            restore_cameras(conn, original_cameras)
            apply_sources_only(cfg, "runtime_sources_apply_restore_after_guard_block.json")
            print(
                f"BLOCKED_ACTIVE_EVIDENCE blocking_count={active['blocking_count']} "
                f"details={cfg.artifact_dir / 'active_evidence_guard_snapshots.json'}"
            )
            return 2

        if cfg.clear_existing_evidence:
            report["initial_evidence_cleanup"] = clear_existing_evidence_state(
                conn,
                evidence_root=cfg.evidence_root,
            )
            write_json(
                cfg.artifact_dir / "initial_evidence_cleanup.json",
                report["initial_evidence_cleanup"],
            )

        if cfg.dual_shard_same_gpu:
            if not cfg.dual_shard_api:
                stop_single_inference_runtime_for_dual_pressure(cfg)
        else:
            set_compose_operating_point(cfg)
        if cfg.dual_shard_same_gpu:
            write_json(
                cfg.artifact_dir / "performance_apply_pressure.json",
                {
                    "skipped": True,
                    "reason": (
                        "dual_shard_uses_8090_topology_api"
                        if cfg.dual_shard_api
                        else "dual_shard_same_gpu_uses_compose_env"
                    ),
                    "analysis_fps": cfg.fps,
                    "analysis_min_fps": cfg.min_fps,
                    "batch_size": cfg.batch_size,
                    "pose_batch_size": cfg.pose_batch_size,
                    "face_detector_batch_size": cfg.face_detector_batch_size,
                    "face_embedding_batch_size": cfg.face_embedding_batch_size,
                    "face_infer_interval": cfg.face_infer_interval,
                    "face_embedding_infer_interval": cfg.face_embedding_infer_interval,
                    "max_parallel_streams": cfg.max_parallel_streams,
                    "batched_push_timeout": cfg.batched_push_timeout,
                    "gpu": cfg.dual_shard_gpu,
                },
            )
        elif cfg.forwarder_null_sink:
            write_json(
                cfg.artifact_dir / "performance_apply_pressure.json",
                {
                    "skipped": True,
                    "reason": "forwarder_null_sink_only",
                    "analysis_fps": cfg.fps,
                    "analysis_min_fps": cfg.min_fps,
                    "forwarder_out_endpoint": "null://pressure",
                },
            )
        else:
            save_performance_config(cfg, original_perf)
        if cfg.rolling_cache_evidence:
            event_worker_pressure = configure_rolling_cache_workers_for_pressure(cfg)
        else:
            event_worker_pressure = configure_event_worker_for_pressure(cfg)
        if event_worker_pressure is not None:
            report[
                "rolling_cache_pressure"
                if cfg.rolling_cache_evidence
                else "event_worker_pressure_admission"
            ] = event_worker_pressure
        if cfg.rolling_cache_evidence:
            report["rolling_cache_sinks_stopped_before_reconfigure"] = (
                stop_rolling_cache_sinks_before_pressure_reconfigure(cfg)
            )
        if original_clip_worker_replay_shards is not None and (
            cfg.keep_evidence < 0 or cfg.pressure_materialization_event_type_quotas
        ):
            report["clip_worker_pressure_limits"] = configure_clip_worker_replay_shards(
                cfg,
                replay_shards_json=original_clip_worker_replay_shards.get(
                    "REPLAY_SHARDS_JSON",
                    "",
                ),
                replay_shards_config_path=original_clip_worker_replay_shards.get(
                    "REPLAY_SHARDS_CONFIG_PATH",
                    "",
                ),
                artifact_name="compose_recreate_clip_worker_pressure_limits.log",
                materialization_event_type_quotas=cfg.pressure_materialization_event_type_quotas,
                density_profile="high_density" if cfg.rolling_cache_evidence else None,
                materialization_pressure_level=(
                    "high_density" if cfg.rolling_cache_evidence else None
                ),
                max_jobs_per_run="0" if cfg.keep_evidence < 0 else None,
            )
        if cfg.cuda_mps:
            report["cuda_mps_start"] = start_cuda_mps(cfg)
        rtsp_republish_local_server = start_rtsp_republish_local_server(cfg)
        if rtsp_republish_local_server is not None:
            report["rtsp_republish_local_server"] = rtsp_republish_local_server
        rtsp_republishers = start_rtsp_republishers(cfg)
        report["pressure_camera_provisioning"] = insert_pressure_cameras(conn, cfg)
        if cfg.dual_shard_same_gpu:
            if cfg.dual_shard_api:
                start_dual_shard_runtime(cfg)
                topology = save_and_apply_topology_config(cfg)
                recreate_dual_video_sinks_for_current_epoch(cfg)
                if cfg.rolling_cache_evidence:
                    report["rolling_cache_sinks_pressure"] = (
                        start_rolling_cache_sinks_for_pressure(
                            cfg,
                            runtime_epoch_id=str(
                                topology.get("runtime_epoch_id") or ""
                            ),
                        )
                    )
                if cfg.evidence_shard_count > 2:
                    shard_plan = write_dual_shard_pressure_sources(conn, cfg)
                    replay_shards_doc = json.loads(
                        Path(str(shard_plan["shard_plan_path"])).read_text(
                            encoding="utf-8"
                        )
                    )
                    replay_shards_json = _compact_json(replay_shards_doc)
                    clip_worker_shards = configure_clip_worker_replay_shards(
                        cfg,
                        replay_shards_json=replay_shards_json,
                        replay_shards_config_path="",
                        artifact_name="compose_recreate_clip_worker_replay_shards.log",
                        materialization_event_type_quotas=(
                            cfg.pressure_materialization_event_type_quotas
                        ),
                        density_profile="high_density" if cfg.rolling_cache_evidence else None,
                        materialization_pressure_level=(
                            "high_density" if cfg.rolling_cache_evidence else None
                        ),
                        max_jobs_per_run="0" if cfg.keep_evidence < 0 else None,
                        materialization_max_concurrency=(
                            _evidence_materialization_global_limit(
                                cfg.evidence_shard_count
                            )
                        ),
                    )
                    clip_worker_shards.update(
                        {
                            "topology_replay_shards_path": shard_plan[
                                "shard_plan_path"
                            ],
                            "shard_count": cfg.evidence_shard_count,
                            "source_count": cfg.stream_count,
                            "mapping_version": replay_shards_doc.get(
                                "mapping_version",
                                "",
                            ),
                        }
                    )
                    write_json(
                        cfg.artifact_dir / "clip_worker_replay_shards_pressure.json",
                        clip_worker_shards,
                    )
                    pressure_sources_path = Path(str(shard_plan["sources_path"]))
                    pressure_started_monotonic = time.time()
                    start_pressure_sources_from_manifest(
                        cfg,
                        sources_path=pressure_sources_path,
                    )
                else:
                    pressure_started_monotonic = time.time()
                    clip_worker_shards = configure_clip_worker_for_topology_shards(
                        cfg,
                        topology,
                    )
                write_json(cfg.artifact_dir / "runtime_restart_pressure.json", topology)
                report["steps"].append(
                    {
                        "name": "dual_shard_8090_topology_started",
                        "mode": topology.get("mode"),
                        "runtime_epoch_id": topology.get("runtime_epoch_id"),
                        "sources_config_path": topology.get("sources_config_path"),
                        "replay_shards_path": topology.get("replay_shards_path"),
                        "evidence_shard_count": cfg.evidence_shard_count,
                        "clip_worker_replay_shards": clip_worker_shards,
                    }
                )
            else:
                module_config = sync_module_config_snapshot(cfg)
                shard_plan = write_dual_shard_pressure_sources(conn, cfg)
                start_dual_shard_runtime(cfg)
                pressure_sources_path = Path(str(shard_plan["sources_path"]))
                pressure_started_monotonic = time.time()
                start_pressure_sources_from_manifest(
                    cfg,
                    sources_path=pressure_sources_path,
                )
                if cfg.rolling_cache_evidence:
                    report["rolling_cache_sinks_pressure"] = (
                        start_rolling_cache_sinks_for_pressure(
                            cfg,
                            runtime_epoch_id=current_runtime_epoch_id(),
                        )
                    )
                report["steps"].append(
                    {
                        "name": "dual_shard_same_gpu_started",
                        "module_config_path": module_config["module_config_path"],
                        "sources_path": shard_plan["sources_path"],
                        "gpu": cfg.dual_shard_gpu,
                        "shards": shard_plan["shards"],
                    }
                )
        elif cfg.forwarder_null_sink:
            pressure_started_monotonic = time.time()
            restart = apply_sources_only(cfg, "runtime_sources_apply_pressure.json")
            write_json(cfg.artifact_dir / "runtime_restart_pressure.json", restart)
            report["steps"].append({"name": "forwarder_null_sources_started"})
        else:
            pressure_started_monotonic = time.time()
            restart = restart_camera_runtime(cfg)
            runtime_epoch_root = restart.get("runtime_epoch_root")
            if cfg.rolling_cache_evidence:
                report["rolling_cache_sinks_pressure"] = start_rolling_cache_sinks_for_pressure(
                    cfg,
                    runtime_epoch_id=str(restart.get("runtime_epoch_id") or ""),
                )
            write_json(cfg.artifact_dir / "runtime_restart_pressure.json", restart)
            report["steps"].append({"name": "runtime_started", "runtime_epoch_root": runtime_epoch_root})

        if original_worker_cpu_isolation is not None:
            report["cpu_isolation_reapply"] = reapply_worker_cpu_isolation(cfg)
        report["pressure_source_visibility"] = wait_for_pressure_source_visibility(
            cfg,
            sources_path=pressure_sources_path,
        )

        sampling_window = prepare_pressure_sampling_window(
            conn,
            cfg,
            pressure_started_monotonic=pressure_started_monotonic,
        )
        cfg = replace(
            cfg,
            pressure_sampling_start_event_ts_ms=int(
                sampling_window["sampling_start_event_ts_ms"]
            ),
        )
        report["config"] = _jsonable_config(cfg)
        write_json(cfg.artifact_dir / "run_config.json", report["config"])
        report["pressure_sampling_window"] = sample_runtime(
            cfg,
            started_at,
            pressure_started_monotonic=float(
                sampling_window["sampling_started_monotonic"]
            ),
        )
        report["pressure_sampling_cutoff"] = pressure_event_sampling_cutoff(conn, cfg)
        if cfg.rolling_cache_evidence:
            # Rolling-cache source directories can disappear on EOS. Capture
            # finalized segment coverage while all pressure sources and sinks
            # are still alive, before postfill eventually stops the sources.
            report["rolling_cache_segment_visibility"] = (
                collect_rolling_cache_segment_visibility(cfg)
            )
            report["rolling_cache_full_rate_gate"] = rolling_cache_full_rate_gate(
                cfg,
                report["rolling_cache_segment_visibility"],
                report.get("rolling_cache_sinks_pressure"),
            )
            write_json(
                cfg.artifact_dir / "rolling_cache_segment_visibility.json",
                report["rolling_cache_segment_visibility"],
            )
        report["rolling_cache_postfill"] = rolling_cache_postfill_after_sampling(cfg)
        if cfg.adaface_roi_redis:
            report["adaface_roi_worker"] = collect_adaface_roi_worker_metrics(cfg)
        report["pressure_source_stop"] = stop_pressure_sources(conn, cfg)
        stop_rtsp_republishers(rtsp_republishers, cfg)
        rtsp_republishers = []
        if rtsp_republish_local_server is not None:
            report["rtsp_republish_local_server_stop"] = (
                stop_rtsp_republish_local_server(cfg, rtsp_republish_local_server)
            )
            rtsp_republish_local_server = None
        if original_worker_cpu_isolation is not None:
            report["cpu_isolation_drain"] = apply_worker_cpu_isolation_for_drain(
                cfg
            )
        if cfg.savant_ablation_stage == "full-evidence":
            report["pressure_event_quiescence"] = wait_for_pressure_event_quiescence(
                cfg,
                redis_client,
            )
        else:
            report["pressure_event_quiescence"] = {
                "skipped": True,
                "reason": "non_evidence_savant_ablation",
                "stage": cfg.savant_ablation_stage,
            }
        if cfg.discard_pressure_results:
            report["pressure_post_sample_cleanup"] = clear_pressure_post_sample_rows(
                conn,
                cfg,
                report["pressure_sampling_cutoff"],
            )
        else:
            report["pressure_post_sample_cleanup"] = (
                prune_pressure_post_sample_nonplayable_rows(
                    conn,
                    cfg,
                    report["pressure_sampling_cutoff"],
                )
            )
        diagnostics = collect_pressure_diagnostics(cfg)
        report["diagnostics"] = diagnostics
        kept: list[dict[str, Any]] = []
        if cfg.forwarder_null_sink:
            report["kept_evidence_count"] = 0
            report["kept_evidence"] = []
            report["steps"].append({"name": "evidence_drain_skipped", "reason": "forwarder_null_sink"})
        else:
            wait_for_drain(cfg)
            kept = select_kept_evidence(conn, cfg)
            write_kept_csv(cfg, kept)
            report["kept_evidence_count"] = len(kept)
            report["kept_evidence"] = kept if cfg.keep_evidence < 0 else kept[: cfg.keep_evidence]
        capture_runtime_logs_since_start(cfg, started_at)
        diagnostics["log_summary"] = summarize_logs(cfg)
        write_json(cfg.artifact_dir / "pressure_diagnostics.json", diagnostics)
        report["db_summary_before_cleanup"] = db_summary(
            conn,
            cfg.run_id,
            sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
        )
        write_json(cfg.artifact_dir / "db_summary_before_cleanup.json", report["db_summary_before_cleanup"])
        report["non_materialized_task_details"] = non_materialized_task_details(
            conn,
            cfg.run_id,
            sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
        )
        write_json(
            cfg.artifact_dir / "non_materialized_task_details.json",
            report["non_materialized_task_details"],
        )
        report["evidence_window_validation"] = evidence_window_validation_summary(
            cfg,
            kept,
        )
        write_json(
            cfg.artifact_dir / "evidence_window_validation.json",
            report["evidence_window_validation"],
        )
        report["downstream_observability"] = collect_downstream_observability(
            cfg,
            conn,
            redis_client,
            kept=kept,
            diagnostics=diagnostics,
        )
        report["failure_reasons"] = pressure_failure_reasons(
            cfg,
            kept,
            diagnostics,
            db_before_cleanup=report["db_summary_before_cleanup"],
            event_quiescence=report.get("pressure_event_quiescence"),
            post_sample_cleanup=report.get("pressure_post_sample_cleanup"),
        )
        evidence_8090 = (
            report.get("downstream_observability", {}).get("evidence_8090", {})
        )
        if evidence_8090.get("annotation_failed"):
            report["failure_reasons"].append("evidence_8090_annotations_missing")
        if cfg.rolling_cache_evidence:
            report["failure_reasons"].extend(
                rolling_cache_full_rate_failure_reasons(
                    report.get("rolling_cache_full_rate_gate") or {}
                )
            )
            report["failure_reasons"].extend(
                evidence_annotation_failure_reasons(evidence_8090)
            )
            report["failure_reasons"].extend(
                evidence_window_failure_reasons(
                    report.get("evidence_window_validation") or {}
                )
            )
        report["warnings"] = pressure_warnings(
            cfg,
            diagnostics,
            report["failure_reasons"],
        )

        keep_event_ids = {str(row["event_id"]) for row in kept}
        keep_event_ids.update(select_covered_event_ids_for_bundles(conn, cfg, keep_event_ids))
        if cfg.cleanup and cfg.discard_pressure_results:
            cleanup = cleanup_pressure_data(
                conn,
                redis_client,
                cfg,
                keep_event_ids=keep_event_ids,
                runtime_epoch_root=runtime_epoch_root,
            )
            report["cleanup"] = cleanup
        elif cfg.cleanup:
            report["cleanup"] = cleanup_pressure_runtime_only(conn, redis_client, cfg)
        restore_cameras(conn, original_cameras)
        restore_runtime(cfg, original_perf)
        if cfg.cuda_mps:
            report["cuda_mps_stop"] = read_cuda_mps_stop_summary(cfg)
        if original_clip_worker_replay_shards is not None:
            report["clip_worker_replay_shards_restore"] = configure_clip_worker_replay_shards(
                cfg,
                replay_shards_json=original_clip_worker_replay_shards.get(
                    "REPLAY_SHARDS_JSON",
                    "",
                ),
                replay_shards_config_path=original_clip_worker_replay_shards.get(
                    "REPLAY_SHARDS_CONFIG_PATH",
                    "",
                ),
                artifact_name="compose_restore_clip_worker_replay_shards.log",
                materialization_event_type_quotas=(
                    original_clip_worker_replay_shards.get(
                        "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
                        "",
                    )
                ),
                density_profile=original_clip_worker_replay_shards.get(
                    "EVIDENCE_DENSITY_PROFILE",
                    "",
                ),
                materialization_pressure_level=original_clip_worker_replay_shards.get(
                    "EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL",
                    "",
                ),
                max_jobs_per_run=original_clip_worker_replay_shards.get(
                    "CLIP_WORKER_MAX_JOBS_PER_RUN",
                    "",
                ),
            )
        if original_event_worker_evidence_env is not None:
            report["event_worker_evidence_admission_restore"] = (
                configure_event_worker_evidence_admission(
                    cfg,
                    values=original_event_worker_evidence_env,
                    artifact_name="compose_restore_event_worker_evidence_admission.log",
                )
            )
        if cfg.rolling_cache_evidence and original_media_worker_rolling_env is not None:
            report["media_worker_rolling_cache_restore"] = (
                configure_media_worker_rolling_cache(
                    cfg,
                    values=original_media_worker_rolling_env,
                    artifact_name="compose_restore_media_worker_rolling_cache.log",
                )
            )
        if cfg.rolling_cache_evidence and original_rolling_cache_sink_states is not None:
            report["rolling_cache_sinks_restore"] = restore_rolling_cache_sinks_after_pressure(
                cfg,
                original_states=original_rolling_cache_sink_states,
                artifact_name="compose_restore_rolling_cache_sinks.log",
            )
            if not _env_bool(
                (original_media_worker_rolling_env or {}).get("ROLLING_CACHE_ENABLED", "")
            ):
                report["rolling_cache_sinks_stop_after_pressure"] = (
                    stop_rolling_cache_sinks_after_pressure(
                        cfg,
                        artifact_name="compose_stop_rolling_cache_sinks_after_pressure.log",
                    )
                )
        if original_topology is not None:
            api_json(cfg.api_base, "PUT", "/runtime/topology-config", original_topology)
        if cfg.cleanup and cfg.discard_pressure_results:
            report["cleanup_after_restore"] = cleanup_pressure_data(
                conn,
                redis_client,
                cfg,
                keep_event_ids=keep_event_ids,
                runtime_epoch_root=runtime_epoch_root,
            )
        elif cfg.cleanup:
            report["pressure_runtime_cleanup_after_restore"] = cleanup_pressure_runtime_only(
                conn,
                redis_client,
                cfg,
            )
        report["source_container_cleanup_stable"] = (
            remove_pressure_source_containers_until_stable(cfg.run_id)
        )
        if original_worker_cpu_isolation is not None:
            report["cpu_isolation_restore"] = restore_worker_cpu_isolation(
                cfg,
                original_worker_cpu_isolation,
            )
            original_worker_cpu_isolation = None
        report["db_summary_after_cleanup"] = db_summary(
            conn,
            cfg.run_id,
            sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
        )
        report["runtime_after_restore"] = api_json(cfg.api_base, "GET", "/runtime/overview")["data"]
        report["status"] = "passed" if not report["failure_reasons"] else "failed_pressure_gates"
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_STATUS={report['status']}")
        print(f"RUN_ID={cfg.run_id}")
        print(f"ARTIFACT_DIR={cfg.artifact_dir}")
        print(f"KEPT_EVIDENCE={len(kept)}")
        if report["failure_reasons"]:
            print("FAILURE_REASONS=" + ",".join(report["failure_reasons"]))
        if report["warnings"]:
            print("WARNINGS=" + ",".join(report["warnings"]))
        return 0 if report["status"] == "passed" else 1
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_INTERRUPTED artifact_dir={cfg.artifact_dir}", file=sys.stderr)
        try:
            stop_rtsp_republishers(rtsp_republishers, cfg)
            cleanup_after_aborted_run(
                conn,
                redis_client,
                cfg,
                report,
                runtime_epoch_root=runtime_epoch_root,
            )
            if original_cameras:
                restore_cameras(conn, original_cameras)
            if original_perf:
                restore_runtime(cfg, original_perf)
            if original_clip_worker_replay_shards is not None:
                configure_clip_worker_replay_shards(
                    cfg,
                    replay_shards_json=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_JSON",
                        "",
                    ),
                    replay_shards_config_path=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_CONFIG_PATH",
                        "",
                    ),
                    artifact_name="compose_restore_clip_worker_replay_shards_after_interrupt.log",
                    materialization_event_type_quotas=(
                        original_clip_worker_replay_shards.get(
                            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
                            "",
                        )
                    ),
                    density_profile=original_clip_worker_replay_shards.get(
                        "EVIDENCE_DENSITY_PROFILE",
                        "",
                    ),
                    materialization_pressure_level=original_clip_worker_replay_shards.get(
                        "EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL",
                        "",
                    ),
                    max_jobs_per_run=original_clip_worker_replay_shards.get(
                        "CLIP_WORKER_MAX_JOBS_PER_RUN",
                        "",
                    ),
                )
            if original_event_worker_evidence_env is not None:
                configure_event_worker_evidence_admission(
                    cfg,
                    values=original_event_worker_evidence_env,
                    artifact_name="compose_restore_event_worker_evidence_admission_after_interrupt.log",
                )
            if cfg.rolling_cache_evidence and original_media_worker_rolling_env is not None:
                configure_media_worker_rolling_cache(
                    cfg,
                    values=original_media_worker_rolling_env,
                    artifact_name="compose_restore_media_worker_rolling_cache_after_interrupt.log",
                )
            if cfg.rolling_cache_evidence and original_rolling_cache_sink_states is not None:
                restore_rolling_cache_sinks_after_pressure(
                    cfg,
                    original_states=original_rolling_cache_sink_states,
                    artifact_name="compose_restore_rolling_cache_sinks_after_interrupt.log",
                )
            if original_topology is not None:
                api_json(cfg.api_base, "PUT", "/runtime/topology-config", original_topology)
        finally:
            return 130
    except Exception as exc:
        error_traceback = traceback.format_exc()
        report["status"] = "failed_exception"
        report["error"] = repr(exc)
        report["traceback"] = error_traceback
        write_text(cfg.artifact_dir / "failed_exception_traceback.txt", error_traceback)
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_FAILED error={exc!r} artifact_dir={cfg.artifact_dir}", file=sys.stderr)
        try:
            stop_rtsp_republishers(rtsp_republishers, cfg)
            cleanup_after_aborted_run(
                conn,
                redis_client,
                cfg,
                report,
                runtime_epoch_root=runtime_epoch_root,
            )
            if original_cameras:
                restore_cameras(conn, original_cameras)
            if original_perf:
                restore_runtime(cfg, original_perf)
            if original_clip_worker_replay_shards is not None:
                configure_clip_worker_replay_shards(
                    cfg,
                    replay_shards_json=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_JSON",
                        "",
                    ),
                    replay_shards_config_path=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_CONFIG_PATH",
                        "",
                    ),
                    artifact_name="compose_restore_clip_worker_replay_shards_after_failure.log",
                    materialization_event_type_quotas=(
                        original_clip_worker_replay_shards.get(
                            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
                            "",
                        )
                    ),
                    density_profile=original_clip_worker_replay_shards.get(
                        "EVIDENCE_DENSITY_PROFILE",
                        "",
                    ),
                    materialization_pressure_level=original_clip_worker_replay_shards.get(
                        "EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL",
                        "",
                    ),
                    max_jobs_per_run=original_clip_worker_replay_shards.get(
                        "CLIP_WORKER_MAX_JOBS_PER_RUN",
                        "",
                    ),
                )
            if original_event_worker_evidence_env is not None:
                configure_event_worker_evidence_admission(
                    cfg,
                    values=original_event_worker_evidence_env,
                    artifact_name="compose_restore_event_worker_evidence_admission_after_failure.log",
                )
            if cfg.rolling_cache_evidence and original_media_worker_rolling_env is not None:
                configure_media_worker_rolling_cache(
                    cfg,
                    values=original_media_worker_rolling_env,
                    artifact_name="compose_restore_media_worker_rolling_cache_after_failure.log",
                )
            if cfg.rolling_cache_evidence and original_rolling_cache_sink_states is not None:
                restore_rolling_cache_sinks_after_pressure(
                    cfg,
                    original_states=original_rolling_cache_sink_states,
                    artifact_name="compose_restore_rolling_cache_sinks_after_failure.log",
                )
            if original_topology is not None:
                api_json(cfg.api_base, "PUT", "/runtime/topology-config", original_topology)
        finally:
            return 1
    finally:
        if rtsp_republish_local_server is not None:
            try:
                stop_rtsp_republish_local_server(cfg, rtsp_republish_local_server)
            except Exception:
                pass
        if cfg.cuda_mps:
            try:
                stop_cuda_mps(cfg)
            except Exception:
                pass
        if original_worker_cpu_isolation is not None:
            try:
                restore_worker_cpu_isolation(cfg, original_worker_cpu_isolation)
            except Exception:
                pass
        try:
            if original_module_config_exists:
                _atomic_write_text(
                    DEFAULT_MODULE_CONFIG_PATH,
                    original_module_config_text or "",
                )
            else:
                DEFAULT_MODULE_CONFIG_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        conn.close()


def _default_run_id(
    fps: str,
    *,
    forwarder_null_sink: bool = False,
    dual_shard_same_gpu: bool = False,
    dual_shard_api: bool = False,
) -> str:
    fps_label = fps.replace("/", "p").replace(".", "_")
    if dual_shard_same_gpu and dual_shard_api:
        prefix = "pressure60_8090topology_dual1gpu"
    elif dual_shard_same_gpu:
        prefix = "pressure60_dual1gpu"
    elif forwarder_null_sink:
        prefix = "forwarder60_null"
    else:
        prefix = "pressure60"
    return f"{prefix}_{fps_label}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def safe_run_token(run_id: str) -> str:
    """Return a Redis/container-safe bounded token for a pressure run."""
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", str(run_id or "pressure"))
    return token[:120] or "pressure"


def adaface_roi_stream_name(cfg: PressureConfig) -> str:
    return f"security.face_rois.{safe_run_token(cfg.run_id)}"


def _jsonable_config(cfg: PressureConfig) -> dict[str, Any]:
    data = cfg.__dict__.copy()
    for key, value in list(data.items()):
        if isinstance(value, Path):
            data[key] = str(value)
    return data


def infer_interval_for_target_fps(fps: str, *, target_fps: Fraction) -> int:
    source_fps = Fraction(str(fps))
    if source_fps <= 0 or target_fps <= 0:
        return 0
    ratio = source_fps / target_fps
    every_n_frames = (ratio.numerator + ratio.denominator - 1) // ratio.denominator
    return max(0, int(every_n_frames) - 1)


def parse_evidence_policy_groups(value: str) -> tuple[tuple[int, int], ...]:
    groups: list[tuple[int, int]] = []
    for raw_item in str(value or "").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if ":" in item:
            pre_raw, post_raw = item.split(":", 1)
        elif "/" in item:
            pre_raw, post_raw = item.split("/", 1)
        else:
            pre_raw = post_raw = item
        try:
            pre_seconds = int(pre_raw)
            post_seconds = int(post_raw)
        except ValueError as exc:
            raise ValueError(f"invalid evidence policy group: {item!r}") from exc
        if pre_seconds < 0 or post_seconds < 0:
            raise ValueError(f"evidence policy group must be non-negative: {item!r}")
        groups.append((pre_seconds, post_seconds))
    if not groups:
        raise ValueError("at least one evidence policy group is required")
    return tuple(groups)


def pressure_materialization_event_type_quotas(
    value: str,
    *,
    keep_evidence: int,
) -> str:
    requested = str(value or "").strip()
    if requested.lower() in {"", "0", "off", "false", "none", "disabled"}:
        return ""
    if requested.lower() != "auto":
        return requested
    if keep_evidence <= 0:
        return ""
    total = max(int(keep_evidence) + 10, (int(keep_evidence) * 6 + 4) // 5)
    watchlist = max(1, int(round(total * 0.4)))
    intrusion = max(1, total - watchlist)
    return f"intrusion:{intrusion},watchlist_hit:{watchlist}"


def evidence_policy_for_index(cfg: PressureConfig, index: int) -> dict[str, Any]:
    group_size = max(1, int(cfg.evidence_group_size))
    group_index = min(int(index) // group_size, len(cfg.evidence_policy_groups) - 1)
    pre_seconds, post_seconds = cfg.evidence_policy_groups[group_index]
    return {
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "clip_required": True,
        "snapshot_required": False,
        "pressure_group_index": group_index,
        "pressure_group_size": group_size,
        "pressure_total_seconds": pre_seconds + post_seconds,
    }


def face_image_evidence_policy() -> dict[str, Any]:
    return {
        "pre_seconds": 5,
        "post_seconds": 5,
        "clip_required": False,
        "snapshot_required": True,
        "evidence_mode": "image_only",
        "playback_kind": "image",
    }


def active_evidence_tasks(conn) -> dict[str, Any]:
    blocking_states = sorted(ACTIVE_TASK_STATES)
    terminal_states = sorted(TERMINAL_EVIDENCE_STATES)
    rows = conn.execute(
        """
        WITH base AS (
          SELECT et.task_id,
                 et.event_id,
                 et.source_id,
                 et.event_type,
                 et.status,
                 et.materialization_status,
                 e.payload->'media'->>'evidence_state' AS event_evidence_state,
                 e.media_status,
                 e.payload->'media'->>'clip_status' AS event_clip_status,
                 (
                   et.status = ANY(%s)
                   OR et.materialization_status = ANY(%s)
                   OR e.payload->'media'->>'evidence_state' = ANY(%s)
                   OR e.media_status = ANY(%s)
                 ) AS terminal_evidence_state,
                 et.created_at,
                 et.updated_at,
                 (
                   SELECT max(deadline_at)
                   FROM (
                     VALUES
                       (et.materialization_deadline_at),
                       (et.replay_deadline_at),
                       (et.annotation_deadline_at)
                   ) AS deadlines(deadline_at)
                 ) AS latest_deadline_at
          FROM evidence_tasks et
          LEFT JOIN events e ON e.id = et.event_id
        )
        SELECT *,
               CASE
                 WHEN latest_deadline_at IS NOT NULL
                  AND latest_deadline_at <= now()
                  AND updated_at <= now() - interval '15 minutes'
                 THEN true
                 ELSE false
               END AS stale_nonblocking
        FROM base
        WHERE NOT terminal_evidence_state
          AND (
            status = ANY(%s)
            OR materialization_status = ANY(%s)
            OR event_evidence_state = ANY(%s)
            OR media_status = ANY(%s)
            OR event_clip_status = ANY(%s)
          )
        ORDER BY created_at
        """,
        (
            terminal_states,
            terminal_states,
            terminal_states,
            terminal_states,
            blocking_states,
            blocking_states,
            blocking_states,
            blocking_states,
            blocking_states,
        ),
    ).fetchall()
    blocking = [row for row in rows if not row.get("stale_nonblocking")]
    return {
        "active_count": len(rows),
        "blocking_count": len(blocking),
        "tasks": [_row_json(row) for row in rows],
    }


def prepare_evidence_guard(conn, cfg: PressureConfig, report: dict[str, Any]) -> dict[str, Any]:
    before = active_evidence_tasks(conn)
    write_json(cfg.artifact_dir / "active_evidence_before.json", before)
    report["active_evidence_before"] = before
    if cfg.clear_existing_evidence:
        # A destructive evidence reset must first stop producers. This is also
        # required with --force-runtime-restart; otherwise new events can race
        # the DELETE and make the advertised clean baseline non-deterministic.
        quiesce_existing_sources(conn, cfg)
        report["steps"].append(
            {"name": "existing_sources_quiesced_before_evidence_reset"}
        )
        return wait_for_evidence_guard_clear(conn, cfg)
    if cfg.force_runtime_restart:
        return before
    if not cfg.no_quiesce_before_guard:
        quiesce_existing_sources(conn, cfg)
        report["steps"].append({"name": "existing_sources_quiesced"})
    return wait_for_evidence_guard_clear(conn, cfg)


def quiesce_existing_sources(conn, cfg: PressureConfig) -> None:
    with conn.transaction():
        conn.execute(
            """
            UPDATE cameras
            SET enabled=false, updated_at=now()
            WHERE source_id NOT LIKE %s
            """,
            (f"{cfg.run_id}_%",),
        )
    apply_sources_only(cfg, "runtime_sources_apply_quiesce_before_guard.json")


def wait_for_evidence_guard_clear(conn, cfg: PressureConfig) -> dict[str, Any]:
    deadline = time.time() + max(0, cfg.guard_wait_s)
    snapshots: list[dict[str, Any]] = []
    latest = active_evidence_tasks(conn)
    while True:
        latest = active_evidence_tasks(conn)
        snapshots.append(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "active_count": latest["active_count"],
                "blocking_count": latest["blocking_count"],
                "tasks": latest["tasks"],
            }
        )
        if not latest["blocking_count"]:
            break
        if time.time() >= deadline:
            break
        time.sleep(min(10, max(1, deadline - time.time())))
    write_json(cfg.artifact_dir / "active_evidence_guard_snapshots.json", snapshots)
    return latest


def fetch_cameras(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, source_id, site_id, enabled, rtsp_url FROM cameras ORDER BY name"
    ).fetchall()
    return [_row_json(row) for row in rows]


def set_compose_operating_point(cfg: PressureConfig) -> None:
    env = os.environ.copy()
    updates = {
        "BATCH_SIZE": str(cfg.batch_size),
        "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
        "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
        "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
        "FACE_INFER_INTERVAL": str(cfg.face_infer_interval),
        "FACE_EMBEDDING_INFER_INTERVAL": str(cfg.face_embedding_infer_interval),
        "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
        "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
        "ANALYSIS_FPS": cfg.fps,
        "ANALYSIS_MIN_FPS": cfg.min_fps,
        "MAX_FPS": cfg.fps,
        "MIN_FPS": cfg.min_fps,
        "FRAME_ANNOTATION_SOURCE_STREAM_ENABLED": "true",
        "FRAME_ANNOTATION_STREAM_MODE": "dual",
        "FRAME_ANNOTATION_SOURCE_REDIS_MAXLEN": "10000",
    }
    services = ["analysis-forwarder", "savant-security"]
    if cfg.forwarder_null_sink:
        updates["FORWARDER_OUT_ENDPOINT"] = "null://pressure"
        services = ["analysis-forwarder"]
    env.update(updates)
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *services,
        ],
        cfg.artifact_dir / "compose_recreate_pressure.log",
        env=env,
    )


def stop_single_inference_runtime_for_dual_pressure(cfg: PressureConfig) -> None:
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "stop",
            *DUAL_SHARD_SINGLE_SERVICES,
        ],
        cfg.artifact_dir / "compose_stop_single_for_dual_pressure.log",
        check=False,
    )


def start_dual_shard_runtime(cfg: PressureConfig) -> None:
    override_path = write_dual_shard_same_gpu_compose_override(cfg)
    env = os.environ.copy()
    env.update(
        {
            "BATCH_SIZE": str(cfg.batch_size),
            "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
            "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
            "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
            "FACE_INFER_INTERVAL": str(cfg.face_infer_interval),
            "FACE_EMBEDDING_INFER_INTERVAL": str(cfg.face_embedding_infer_interval),
            "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
            "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
            "ANALYSIS_FPS": cfg.fps,
            "ANALYSIS_MIN_FPS": cfg.min_fps,
            "MAX_FPS": cfg.fps,
            "MIN_FPS": cfg.min_fps,
            "FRAME_ANNOTATION_SOURCE_STREAM_ENABLED": "true",
            "FRAME_ANNOTATION_STREAM_MODE": "dual",
            "FRAME_ANNOTATION_SOURCE_REDIS_MAXLEN": "10000",
            "SAVANT_A_NVIDIA_VISIBLE_DEVICES": cfg.dual_shard_gpu,
            "SAVANT_B_NVIDIA_VISIBLE_DEVICES": cfg.dual_shard_gpu,
            "SAVANT_B_CUDA_VISIBLE_DEVICES": "0",
            "SAVANT_B_MODEL_ROOT": "/data/video-analytics/models",
        }
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "-f",
            str(override_path),
            "--profile",
            DUAL_SHARD_PROFILE,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *dual_shard_services(cfg),
        ],
        cfg.artifact_dir / "compose_recreate_dual_shard_same_gpu.log",
        env=env,
    )
    wait_for_dual_shard_metrics(cfg)


def stop_dual_shard_runtime(cfg: PressureConfig) -> None:
    override_path = cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml"
    compose = [
        "docker",
        "compose",
        "--env-file",
        cfg.env_file,
        "-f",
        cfg.compose_file,
    ]
    if override_path.exists():
        compose.extend(["-f", str(override_path)])
    compose.extend(
        ["--profile", DUAL_SHARD_PROFILE, "stop", *dual_shard_services(cfg)]
    )
    run(
        compose,
        cfg.artifact_dir / "compose_stop_dual_shard_same_gpu.log",
        check=False,
    )


def dual_shard_services(cfg: PressureConfig) -> list[str]:
    services = list(DUAL_SHARD_SERVICES)
    if cfg.adaface_decoupled:
        services.extend(["adaface-forwarder-a", "adaface-forwarder-b"])
        services.extend(adaface_central_service_names(cfg))
    if cfg.adaface_roi_redis:
        services.append(ADAFACE_ROI_WORKER_SERVICE)
    return services


def adaface_central_service_names(cfg: PressureConfig) -> list[str]:
    if cfg.adaface_decoupled_sharded:
        return ["savant-adaface-a", "savant-adaface-b"]
    return ["savant-adaface-central"]


def adaface_central_metrics_urls(cfg: PressureConfig) -> dict[str, str]:
    if cfg.adaface_decoupled_sharded:
        return dict(ADAFACE_SHARDED_CENTRAL_METRICS)
    return {"central": ADAFACE_CENTRAL_METRICS_URL}


def recreate_dual_video_sinks_for_current_epoch(cfg: PressureConfig) -> None:
    override_path = cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml"
    compose = [
        "docker",
        "compose",
        "--env-file",
        cfg.env_file,
        "-f",
        cfg.compose_file,
    ]
    if override_path.exists():
        compose.extend(["-f", str(override_path)])
    compose.extend(
        [
            "--profile",
            DUAL_SHARD_PROFILE,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "video-file-sink-a",
            "video-file-sink-b",
            "video-file-sink-c",
            "video-file-sink-d",
            "video-file-sink-e",
            "video-file-sink-f",
            "video-file-sink-g",
            "video-file-sink-h",
        ]
    )
    run(
        compose,
        cfg.artifact_dir / "compose_recreate_dual_video_sinks_current_epoch.log",
    )


def write_dual_shard_same_gpu_compose_override(cfg: PressureConfig) -> Path:
    path = cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml"
    device = str(cfg.dual_shard_gpu)
    ablation_module_path = write_savant_ablation_module(cfg)
    # The artifact root is already bind-mounted at the same absolute path in
    # both Savant services. Avoid a nested file mount under /opt/savant/src/module:
    # Docker creates the nested target in the repo-backed parent mount, which
    # leaves a root-owned module.pressure.yml in the working tree.
    module_path_in_container = str(ablation_module_path)
    output_frame = (
        '{"codec":"copy"}'
        if cfg.savant_output_mode == "copy" or cfg.adaface_decoupled
        else "null"
    )
    doc = {
        "services": {
            "savant-a": {
                "environment": {
                    "NVIDIA_VISIBLE_DEVICES": device,
                    "BATCH_SIZE": str(cfg.batch_size),
                    "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
                    "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
                    "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
                    "FACE_INFER_INTERVAL": str(cfg.face_infer_interval),
                    "FACE_EMBEDDING_INFER_INTERVAL": str(cfg.face_embedding_infer_interval),
                    "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
                    "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
                    "SAVANT_STAGE_METRICS_ENABLED": "true",
                    "SAVANT_MODULE_FILE": module_path_in_container,
                    "OUTPUT_FRAME": output_frame,
                    "FRAME_ANNOTATION_SOURCE_STREAM_ENABLED": "true",
                    "FRAME_ANNOTATION_STREAM_MODE": "dual",
                    "FRAME_ANNOTATION_SOURCE_REDIS_MAXLEN": "10000",
                    "FACE_SECONDARY_TRACK_ID_ENABLED": (
                        "true" if cfg.face_secondary_track_id else "false"
                    ),
                },
                "deploy": {
                    "resources": {
                        "reservations": {
                            "devices": [
                                {
                                    "driver": "nvidia",
                                    "device_ids": [device],
                                    "capabilities": ["gpu"],
                                }
                            ]
                        }
                    }
                },
            },
            "savant-b": {
                "environment": {
                    "NVIDIA_VISIBLE_DEVICES": device,
                    "CUDA_VISIBLE_DEVICES": "0",
                    "BATCH_SIZE": str(cfg.batch_size),
                    "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
                    "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
                    "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
                    "FACE_INFER_INTERVAL": str(cfg.face_infer_interval),
                    "FACE_EMBEDDING_INFER_INTERVAL": str(cfg.face_embedding_infer_interval),
                    "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
                    "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
                    "SAVANT_STAGE_METRICS_ENABLED": "true",
                    "SAVANT_MODULE_FILE": module_path_in_container,
                    "OUTPUT_FRAME": output_frame,
                    "FRAME_ANNOTATION_SOURCE_STREAM_ENABLED": "true",
                    "FRAME_ANNOTATION_STREAM_MODE": "dual",
                    "FRAME_ANNOTATION_SOURCE_REDIS_MAXLEN": "10000",
                    "FACE_SECONDARY_TRACK_ID_ENABLED": (
                        "true" if cfg.face_secondary_track_id else "false"
                    ),
                },
                "deploy": {
                    "resources": {
                        "reservations": {
                            "devices": [
                                {
                                    "driver": "nvidia",
                                    "device_ids": [device],
                                    "capabilities": ["gpu"],
                                }
                            ]
                        }
                    }
                },
            },
        }
    }
    if cfg.adaface_roi_redis:
        roi_stream = adaface_roi_stream_name(cfg)
        for service in ("savant-a", "savant-b"):
            doc["services"][service]["environment"].update(
                {
                    "FACE_ROI_EXPORT_ENABLED": "true",
                    "FACE_ROI_STREAM": roi_stream,
                    "FACE_ROI_STREAM_MAXLEN": "20000",
                    "FACE_ROI_QUEUE_MAXSIZE": "4096",
                    "FACE_ROI_TTL_MS": "5000",
                    "FACE_ROI_JPEG_QUALITY": "95",
                    "ADAFACE_INPUT_OBJECT": "disabled.face",
                    "FACE_OBSERVATION_EXPORT_ENABLED": "false",
                }
            )
        doc["services"][ADAFACE_ROI_WORKER_SERVICE] = {
            "profiles": [DUAL_SHARD_PROFILE],
            "environment": {
                "FACE_ROI_STREAM": roi_stream,
                "FACE_ROI_CONSUMER_GROUP": f"adaface-roi-{safe_run_token(cfg.run_id)}",
                "FACE_ROI_CONSUMER_NAME": "adaface-roi-worker-1",
                "FACE_ROI_TTL_MS": "5000",
                "FACE_ROI_BATCH_TIMEOUT_MS": str(
                    cfg.adaface_roi_batch_timeout_ms
                ),
                "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
            },
            "restart": "no",
        }
    if cfg.adaface_decoupled:
        central_module = str(write_central_adaface_module(cfg))
        forwarder_image = "video-analytics-midterm-analysis-forwarder:latest"
        for shard in ("a", "b"):
            service = f"adaface-forwarder-{shard}"
            central_service = (
                f"savant-adaface-{shard}"
                if cfg.adaface_decoupled_sharded
                else "savant-adaface-central"
            )
            doc["services"][service] = {
                "image": forwarder_image,
                "container_name": f"video-analytics-midterm-{service}",
                "profiles": [DUAL_SHARD_PROFILE],
                "volumes": [
                    f"{Path('services/analysis-forwarder/app').resolve()}:/app/app:ro"
                ],
                "ports": [f"{18188 if shard == 'a' else 18189}:8081"],
                "environment": {
                    "FORWARDER_IN_ENDPOINT": (
                        f"sub+connect:tcp://savant-{shard}:5558"
                    ),
                    "FORWARDER_OUT_ENDPOINT": (
                        f"dealer+connect:tcp://{central_service}:5557"
                    ),
                    "FORWARDER_RAW_OUT_ENDPOINT": "null://",
                    "FORWARDER_SAMPLER_ENABLED": "false",
                    "FORWARDER_REQUIRE_OBJECT_NAMESPACE": "yolov8_face",
                    "FORWARDER_REQUIRE_OBJECT_LABEL": "face",
                    "FORWARDER_REQUIRE_ATTRIBUTE_NAMESPACE": (
                        "face_person_associator"
                    ),
                    "FORWARDER_REQUIRE_ATTRIBUTE_NAME": "person_track_id",
                    "FORWARDER_QUEUE_MAX_SIZE": "512",
                    "FORWARDER_RECEIVE_TIMEOUT_MS": "250",
                    "FORWARDER_RECEIVE_HWM": "2000",
                    "FORWARDER_SEND_TIMEOUT_MS": "50",
                    "FORWARDER_SEND_RETRIES": "0",
                    "FORWARDER_SEND_HWM": "1000",
                    "FORWARDER_METRICS_PORT": "8081",
                },
                "restart": "no",
            }
        central_template = {
            "image": CUDA_MPS_IMAGE_DEFAULT,
            "profiles": [DUAL_SHARD_PROFILE],
            "entrypoint": [
                "bash",
                "-c",
                "export PYTHONPATH=/opt/savant/poc_deps$${PYTHONPATH:+:$$PYTHONPATH}; python /opt/savant/src/module/savant_patches/apply_patches.py || exit 1; exec python -m savant.entrypoint $${SAVANT_MODULE_FILE}",
            ],
            "working_dir": "/opt/savant/src/module",
            "env_file": [str((Path(cfg.env_file).resolve()))],
            "environment": {
                "LOGLEVEL": os.getenv("LOGLEVEL", "INFO"),
                "NVIDIA_VISIBLE_DEVICES": device,
                "CUDA_VISIBLE_DEVICES": "0",
                "ZMQ_SRC_ENDPOINT": "router+bind:tcp://0.0.0.0:5557",
                "ZMQ_SINK_ENDPOINT": "pub+bind:tcp://0.0.0.0:5558",
                "MODEL_PATH": "/models",
                "DOWNLOAD_PATH": "/downloads",
                "WEBSERVER_PORT": "8080",
                "METRICS_FRAME_PERIOD": "1000",
                "METRICS_TIME_PERIOD": "5",
                "METRICS_HISTORY": "100",
                # The central module contains only AdaFace. Reusing the dual
                # YOLO mux batch (4) capped every measured central batch at 4
                # even though the embedding engine is built for batch 16.
                "BATCH_SIZE": str(cfg.face_embedding_batch_size),
                "MAX_PARALLEL_STREAMS": str(
                    32 if cfg.adaface_decoupled_sharded else cfg.max_parallel_streams
                ),
                "MAX_FPS_CONTROL": "false",
                "INGRESS_FPS_GATE_ENABLED": "false",
                "MAX_FPS": cfg.fps,
                "MIN_FPS": cfg.min_fps,
                "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
                "FACE_EMBEDDING_INFER_INTERVAL": "0",
                "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
                "SAVANT_STAGE_METRICS_ENABLED": "true",
                "SAVANT_MODULE_FILE": central_module,
                "OUTPUT_FRAME": "null",
                "REDIS_URL": "redis://redis:6379/0",
                "CAMERAS_CONFIG_PATH": (
                    "/opt/savant/src/module/config/cameras.midterm.yml"
                ),
                "FACE_OBSERVATION_EXPORT_ENABLED": "true",
            },
            "volumes": [
                f"{Path('modules/savant_security').resolve()}:/opt/savant/src/module:rw",
                f"{Path('modules/savant_security/poc_deps').resolve()}:/opt/savant/poc_deps:ro",
                "/data/video-analytics/models:/models:rw",
                "/data/video-analytics/downloads:/downloads:rw",
                "/data/video-analytics/artifacts:/data/video-analytics/artifacts:rw",
                "/data/video-analytics/media:/data/video-analytics/media:rw",
            ],
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [
                            {
                                "driver": "nvidia",
                                "device_ids": [device],
                                "capabilities": ["gpu"],
                            }
                        ]
                    }
                }
            },
            "restart": "no",
        }
        central_specs = (
            [("savant-adaface-a", 18187), ("savant-adaface-b", 18190)]
            if cfg.adaface_decoupled_sharded
            else [("savant-adaface-central", 18187)]
        )
        for service, metrics_port in central_specs:
            service_doc = copy.deepcopy(central_template)
            service_doc["container_name"] = f"video-analytics-midterm-{service}"
            service_doc["ports"] = [f"{metrics_port}:8080"]
            doc["services"][service] = service_doc
    if cfg.cuda_mps:
        mps_root, pipe_dir, log_dir = cuda_mps_paths(cfg)
        for service in ("savant-a", "savant-b"):
            service_doc = doc["services"][service]
            service_doc["ipc"] = "host"
            service_doc["environment"].update(
                {
                    "CUDA_MPS_PIPE_DIRECTORY": str(pipe_dir),
                    "CUDA_MPS_LOG_DIRECTORY": str(log_dir),
                }
            )
            if cfg.mps_savant_active_thread_percentage > 0:
                service_doc["environment"][
                    "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"
                ] = str(cfg.mps_savant_active_thread_percentage)
            service_doc.setdefault("volumes", []).append(
                f"{mps_root}:{mps_root}:rw"
            )
        if cfg.adaface_roi_redis:
            roi_service = doc["services"][ADAFACE_ROI_WORKER_SERVICE]
            roi_service["ipc"] = "host"
            roi_service["environment"].update(
                {
                    "CUDA_MPS_PIPE_DIRECTORY": str(pipe_dir),
                    "CUDA_MPS_LOG_DIRECTORY": str(log_dir),
                }
            )
            if cfg.mps_adaface_active_thread_percentage > 0:
                roi_service["environment"][
                    "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"
                ] = str(cfg.mps_adaface_active_thread_percentage)
            roi_service.setdefault("volumes", []).append(
                f"{mps_root}:{mps_root}:rw"
            )
    cpu_profile = CPU_ISOLATION_PROFILES[cfg.cpu_isolation_profile]
    for service in (
        "savant-a",
        "savant-b",
        "analysis-forwarder-a",
        "analysis-forwarder-b",
    ):
        cpuset = cpu_profile.get(service)
        if cpuset:
            doc["services"].setdefault(service, {})["cpuset"] = cpuset
    write_text(path, yaml.safe_dump(doc, sort_keys=False))
    return path


def cuda_mps_paths(cfg: PressureConfig) -> tuple[Path, Path, Path]:
    # CUDA MPS uses Unix-domain sockets whose path is limited to roughly 108
    # bytes. Artifact/run-id paths can exceed that on production, so runtime
    # IPC must stay at a fixed short path; logs are copied into the artifact.
    root = CUDA_MPS_RUNTIME_ROOT
    return root, root / "pipe", root / "log"


def _cleanup_cuda_mps_runtime_root() -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "-v",
            "/tmp:/host-tmp",
            "--entrypoint",
            "rm",
            _cuda_mps_image(),
            "-rf",
            f"/host-tmp/{CUDA_MPS_RUNTIME_ROOT.name}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _capture_cuda_mps_daemon_logs(cfg: PressureConfig) -> None:
    for source_name, artifact_name in (
        ("control.log", "cuda_mps_control.log"),
        ("server.log", "cuda_mps_server.log"),
    ):
        completed = subprocess.run(
            [
                "docker",
                "exec",
                CUDA_MPS_CONTAINER,
                "bash",
                "-lc",
                f'test ! -f "$CUDA_MPS_LOG_DIRECTORY/{source_name}" || '
                f'cat "$CUDA_MPS_LOG_DIRECTORY/{source_name}"',
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        write_text(cfg.artifact_dir / artifact_name, completed.stdout)


def _cuda_mps_image() -> str:
    completed = subprocess.run(
        [
            "docker",
            "inspect",
            "video-analytics-midterm-savant",
            "--format",
            "{{.Config.Image}}",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.strip() if completed.returncode == 0 else CUDA_MPS_IMAGE_DEFAULT


def _cuda_mps_container_exists() -> bool:
    completed = subprocess.run(
        ["docker", "inspect", CUDA_MPS_CONTAINER],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def cuda_mps_status(cfg: PressureConfig) -> dict[str, Any]:
    root, pipe_dir, log_dir = cuda_mps_paths(cfg)
    status: dict[str, Any] = {
        "enabled": cfg.cuda_mps,
        "container": CUDA_MPS_CONTAINER,
        "root": str(root),
        "pipe_directory": str(pipe_dir),
        "log_directory": str(log_dir),
        "container_exists": _cuda_mps_container_exists(),
    }
    if not status["container_exists"]:
        status["ready"] = False
        return status
    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            CUDA_MPS_CONTAINER,
            "--format",
            "{{.State.Status}}|{{.State.Running}}|{{index .Config.Labels \"com.video-analytics.pressure60.run-id\"}}",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    control = subprocess.run(
        [
            "docker",
            "exec",
            CUDA_MPS_CONTAINER,
            "bash",
            "-lc",
            "echo get_server_list | nvidia-cuda-mps-control",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    status.update(
        {
            "inspect": inspect.stdout.strip(),
            "inspect_rc": inspect.returncode,
            "control_rc": control.returncode,
            "server_list": control.stdout.strip().splitlines(),
            "ready": inspect.returncode == 0 and control.returncode == 0,
        }
    )
    return status


def start_cuda_mps(cfg: PressureConfig) -> dict[str, Any]:
    if not cfg.dual_shard_same_gpu:
        raise RuntimeError("CUDA MPS requires dual_shard_same_gpu")
    root, pipe_dir, log_dir = cuda_mps_paths(cfg)
    if len(str(pipe_dir / "control").encode("utf-8")) >= 100:
        raise RuntimeError(f"CUDA MPS pipe path is too long: {pipe_dir}")
    if _cuda_mps_container_exists():
        existing = cuda_mps_status(cfg)
        if cfg.run_id not in str(existing.get("inspect") or ""):
            raise RuntimeError(
                f"refusing to replace unrelated CUDA MPS container: {existing}"
            )
        subprocess.run(
            ["docker", "rm", "-f", CUDA_MPS_CONTAINER],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    _cleanup_cuda_mps_runtime_root()
    pipe_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    command = (
        "set -eu; "
        "nvidia-cuda-mps-control -d; "
        "trap 'echo quit | nvidia-cuda-mps-control >/tmp/mps-quit.log 2>&1 || true' TERM INT EXIT; "
        "while :; do sleep 5; done"
    )
    completed = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--pull=never",
            "--name",
            CUDA_MPS_CONTAINER,
            "--label",
            "com.video-analytics.pressure60.mps=true",
            "--label",
            f"com.video-analytics.pressure60.run-id={cfg.run_id}",
            "--gpus",
            f"device={cfg.dual_shard_gpu}",
            "--ipc=host",
            "-e",
            f"CUDA_MPS_PIPE_DIRECTORY={pipe_dir}",
            "-e",
            f"CUDA_MPS_LOG_DIRECTORY={log_dir}",
            "-v",
            f"{root}:{root}:rw",
            "--entrypoint",
            "bash",
            _cuda_mps_image(),
            "-lc",
            command,
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    write_text(cfg.artifact_dir / "cuda_mps_start.log", completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(f"CUDA MPS helper failed to start: {completed.stdout.strip()}")
    deadline = time.time() + 30
    status: dict[str, Any] = {}
    while time.time() < deadline:
        status = cuda_mps_status(cfg)
        if status.get("ready"):
            write_json(cfg.artifact_dir / "cuda_mps_start.json", status)
            return status
        time.sleep(0.5)
    stop_cuda_mps(cfg)
    raise RuntimeError(f"CUDA MPS helper did not become ready: {status}")


def stop_cuda_mps(cfg: PressureConfig) -> dict[str, Any]:
    if not _cuda_mps_container_exists():
        previous_path = cfg.artifact_dir / "cuda_mps_stop.json"
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        if previous.get("stopped"):
            return previous
        _cleanup_cuda_mps_runtime_root()
        summary = {
            "before": cuda_mps_status(cfg),
            "stop_rc": 0,
            "stop_output": "already_stopped",
            "container_exists_after": False,
            "stopped": True,
        }
        write_json(previous_path, summary)
        return summary
    before = cuda_mps_status(cfg)
    _capture_cuda_mps_daemon_logs(cfg)
    logs = subprocess.run(
        ["docker", "logs", CUDA_MPS_CONTAINER],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    write_text(cfg.artifact_dir / "cuda_mps_container.log", logs.stdout)
    stop = subprocess.run(
        ["docker", "stop", "--time", "15", CUDA_MPS_CONTAINER],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if _cuda_mps_container_exists():
        subprocess.run(
            ["docker", "rm", "-f", CUDA_MPS_CONTAINER],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    summary = {
        "before": before,
        "stop_rc": stop.returncode,
        "stop_output": stop.stdout.strip(),
        "container_exists_after": _cuda_mps_container_exists(),
        "stopped": not _cuda_mps_container_exists(),
    }
    _cleanup_cuda_mps_runtime_root()
    write_json(cfg.artifact_dir / "cuda_mps_stop.json", summary)
    return summary


def read_cuda_mps_stop_summary(cfg: PressureConfig) -> dict[str, Any]:
    path = cfg.artifact_dir / "cuda_mps_stop.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stopped": not _cuda_mps_container_exists()}


def write_savant_ablation_module(cfg: PressureConfig) -> Path:
    if cfg.adaface_decoupled and cfg.adaface_pre_gate:
        raise ValueError(
            "--adaface-decoupled already pre-gates in the central module; "
            "do not combine it with --adaface-pre-gate"
        )
    source_path = SAVANT_MODULE_PATH.resolve()
    module_doc = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    elements = list(((module_doc.get("pipeline") or {}).get("elements") or []))
    keep = SAVANT_ABLATION_ELEMENTS[cfg.savant_ablation_stage]
    if keep is None:
        selected = elements
    else:
        selected = [element for element in elements if element.get("name") in keep]
    module_doc["pipeline"]["elements"] = selected
    if cfg.adaface_input_queue:
        adaface_index = next(
            (
                index
                for index, element in enumerate(selected)
                if element.get("name") == "adaface"
            ),
            None,
        )
        if adaface_index is None:
            raise ValueError(
                "--adaface-input-queue requires an ablation stage with AdaFace"
            )
        selected.insert(
            adaface_index,
            {
                "element": "queue",
                "name": "adaface_input_queue",
                "properties": {
                    "max-size-buffers": 32,
                    "max-size-bytes": 0,
                    "max-size-time": 0,
                    "leaky": 0,
                },
            },
        )
    if cfg.adaface_crop_resize:
        adaface = next(
            (element for element in selected if element.get("name") == "adaface"),
            None,
        )
        if adaface is None:
            raise ValueError(
                "--adaface-crop-resize requires an ablation stage with AdaFace"
            )
        adaface["model"]["input"]["preprocess_object_image"] = {
            "module": "custom.preprocessors.face_crop_resize",
            "class_name": "FaceCropResizePreprocessingObjectImageGPU",
        }
    if cfg.adaface_pre_gate:
        apply_adaface_pre_gate(selected)
    if cfg.adaface_decoupled or cfg.adaface_roi_redis:
        selected[:] = [
            element
            for element in selected
            if element.get("name")
            not in {"adaface", "face_reid_gate", "face_observation_exporter"}
        ]
    adaface_async_config_path: Path | None = None
    if cfg.adaface_classifier_async:
        adaface = next(
            (element for element in selected if element.get("name") == "adaface"),
            None,
        )
        if adaface is None:
            raise ValueError(
                "--adaface-classifier-async requires an ablation stage with AdaFace"
            )
        adaface_async_config_path = (
            cfg.artifact_dir / "adaface_nvinfer_classifier_async.txt"
        ).resolve()
        write_text(
            adaface_async_config_path,
            "[property]\nclassifier-async-mode=1\n",
        )
        adaface_model = adaface.setdefault("model", {})
        adaface_model["local_path"] = str(adaface_async_config_path.parent)
        adaface_model["config_file"] = adaface_async_config_path.name
    output_path = (cfg.artifact_dir / "module.pressure.yml").resolve()
    write_text(output_path, yaml.safe_dump(module_doc, sort_keys=False))
    selected_names = [str(element.get("name") or "") for element in selected]
    manifest = {
        "stage": cfg.savant_ablation_stage,
        "source_module": str(source_path),
        "generated_module": str(output_path),
        "output_mode": cfg.savant_output_mode,
        "adaface_classifier_async": cfg.adaface_classifier_async,
        "face_secondary_track_id": cfg.face_secondary_track_id,
        "adaface_input_queue": cfg.adaface_input_queue,
        "adaface_crop_resize": cfg.adaface_crop_resize,
        "adaface_pre_gate": cfg.adaface_pre_gate,
        "adaface_decoupled": cfg.adaface_decoupled,
        "adaface_roi_redis": cfg.adaface_roi_redis,
        "adaface_classifier_async_config": (
            str(adaface_async_config_path) if adaface_async_config_path else ""
        ),
        "selected_elements": selected_names,
        "removed_elements": [
            str(element.get("name") or "")
            for element in elements
            if str(element.get("name") or "") not in selected_names
        ],
    }
    write_json(cfg.artifact_dir / "savant_ablation_manifest.json", manifest)
    return output_path


def apply_adaface_pre_gate(selected: list[dict[str, Any]]) -> None:
    """Insert the candidate gate and keep temporary objects out of output."""
    by_name = {str(element.get("name") or ""): element for element in selected}
    required = {
        "adaface",
        "face_reid_gate",
        "face_observation_exporter",
        "savant_perf_metrics",
    }
    missing = sorted(required - set(by_name))
    if missing:
        raise ValueError(
            "AdaFace pre-gate requires full exporter elements: " + ",".join(missing)
        )
    candidate_name = "face_reid_candidate"
    adaface = by_name["adaface"]
    adaface["model"]["input"]["object"] = f"{candidate_name}.face"
    by_name["face_reid_gate"].setdefault("kwargs", {})[
        "face_element_name"
    ] = candidate_name
    by_name["face_observation_exporter"].setdefault("kwargs", {})[
        "face_element_name"
    ] = candidate_name
    selected.insert(
        selected.index(adaface),
        {
            "element": "pyfunc",
            "name": "face_reid_candidate_gate",
            "module": "custom.pyfuncs.face_reid_candidate_gate",
            "class_name": "FaceReidCandidateGatePyFunc",
            "kwargs": {
                "candidate_element_name": candidate_name,
                "cameras_config_path": "${oc.env:CAMERAS_CONFIG_PATH, /opt/savant/src/module/config/cameras.midterm.yml}",
                "face_reid_min_confidence": "${oc.decode:${oc.env:FACE_REID_MIN_CONFIDENCE, 0.45}}",
                "face_reid_min_face_size": "${oc.decode:${oc.env:FACE_REID_MIN_FACE_SIZE, 40.0}}",
                "face_reid_min_interval_ms": "${oc.decode:${oc.env:FACE_REID_MIN_INTERVAL_MS, 1000}}",
                "log_every_n_frames": 30,
            },
        },
    )
    perf_metrics = by_name["savant_perf_metrics"]
    selected.remove(perf_metrics)
    exporter_index = next(
        index
        for index, element in enumerate(selected)
        if element.get("name") == "face_observation_exporter"
    )
    selected.insert(exporter_index + 1, perf_metrics)
    selected.insert(
        exporter_index + 2,
        {
            "element": "pyfunc",
            "name": "face_reid_candidate_cleanup",
            "module": "custom.pyfuncs.face_reid_candidate_cleanup",
            "class_name": "FaceReidCandidateCleanupPyFunc",
            "kwargs": {"candidate_element_name": candidate_name},
        },
    )


def write_central_adaface_module(cfg: PressureConfig) -> Path:
    """Write an AdaFace-only Savant module fed by dual-YOLO output metadata."""
    source_path = SAVANT_MODULE_PATH.resolve()
    module_doc = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    elements = list(((module_doc.get("pipeline") or {}).get("elements") or []))
    keep = {
        "adaface",
        "face_reid_gate",
        "face_observation_exporter",
        "savant_perf_metrics",
    }
    selected = [element for element in elements if element.get("name") in keep]
    apply_adaface_pre_gate(selected)
    module_doc["pipeline"]["elements"] = selected
    output_path = (cfg.artifact_dir / "module.adaface-central.yml").resolve()
    write_text(output_path, yaml.safe_dump(module_doc, sort_keys=False))
    write_json(
        cfg.artifact_dir / "savant_adaface_central_manifest.json",
        {
            "source_module": str(source_path),
            "generated_module": str(output_path),
            "selected_elements": [str(item.get("name") or "") for item in selected],
            "input": "dual_savant_output_copy",
            "output": "metadata_only",
            "bounded_forwarders": ["adaface-forwarder-a", "adaface-forwarder-b"],
        },
    )
    return output_path


def wait_for_dual_shard_metrics(cfg: PressureConfig) -> None:
    urls = {
        **{
            f"{shard_id}-forwarder-health": url
            for shard_id, url in DUAL_SHARD_FORWARDER_HEALTH.items()
        },
        **{
            f"{shard_id}-savant-metrics": url
            for shard_id, url in DUAL_SHARD_SAVANT_METRICS.items()
        },
    }
    if cfg.adaface_decoupled:
        for shard_id, url in adaface_central_metrics_urls(cfg).items():
            urls[f"adaface-{shard_id}-metrics"] = url
    if cfg.adaface_roi_redis:
        urls["adaface-roi-worker-metrics"] = ADAFACE_ROI_METRICS_URL
    deadline = time.time() + 300
    pending = dict(urls)
    observations: list[dict[str, Any]] = []
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                body = fetch_text_url(url, timeout_s=3)
            except Exception as exc:
                observations.append(
                    {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "name": name,
                        "url": url,
                        "ready": False,
                        "error": repr(exc),
                    }
                )
                continue
            if body:
                observations.append(
                    {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "name": name,
                        "url": url,
                        "ready": True,
                    }
                )
                pending.pop(name, None)
        if pending:
            time.sleep(2)
    write_json(
        cfg.artifact_dir / "dual_shard_metrics_ready.json",
        {"pending": pending, "observations": observations[-100:]},
    )
    if pending:
        raise RuntimeError(f"dual shard metrics endpoints not ready: {pending}")


def save_performance_config(cfg: PressureConfig, original: dict[str, Any]) -> None:
    payload = dict(original)
    payload.update(
        {
            "forwarder_sampler_enabled": True,
            "analysis_fps": cfg.fps,
            "analysis_min_fps": cfg.min_fps,
            "ingress_fps_gate_enabled": True,
            "savant_max_fps": cfg.fps,
            "savant_min_fps": cfg.min_fps,
            "savant_batch_size": cfg.batch_size,
            "pose_batch_size": cfg.pose_batch_size,
            "face_detector_batch_size": cfg.face_detector_batch_size,
            "face_embedding_batch_size": cfg.face_embedding_batch_size,
            "face_infer_interval": cfg.face_infer_interval,
            "face_embedding_infer_interval": cfg.face_embedding_infer_interval,
            "max_parallel_streams": cfg.max_parallel_streams,
            "batched_push_timeout": cfg.batched_push_timeout,
        }
    )
    write_json(cfg.artifact_dir / "performance_pressure_payload.json", payload)
    write_json(
        cfg.artifact_dir / "performance_save_pressure.json",
        api_json(cfg.api_base, "PUT", "/runtime/performance-config", payload),
    )
    write_json(
        cfg.artifact_dir / "performance_apply_pressure.json",
        api_json(
            cfg.api_base,
            "POST",
            f"/runtime/performance-config/apply?force={str(cfg.force_runtime_restart).lower()}",
        ),
    )


def topology_pressure_payload(cfg: PressureConfig) -> dict[str, Any]:
    branch = {
        "gpu_id": int(cfg.dual_shard_gpu),
        "savant_batch_size": cfg.batch_size,
        "pose_batch_size": cfg.pose_batch_size,
        "face_detector_batch_size": cfg.face_detector_batch_size,
        "face_embedding_batch_size": cfg.face_embedding_batch_size,
        "face_infer_interval": cfg.face_infer_interval,
        "face_embedding_infer_interval": cfg.face_embedding_infer_interval,
        "max_parallel_streams": cfg.max_parallel_streams,
        "analysis_fps": cfg.fps,
        "analysis_min_fps": cfg.min_fps,
        "savant_max_fps": cfg.fps,
        "savant_min_fps": cfg.min_fps,
        "batched_push_timeout": cfg.batched_push_timeout,
    }
    payload: dict[str, Any] = {
        "topology_mode": "dual_same_gpu",
        "shard_strategy": (
            "manual"
            if cfg.dual_shard_source_mode in {"all-a", "all-b"}
            else "balanced"
        ),
        "streams_per_branch": max(1, cfg.stream_count // 2),
        "branches": {
            "a": dict(branch),
            "b": dict(branch),
        },
    }
    if cfg.dual_shard_source_mode in {"all-a", "all-b"}:
        target_branch = "a" if cfg.dual_shard_source_mode == "all-a" else "b"
        payload["manual_assignments"] = {
            f"{cfg.run_id}_{index:02d}": target_branch
            for index in range(cfg.stream_count)
        }
    return payload


def save_and_apply_topology_config(cfg: PressureConfig) -> dict[str, Any]:
    payload = topology_pressure_payload(cfg)
    write_json(cfg.artifact_dir / "topology_pressure_payload.json", payload)
    write_json(
        cfg.artifact_dir / "topology_save_pressure.json",
        api_json(cfg.api_base, "PUT", "/runtime/topology-config", payload, timeout_s=60),
    )
    response = api_json(
        cfg.api_base,
        "POST",
        f"/runtime/topology-config/apply?force={str(cfg.force_runtime_restart).lower()}",
        timeout_s=900,
    )
    write_json(cfg.artifact_dir / "topology_apply_pressure.json", response)
    if response.get("error"):
        raise RuntimeError(f"runtime topology apply failed: {response['error']}")
    return response["data"]


def docker_container_env(name: str) -> dict[str, str]:
    completed = subprocess.run(
        ["docker", "inspect", name, "--format", "{{json .Config.Env}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        return {}
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    result: dict[str, str] = {}
    for item in values or []:
        key, sep, value = str(item).partition("=")
        if sep:
            result[key] = value
    return result


def apply_worker_cpu_isolation(cfg: PressureConfig) -> dict[str, Any]:
    profile = CPU_ISOLATION_PROFILES[cfg.cpu_isolation_profile]
    target_cpuset = str(profile.get("workers") or "")
    available_cpus = int(os.cpu_count() or 1)
    requested_cpus = {
        cpu
        for value in profile.values()
        for cpu in _expand_cpuset(str(value))
    }
    if requested_cpus and max(requested_cpus) >= available_cpus:
        raise RuntimeError(
            f"cpu isolation profile {cfg.cpu_isolation_profile} requires CPU "
            f"{max(requested_cpus)} but host exposes {available_cpus} CPUs"
        )
    containers = sorted(set(WORKER_CONTAINER_NAMES.values()))
    snapshot: dict[str, Any] = {
        "profile": cfg.cpu_isolation_profile,
        "target_cpuset": target_cpuset,
        "containers": {},
    }
    for container in containers:
        container_target_cpuset = _worker_target_cpuset(profile, container)
        original = docker_container_cpuset(container)
        row = {
            "original_cpuset": original,
            "target_cpuset": container_target_cpuset,
            "applied": False,
            "error": "",
        }
        snapshot["containers"][container] = row
        if container_target_cpuset:
            completed = subprocess.run(
                [
                    "docker",
                    "update",
                    "--cpuset-cpus",
                    container_target_cpuset,
                    container,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            observed = docker_container_cpuset(container)
            row["observed_cpuset"] = observed
            row["applied"] = (
                completed.returncode == 0 and observed == container_target_cpuset
            )
            if not row["applied"]:
                row["error"] = (
                    completed.stdout.strip()
                    if completed.returncode != 0
                    else f"observed cpuset {observed!r}"
                )
                restore_worker_cpu_isolation(cfg, snapshot)
                raise RuntimeError(
                    f"failed to apply worker cpuset container={container}: {row['error']}"
                )
    write_json(cfg.artifact_dir / "cpu_isolation_apply.json", snapshot)
    return snapshot


def reapply_worker_cpu_isolation(cfg: PressureConfig) -> dict[str, Any]:
    """Reapply worker cpusets after pressure-specific container recreates."""
    profile = CPU_ISOLATION_PROFILES[cfg.cpu_isolation_profile]
    target_cpuset = str(profile.get("workers") or "")
    result: dict[str, Any] = {
        "profile": cfg.cpu_isolation_profile,
        "target_cpuset": target_cpuset,
        "containers": {},
    }
    if not target_cpuset:
        return result
    for container in sorted(set(WORKER_CONTAINER_NAMES.values())):
        container_target_cpuset = _worker_target_cpuset(profile, container)
        completed = subprocess.run(
            [
                "docker",
                "update",
                "--cpuset-cpus",
                container_target_cpuset,
                container,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        observed = docker_container_cpuset(container)
        ok = completed.returncode == 0 and observed == container_target_cpuset
        result["containers"][container] = {
            "target_cpuset": container_target_cpuset,
            "observed_cpuset": observed,
            "ok": ok,
            "error": "" if ok else completed.stdout.strip(),
        }
        if not ok:
            write_json(cfg.artifact_dir / "cpu_isolation_reapply.json", result)
            raise RuntimeError(
                f"failed to reapply worker cpuset container={container} "
                f"observed={observed!r} output={completed.stdout.strip()!r}"
            )
    write_json(cfg.artifact_dir / "cpu_isolation_reapply.json", result)
    return result


def apply_worker_cpu_isolation_for_drain(cfg: PressureConfig) -> dict[str, Any]:
    """Expand evidence workers only after measured sources have stopped."""
    profile = CPU_ISOLATION_PROFILES[cfg.cpu_isolation_profile]
    drain_cpuset = str(profile.get("drain-workers") or "")
    result: dict[str, Any] = {
        "profile": cfg.cpu_isolation_profile,
        "target_cpuset": drain_cpuset,
        "containers": {},
    }
    if not drain_cpuset:
        return result
    for container in sorted(set(WORKER_CONTAINER_NAMES.values())):
        completed = subprocess.run(
            ["docker", "update", "--cpuset-cpus", drain_cpuset, container],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        observed = docker_container_cpuset(container)
        ok = completed.returncode == 0 and observed == drain_cpuset
        result["containers"][container] = {
            "target_cpuset": drain_cpuset,
            "observed_cpuset": observed,
            "ok": ok,
            "error": "" if ok else completed.stdout.strip(),
        }
        if not ok:
            write_json(cfg.artifact_dir / "cpu_isolation_drain.json", result)
            raise RuntimeError(
                f"failed to expand drain worker cpuset container={container} "
                f"observed={observed!r} output={completed.stdout.strip()!r}"
            )
    write_json(cfg.artifact_dir / "cpu_isolation_drain.json", result)
    return result


def _worker_target_cpuset(profile: dict[str, str], container: str) -> str:
    service = WORKER_COMPOSE_SERVICES.get(container, "")
    override = profile.get(f"worker-{service}") if service else None
    return str(override or profile.get("workers") or "")


def restore_worker_cpu_isolation(cfg: PressureConfig, snapshot: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"profile": snapshot.get("profile"), "containers": {}}
    recreate: list[tuple[str, str]] = []
    for container, original in (snapshot.get("containers") or {}).items():
        cpuset = str((original or {}).get("original_cpuset") or "")
        if not cpuset:
            service = WORKER_COMPOSE_SERVICES.get(container)
            if service:
                recreate.append((container, service))
                continue
        completed = subprocess.run(
            ["docker", "update", "--cpuset-cpus", cpuset, container],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        observed = docker_container_cpuset(container)
        result["containers"][container] = {
            "restored_cpuset": cpuset,
            "observed_cpuset": observed,
            "method": "docker_update",
            "ok": completed.returncode == 0 and observed == cpuset,
            "error": (
                completed.stdout.strip()
                if completed.returncode != 0
                else ("" if observed == cpuset else f"observed cpuset {observed!r}")
            ),
        }
    if recreate:
        storage_override = Path(cfg.compose_file).with_name(
            "midterm-storage.override.yml"
        )
        compose = [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
        ]
        if storage_override.is_file():
            compose.extend(["-f", str(storage_override)])
        compose.extend(
            [
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                *[service for _container, service in recreate],
            ]
        )
        completed = run(
            compose,
            cfg.artifact_dir / "compose_restore_worker_cpu_isolation.log",
            check=False,
        )
        for container, _service in recreate:
            observed = docker_container_cpuset(container)
            result["containers"][container] = {
                "restored_cpuset": "",
                "observed_cpuset": observed,
                "method": "compose_recreate",
                "ok": completed.returncode == 0 and observed == "",
                "error": (
                    f"compose recreate rc={completed.returncode}"
                    if completed.returncode != 0
                    else ("" if observed == "" else f"observed cpuset {observed!r}")
                ),
            }
    write_json(cfg.artifact_dir / "cpu_isolation_restore.json", result)
    return result


def docker_container_cpuset(name: str) -> str:
    completed = subprocess.run(
        ["docker", "inspect", name, "--format", "{{.HostConfig.CpusetCpus}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _expand_cpuset(value: str) -> set[int]:
    cpus: set[int] = set()
    for token in str(value or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            cpus.update(range(int(start), int(end) + 1))
        else:
            cpus.add(int(token))
    return cpus


def docker_container_state(name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "inspect", name, "--format", "{{json .State}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        return {
            "container": name,
            "exists": False,
            "running": False,
            "status": "missing",
            "inspect_error": completed.stdout.strip(),
        }
    try:
        state = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "container": name,
            "exists": True,
            "running": False,
            "status": "inspect_parse_failed",
            "inspect_error": completed.stdout.strip(),
        }
    health = state.get("Health") if isinstance(state, dict) else None
    return {
        "container": name,
        "exists": True,
        "running": bool((state or {}).get("Running")),
        "status": str((state or {}).get("Status") or ""),
        "health": (
            str(health.get("Status") or "")
            if isinstance(health, dict)
            else ""
        ),
    }


def clip_worker_replay_shard_env_snapshot() -> dict[str, str]:
    env = docker_container_env("video-analytics-midterm-clip-worker")
    return {
        "REPLAY_SHARDS_JSON": env.get("REPLAY_SHARDS_JSON", ""),
        "REPLAY_SHARDS_CONFIG_PATH": env.get("REPLAY_SHARDS_CONFIG_PATH", ""),
        "EVIDENCE_DENSITY_PROFILE": env.get("EVIDENCE_DENSITY_PROFILE", ""),
        "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS": env.get(
            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
            "",
        ),
        "EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL": env.get(
            "EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL",
            "",
        ),
        "CLIP_WORKER_MAX_JOBS_PER_RUN": env.get("CLIP_WORKER_MAX_JOBS_PER_RUN", ""),
        "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY": env.get(
            "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY",
            "",
        ),
        "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD": env.get(
            "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD",
            "",
        ),
    }


def event_worker_evidence_env_snapshot() -> dict[str, str]:
    env = docker_container_env("video-analytics-midterm-event-worker")
    keys = (
        "EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE",
        "RECORDING_COOLDOWN_SECONDS",
        "RECORDING_COOLDOWN_SCOPE",
        "ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS",
        "EVIDENCE_DENSITY_PROFILE",
        "EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS",
        "EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED",
        "EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS",
        "EVIDENCE_EVENT_COVERAGE_EVENT_TYPES",
        "EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS",
        "EVIDENCE_TASK_CREATION_ENABLED",
    )
    result = {key: env.get(key, "") for key in keys}
    result["EVIDENCE_TASK_CREATION_ENABLED"] = env.get(
        "EVIDENCE_TASK_CREATION_ENABLED", "true"
    )
    return result


def media_worker_rolling_cache_env_snapshot() -> dict[str, str]:
    env = docker_container_env("video-analytics-midterm-media-worker")
    keys = (
        "MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE",
        "MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT",
        "MEDIA_WORKER_FFMPEG_X264_PRESET",
        "MEDIA_WORKER_IMAGE_WORKERS",
        "MEDIA_WORKER_IMAGE_QUEUE_CAPACITY",
        "MEDIA_WORKER_REMUX_QUEUE_CAPACITY",
        "MEDIA_WORKER_FINALIZER_WORKERS",
        "MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY",
        "MEDIA_WORKER_SCHEDULER_V2_ENABLED",
        "MEDIA_WORKER_DB_POOL_ENABLED",
        "MEDIA_WORKER_SEGMENT_INDEX_ENABLED",
        "ROLLING_CACHE_ENABLED",
        "ROLLING_CACHE_MATERIALIZATION_ENABLED",
        "ROLLING_CACHE_SOURCES",
        "ROLLING_CACHE_ROOT",
        "ROLLING_CACHE_MATERIALIZED_ROOT",
        "ROLLING_CACHE_RETENTION_SECONDS",
        "ROLLING_CACHE_SEGMENT_SECONDS",
        "ROLLING_CACHE_FALLBACK_TO_REPLAY",
        "ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL",
        "ROLLING_CACHE_MATERIALIZATION_WORKERS",
        "ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S",
        "ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS",
        "ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS",
        "EVIDENCE_DENSITY_PROFILE",
        "POST_SAVANT_FAST_RAW_CLIP_ENABLED",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_BUCKET_MS",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_TTL_S",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_MAX_ENTRIES",
        "FRAME_CACHE_SIDECAR_MAX_SCAN",
        "FRAME_CACHE_SIDECAR_MAX_EVENTS_PER_RUN",
        "FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED",
        "FRAME_CACHE_SIDECAR_SOURCE_STREAM_FALLBACK_GLOBAL",
        "EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED",
    )
    return {key: env.get(key, "") for key in keys}


def rolling_cache_sink_state_snapshot() -> dict[str, dict[str, Any]]:
    containers = {
        "rolling-cache-sink": "video-analytics-midterm-rolling-cache-sink",
        "rolling-cache-sink-a": "video-analytics-midterm-rolling-cache-sink-a",
        "rolling-cache-sink-b": "video-analytics-midterm-rolling-cache-sink-b",
    }
    return {
        service: docker_container_state(container)
        for service, container in containers.items()
    }


def pressure_source_ids(cfg: PressureConfig) -> list[str]:
    return [f"{cfg.run_id}_{index:02d}" for index in range(cfg.stream_count)]


def _env_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def configure_media_worker_rolling_cache(
    cfg: PressureConfig,
    *,
    values: dict[str, str],
    artifact_name: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(values)
    override_path = cfg.artifact_dir / artifact_name.replace(
        ".log", ".override.yml"
    )
    write_text(
        override_path,
        yaml.safe_dump(
            {"services": {"media-worker": {"environment": values}}},
            sort_keys=False,
        ),
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "-f",
            str(override_path),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "media-worker",
        ],
        cfg.artifact_dir / artifact_name,
        env=env,
    )
    observed = media_worker_rolling_cache_env_snapshot()
    summary = {"requested": values, "observed": observed}
    write_json(cfg.artifact_dir / artifact_name.replace(".log", ".json"), summary)
    for key, expected in values.items():
        if expected == "":
            continue
        if observed.get(key, "") != expected:
            raise RuntimeError(
                f"media-worker {key} was not applied after compose recreate"
            )
    return summary


def start_rolling_cache_sinks_for_pressure(
    cfg: PressureConfig,
    *,
    runtime_epoch_id: str = "",
) -> dict[str, Any]:
    if cfg.dual_shard_same_gpu:
        profiles = [DUAL_SHARD_PROFILE, "rolling-cache-dual"]
        services = ["rolling-cache-sink-a", "rolling-cache-sink-b"]
        dependency_services = ["replay-raw-fanout-a", "replay-raw-fanout-b"]
    else:
        profiles = ["rolling-cache"]
        services = ["rolling-cache-sink"]
        dependency_services = ["replay-raw-fanout"]
    # The rolling sinks consume the per-source republish output.  When the
    # pressure profile uses a fixed fixture, that output inherits the fixture
    # cadence rather than the unrelated shared/default RTSP URI cadence.
    input_fps_probe = probe_video_input_fps(cfg.rtsp_republish_input_uri)
    rolling_cache_input_fps = (
        float(input_fps_probe.get("fps") or 0.0)
        or _fps_to_float(os.environ.get("ROLLING_CACHE_FPS", ""))
        or 24.0
    )
    env = os.environ.copy()
    env.update(
        {
            "ROLLING_CACHE_ROOT": "/media/rolling-cache",
            "ROLLING_CACHE_SEGMENT_SECONDS": "4",
            "ROLLING_CACHE_FPS": _format_fps_float(rolling_cache_input_fps),
            "ROLLING_CACHE_RUNTIME_EPOCH_ID": runtime_epoch_id,
        }
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            *[item for profile in profiles for item in ("--profile", profile)],
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *services,
        ],
        cfg.artifact_dir / "compose_recreate_rolling_cache_sinks.log",
        env=env,
    )
    service_states = {
        service: docker_container_state(
            "video-analytics-midterm-rolling-cache-sink"
            if service == "rolling-cache-sink"
            else f"video-analytics-midterm-{service}"
        )
        for service in services
    }
    dependency_states = {
        service: docker_container_state(f"video-analytics-midterm-{service}")
        for service in dependency_services
    }
    missing_dependencies = [
        service
        for service, state in dependency_states.items()
        if not bool(state.get("running"))
    ]
    summary = {
        "profiles": profiles,
        "services": services,
        "dependency_services": dependency_services,
        "runtime_epoch_id": runtime_epoch_id,
        "input_fps_probe": input_fps_probe,
        "rolling_cache_expected_raw_fps": rolling_cache_input_fps,
        "states": service_states,
        "dependency_states": dependency_states,
        "missing_dependencies": missing_dependencies,
    }
    write_json(cfg.artifact_dir / "rolling_cache_sinks_pressure.json", summary)
    if missing_dependencies:
        raise RuntimeError(
            "rolling cache raw fanout dependencies are not running: "
            + ",".join(missing_dependencies)
        )
    return summary


def stop_rolling_cache_sinks_before_pressure_reconfigure(
    cfg: PressureConfig,
) -> dict[str, Any]:
    if cfg.dual_shard_same_gpu:
        profiles = [DUAL_SHARD_PROFILE, "rolling-cache-dual"]
        services = ["rolling-cache-sink-a", "rolling-cache-sink-b"]
    else:
        profiles = ["rolling-cache"]
        services = ["rolling-cache-sink"]
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            *[item for profile in profiles for item in ("--profile", profile)],
            "stop",
            *services,
        ],
        cfg.artifact_dir / "compose_stop_rolling_cache_sinks_before_reconfigure.log",
        check=False,
    )
    states = {
        service: docker_container_state(
            "video-analytics-midterm-rolling-cache-sink"
            if service == "rolling-cache-sink"
            else f"video-analytics-midterm-{service}"
        )
        for service in services
    }
    summary = {"profiles": profiles, "services": services, "states": states}
    write_json(
        cfg.artifact_dir / "rolling_cache_sinks_stopped_before_reconfigure.json",
        summary,
    )
    return summary


def restore_rolling_cache_sinks_after_pressure(
    cfg: PressureConfig,
    *,
    original_states: dict[str, dict[str, Any]],
    artifact_name: str,
) -> dict[str, Any]:
    if cfg.dual_shard_same_gpu:
        profiles = [DUAL_SHARD_PROFILE, "rolling-cache-dual"]
        services = ["rolling-cache-sink-a", "rolling-cache-sink-b"]
    else:
        profiles = ["rolling-cache"]
        services = ["rolling-cache-sink"]
    services_to_stop = [
        service
        for service in services
        if not bool((original_states.get(service) or {}).get("running"))
    ]
    if services_to_stop:
        run(
            [
                "docker",
                "compose",
                "--env-file",
                cfg.env_file,
                "-f",
                cfg.compose_file,
                *[item for profile in profiles for item in ("--profile", profile)],
                "stop",
                *services_to_stop,
            ],
            cfg.artifact_dir / artifact_name,
            check=False,
        )
    else:
        write_text(
            cfg.artifact_dir / artifact_name,
            "No rolling-cache sink services were stopped; all pressure services were already running before the run.\n",
        )
    summary = {
        "profiles": profiles,
        "services": services,
        "stopped": services_to_stop,
        "original": original_states,
        "after": rolling_cache_sink_state_snapshot(),
    }
    write_json(cfg.artifact_dir / artifact_name.replace(".log", ".json"), summary)
    return summary


def stop_rolling_cache_sinks_after_pressure(
    cfg: PressureConfig,
    *,
    artifact_name: str,
) -> dict[str, Any]:
    containers = [
        "video-analytics-midterm-rolling-cache-sink",
        "video-analytics-midterm-rolling-cache-sink-a",
        "video-analytics-midterm-rolling-cache-sink-b",
    ]
    run(
        ["docker", "stop", *containers],
        cfg.artifact_dir / artifact_name,
        check=False,
    )
    summary = {
        "containers": containers,
        "after": rolling_cache_sink_state_snapshot(),
    }
    write_json(cfg.artifact_dir / artifact_name.replace(".log", ".json"), summary)
    return summary


def configure_rolling_cache_workers_for_pressure(cfg: PressureConfig) -> dict[str, Any] | None:
    if not cfg.rolling_cache_evidence:
        return None
    max_policy_window_s = max(
        1,
        max(
            int(pre_seconds) + int(post_seconds)
            for pre_seconds, post_seconds in cfg.evidence_policy_groups
        ),
    )
    event_values = {
        "ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS": "true",
        "EVIDENCE_DENSITY_PROFILE": "high_density",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL": "0",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE": "0",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE": "0",
        "EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED": (
            "true" if cfg.rolling_cache_enable_coverage_merge else "false"
        ),
        "EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS": "60",
        "EVIDENCE_EVENT_COVERAGE_EVENT_TYPES": (
            "intrusion,watchlist_hit,live_search_hit"
        ),
        "EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS": str(max_policy_window_s),
        # Pressure runs need the same cooldown semantics as the generated
        # camera policies: one 30s budget per camera and algorithm, not one
        # shared budget per camera across intrusion/watchlist events.
        "RECORDING_COOLDOWN_SCOPE": "algorithm",
        # Task readiness is event time + post window + rolling segment close /
        # index-visible delay, so natural waiting never consumes a media-worker
        # slot. Live pressure p95 showed 5s was still early; 9s matches the
        # observed segment-visible tail without pushing real processing into
        # worker occupancy.
        "EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": "9",
    }
    if cfg.pressure_disable_evidence_admission:
        event_values.update(
            {
                "EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL": "0",
                "EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE": "0",
                "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE": "0",
                "RECORDING_COOLDOWN_SECONDS": "0",
            }
        )
    media_values = {
        # The Phase 6 4/8/12 matrix keeps the remux candidate at one and changes
        # only shared WIP.  A non-default value is a separately labeled remux
        # lane experiment; the complete override remains in the artifact.
        "MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE": str(
            cfg.media_worker_materialization_max_active
        ),
        "MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT": "4",
        "MEDIA_WORKER_FFMPEG_X264_PRESET": "ultrafast",
        "MEDIA_WORKER_IMAGE_WORKERS": "4",
        "MEDIA_WORKER_IMAGE_QUEUE_CAPACITY": "4",
        "MEDIA_WORKER_REMUX_QUEUE_CAPACITY": "4",
        "MEDIA_WORKER_FINALIZER_WORKERS": "32",
        "MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY": "4",
        "MEDIA_WORKER_SCHEDULER_V2_ENABLED": "true",
        "MEDIA_WORKER_DB_POOL_ENABLED": "true",
        "MEDIA_WORKER_SEGMENT_INDEX_ENABLED": "true",
        "EVIDENCE_DENSITY_PROFILE": "high_density",
        "ROLLING_CACHE_ENABLED": "true",
        "ROLLING_CACHE_MATERIALIZATION_ENABLED": "true",
        "ROLLING_CACHE_SOURCES": ",".join(pressure_source_ids(cfg)),
        "ROLLING_CACHE_ROOT": "/media/rolling-cache",
        "ROLLING_CACHE_MATERIALIZED_ROOT": "/media/rolling-cache-materialized",
        "ROLLING_CACHE_RETENTION_SECONDS": str(
            max(900, cfg.duration_s + cfg.drain_s + 120)
        ),
        "ROLLING_CACHE_SEGMENT_SECONDS": "4",
        # Pressure acceptance must prove the Replay raw tap -> rolling-cache
        # path. Falling back to Replay jobs can silently mix in the legacy
        # video-file-sink route and hide a wrong tap point.
        "ROLLING_CACHE_FALLBACK_TO_REPLAY": "false",
        "ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL": str(
            max(16, min(256, cfg.stream_count * 4))
        ),
        "ROLLING_CACHE_MATERIALIZATION_WORKERS": str(
            cfg.media_worker_rolling_remux_workers
        ),
        "ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S": "1",
        # Fallback for tasks created before materialization_ready_at existed.
        # Keep it aligned with the event-worker ready_at grace so old rows do
        # not get claimed earlier than newly inserted rows.
        "ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": "9",
        "ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS": "120",
        # Finalizer sidecar generation otherwise re-reads and reparses the same
        # multi-source frame-annotation Redis ranges once per evidence event.
        # Bucketed range caching keeps the operator overlays intact while
        # avoiding the 60-source fan-out cost in pressure runs.
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_BUCKET_MS": "10000",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_TTL_S": "900",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_MAX_ENTRIES": "64",
        "FRAME_CACHE_SIDECAR_MAX_SCAN": "30000",
        "FRAME_CACHE_SIDECAR_MAX_EVENTS_PER_RUN": str(
            max(1000, cfg.stream_count * 40)
        ),
        "FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED": "true",
        "FRAME_CACHE_SIDECAR_SOURCE_STREAM_FALLBACK_GLOBAL": "false",
        # Canonical pressure evidence must be reviewable from the same
        # DB-backed 8090 path operators use: timeline rows, overlay rows,
        # person boxes, and track trails are acceptance artifacts, not
        # optional filesystem fallbacks.
        "EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED": "true",
    }
    return {
        "event_worker": configure_event_worker_evidence_admission(
            cfg,
            values=event_values,
            artifact_name="compose_recreate_event_worker_rolling_cache.log",
        ),
        "media_worker": configure_media_worker_rolling_cache(
            cfg,
            values=media_values,
            artifact_name="compose_recreate_media_worker_rolling_cache.log",
        ),
    }


def configure_event_worker_evidence_admission(
    cfg: PressureConfig,
    *,
    values: dict[str, str],
    artifact_name: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(values)
    override_path = cfg.artifact_dir / artifact_name.replace(
        ".log", ".override.yml"
    )
    write_text(
        override_path,
        yaml.safe_dump(
            {"services": {"event-worker": {"environment": values}}},
            sort_keys=False,
        ),
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "-f",
            str(override_path),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "event-worker",
        ],
        cfg.artifact_dir / artifact_name,
        env=env,
    )
    observed = event_worker_evidence_env_snapshot()
    summary = {"requested": values, "observed": observed}
    write_json(cfg.artifact_dir / artifact_name.replace(".log", ".json"), summary)
    for key, expected in values.items():
        if expected == "":
            continue
        if observed.get(key, "") != expected:
            raise RuntimeError(
                f"event-worker {key} was not applied after compose recreate"
            )
    return summary


def configure_event_worker_for_pressure(cfg: PressureConfig) -> dict[str, Any] | None:
    values: dict[str, str] = {}
    if cfg.savant_ablation_stage != "full-evidence":
        values["EVIDENCE_TASK_CREATION_ENABLED"] = "false"
    if cfg.pressure_disable_evidence_admission:
        values.update(
            {
                "EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL": "0",
                "EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE": "0",
                "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE": "0",
                "RECORDING_COOLDOWN_SECONDS": "0",
            }
        )
    if not values:
        return None
    return configure_event_worker_evidence_admission(
        cfg,
        values=values,
        artifact_name="compose_recreate_event_worker_pressure_admission.log",
    )


def replay_topology_summary() -> dict[str, Any]:
    env = clip_worker_replay_shard_env_snapshot()
    shard_json = env.get("REPLAY_SHARDS_JSON", "")
    shard_config_path = env.get("REPLAY_SHARDS_CONFIG_PATH", "")
    containers = {
        service: docker_container_state(container_name)
        for service, container_name in REPLAY_TOPOLOGY_CONTAINERS.items()
    }
    shard_services = ["replay-a", "replay-b", "video-file-sink-a", "video-file-sink-b"]
    for shard_id in EVIDENCE_SHARD_IDS[2:]:
        suffix = remove_prefix(shard_id, "replay-")
        if shard_id in shard_json:
            shard_services.extend([shard_id, f"video-file-sink-{suffix}"])
    shard_containers_running = all(
        bool(containers.get(service, {}).get("running")) for service in shard_services
    )
    shard_config_present = bool(shard_json.strip() or shard_config_path.strip())
    dual_replay_enabled = shard_config_present and shard_containers_running
    reason_details: list[str] = []
    if not shard_config_path.strip() and not shard_json.strip():
        reason_details.append("REPLAY_SHARDS_CONFIG_PATH empty and REPLAY_SHARDS_JSON empty")
    stopped = [
        service
        for service in shard_services
        if not bool(containers.get(service, {}).get("running"))
    ]
    if stopped:
        reason_details.append(
            "replay/video-file-sink shard containers stopped: " + ",".join(stopped)
        )
    topology = "dual_shard" if dual_replay_enabled else "single_sink"
    summary: dict[str, Any] = {
        "replay_topology": topology,
        "dual_replay_enabled": dual_replay_enabled,
        "reason": (
            ""
            if dual_replay_enabled
            else "REPLAY_SHARDS_CONFIG_PATH empty or replay/video-file-sink shard containers stopped"
        ),
        "reason_details": reason_details,
        "clip_worker": {
            "REPLAY_SHARDS_CONFIG_PATH": shard_config_path,
            "REPLAY_SHARDS_JSON_present": bool(shard_json.strip()),
            "REPLAY_SHARDS_JSON_bytes": len(shard_json.encode("utf-8")),
            "REPLAY_SHARDS_JSON_sha256": (
                hashlib.sha256(shard_json.encode("utf-8")).hexdigest()
                if shard_json
                else ""
            ),
            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS": env.get(
                "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
                "",
            ),
        },
        "containers": containers,
    }
    if dual_replay_enabled:
        summary.update({service: containers[service] for service in shard_services})
    return summary


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence_materialization_global_limit(shard_count: int) -> str:
    per_shard = int(os.getenv("EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD", "9"))
    return str(max(1, shard_count) * max(1, per_shard))


def configure_clip_worker_replay_shards(
    cfg: PressureConfig,
    *,
    replay_shards_json: str,
    replay_shards_config_path: str,
    artifact_name: str,
    materialization_event_type_quotas: str | None = None,
    density_profile: str | None = None,
    materialization_pressure_level: str | None = None,
    max_jobs_per_run: str | None = None,
    materialization_max_concurrency: str | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "REPLAY_SHARDS_JSON": replay_shards_json,
            "REPLAY_SHARDS_CONFIG_PATH": replay_shards_config_path,
        }
    )
    if materialization_event_type_quotas is not None:
        env["EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS"] = (
            materialization_event_type_quotas
        )
    if density_profile is not None:
        env["EVIDENCE_DENSITY_PROFILE"] = density_profile
    if materialization_pressure_level is not None:
        env["EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL"] = materialization_pressure_level
    if max_jobs_per_run is not None:
        env["CLIP_WORKER_MAX_JOBS_PER_RUN"] = max_jobs_per_run
    if materialization_max_concurrency is not None:
        env["EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY"] = (
            materialization_max_concurrency
        )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "clip-worker",
        ],
        cfg.artifact_dir / artifact_name,
        env=env,
    )
    digest = hashlib.sha256(replay_shards_json.encode("utf-8")).hexdigest() if replay_shards_json else ""
    observed = clip_worker_replay_shard_env_snapshot()
    observed_json = observed.get("REPLAY_SHARDS_JSON", "")
    observed_config_path = observed.get("REPLAY_SHARDS_CONFIG_PATH", "")
    observed_event_type_quotas = observed.get(
        "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
        "",
    )
    observed_density_profile = observed.get("EVIDENCE_DENSITY_PROFILE", "")
    observed_pressure_level = observed.get("EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL", "")
    observed_max_jobs_per_run = observed.get("CLIP_WORKER_MAX_JOBS_PER_RUN", "")
    observed_materialization_max_concurrency = observed.get(
        "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY",
        "",
    )
    observed_materialization_max_concurrency_per_shard = observed.get(
        "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD",
        "",
    )
    summary = {
        "container": "video-analytics-midterm-clip-worker",
        "replay_shards_json_sha256": digest,
        "replay_shards_config_path": replay_shards_config_path,
        "replay_shards_json_bytes": len(replay_shards_json.encode("utf-8")),
        "materialization_event_type_quotas": (
            materialization_event_type_quotas
            if materialization_event_type_quotas is not None
            else ""
        ),
        "observed_replay_shards_json_sha256": (
            hashlib.sha256(observed_json.encode("utf-8")).hexdigest()
            if observed_json
            else ""
        ),
        "observed_replay_shards_config_path": observed_config_path,
        "observed_replay_shards_json_bytes": len(observed_json.encode("utf-8")),
        "density_profile": density_profile if density_profile is not None else "",
        "observed_density_profile": observed_density_profile,
        "materialization_pressure_level": (
            materialization_pressure_level
            if materialization_pressure_level is not None
            else ""
        ),
        "observed_materialization_pressure_level": observed_pressure_level,
        "observed_materialization_event_type_quotas": observed_event_type_quotas,
        "max_jobs_per_run": max_jobs_per_run if max_jobs_per_run is not None else "",
        "observed_max_jobs_per_run": observed_max_jobs_per_run,
        "materialization_max_concurrency": (
            materialization_max_concurrency
            if materialization_max_concurrency is not None
            else ""
        ),
        "observed_materialization_max_concurrency": (
            observed_materialization_max_concurrency
        ),
        "observed_materialization_max_concurrency_per_shard": (
            observed_materialization_max_concurrency_per_shard
        ),
    }
    write_json(cfg.artifact_dir / artifact_name.replace(".log", ".json"), summary)
    if observed_json != replay_shards_json:
        raise RuntimeError(
            "clip-worker REPLAY_SHARDS_JSON was not applied after compose recreate"
        )
    if observed_config_path != replay_shards_config_path:
        raise RuntimeError(
            "clip-worker REPLAY_SHARDS_CONFIG_PATH was not applied after compose recreate"
        )
    if (
        materialization_event_type_quotas is not None
        and observed_event_type_quotas != materialization_event_type_quotas
    ):
        raise RuntimeError(
            "clip-worker EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS was not applied "
            "after compose recreate"
        )
    if density_profile is not None and observed_density_profile != (
        density_profile or "normal"
    ):
        raise RuntimeError(
            "clip-worker EVIDENCE_DENSITY_PROFILE was not applied after compose recreate"
        )
    if materialization_pressure_level is not None and observed_pressure_level != (
        materialization_pressure_level or "normal"
    ):
        raise RuntimeError(
            "clip-worker EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL was not applied "
            "after compose recreate"
        )
    if max_jobs_per_run is not None and observed_max_jobs_per_run != max_jobs_per_run:
        raise RuntimeError(
            "clip-worker CLIP_WORKER_MAX_JOBS_PER_RUN was not applied after compose recreate"
        )
    if (
        materialization_max_concurrency is not None
        and observed_materialization_max_concurrency
        != materialization_max_concurrency
    ):
        raise RuntimeError(
            "clip-worker EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY was not applied "
            "after compose recreate"
        )
    return summary


def configure_clip_worker_for_topology_shards(
    cfg: PressureConfig,
    topology: dict[str, Any],
) -> dict[str, Any]:
    replay_shards_path = Path(str(topology.get("replay_shards_path") or ""))
    if not replay_shards_path.exists():
        raise RuntimeError(f"topology replay shard file not found: {replay_shards_path}")
    replay_shards_doc = json.loads(replay_shards_path.read_text(encoding="utf-8"))
    replay_shards_doc.setdefault("mapping_version", f"{cfg.run_id}:topology:v1")
    replay_shards_json = _compact_json(replay_shards_doc)
    summary = configure_clip_worker_replay_shards(
        cfg,
        replay_shards_json=replay_shards_json,
        replay_shards_config_path="",
        artifact_name="compose_recreate_clip_worker_replay_shards.log",
        materialization_event_type_quotas=(
            cfg.pressure_materialization_event_type_quotas
        ),
        density_profile="high_density" if cfg.rolling_cache_evidence else None,
        materialization_pressure_level=(
            "high_density" if cfg.rolling_cache_evidence else None
        ),
        max_jobs_per_run="0" if cfg.keep_evidence < 0 else None,
        materialization_max_concurrency=_evidence_materialization_global_limit(
            len(replay_shards_doc.get("shards") or [])
        ),
    )
    summary.update(
        {
            "topology_replay_shards_path": str(replay_shards_path),
            "topology_replay_shards_sha256": hashlib.sha256(
                replay_shards_json.encode("utf-8")
            ).hexdigest(),
            "shard_count": len(replay_shards_doc.get("shards") or []),
            "source_count": sum(
                len(shard.get("source_ids") or [])
                for shard in replay_shards_doc.get("shards") or []
                if isinstance(shard, dict)
            ),
            "mapping_version": replay_shards_doc.get("mapping_version", ""),
        }
    )
    write_json(cfg.artifact_dir / "clip_worker_replay_shards_pressure.json", summary)
    return summary


def pressure_camera_name(index: int) -> str:
    """Return the 1-based, operator-facing name used throughout 8090."""
    if index < 0:
        raise ValueError("pressure camera index must be non-negative")
    return f"压力摄像头 {index + 1:02d}"


def pressure_camera_location(index: int) -> str:
    """Return the stable database slot for one reusable pressure camera."""
    if index < 0:
        raise ValueError("pressure camera index must be non-negative")
    return f"pressure-{index:02d}"


def _ensure_pressure_rule(
    conn,
    *,
    camera_id: str,
    zone_id: str | None,
    rule_type: str,
    algorithm_id: str,
    rule_id: str,
    config: dict[str, Any],
    evidence_policy: dict[str, Any],
) -> None:
    """Update an existing pressure rule in place, or create it once."""
    existing = conn.execute(
        """
        SELECT id
        FROM camera_rules
        WHERE camera_id=%s
          AND (rule_id=%s OR algorithm_id=%s OR rule_type=%s)
        ORDER BY
            CASE WHEN rule_id=%s THEN 0 WHEN algorithm_id=%s THEN 1 ELSE 2 END,
            id
        LIMIT 1
        FOR UPDATE
        """,
        (camera_id, rule_id, algorithm_id, rule_type, rule_id, algorithm_id),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO camera_rules (
                camera_id, zone_id, rule_type, config, enabled,
                rule_id, algorithm_id, evidence_policy
            )
            VALUES (%s, %s, %s, %s::jsonb, true, %s, %s, %s::jsonb)
            """,
            (
                camera_id,
                zone_id,
                rule_type,
                json.dumps(config),
                rule_id,
                algorithm_id,
                json.dumps(evidence_policy),
            ),
        )
        return

    row = _row_json(existing)
    conn.execute(
        """
        UPDATE camera_rules
        SET zone_id=%s, rule_type=%s, config=%s::jsonb, enabled=true,
            rule_id=%s, algorithm_id=%s, evidence_policy=%s::jsonb,
            updated_at=now()
        WHERE id=%s
        """,
        (
            zone_id,
            rule_type,
            json.dumps(config),
            rule_id,
            algorithm_id,
            json.dumps(evidence_policy),
            row["id"],
        ),
    )


def insert_pressure_cameras(conn, cfg: PressureConfig) -> dict[str, Any]:
    """Enable stable pressure-camera slots, creating only missing slots.

    A source ID remains scoped to the current run so events and artifacts are
    attributable to that run.  The camera row itself is instead identified by
    the stable ``site_id=pressure`` and ``location=pressure-NN`` slot.
    """
    created: list[str] = []
    reused: list[str] = []
    with conn.transaction():
        conn.execute("UPDATE cameras SET enabled=false, updated_at=now()")
        for index in range(cfg.stream_count):
            source_id = f"{cfg.run_id}_{index:02d}"
            camera_name = pressure_camera_name(index)
            location = pressure_camera_location(index)
            zone_id = f"{location}_full_frame"
            rtsp_uri = pressure_rtsp_uri(cfg, index=index, source_id=source_id)
            evidence_policy = evidence_policy_for_index(cfg, index)
            watchlist_evidence_policy = face_image_evidence_policy()
            evidence_enabled = cfg.savant_ablation_stage == "full-evidence"
            if not evidence_enabled:
                evidence_policy = {
                    "snapshot_required": False,
                    "clip_required": False,
                    "pre_seconds": 0,
                    "post_seconds": 0,
                }
                watchlist_evidence_policy = dict(evidence_policy)
            alert_policy = {
                "global_alert_cooldown_s": cfg.pressure_algorithm_cooldown_s,
                "cooldown_scope": "algorithm",
                "critical_bypass": False,
                "store_suppressed_events": True,
            }
            points = [[0.0, 0.0], [1920.0, 0.0], [1920.0, 1080.0], [0.0, 1080.0]]
            existing = conn.execute(
                """
                SELECT id
                FROM cameras
                WHERE site_id='pressure' AND location=%s
                ORDER BY updated_at DESC, created_at DESC, id
                LIMIT 1
                FOR UPDATE
                """,
                (location,),
            ).fetchone()
            if existing is None:
                camera_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO cameras (
                        id, source_id, name, rtsp_url, site_id, location, gpu_id,
                        enabled, input_type, rtsp_transport, fps_policy, alert_policy
                    )
                    VALUES (%s, %s, %s, %s, 'pressure', %s, 0, true, 'rtsp', 'tcp', '{}'::jsonb, %s::jsonb)
                    """,
                    (
                        camera_id,
                        source_id,
                        camera_name,
                        rtsp_uri,
                        location,
                        json.dumps(alert_policy),
                    ),
                )
                created.append(camera_id)
            else:
                camera_id = str(_row_json(existing)["id"])
                conn.execute(
                    """
                    UPDATE cameras
                    SET source_id=%s, name=%s, rtsp_url=%s, gpu_id=0, enabled=true,
                        input_type='rtsp', rtsp_transport='tcp', fps_policy='{}'::jsonb,
                        alert_policy=%s::jsonb, updated_at=now()
                    WHERE id=%s
                    """,
                    (
                        source_id,
                        camera_name,
                        rtsp_uri,
                        json.dumps(alert_policy),
                        camera_id,
                    ),
                )
                reused.append(camera_id)
            conn.execute(
                """
                INSERT INTO camera_zones (
                    camera_id, zone_id, zone_name, zone_type,
                    coordinate_space, points, enabled, payload
                )
                VALUES (%s, %s, %s, 'polygon', 'pixel', %s::jsonb, true, '{}'::jsonb)
                ON CONFLICT (camera_id, zone_id) DO UPDATE
                SET zone_name=EXCLUDED.zone_name, zone_type=EXCLUDED.zone_type,
                    coordinate_space=EXCLUDED.coordinate_space,
                    points=EXCLUDED.points, enabled=true,
                    payload=EXCLUDED.payload, updated_at=now()
                """,
                (camera_id, zone_id, "full frame", json.dumps(points)),
            )
            intrusion_config = {
                "zone": zone_id,
                "zone_id": zone_id,
                "severity": "medium",
                "cooldown_s": cfg.pressure_algorithm_cooldown_s,
                "min_inside_ms": 1000,
                "min_person_width": 20,
                "min_person_height": 40,
                "snapshot_required": False,
                "clip_required": evidence_enabled,
                "max_bbox_area_ratio": 0.9,
                "min_person_confidence": 0.25,
                "min_visible_keypoints": 0,
            }
            watchlist_config = {
                "severity": "medium",
                "threshold": 0.60,
                "min_similarity": 0.60,
                "cooldown_s": cfg.pressure_algorithm_cooldown_s,
                "watchlist_enabled": True,
                "live_search_enabled": True,
                "target_names": ["Reese", "Finch"],
                "target_external_person_ids": ["demo:midterm:reese", "demo:midterm:finch"],
            }
            _ensure_pressure_rule(
                conn,
                camera_id=camera_id,
                zone_id=zone_id,
                rule_type="intrusion",
                algorithm_id="behavior.intrusion",
                rule_id=f"{location}_intrusion",
                config=intrusion_config,
                evidence_policy=evidence_policy,
            )
            _ensure_pressure_rule(
                conn,
                camera_id=camera_id,
                zone_id=None,
                rule_type="face.watchlist",
                algorithm_id="face.watchlist",
                rule_id=f"{location}_watchlist",
                config=watchlist_config,
                evidence_policy=watchlist_evidence_policy,
            )
    return {
        "camera_slots_requested": cfg.stream_count,
        "created_count": len(created),
        "reused_count": len(reused),
        "created_camera_ids": created,
        "reused_camera_ids": reused,
        "source_ids": pressure_source_ids(cfg),
    }


def sync_module_config_snapshot(cfg: PressureConfig) -> dict[str, Any]:
    body = api_text(cfg.api_base, "/cameras/config/export?include_disabled=true", timeout_s=60)
    artifact_path = cfg.artifact_dir / "cameras.midterm.pressure.yml"
    write_text(artifact_path, body)
    module_path = DEFAULT_MODULE_CONFIG_PATH
    _atomic_write_text(module_path, body)
    return {
        "module_config_path": str(module_path),
        "artifact_config_path": str(artifact_path),
    }


def _active_evidence_shard_ids(cfg: PressureConfig) -> list[str]:
    count = int(cfg.evidence_shard_count or 2)
    if count not in (2, 4, 8):
        raise ValueError(f"unsupported evidence_shard_count={count}")
    return list(EVIDENCE_SHARD_IDS[:count])


def _evidence_shard_for_index(
    cfg: PressureConfig,
    *,
    index: int,
    shard_ids: list[str],
) -> str:
    if cfg.dual_shard_source_mode == "all-a":
        return "replay-a"
    if cfg.dual_shard_source_mode == "all-b":
        return "replay-b"
    return shard_ids[index % len(shard_ids)]


def write_dual_shard_pressure_sources(conn, cfg: PressureConfig) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, name, source_id, rtsp_url, enabled
        FROM cameras
        WHERE source_id LIKE %(prefix)s
        ORDER BY source_id
        """,
        {"prefix": f"{cfg.run_id}_%"},
    ).fetchall()
    if len(rows) != cfg.stream_count:
        raise RuntimeError(
            f"expected {cfg.stream_count} pressure cameras, found {len(rows)}"
        )
    shard_ids = _active_evidence_shard_ids(cfg)
    source_ids_by_shard: dict[str, list[str]] = {shard_id: [] for shard_id in shard_ids}
    sources: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        camera = _row_json(row)
        source_id = str(camera["source_id"])
        shard_id = _evidence_shard_for_index(cfg, index=index, shard_ids=shard_ids)
        source_ids_by_shard[shard_id].append(source_id)
        sources[str(camera["id"])] = {
            "camera_id": str(camera["id"]),
            "source_id": source_id,
            "uri": str(camera["rtsp_url"]),
            "enabled": bool(camera.get("enabled", True)),
            "adapter_type": "gstreamer",
            "zmq_endpoint": f"dealer+connect:tcp://{shard_id}:5555",
            "replay_shard_id": shard_id,
            "camera_name": str(camera.get("name") or ""),
        }
    sources_path = cfg.artifact_dir / "sources.dual-shard.generated.yml"
    write_text(
        sources_path,
        yaml.safe_dump({"sources": sources}, sort_keys=False, allow_unicode=True),
    )
    shard_plan = {
        "default_shard_id": "replay-a",
        "mapping_version": f"{cfg.run_id}:evidence-shards-{len(shard_ids)}:v1",
        "shards": [
            {
                "shard_id": shard_id,
                "replay_api_url": f"http://{shard_id}:8080",
                "in_stream_endpoint": f"dealer+connect:tcp://{shard_id}:5555",
                "replay_job_sink_url": (
                    "dealer+connect:tcp://"
                    f"video-file-sink-{remove_prefix(shard_id, 'replay-')}:6666"
                ),
                "source_ids": source_ids_by_shard[shard_id],
            }
            for shard_id in shard_ids
        ],
    }
    shard_plan_path = cfg.artifact_dir / (
        f"replay_shards.evidence-{len(shard_ids)}.pressure.json"
    )
    write_json(shard_plan_path, shard_plan)
    result = {
        "sources_path": str(sources_path),
        "shard_plan_path": str(shard_plan_path),
        "source_mode": cfg.dual_shard_source_mode,
        "evidence_shard_count": len(shard_ids),
        "shards": {
            shard_id: len(source_ids_by_shard[shard_id])
            for shard_id in shard_ids
        },
    }
    write_json(cfg.artifact_dir / "dual_shard_source_plan.json", result)
    return result


def pressure_rtsp_uri(cfg: PressureConfig, *, index: int, source_id: str) -> str:
    if not cfg.rtsp_republish_output_base:
        return cfg.rtsp_uri
    base = cfg.rtsp_republish_output_base.rstrip("/")
    mapping = {
        "run_id": cfg.run_id,
        "index": f"{index:02d}",
        "source_id": source_id,
    }
    if re.search(r"\{(run_id|index|source_id)\}", base):
        return base.format(**mapping)
    return f"{base}/{source_id}"


def docker_network_gateway(network: str) -> str:
    completed = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            str(network),
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    gateway = completed.stdout.strip()
    if completed.returncode != 0 or not gateway:
        raise RuntimeError(
            f"cannot resolve Docker network gateway for {network!r}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return gateway


def pressure_rtsp_server_container_name(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(run_id)).strip("-.")
    digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:8]
    return f"video-analytics-pressure-rtsp-{safe[:40]}-{digest}"


def start_rtsp_republish_local_server(
    cfg: PressureConfig,
) -> dict[str, Any] | None:
    if not cfg.rtsp_republish_local_server:
        return None
    container_name = pressure_rtsp_server_container_name(cfg.run_id)
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--network",
        cfg.rtsp_republish_local_server_network,
        "-p",
        f"{cfg.rtsp_republish_local_server_host_port}:8554",
        cfg.rtsp_republish_local_server_image,
    ]
    completed = run(
        command,
        cfg.artifact_dir / "rtsp_republish_local_server_start.log",
    )
    container_id = completed.stdout.strip()
    try:
        deadline = time.monotonic() + 15
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    (
                        cfg.rtsp_republish_local_server_gateway,
                        cfg.rtsp_republish_local_server_host_port,
                    ),
                    timeout=1,
                ):
                    last_error = ""
                    break
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.25)
        else:
            raise RuntimeError(
                "run-scoped MediaMTX did not become reachable: "
                f"{last_error}"
            )
        image_inspect = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                cfg.rtsp_republish_local_server_image,
                "--format",
                "{{.Id}}",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        summary = {
            "status": "ready",
            "container_name": container_name,
            "container_id": container_id,
            "image": cfg.rtsp_republish_local_server_image,
            "image_id": image_inspect.stdout.strip(),
            "network": cfg.rtsp_republish_local_server_network,
            "gateway": cfg.rtsp_republish_local_server_gateway,
            "host_port": cfg.rtsp_republish_local_server_host_port,
            "output_base": cfg.rtsp_republish_output_base,
            "ready_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json(
            cfg.artifact_dir / "rtsp_republish_local_server.json",
            summary,
        )
        return summary
    except Exception:
        with (cfg.artifact_dir / "rtsp_republish_local_server.log").open(
            "w", encoding="utf-8"
        ) as log_fh:
            subprocess.run(
                ["docker", "logs", "--tail", "10000", container_name],
                check=False,
                text=True,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raise


def stop_rtsp_republish_local_server(
    cfg: PressureConfig,
    server: dict[str, Any],
) -> dict[str, Any]:
    container_name = str(server.get("container_name") or "")
    log_path = cfg.artifact_dir / "rtsp_republish_local_server.log"
    logs = subprocess.run(
        ["docker", "logs", "--tail", "10000", container_name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    write_text(log_path, logs.stdout or "")
    removed = subprocess.run(
        ["docker", "rm", "-f", container_name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    summary = {
        "status": "stopped" if removed.returncode == 0 else "stop_failed",
        "container_name": container_name,
        "returncode": removed.returncode,
        "output": removed.stdout[-1000:],
        "log_path": str(log_path),
        "stopped_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(
        cfg.artifact_dir / "rtsp_republish_local_server_stop.json",
        summary,
    )
    return summary


def start_rtsp_republishers(cfg: PressureConfig) -> list[subprocess.Popen]:
    if not cfg.rtsp_republish_output_base:
        return []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("rtsp republish requested but host ffmpeg is not available")
    log_dir = cfg.artifact_dir / "rtsp_republish"
    log_dir.mkdir(parents=True, exist_ok=True)
    input_identity = rtsp_republish_input_identity(cfg.rtsp_republish_input_uri)
    write_json(
        cfg.artifact_dir / "rtsp_republish_input_identity.json",
        input_identity,
    )
    manifest: list[dict[str, Any]] = []
    processes: list[subprocess.Popen] = []
    for index in range(cfg.stream_count):
        source_id = f"{cfg.run_id}_{index:02d}"
        output_uri = pressure_rtsp_uri(cfg, index=index, source_id=source_id)
        command = rtsp_republish_command(
            ffmpeg=ffmpeg,
            input_uri=cfg.rtsp_republish_input_uri,
            output_uri=output_uri,
            mode=cfg.rtsp_republish_mode,
            input_offset_s=cfg.rtsp_republish_input_offset_s,
            input_loop=cfg.rtsp_republish_input_loop,
            h264_repeat_headers=cfg.rtsp_republish_h264_repeat_headers,
        )
        log_path = log_dir / f"{source_id}.log"
        log_fh = log_path.open("wb")
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_fh.close()
        processes.append(proc)
        manifest.append(
            {
                "source_id": source_id,
                "input_uri": cfg.rtsp_republish_input_uri,
                "input_sha256": input_identity.get("sha256"),
                "input_size_bytes": input_identity.get("size_bytes"),
                "input_offset_s": cfg.rtsp_republish_input_offset_s,
                "input_loop": cfg.rtsp_republish_input_loop,
                "output_uri": output_uri,
                "mode": cfg.rtsp_republish_mode,
                "pid": proc.pid,
                "log_path": str(log_path),
                "command": command,
            }
        )
    write_json(cfg.artifact_dir / "rtsp_republish_manifest.json", manifest)
    if cfg.rtsp_republish_warmup_s > 0:
        time.sleep(cfg.rtsp_republish_warmup_s)
    failed = [
        {"pid": proc.pid, "returncode": proc.poll()}
        for proc in processes
        if proc.poll() is not None
    ]
    if failed:
        write_json(cfg.artifact_dir / "rtsp_republish_start_failures.json", failed)
        stop_rtsp_republishers(processes, cfg)
        raise RuntimeError(f"rtsp republishers exited during warmup: {failed[:5]}")
    try:
        wait_for_rtsp_republish_readiness(cfg, manifest, processes)
    except Exception:
        # The caller cannot receive the local Popen list when this function
        # raises. Stop it here so a failed preflight never leaks publishers.
        stop_rtsp_republishers(processes, cfg)
        raise
    return processes


def probe_rtsp_republish_uri(
    ffprobe: str,
    *,
    source_id: str,
    uri: str,
    timeout_s: int,
) -> dict[str, Any]:
    started = time.monotonic()
    command = [
        ffprobe,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-rw_timeout",
        str(max(1, int(timeout_s)) * 1_000_000),
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate",
        "-of",
        "json",
        uri,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(2, int(timeout_s) + 2),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "source_id": source_id,
            "ok": False,
            "reason": "probe_timeout",
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": str(exc),
        }
    if completed.returncode != 0:
        return {
            "source_id": source_id,
            "ok": False,
            "reason": "ffprobe_failed",
            "returncode": completed.returncode,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": (completed.stderr or completed.stdout or "")[-1000:],
        }
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "source_id": source_id,
            "ok": False,
            "reason": "invalid_probe_json",
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": str(exc),
        }
    streams = payload.get("streams") or []
    if not streams:
        return {
            "source_id": source_id,
            "ok": False,
            "reason": "video_stream_missing",
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    stream = streams[0]
    return {
        "source_id": source_id,
        "ok": True,
        "reason": "readable",
        "elapsed_s": round(time.monotonic() - started, 3),
        "codec_name": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "avg_frame_rate": stream.get("avg_frame_rate"),
    }


def wait_for_rtsp_republish_readiness(
    cfg: PressureConfig,
    manifest: list[dict[str, Any]],
    processes: list[subprocess.Popen],
) -> dict[str, Any]:
    artifact_path = cfg.artifact_dir / "rtsp_republish_readiness.json"
    if not manifest or cfg.rtsp_republish_readiness_timeout_s <= 0:
        summary = {
            "status": "skipped",
            "reason": "disabled_or_no_republishers",
            "expected_count": len(manifest),
        }
        write_json(artifact_path, summary)
        return summary
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("RTSP readiness requested but host ffprobe is unavailable")

    expected = {
        str(item.get("source_id") or ""): str(item.get("output_uri") or "")
        for item in manifest
    }
    pending = set(expected)
    attempts = {source_id: 0 for source_id in expected}
    restart_counts = {source_id: 0 for source_id in expected}
    restart_history: list[dict[str, Any]] = []
    last_results: dict[str, dict[str, Any]] = {}
    snapshots: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + cfg.rtsp_republish_readiness_timeout_s

    while pending:
        exited = [
            {"pid": proc.pid, "returncode": proc.poll()}
            for proc in processes
            if proc.poll() is not None
        ]
        if exited:
            summary = {
                "status": "failed",
                "reason": "republisher_exited_during_readiness",
                "started_at": started_at.isoformat(),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "expected_count": len(expected),
                "ready_count": len(expected) - len(pending),
                "missing_sources": sorted(pending),
                "exited": exited,
                "attempts": attempts,
                "restart_history": restart_history,
                "last_results": last_results,
                "snapshots": snapshots,
            }
            write_json(artifact_path, summary)
            raise RuntimeError(
                f"rtsp republisher exited during readability gate: {exited[:5]}"
            )

        remaining_s = max(1, int(deadline - time.monotonic()))
        probe_timeout_s = min(10, remaining_s)
        probe_ids = sorted(pending)
        with ThreadPoolExecutor(
            max_workers=min(cfg.rtsp_republish_readiness_parallelism, len(probe_ids))
        ) as executor:
            futures = {
                executor.submit(
                    probe_rtsp_republish_uri,
                    ffprobe,
                    source_id=source_id,
                    uri=expected[source_id],
                    timeout_s=probe_timeout_s,
                ): source_id
                for source_id in probe_ids
            }
            for future in as_completed(futures):
                source_id = futures[future]
                attempts[source_id] += 1
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "source_id": source_id,
                        "ok": False,
                        "reason": "probe_exception",
                        "error": repr(exc),
                    }
                result["attempt"] = attempts[source_id]
                last_results[source_id] = result
                if result.get("ok"):
                    pending.discard(source_id)

        snapshots.append(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "ready_count": len(expected) - len(pending),
                "missing_count": len(pending),
                "missing_sources": sorted(pending),
            }
        )
        restart_candidates = [
            source_id
            for source_id in sorted(pending)
            if restart_counts[source_id]
            < cfg.rtsp_republish_readiness_restart_attempts
            and attempts[source_id] >= 2 * (restart_counts[source_id] + 1)
        ]
        if restart_candidates:
            restarted = restart_rtsp_republishers_for_sources(
                cfg,
                manifest,
                processes,
                source_ids=restart_candidates,
            )
            restart_history.extend(restarted)
            for source_id in restart_candidates:
                restart_counts[source_id] += 1
            time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
        if not pending:
            summary = {
                "status": "ready",
                "started_at": started_at.isoformat(),
                "ready_at": datetime.now(timezone.utc).isoformat(),
                "expected_count": len(expected),
                "ready_count": len(expected),
                "parallelism": cfg.rtsp_republish_readiness_parallelism,
                "attempts": attempts,
                "restart_history": restart_history,
                "results": last_results,
                "snapshots": snapshots,
            }
            write_json(artifact_path, summary)
            return summary
        if time.monotonic() >= deadline:
            summary = {
                "status": "failed",
                "reason": "readiness_timeout",
                "started_at": started_at.isoformat(),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "timeout_s": cfg.rtsp_republish_readiness_timeout_s,
                "expected_count": len(expected),
                "ready_count": len(expected) - len(pending),
                "missing_sources": sorted(pending),
                "attempts": attempts,
                "restart_history": restart_history,
                "last_results": last_results,
                "snapshots": snapshots,
            }
            write_json(artifact_path, summary)
            raise RuntimeError(
                "rtsp republish readability gate failed: "
                f"ready={len(expected) - len(pending)}/{len(expected)} "
                f"missing={sorted(pending)}"
            )
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def restart_rtsp_republishers_for_sources(
    cfg: PressureConfig,
    manifest: list[dict[str, Any]],
    processes: list[subprocess.Popen],
    *,
    source_ids: list[str],
) -> list[dict[str, Any]]:
    by_source = {
        str(item.get("source_id") or ""): index
        for index, item in enumerate(manifest)
    }
    restarted: list[dict[str, Any]] = []
    for source_id in source_ids:
        index = by_source[source_id]
        old = processes[index]
        _terminate_rtsp_republisher(old, timeout_s=5)
        item = manifest[index]
        log_path = Path(str(item.get("log_path") or ""))
        with log_path.open("ab") as log_fh:
            proc = subprocess.Popen(
                list(item.get("command") or []),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        processes[index] = proc
        old_pid = int(item.get("pid") or 0)
        item["pid"] = proc.pid
        item["readiness_restart_count"] = int(
            item.get("readiness_restart_count") or 0
        ) + 1
        history_item = {
            "source_id": source_id,
            "old_pid": old_pid,
            "old_returncode": old.poll(),
            "new_pid": proc.pid,
            "restarted_at": datetime.now(timezone.utc).isoformat(),
        }
        item.setdefault("readiness_restart_history", []).append(history_item)
        restarted.append(history_item)
    write_json(cfg.artifact_dir / "rtsp_republish_manifest.json", manifest)
    return restarted


def rtsp_republish_input_identity(input_uri: str) -> dict[str, Any]:
    path = Path(str(input_uri or ""))
    if not path.is_file():
        return {
            "input_uri": str(input_uri or ""),
            "kind": "non_local_uri",
            "sha256": None,
            "size_bytes": None,
        }
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "input_uri": str(input_uri),
        "resolved_path": str(path.resolve()),
        "kind": "local_file",
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def rtsp_republish_command(
    *,
    ffmpeg: str,
    input_uri: str,
    output_uri: str,
    mode: str,
    input_offset_s: float = 0.0,
    input_loop: bool = False,
    h264_repeat_headers: bool = False,
) -> list[str]:
    input_is_local_file = "://" not in input_uri or input_uri.startswith("file://")
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts",
    ]
    if not input_is_local_file:
        command.extend(["-use_wallclock_as_timestamps", "1"])
    if input_uri.startswith(("rtsp://", "rtsps://")):
        command.extend(["-rtsp_transport", "tcp"])
    if input_loop:
        command.extend(["-stream_loop", "-1"])
    if input_offset_s > 0:
        command.extend(["-ss", _format_seconds(input_offset_s)])
    command.extend(["-re", "-i", input_uri, "-map", "0:v:0", "-an"])
    if mode == "transcode":
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "8",
                "-keyint_min",
                "8",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    else:
        command.extend(["-c:v", "copy"])
        if h264_repeat_headers:
            command.extend(
                [
                    "-bsf:v",
                    "h264_mp4toannexb,dump_extra=freq=keyframe",
                ]
            )
    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            output_uri,
        ]
    )
    return command


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _terminate_rtsp_republisher(
    proc: subprocess.Popen,
    *,
    timeout_s: float,
) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=max(0.1, timeout_s))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def stop_rtsp_republishers(processes: list[subprocess.Popen], cfg: PressureConfig) -> None:
    if not processes:
        return
    stopped: list[dict[str, Any]] = []
    for proc in processes:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
        proc.poll() is None for proc in processes
    ):
        time.sleep(0.1)
    for proc in processes:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        stopped.append({"pid": proc.pid, "returncode": proc.poll()})
    write_json(cfg.artifact_dir / "rtsp_republish_stop.json", stopped)


def restart_camera_runtime(cfg: PressureConfig) -> dict[str, Any]:
    response = api_json(
        cfg.api_base,
        "POST",
        f"/cameras/runtime/restart?include_disabled=true&force={str(cfg.force_runtime_restart).lower()}",
        timeout_s=360,
    )
    if response.get("error"):
        raise RuntimeError(f"runtime restart failed: {response['error']}")
    return response["data"]


def current_runtime_epoch_id() -> str:
    state_path = Path("/data/video-analytics/media/replay-sink-output/midterm/.current_epoch.json")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str((payload or {}).get("runtime_epoch_id") or "")


def sample_runtime(
    cfg: PressureConfig,
    started_at: datetime,
    *,
    pressure_started_monotonic: float | None = None,
) -> dict[str, Any]:
    samples_dir = cfg.artifact_dir / "samples"
    samples_dir.mkdir(exist_ok=True)
    sampling_started_wall = datetime.now(timezone.utc)
    end_at = (pressure_started_monotonic or time.time()) + cfg.duration_s
    index = 0
    while True:
        now = time.time()
        if cfg.dual_shard_same_gpu:
            overview = dual_shard_runtime_overview(cfg)
        else:
            overview = api_json(cfg.api_base, "GET", "/runtime/overview", timeout_s=10)["data"]
        if isinstance(overview, dict):
            overview["_pressure_sample_observed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(samples_dir / f"runtime_{index:03d}.json", overview)
        write_text(samples_dir / f"gpu_{index:03d}.csv", nvidia_smi_csv())
        if cfg.cuda_mps:
            write_json(samples_dir / f"mps_{index:03d}.json", cuda_mps_status(cfg))
        write_json(samples_dir / f"docker_stats_{index:03d}.json", docker_stats_json(cfg))
        write_json(samples_dir / f"db_{index:03d}.json", db_summary_connect(cfg))
        if now >= end_at:
            break
        index += 1
        time.sleep(max(1, min(cfg.sample_interval_s, end_at - now)))
    capture_runtime_logs_since_start(cfg, started_at)
    summary = {
        "status": "completed",
        "duration_s": cfg.duration_s,
        "sample_count": index + 1,
        "started_at": sampling_started_wall.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(cfg.artifact_dir / "pressure_sampling_window.json", summary)
    return summary


def pressure_runtime_overview(cfg: PressureConfig) -> dict[str, Any]:
    if cfg.dual_shard_same_gpu:
        return dual_shard_runtime_overview(cfg)
    return api_json(cfg.api_base, "GET", "/runtime/overview", timeout_s=10)["data"]


def pressure_source_visibility_snapshot(cfg: PressureConfig) -> dict[str, Any]:
    expected = set(pressure_source_ids(cfg))
    try:
        overview = pressure_runtime_overview(cfg)
    except Exception as exc:
        return {
            "status": "overview_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "expected_count": len(expected),
            "forwarder_visible_count": 0,
            "savant_visible_count": 0 if not cfg.forwarder_null_sink else len(expected),
            "missing_forwarder_sources": sorted(expected),
            "missing_savant_sources": [] if cfg.forwarder_null_sink else sorted(expected),
        }

    forwarder_sources = _dedupe_sources_by_id(
        ((overview.get("forwarder") or {}).get("sources") or []),
        score_keys=(
            "frames_seen_total",
            "frames_forwarded_total",
            "frames_dropped_total",
            "savant_send_failures_total",
        ),
    )
    savant_sources = _dedupe_sources_by_id(
        ((overview.get("metrics") or {}).get("sources") or []),
        score_keys=(
            "frames_seen_total",
            "pose_objects_total",
            "face_objects_total",
            "person_observations_exported_total",
            "face_observations_exported_total",
        ),
    )
    adaface_forwarder_sources = _dedupe_sources_by_id(
        ((overview.get("adaface_forwarder") or {}).get("sources") or []),
        score_keys=(
            "frames_seen_total",
            "frames_forwarded_total",
            "metadata_filtered_total",
            "savant_send_failures_total",
        ),
    )
    adaface_central_sources = _dedupe_sources_by_id(
        ((overview.get("adaface_central") or {}).get("sources") or []),
        score_keys=(
            "frames_seen_total",
            "adaface_embeddings_total",
            "face_observations_exported_total",
        ),
    )
    forwarder_visible = _metric_visible_source_ids(
        forwarder_sources,
        expected,
        "frames_seen_total",
    )
    if cfg.forwarder_null_sink:
        savant_visible = set(expected)
    else:
        savant_visible = _metric_visible_source_ids(
            savant_sources,
            expected,
            "frames_seen_total",
        )
    missing_forwarder = sorted(expected - forwarder_visible)
    missing_savant = sorted(expected - savant_visible)
    adaface_forwarder_visible = (
        _metric_visible_source_ids(
            adaface_forwarder_sources,
            expected,
            "frames_seen_total",
        )
        if cfg.adaface_decoupled
        else set()
    )
    adaface_eligible = (
        _metric_visible_source_ids(
            adaface_forwarder_sources,
            expected,
            "frames_forwarded_total",
        )
        if cfg.adaface_decoupled
        else set()
    )
    adaface_central_visible = (
        _metric_visible_source_ids(
            adaface_central_sources,
            expected,
            "frames_seen_total",
        )
        if cfg.adaface_decoupled
        else set()
    )
    missing_adaface_forwarder = (
        sorted(expected - adaface_forwarder_visible)
        if cfg.adaface_decoupled
        else []
    )
    missing_adaface_central_eligible = (
        sorted(adaface_eligible - adaface_central_visible)
        if cfg.adaface_decoupled
        else []
    )
    status = (
        "all_visible"
        if not missing_forwarder
        and not missing_savant
        and not missing_adaface_forwarder
        and not missing_adaface_central_eligible
        else "waiting"
    )
    return {
        "status": status,
        "expected_count": len(expected),
        "forwarder_visible_count": len(forwarder_visible),
        "savant_visible_count": len(savant_visible),
        "missing_forwarder_sources": missing_forwarder,
        "missing_savant_sources": missing_savant,
        "adaface_forwarder_visible_count": len(adaface_forwarder_visible),
        "adaface_eligible_count": len(adaface_eligible),
        "adaface_central_visible_count": len(adaface_central_visible),
        "missing_adaface_forwarder_sources": missing_adaface_forwarder,
        "missing_adaface_central_eligible_sources": (
            missing_adaface_central_eligible
        ),
        "forwarder_visible_sources": sorted(forwarder_visible),
        "savant_visible_sources": sorted(savant_visible),
        "forwarder_total_sources": len(forwarder_sources),
        "savant_total_sources": len(savant_sources),
        "forwarder_queue_depth": (
            (overview.get("forwarder") or {}).get("global") or {}
        ).get("queue_depth"),
    }


def _metric_visible_source_ids(
    sources: list[dict[str, Any]],
    expected: set[str],
    metric_name: str,
) -> set[str]:
    visible: set[str] = set()
    for source in sources:
        source_id = str(source.get("source_id") or "")
        if source_id not in expected:
            continue
        try:
            value = float(source.get(metric_name) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            visible.add(source_id)
    return visible


def wait_for_pressure_source_visibility(
    cfg: PressureConfig,
    *,
    sources_path: Path | None = None,
) -> dict[str, Any]:
    if cfg.stream_count <= 0 or cfg.pressure_source_visibility_timeout_s <= 0:
        summary = {
            "status": "skipped",
            "reason": "disabled",
            "timeout_s": cfg.pressure_source_visibility_timeout_s,
        }
        write_json(cfg.artifact_dir / "pressure_source_visibility_ready.json", summary)
        return summary

    started_at = datetime.now(timezone.utc)
    deadline = time.time() + cfg.pressure_source_visibility_timeout_s
    stable_samples = 0
    restart_attempts = 0
    snapshots: list[dict[str, Any]] = []
    restart_history: list[dict[str, Any]] = []
    last_snapshot: dict[str, Any] = {}

    while True:
        snapshot = pressure_source_visibility_snapshot(cfg)
        snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
        snapshot["stable_samples"] = stable_samples
        snapshots.append(snapshot)
        if len(snapshots) > 200:
            snapshots = snapshots[-200:]
        last_snapshot = snapshot

        if snapshot.get("status") == "all_visible":
            stable_samples += 1
            if stable_samples >= cfg.pressure_source_visibility_stable_samples:
                summary = {
                    "status": "ready",
                    "started_at": started_at.isoformat(),
                    "ready_at": datetime.now(timezone.utc).isoformat(),
                    "timeout_s": cfg.pressure_source_visibility_timeout_s,
                    "poll_s": cfg.pressure_source_visibility_poll_s,
                    "stable_samples_required": (
                        cfg.pressure_source_visibility_stable_samples
                    ),
                    "stable_samples": stable_samples,
                    "restart_attempts": restart_attempts,
                    "restart_history": restart_history,
                    "last_snapshot": snapshot,
                }
                write_json(
                    cfg.artifact_dir / "pressure_source_visibility_ready.json",
                    summary,
                )
                write_json(
                    cfg.artifact_dir / "pressure_source_visibility_snapshots.json",
                    snapshots,
                )
                return summary
        else:
            stable_samples = 0

        now = time.time()
        if now >= deadline:
            # Only restart adapters that the forwarder has not seen at all.
            # A source can be forwarder-visible while Savant has not yet emitted
            # per-source metrics under load; restarting that live adapter creates
            # a stream discontinuity and can make the Savant pipeline unstable.
            missing = sorted(set(snapshot.get("missing_forwarder_sources") or []))
            if (
                missing
                and sources_path is not None
                and restart_attempts < cfg.pressure_source_visibility_restart_attempts
            ):
                restart_attempts += 1
                restart_history.append(
                    restart_pressure_source_ids_from_manifest(
                        cfg,
                        sources_path=sources_path,
                        source_ids=missing,
                        attempt=restart_attempts,
                    )
                )
                deadline = time.time() + cfg.pressure_source_visibility_timeout_s
                stable_samples = 0
                continue

            summary = {
                "status": "failed",
                "started_at": started_at.isoformat(),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "timeout_s": cfg.pressure_source_visibility_timeout_s,
                "poll_s": cfg.pressure_source_visibility_poll_s,
                "stable_samples_required": (
                    cfg.pressure_source_visibility_stable_samples
                ),
                "stable_samples": stable_samples,
                "restart_attempts": restart_attempts,
                "restart_history": restart_history,
                "last_snapshot": last_snapshot,
                "source_containers": inspect_pressure_source_containers(cfg.run_id),
            }
            write_json(
                cfg.artifact_dir / "pressure_source_visibility_ready.json",
                summary,
            )
            write_json(
                cfg.artifact_dir / "pressure_source_visibility_snapshots.json",
                snapshots,
            )
            raise RuntimeError(
                "pressure source visibility barrier failed: "
                f"missing_forwarder={last_snapshot.get('missing_forwarder_sources') or []} "
                f"missing_savant={last_snapshot.get('missing_savant_sources') or []}"
            )

        time.sleep(
            max(
                1,
                min(cfg.pressure_source_visibility_poll_s, max(deadline - now, 1)),
            )
        )


def prepare_pressure_sampling_window(
    conn,
    cfg: PressureConfig,
    *,
    pressure_started_monotonic: float | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    if cfg.rolling_cache_evidence and cfg.rolling_cache_prefill_s > 0:
        time.sleep(cfg.rolling_cache_prefill_s)
    # Visibility and rolling-cache prefill happen before the measured window.
    # Production-facing runs retain those visual results and fence formal
    # queries by the sampling start. The destructive legacy isolation remains
    # available only when preserve_warmup_results is not requested.
    cleanup = (
        pressure_warmup_rows_snapshot(conn, cfg)
        if cfg.preserve_warmup_results
        else clear_pressure_warmup_rows(conn, cfg)
    )
    sampling_started_monotonic = time.time()
    sampling_started_at = datetime.now(timezone.utc)
    summary = {
        "status": "completed",
        "prefill_s": (
            cfg.rolling_cache_prefill_s if cfg.rolling_cache_evidence else 0
        ),
        "started_at": started_at.isoformat(),
        "sampling_started_at": sampling_started_at.isoformat(),
        "sampling_start_event_ts_ms": int(sampling_started_at.timestamp() * 1000),
        "warmup_results_preserved": bool(cfg.preserve_warmup_results),
        "cleanup": cleanup,
    }
    write_json(cfg.artifact_dir / "rolling_cache_prefill_summary.json", summary)
    return {
        **summary,
        "sampling_started_monotonic": sampling_started_monotonic,
    }


def pressure_warmup_rows_snapshot(conn, cfg: PressureConfig) -> dict[str, Any]:
    source_ids = pressure_source_ids(cfg)
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM events
             WHERE source_id = ANY(%(source_ids)s)) AS events,
          (SELECT count(*) FROM evidence_tasks
             WHERE source_id = ANY(%(source_ids)s)) AS evidence_tasks,
          (SELECT count(*) FROM evidence_bundles
             WHERE source_id = ANY(%(source_ids)s)) AS evidence_bundles,
          (SELECT count(*) FROM face_observations
             WHERE source_id = ANY(%(source_ids)s)) AS face_observations,
          (SELECT count(*) FROM person_bbox_observations
             WHERE source_id = ANY(%(source_ids)s)) AS person_observations
        """,
        {"source_ids": source_ids},
    ).fetchone() or {}
    return {
        "status": "preserved",
        "event_rows_deleted": 0,
        "face_observation_rows_deleted": 0,
        "person_observation_rows_deleted": 0,
        "evidence_dirs_removed": 0,
        "event_rows_preserved": int(row.get("events") or 0),
        "evidence_task_rows_preserved": int(row.get("evidence_tasks") or 0),
        "evidence_bundle_rows_preserved": int(row.get("evidence_bundles") or 0),
        "face_observation_rows_preserved": int(row.get("face_observations") or 0),
        "person_observation_rows_preserved": int(
            row.get("person_observations") or 0
        ),
    }


def _formal_pressure_event_predicate(
    alias: str,
    *,
    bounded_end: bool = False,
) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError(f"invalid SQL alias: {alias!r}")
    # Epoch-ms event timestamps are authoritative. Stream-relative or missing
    # timestamps fall back to the DB creation clock so delayed warmup inserts
    # still cannot enter the measured result set.
    start_predicate = f"""
      (
        %(sampling_start_event_ts_ms)s <= 0
        OR (
          {alias}.event_ts_ms >= 946684800000
          AND {alias}.event_ts_ms >= %(sampling_start_event_ts_ms)s
        )
        OR (
          COALESCE({alias}.event_ts_ms, 0) < 946684800000
          AND {alias}.created_at >= to_timestamp(
            %(sampling_start_event_ts_ms)s / 1000.0
          )
        )
      )
    """
    if not bounded_end:
        return start_predicate
    # A retained postfill bundle remains visible to operators, but events whose
    # authoritative event clock is beyond the sampling cutoff must not extend
    # formal pressure-window statistics.  Non-epoch timestamps use the same DB
    # clock fallback as the lower fence.
    return f"""
      ({start_predicate})
      AND (
        %(sampling_end_event_ts_ms)s <= 0
        OR (
          {alias}.event_ts_ms >= 946684800000
          AND {alias}.event_ts_ms <= %(sampling_end_event_ts_ms)s
        )
        OR (
          COALESCE({alias}.event_ts_ms, 0) < 946684800000
          AND {alias}.created_at <= to_timestamp(
            %(sampling_end_event_ts_ms)s / 1000.0
          )
        )
      )
    """


def remove_tree_best_effort(path: Path) -> tuple[bool, str | None]:
    if not path.exists():
        return False, None
    try:
        shutil.rmtree(path)
        return True, None
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        removed, container_error = remove_host_path_via_container(path)
        if removed:
            return True, None
        detail = f"{type(exc).__name__}: {exc}"
        if container_error:
            detail = f"{detail}; container_fallback={container_error}"
        return False, detail


def clear_pressure_warmup_rows(conn, cfg: PressureConfig) -> dict[str, Any]:
    source_ids = pressure_source_ids(cfg)
    event_rows = conn.execute(
        "SELECT id::text FROM events WHERE source_id = ANY(%(source_ids)s)",
        {"source_ids": source_ids},
    ).fetchall()
    event_ids = [str(row["id"]) for row in event_rows]
    removed_dirs = 0
    remove_failures: list[dict[str, str]] = []
    for event_id in event_ids:
        target = cfg.evidence_root / event_id
        removed, error = remove_tree_best_effort(target)
        if removed:
            removed_dirs += 1
        if error:
            remove_failures.append({"event_id": event_id, "error": error})
    preserved = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM face_observations
             WHERE source_id = ANY(%(source_ids)s)) AS face_observations,
          (SELECT count(*) FROM person_bbox_observations
             WHERE source_id = ANY(%(source_ids)s)) AS person_observations
        """,
        {"source_ids": source_ids},
    ).fetchone() or {}
    with conn.transaction():
        event_deleted = conn.execute(
            "DELETE FROM events WHERE source_id = ANY(%(source_ids)s)",
            {"source_ids": source_ids},
        ).rowcount
    return {
        "event_rows_deleted": event_deleted or 0,
        "face_observation_rows_deleted": 0,
        "person_observation_rows_deleted": 0,
        "face_observation_rows_preserved": int(
            preserved.get("face_observations") or 0
        ),
        "person_observation_rows_preserved": int(
            preserved.get("person_observations") or 0
        ),
        "evidence_dirs_removed": removed_dirs,
        "evidence_dir_remove_failures": len(remove_failures),
        "evidence_dir_remove_failure_samples": remove_failures[:20],
    }


def pressure_event_sampling_cutoff(conn, cfg: PressureConfig) -> dict[str, Any]:
    cutoff_created_at = datetime.now(timezone.utc)
    source_ids = pressure_source_ids(cfg)
    row = conn.execute(
        f"""
        SELECT count(*) AS event_count,
               max(event_ts_ms) AS max_event_ts_ms,
               max(created_at) AS max_created_at
        FROM events e
        WHERE source_id = ANY(%(source_ids)s)
          AND {_formal_pressure_event_predicate("e")}
        """,
        {
            "source_ids": source_ids,
            "sampling_start_event_ts_ms": cfg.pressure_sampling_start_event_ts_ms,
        },
    ).fetchone() or {}
    summary = {
        "status": "captured",
        "created_at_cutoff": cutoff_created_at.isoformat(),
        "event_count_at_cutoff": int(row.get("event_count") or 0),
        "max_event_ts_ms_at_cutoff": _safe_int(row.get("max_event_ts_ms")),
        "max_created_at_at_cutoff": (
            row.get("max_created_at").isoformat()
            if row.get("max_created_at") is not None
            else None
        ),
    }
    write_json(cfg.artifact_dir / "pressure_sampling_cutoff.json", summary)
    return summary


def rolling_cache_postfill_after_sampling(cfg: PressureConfig) -> dict[str, Any]:
    rolling_cache_postfill_s = (
        cfg.rolling_cache_postfill_s if cfg.rolling_cache_evidence else 0
    )
    adaface_visibility_grace_s = (
        5 if (cfg.adaface_decoupled or cfg.adaface_roi_redis) else 0
    )
    postfill_s = max(rolling_cache_postfill_s, adaface_visibility_grace_s)
    summary = {
        "status": "skipped",
        "postfill_s": postfill_s,
        "rolling_cache_postfill_s": rolling_cache_postfill_s,
        "adaface_visibility_grace_s": adaface_visibility_grace_s,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
    }
    if postfill_s > 0:
        summary["status"] = "completed"
        time.sleep(postfill_s)
    if cfg.adaface_decoupled:
        summary["adaface_visibility"] = pressure_source_visibility_snapshot(cfg)
    if cfg.adaface_roi_redis:
        summary["adaface_roi_metrics"] = collect_adaface_roi_worker_metrics(cfg)
    summary["ended_at"] = datetime.now(timezone.utc).isoformat()
    write_json(cfg.artifact_dir / "rolling_cache_postfill_summary.json", summary)
    return summary


def clear_pressure_post_sample_rows(
    conn,
    cfg: PressureConfig,
    cutoff: dict[str, Any],
) -> dict[str, Any]:
    cutoff_raw = str(cutoff.get("created_at_cutoff") or "")
    if not cutoff_raw:
        return {"status": "skipped", "reason": "missing_created_at_cutoff"}
    cutoff_created_at = datetime.fromisoformat(cutoff_raw.replace("Z", "+00:00"))
    source_ids = pressure_source_ids(cfg)
    event_rows = conn.execute(
        """
        SELECT id::text
        FROM events
        WHERE source_id = ANY(%(source_ids)s)
          AND created_at > %(cutoff_created_at)s
        """,
        {"source_ids": source_ids, "cutoff_created_at": cutoff_created_at},
    ).fetchall()
    event_ids = [str(row["id"]) for row in event_rows]
    removed_dirs = 0
    remove_failures: list[dict[str, str]] = []
    for event_id in event_ids:
        target = cfg.evidence_root / event_id
        removed, error = remove_tree_best_effort(target)
        if removed:
            removed_dirs += 1
        if error:
            remove_failures.append({"event_id": event_id, "error": error})
    with conn.transaction():
        face_deleted = conn.execute(
            """
            DELETE FROM face_observations
            WHERE source_id = ANY(%(source_ids)s)
              AND created_at > %(cutoff_created_at)s
            """,
            {"source_ids": source_ids, "cutoff_created_at": cutoff_created_at},
        ).rowcount
        person_deleted = conn.execute(
            """
            DELETE FROM person_bbox_observations
            WHERE source_id = ANY(%(source_ids)s)
              AND created_at > %(cutoff_created_at)s
            """,
            {"source_ids": source_ids, "cutoff_created_at": cutoff_created_at},
        ).rowcount
        event_deleted = conn.execute(
            """
            DELETE FROM events
            WHERE source_id = ANY(%(source_ids)s)
              AND created_at > %(cutoff_created_at)s
            """,
            {"source_ids": source_ids, "cutoff_created_at": cutoff_created_at},
        ).rowcount
    post_cleanup_db_ingest = db_pressure_event_ingest_summary(
        conn,
        cfg.run_id,
        cooldown_s=cfg.pressure_algorithm_cooldown_s,
        sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
        sampling_end_event_ts_ms=int(cutoff_created_at.timestamp() * 1000),
    )
    summary = {
        "status": "completed",
        "created_at_cutoff": cutoff_raw,
        "event_rows_deleted": event_deleted or 0,
        "face_observation_rows_deleted": face_deleted or 0,
        "person_observation_rows_deleted": person_deleted or 0,
        "evidence_dirs_removed": removed_dirs,
        "evidence_dir_remove_failures": len(remove_failures),
        "evidence_dir_remove_failure_samples": remove_failures[:20],
        "post_cleanup_db_ingest": post_cleanup_db_ingest,
    }
    write_json(cfg.artifact_dir / "pressure_post_sample_cleanup.json", summary)
    return summary


def prune_pressure_post_sample_nonplayable_rows(
    conn,
    cfg: PressureConfig,
    cutoff: dict[str, Any],
) -> dict[str, Any]:
    """Prune only postfill tasks that never produced or linked to a bundle."""
    cutoff_raw = str(cutoff.get("created_at_cutoff") or "")
    if not cutoff_raw:
        return {"status": "skipped", "reason": "missing_created_at_cutoff"}
    cutoff_created_at = datetime.fromisoformat(cutoff_raw.replace("Z", "+00:00"))
    cutoff_event_ts_ms = int(cutoff_created_at.timestamp() * 1000)
    source_ids = pressure_source_ids(cfg)
    params = {
        "source_ids": source_ids,
        "cutoff_created_at": cutoff_created_at,
        "cutoff_event_ts_ms": cutoff_event_ts_ms,
    }
    rows = conn.execute(
        """
        SELECT DISTINCT e.id::text
        FROM events e
        JOIN evidence_tasks et ON et.event_id = e.id
        LEFT JOIN evidence_bundles direct_bundle ON direct_bundle.event_id = e.id
        LEFT JOIN evidence_event_links eel ON eel.event_id = e.id
        LEFT JOIN evidence_bundles linked_bundle
          ON linked_bundle.event_id = eel.bundle_event_id
        WHERE e.source_id = ANY(%(source_ids)s)
          AND (
            (e.event_ts_ms > 0 AND e.event_ts_ms > %(cutoff_event_ts_ms)s)
            OR (e.event_ts_ms <= 0 AND e.created_at > %(cutoff_created_at)s)
          )
          AND direct_bundle.event_id IS NULL
          AND linked_bundle.event_id IS NULL
          AND COALESCE(et.materialization_status, '') <> 'materialized'
        ORDER BY e.id::text
        """,
        params,
    ).fetchall()
    event_ids = [str(row["id"]) for row in rows]
    removed_dirs = 0
    remove_failures: list[dict[str, str]] = []
    for event_id in event_ids:
        removed, error = remove_tree_best_effort(cfg.evidence_root / event_id)
        if removed:
            removed_dirs += 1
        if error:
            remove_failures.append({"event_id": event_id, "error": error})

    deleted = 0
    if event_ids:
        with conn.transaction():
            deleted = conn.execute(
                "DELETE FROM events WHERE id = ANY(%s::uuid[])",
                (event_ids,),
            ).rowcount or 0

    counts = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM events
             WHERE source_id = ANY(%(source_ids)s)
               AND (
                 (event_ts_ms > 0 AND event_ts_ms > %(cutoff_event_ts_ms)s)
                 OR (event_ts_ms <= 0 AND created_at > %(cutoff_created_at)s)
               )) AS events,
          (SELECT count(*)
             FROM evidence_bundles eb
             JOIN events e ON e.id = eb.event_id
            WHERE e.source_id = ANY(%(source_ids)s)
              AND (
                (e.event_ts_ms > 0 AND e.event_ts_ms > %(cutoff_event_ts_ms)s)
                OR (e.event_ts_ms <= 0 AND e.created_at > %(cutoff_created_at)s)
              )) AS playable_bundles,
          (SELECT count(*) FROM face_observations
             WHERE source_id = ANY(%(source_ids)s)
               AND timestamp_ms > %(cutoff_event_ts_ms)s) AS face_observations,
          (SELECT count(*) FROM person_bbox_observations
             WHERE source_id = ANY(%(source_ids)s)
               AND timestamp_ms > %(cutoff_event_ts_ms)s) AS person_observations
        """,
        params,
    ).fetchone() or {}
    summary = {
        "status": "completed",
        "reason": "postfill_nonplayable_tasks_pruned",
        "created_at_cutoff": cutoff_raw,
        "event_ts_ms_cutoff": cutoff_event_ts_ms,
        "nonplayable_event_candidates": len(event_ids),
        "event_rows_deleted": deleted,
        "event_rows_preserved": int(counts.get("events") or 0),
        "playable_bundle_rows_preserved": int(counts.get("playable_bundles") or 0),
        "face_observation_rows_preserved": int(counts.get("face_observations") or 0),
        "person_observation_rows_preserved": int(counts.get("person_observations") or 0),
        "face_observation_rows_deleted": 0,
        "person_observation_rows_deleted": 0,
        "evidence_dirs_removed": removed_dirs,
        "evidence_dir_remove_failures": len(remove_failures),
        "evidence_dir_remove_failure_samples": remove_failures[:20],
        "post_cleanup_db_ingest": db_pressure_event_ingest_summary(
            conn,
            cfg.run_id,
            cooldown_s=cfg.pressure_algorithm_cooldown_s,
            sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
            sampling_end_event_ts_ms=cutoff_event_ts_ms,
        ),
    }
    write_json(cfg.artifact_dir / "pressure_post_sample_cleanup.json", summary)
    return summary


def capture_runtime_logs_since_start(cfg: PressureConfig, started_at: datetime) -> None:
    since = started_at.isoformat().replace("+00:00", "Z")
    if cfg.dual_shard_same_gpu:
        write_combined_docker_logs(
            ["video-analytics-midterm-savant-a", "video-analytics-midterm-savant-b"],
            cfg.artifact_dir / "savant_logs_since_start.txt",
            since=since,
        )
        if cfg.adaface_decoupled:
            write_combined_docker_logs(
                [
                    f"video-analytics-midterm-{service}"
                    for service in adaface_central_service_names(cfg)
                ],
                cfg.artifact_dir / "adaface_central_logs_since_start.txt",
                since=since,
            )
        if cfg.adaface_roi_redis:
            run(
                ["docker", "logs", "--since", since, ADAFACE_ROI_WORKER_CONTAINER],
                cfg.artifact_dir / "adaface_roi_worker_logs_since_start.txt",
                check=False,
            )
            write_combined_docker_logs(
                [
                    "video-analytics-midterm-adaface-forwarder-a",
                    "video-analytics-midterm-adaface-forwarder-b",
                ],
                cfg.artifact_dir / "adaface_forwarder_logs_since_start.txt",
                since=since,
            )
        write_combined_docker_logs(
            [
                "video-analytics-midterm-analysis-forwarder-a",
                "video-analytics-midterm-analysis-forwarder-b",
            ],
            cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt",
            since=since,
        )
        write_combined_docker_logs(
            [
                "video-analytics-midterm-replay-raw-fanout-a",
                "video-analytics-midterm-replay-raw-fanout-b",
            ],
            cfg.artifact_dir / "replay_raw_fanout_logs_since_start.txt",
            since=since,
        )
    else:
        run(["docker", "logs", "--since", since, "video-analytics-midterm-savant"], cfg.artifact_dir / "savant_logs_since_start.txt", check=False)
        run(["docker", "logs", "--since", since, "video-analytics-midterm-analysis-forwarder"], cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-media-worker"], cfg.artifact_dir / "media_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-clip-worker"], cfg.artifact_dir / "clip_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-event-worker"], cfg.artifact_dir / "event_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-face-worker"], cfg.artifact_dir / "face_worker_logs_since_start.txt", check=False)
    if cfg.dual_shard_same_gpu:
        run(
            ["docker", "logs", "--since", since, "video-analytics-midterm-rolling-cache-sink-a"],
            cfg.artifact_dir / "rolling_cache_sink_a_logs_since_start.txt",
            check=False,
        )
        run(
            ["docker", "logs", "--since", since, "video-analytics-midterm-rolling-cache-sink-b"],
            cfg.artifact_dir / "rolling_cache_sink_b_logs_since_start.txt",
            check=False,
        )
    else:
        run(
            ["docker", "logs", "--since", since, "video-analytics-midterm-rolling-cache-sink"],
            cfg.artifact_dir / "rolling_cache_sink_logs_since_start.txt",
            check=False,
        )
    for sink_instance, container_name in VIDEO_FILE_SINK_CONTAINERS.items():
        run(
            ["docker", "logs", "--since", since, container_name],
            cfg.artifact_dir / VIDEO_FILE_SINK_LOG_PATHS[sink_instance],
            check=False,
        )


def start_pressure_sources_from_manifest(cfg: PressureConfig, *, sources_path: Path) -> None:
    remove_pressure_source_containers(cfg.run_id)
    entries = start_pressure_source_ids_from_manifest(
        cfg,
        sources_path=sources_path,
        source_ids=pressure_source_ids(cfg),
        log_name="source_controller_start_dual_shard",
    )
    write_json(cfg.artifact_dir / "source_controller_start_dual_shard.json", entries)


def start_pressure_source_ids_from_manifest(
    cfg: PressureConfig,
    *,
    sources_path: Path,
    source_ids: list[str],
    log_name: str,
) -> list[dict[str, Any]]:
    log_path = cfg.artifact_dir / "source_controller_start_dual_shard.log"
    if log_name != "source_controller_start_dual_shard":
        log_path = cfg.artifact_dir / f"{log_name}.log"
    entries: list[dict[str, Any]] = []
    with log_path.open("w", encoding="utf-8") as log_fh:
        for index, source_id in enumerate(source_ids):
            cmd = [
                sys.executable,
                str(SOURCE_CONTROLLER),
                "start",
                "--sources",
                str(sources_path),
                "--source-id",
                source_id,
                "--ffmpeg-timeout-ms",
                str(cfg.pressure_source_ffmpeg_timeout_ms),
                "--ffmpeg-init-timeout-ms",
                str(cfg.pressure_source_ffmpeg_init_timeout_ms),
                "--ffmpeg-sitecustomize",
                str(SOURCE_ADAPTER_SITECUSTOMIZE),
            ]
            completed = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log_fh.write(f"$ {' '.join(cmd)}\n")
            log_fh.write(completed.stdout)
            if completed.stdout and not completed.stdout.endswith("\n"):
                log_fh.write("\n")
            entries.append(
                {
                    "source_id": source_id,
                    "returncode": completed.returncode,
                    "output_tail": completed.stdout[-1000:],
                }
            )
            if completed.returncode != 0:
                raise RuntimeError(f"failed to start pressure source {source_id}")
            if cfg.pressure_source_start_stagger_s > 0 and index + 1 < len(source_ids):
                time.sleep(cfg.pressure_source_start_stagger_s)
    return entries


def restart_pressure_source_ids_from_manifest(
    cfg: PressureConfig,
    *,
    sources_path: Path,
    source_ids: list[str],
    attempt: int,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    log_name = f"source_controller_visibility_restart_{attempt}"
    log_path = cfg.artifact_dir / f"{log_name}.log"
    with log_path.open("w", encoding="utf-8") as log_fh:
        for source_id in source_ids:
            stop_cmd = [
                sys.executable,
                str(SOURCE_CONTROLLER),
                "stop",
                "--sources",
                str(sources_path),
                "--source-id",
                source_id,
                "--ffmpeg-timeout-ms",
                str(cfg.pressure_source_ffmpeg_timeout_ms),
                "--ffmpeg-init-timeout-ms",
                str(cfg.pressure_source_ffmpeg_init_timeout_ms),
                "--ffmpeg-sitecustomize",
                str(SOURCE_ADAPTER_SITECUSTOMIZE),
            ]
            stop = subprocess.run(
                stop_cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            start_cmd = [
                sys.executable,
                str(SOURCE_CONTROLLER),
                "start",
                "--sources",
                str(sources_path),
                "--source-id",
                source_id,
                "--ffmpeg-timeout-ms",
                str(cfg.pressure_source_ffmpeg_timeout_ms),
                "--ffmpeg-init-timeout-ms",
                str(cfg.pressure_source_ffmpeg_init_timeout_ms),
                "--ffmpeg-sitecustomize",
                str(SOURCE_ADAPTER_SITECUSTOMIZE),
            ]
            start = subprocess.run(
                start_cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log_fh.write(f"$ {' '.join(stop_cmd)}\n{stop.stdout}")
            if stop.stdout and not stop.stdout.endswith("\n"):
                log_fh.write("\n")
            log_fh.write(f"$ {' '.join(start_cmd)}\n{start.stdout}")
            if start.stdout and not start.stdout.endswith("\n"):
                log_fh.write("\n")
            entries.append(
                {
                    "source_id": source_id,
                    "stop_returncode": stop.returncode,
                    "start_returncode": start.returncode,
                    "stop_output_tail": stop.stdout[-1000:],
                    "start_output_tail": start.stdout[-1000:],
                }
            )
    summary = {
        "attempt": attempt,
        "source_ids": source_ids,
        "log_path": str(log_path),
        "entries": entries,
    }
    write_json(cfg.artifact_dir / f"{log_name}.json", summary)
    return summary


def stop_pressure_sources(conn, cfg: PressureConfig) -> dict[str, Any]:
    # Capture adapter failures before the runtime apply removes disabled source
    # containers.  On a visibility-gate failure these logs are the only direct
    # proof that ffmpeg_input initialization, rather than the publisher or
    # model chain, caused the restart storm.
    source_containers = inspect_pressure_source_containers(cfg.run_id)
    write_json(
        cfg.artifact_dir / "source_containers_before_stop.json",
        source_containers,
    )
    source_logs = capture_pressure_source_logs(cfg, artifact_name="source_adapter_logs")
    with conn.transaction():
        conn.execute(
            "UPDATE cameras SET enabled=false, updated_at=now() WHERE source_id LIKE %s",
            (f"{cfg.run_id}_%",),
        )
    sources_apply = apply_sources_only(
        cfg,
        "runtime_sources_apply_stop_pressure_sources.json",
        raise_on_error=False,
    )
    removed = remove_pressure_source_containers(cfg.run_id)
    stable_removal = remove_pressure_source_containers_until_stable(cfg.run_id)
    sources_apply_error = sources_apply.get("error")
    summary = {
        "direct_source_container_stop": True,
        "reason": "preserve_current_runtime_epoch_until_evidence_drain",
        "dual_shard_same_gpu": bool(cfg.dual_shard_same_gpu),
        "source_containers_before_stop": source_containers,
        "sources_apply": sources_apply,
        "sources_apply_error_ignored": bool(
            sources_apply_error and stable_removal.get("stable")
        ),
        "source_logs": source_logs,
        "removed_source_containers": removed,
        "stable_source_container_removal": stable_removal,
    }
    write_json(
        cfg.artifact_dir / "runtime_sources_apply_stop_pressure.json",
        summary,
    )
    if not stable_removal.get("stable"):
        raise RuntimeError(
            "pressure sources did not stay removed before evidence drain; "
            f"summary={stable_removal}"
        )
    return summary


def capture_pressure_source_logs(
    cfg: PressureConfig,
    *,
    artifact_name: str,
    tail_lines: int = 300,
) -> dict[str, Any]:
    prefix = f"video-analytics-source-{cfg.run_id}_"
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    names = [
        name
        for name in completed.stdout.splitlines()
        if name.startswith(prefix)
    ]
    log_dir = cfg.artifact_dir / artifact_name
    log_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    for name in names:
        source_id = remove_prefix(name, "video-analytics-source-")
        log_path = log_dir / f"{source_id}.log"
        result = subprocess.run(
            ["docker", "logs", "--tail", str(max(1, tail_lines)), name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log_path.write_text(result.stdout or "", encoding="utf-8")
        captured.append(
            {
                "container": name,
                "source_id": source_id,
                "returncode": result.returncode,
                "log_path": str(log_path),
                "bytes": log_path.stat().st_size,
            }
        )
    summary = {
        "container_count": len(names),
        "captured_count": len(captured),
        "artifact_dir": str(log_dir),
        "logs": captured[:20],
    }
    write_json(cfg.artifact_dir / f"{artifact_name}.json", summary)
    return summary


def apply_sources_only(
    cfg: PressureConfig,
    artifact_name: str,
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    response = api_json(
        cfg.api_base,
        "POST",
        "/cameras/runtime/sources/apply?include_disabled=true",
        timeout_s=180,
    )
    write_json(cfg.artifact_dir / artifact_name, response)
    if response.get("error") and raise_on_error:
        raise RuntimeError(f"runtime source apply failed: {response['error']}")
    return response


def wait_for_drain(cfg: PressureConfig) -> None:
    deadline = time.time() + cfg.drain_s
    snapshots: list[dict[str, Any]] = []
    while time.time() < deadline:
        summary = db_summary_connect(cfg)
        snapshots.append({"observed_at": datetime.now(timezone.utc).isoformat(), "summary": summary})
        if cfg.keep_evidence >= 0 and summary["playable_bundles"] >= cfg.keep_evidence:
            break
        event_count = int(summary.get("events") or 0)
        distinct_playable = int(
            summary.get("distinct_events_with_playable_evidence") or 0
        )
        blocking_count = int(summary.get("blocking_materialization_tasks") or 0)
        terminal_nonplayable = int(
            summary.get("distinct_events_with_terminal_nonplayable_outcome") or 0
        )
        suppressed_events = int(summary.get("suppressed_events") or 0)
        if cfg.keep_evidence < 0 and event_count == 0 and blocking_count == 0:
            break
        if (
            cfg.keep_evidence < 0
            and event_count > 0
            and blocking_count == 0
            and distinct_playable + suppressed_events >= event_count
        ):
            break
        time.sleep(10)
    write_json(cfg.artifact_dir / "drain_snapshots.json", snapshots)


def wait_for_pressure_event_quiescence(
    cfg: PressureConfig,
    redis_client: Redis,
    *,
    stable_samples_required: int = 2,
    poll_s: int = 10,
) -> dict[str, Any]:
    """Wait until stopped pressure sources no longer add run-scoped events.

    Evidence drain must not restore lab/new runtime epochs while old pressure
    frames are still landing as DB events. Redis streams retain consumed history,
    so the gate checks run-id stream entry counts for stability rather than zero.
    """
    timeout_s = max(1.0, float(cfg.drain_s)) + max(
        60.0,
        float(poll_s * stable_samples_required * 2),
    )
    deadline = time.time() + timeout_s
    snapshots: list[dict[str, Any]] = []
    stable_samples = 0
    last_key: tuple[Any, ...] | None = None
    while time.time() < deadline:
        source_containers = inspect_pressure_source_containers(cfg.run_id)
        db_ingest = db_pressure_event_ingest_summary_connect(cfg)
        redis_ingest = redis_run_id_stream_summary(redis_client, cfg.run_id)
        key = _pressure_event_quiescence_key(db_ingest, redis_ingest)
        stable_samples = stable_samples + 1 if key == last_key else 1
        last_key = key
        snapshot = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "stable_samples": stable_samples,
            "stable_samples_required": stable_samples_required,
            "source_containers": {
                "total": source_containers.get("total", 0),
                "running": source_containers.get("running", 0),
            },
            "db_ingest": db_ingest,
            "redis_ingest": redis_ingest,
        }
        snapshots.append(snapshot)
        no_pressure_sources = int(source_containers.get("total") or 0) == 0
        if no_pressure_sources and stable_samples >= stable_samples_required:
            summary = {
                "status": "quiesced",
                "timeout_s": timeout_s,
                "stable_samples": stable_samples,
                "stable_samples_required": stable_samples_required,
                "last_snapshot": snapshot,
            }
            write_json(cfg.artifact_dir / "pressure_event_quiescence_snapshots.json", snapshots)
            write_json(cfg.artifact_dir / "pressure_event_quiescence_summary.json", summary)
            return summary
        time.sleep(max(1, min(poll_s, int(max(1, deadline - time.time())))))
    summary = {
        "status": "timeout",
        "timeout_s": timeout_s,
        "stable_samples": stable_samples,
        "stable_samples_required": stable_samples_required,
        "last_snapshot": snapshots[-1] if snapshots else {},
    }
    write_json(cfg.artifact_dir / "pressure_event_quiescence_snapshots.json", snapshots)
    write_json(cfg.artifact_dir / "pressure_event_quiescence_summary.json", summary)
    raise RuntimeError(
        "pressure event ingest did not quiesce before evidence drain; "
        f"last_snapshot={summary['last_snapshot']}"
    )


def _pressure_event_quiescence_key(
    db_ingest: dict[str, Any],
    redis_ingest: dict[str, Any],
) -> tuple[Any, ...]:
    return (
        int(db_ingest.get("events") or 0),
        int(db_ingest.get("new_events") or 0),
        int(db_ingest.get("suppressed_events") or 0),
        str(db_ingest.get("max_event_ts_ms") or ""),
        str(db_ingest.get("max_created_at") or ""),
        int(redis_ingest.get("total_run_id_entries") or 0),
    )


def db_pressure_event_ingest_summary_connect(cfg: PressureConfig) -> dict[str, Any]:
    with psycopg.connect(cfg.db_url, row_factory=dict_row) as conn:
        return db_pressure_event_ingest_summary(
            conn,
            cfg.run_id,
            cooldown_s=cfg.pressure_algorithm_cooldown_s,
            sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
        )


def db_pressure_event_ingest_summary(
    conn,
    run_id: str,
    *,
    cooldown_s: int = PRESSURE_COOLDOWN_SECONDS,
    sampling_start_event_ts_ms: int = 0,
    sampling_end_event_ts_ms: int = 0,
) -> dict[str, Any]:
    prefix = f"{run_id}_%"
    row = conn.execute(
        f"""
        SELECT
          count(*) AS events,
          count(*) FILTER (WHERE status = 'new') AS new_events,
          count(*) FILTER (WHERE status = 'suppressed') AS suppressed_events,
          count(DISTINCT source_id) AS source_count,
          min(event_ts_ms) AS min_event_ts_ms,
          max(event_ts_ms) AS max_event_ts_ms,
          COALESCE((max(event_ts_ms) - min(event_ts_ms)) / 1000.0, 0) AS event_ts_span_s,
          min(created_at) AS min_created_at,
          max(created_at) AS max_created_at,
          COALESCE(EXTRACT(EPOCH FROM (max(created_at) - min(created_at))), 0) AS created_at_span_s,
          COALESCE(
            max(EXTRACT(EPOCH FROM (created_at - to_timestamp(event_ts_ms / 1000.0))))
              FILTER (WHERE event_ts_ms IS NOT NULL),
            0
          ) AS max_created_minus_event_ts_s
        FROM events e
        WHERE source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e", bounded_end=True)}
        """,
        {
            "prefix": prefix,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            "sampling_end_event_ts_ms": sampling_end_event_ts_ms,
        },
    ).fetchone()
    summary = _row_json(row)
    summary["formal_sampling_window"] = {
        "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
        "sampling_end_event_ts_ms": sampling_end_event_ts_ms,
        "end_bounded": sampling_end_event_ts_ms > 0,
    }
    cooldown_s = max(1, int(cooldown_s or PRESSURE_COOLDOWN_SECONDS))
    summary[f"cooldown_algorithm_{cooldown_s}s"] = (
        db_pressure_algorithm_cooldown_summary(
            conn,
            run_id,
            cooldown_s=cooldown_s,
            grace_ms=PRESSURE_COOLDOWN_GRACE_MS,
            event_types=PRESSURE_COOLDOWN_EVENT_TYPES,
            sampling_start_event_ts_ms=sampling_start_event_ts_ms,
            sampling_end_event_ts_ms=sampling_end_event_ts_ms,
        )
    )
    return summary


def db_pressure_algorithm_cooldown_summary(
    conn,
    run_id: str,
    *,
    cooldown_s: int,
    grace_ms: int,
    event_types: tuple[str, ...],
    sampling_start_event_ts_ms: int = 0,
    sampling_end_event_ts_ms: int = 0,
) -> dict[str, Any]:
    prefix = f"{run_id}_%"
    threshold_ms = max(0, cooldown_s * 1000 - max(0, grace_ms))
    rows = conn.execute(
        f"""
        WITH ordered AS (
          SELECT
            source_id,
            event_type,
            event_ts_ms,
            LAG(event_ts_ms) OVER (
              PARTITION BY source_id, event_type
              ORDER BY event_ts_ms, created_at, id
            ) AS prev_event_ts_ms
          FROM events e
          WHERE source_id LIKE %(prefix)s
            AND {_formal_pressure_event_predicate("e", bounded_end=True)}
            AND status = 'new'
            AND event_type = ANY(%(event_types)s)
            AND event_ts_ms IS NOT NULL
        ),
        deltas AS (
          SELECT
            source_id,
            event_type,
            event_ts_ms - prev_event_ts_ms AS delta_ms
          FROM ordered
          WHERE prev_event_ts_ms IS NOT NULL
        )
        SELECT
          count(*) FILTER (WHERE delta_ms < %(threshold_ms)s) AS violation_count,
          min(delta_ms) AS min_delta_ms
        FROM deltas
        """,
        {
            "prefix": prefix,
            "event_types": list(event_types),
            "threshold_ms": threshold_ms,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            "sampling_end_event_ts_ms": sampling_end_event_ts_ms,
        },
    ).fetchone()
    source_count = conn.execute(
        f"""
        SELECT count(DISTINCT source_id) AS source_count
        FROM events e
        WHERE source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e", bounded_end=True)}
        """,
        {
            "prefix": prefix,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            "sampling_end_event_ts_ms": sampling_end_event_ts_ms,
        },
    ).fetchone()
    type_rows = conn.execute(
        f"""
        SELECT event_type, count(*) AS count
        FROM events e
        WHERE source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e", bounded_end=True)}
          AND status = 'new'
          AND event_type = ANY(%(event_types)s)
        GROUP BY event_type
        ORDER BY event_type
        """,
        {
            "prefix": prefix,
            "event_types": list(event_types),
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            "sampling_end_event_ts_ms": sampling_end_event_ts_ms,
        },
    ).fetchall()
    summary = _row_json(rows)
    source_total = int((_row_json(source_count).get("source_count") or 0))
    span_s = float(
        db_pressure_event_span_seconds(
            conn,
            run_id,
            sampling_start_event_ts_ms=sampling_start_event_ts_ms,
            sampling_end_event_ts_ms=sampling_end_event_ts_ms,
        )
    )
    windows_per_source_type = int(span_s // max(cooldown_s, 1)) + 1 if source_total else 0
    summary.update(
        {
            "cooldown_s": cooldown_s,
            "grace_ms": grace_ms,
            "threshold_ms": threshold_ms,
            "scope": "source:event_type",
            "event_types": list(event_types),
            "source_count": source_total,
            "observed_event_ts_span_s": span_s,
            "paper_upper_bound_new_events": (
                source_total * len(event_types) * windows_per_source_type
            ),
            "new_events_by_type": [_row_json(row) for row in type_rows],
        }
    )
    return summary


def db_pressure_event_span_seconds(
    conn,
    run_id: str,
    *,
    sampling_start_event_ts_ms: int = 0,
    sampling_end_event_ts_ms: int = 0,
) -> float:
    row = conn.execute(
        f"""
        SELECT COALESCE((max(event_ts_ms) - min(event_ts_ms)) / 1000.0, 0) AS span_s
        FROM events e
        WHERE source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e", bounded_end=True)}
        """,
        {
            "prefix": f"{run_id}_%",
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            "sampling_end_event_ts_ms": sampling_end_event_ts_ms,
        },
    ).fetchone()
    try:
        return float((_row_json(row).get("span_s") or 0))
    except (TypeError, ValueError):
        return 0.0


def redis_run_id_stream_summary(redis_client: Redis, run_id: str) -> dict[str, Any]:
    token = run_id.encode("utf-8")
    streams: dict[str, Any] = {}
    total = 0
    for stream in SECURITY_STREAMS:
        count = 0
        last_id = b"-"
        last_matching_id = b""
        while True:
            rows = redis_client.xrange(stream, min=last_id, max=b"+", count=500)
            if not rows:
                break
            for entry_id, fields in rows:
                if entry_id == last_id:
                    continue
                haystack = b" ".join(
                    key + b"=" + value
                    for key, value in fields.items()
                    if isinstance(key, bytes) and isinstance(value, bytes)
                )
                if token in haystack:
                    count += 1
                    last_matching_id = entry_id
                last_id = entry_id
            if len(rows) < 500:
                break
        streams[stream] = {
            "run_id_entries": count,
            "last_matching_id": _decode_redis_value(last_matching_id) if last_matching_id else "",
        }
        total += count
    return {"total_run_id_entries": total, "streams": streams}


def _summary_materialization_expired_count(summary: dict[str, Any]) -> int:
    return sum(
        int(row.get("count") or 0)
        for row in summary.get("task_statuses") or []
        if isinstance(row, dict)
        and row.get("materialization_status") == "materialization_expired"
    )


def collect_pressure_diagnostics(cfg: PressureConfig) -> dict[str, Any]:
    source_containers = inspect_pressure_source_containers(cfg.run_id)
    save_pressure_source_logs(cfg, source_containers)
    diagnostics: dict[str, Any] = {
        "sample_summary": summarize_runtime_samples(cfg),
        "source_containers": source_containers,
        "rtsp_republishers": inspect_rtsp_republishers(cfg),
        "log_summary": summarize_logs(cfg),
        "cuda_mps": cuda_mps_status(cfg) if cfg.cuda_mps else {"enabled": False},
        "adaface_roi_worker": collect_adaface_roi_worker_metrics(cfg),
    }
    write_json(cfg.artifact_dir / "pressure_diagnostics.json", diagnostics)
    return diagnostics


def _savant_counter_delta_fps(
    rows: list[dict[str, Any]],
    *,
    stream_count: int,
) -> float | None:
    if len(rows) < 2 or stream_count <= 0:
        return None
    first = rows[0]
    last = rows[-1]
    if (
        int(first.get("savant_sources") or 0) < stream_count
        or int(last.get("savant_sources") or 0) < stream_count
    ):
        return None
    try:
        started_at = datetime.fromisoformat(str(first["observed_at"]))
        ended_at = datetime.fromisoformat(str(last["observed_at"]))
        elapsed_s = (ended_at - started_at).total_seconds()
        frame_delta = int(last["savant_frames_seen_total"]) - int(
            first["savant_frames_seen_total"]
        )
    except (KeyError, TypeError, ValueError):
        return None
    if elapsed_s <= 0 or frame_delta < 0:
        return None
    return frame_delta / elapsed_s / stream_count


def summarize_runtime_samples(cfg: PressureConfig) -> dict[str, Any]:
    samples_dir = cfg.artifact_dir / "samples"
    rows: list[dict[str, Any]] = []
    max_queue_depth = 0.0
    max_send_failures = 0.0
    max_forwarder_sources = 0
    max_savant_sources = 0
    max_forwarder_cpu_percent = 0.0
    max_savant_cpu_percent = 0.0
    max_adaface_central_cpu_percent = 0.0
    max_adaface_central_sources = 0
    max_adaface_forwarder_sources = 0
    max_adaface_forwarder_eligible_sources = 0
    max_adaface_central_missing_eligible_sources = 0
    max_adaface_forwarder_queue_depth = 0.0
    max_raw_forwarder_queue_depth = 0.0
    max_raw_forwarder_frames_dropped = 0.0
    max_raw_forwarder_send_failures = 0.0
    final_raw_forwarder_frames_forwarded = 0.0
    max_source_adapter_cpu_percent = 0.0
    max_worker_cpu_percent: dict[str, float] = {
        key: 0.0 for key in WORKER_CONTAINER_NAMES
    }
    queue_full_samples = 0
    final_forwarder_seen = 0.0
    final_forwarder_forwarded = 0.0
    final_forwarder_dropped = 0.0
    final_adaface_forwarder_seen = 0.0
    final_adaface_forwarder_forwarded = 0.0
    final_adaface_forwarder_filtered = 0.0
    final_adaface_forwarder_send_failures = 0.0
    final_adaface_central_missing_eligible_source_ids: list[str] = []
    final_savant_pose_objects = 0.0
    final_savant_face_objects = 0.0
    final_savant_adaface_embeddings = 0.0
    final_savant_person_observations = 0.0
    final_savant_face_observations = 0.0
    final_savant_stage_metrics: dict[str, Any] = {}
    baseline_send_failures: float | None = None
    max_send_failures_delta = 0.0
    stable_samples = 0
    for path in sorted(samples_dir.glob("runtime_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            rows.append({"sample": path.name, "error": repr(exc)})
            continue
        metrics = payload.get("metrics") or {}
        adaface_central = payload.get("adaface_central") or {}
        adaface_forwarder = payload.get("adaface_forwarder") or {}
        raw_forwarder = payload.get("raw_forwarder") or {}
        forwarder = payload.get("forwarder") or {}
        savant_stage_metrics = aggregate_savant_stage_metrics(metrics)
        if cfg.adaface_decoupled:
            central_stages = aggregate_savant_stage_metrics(adaface_central)
            savant_stage_metrics.update(
                {f"central_{name}": row for name, row in central_stages.items()}
            )
        final_savant_stage_metrics = savant_stage_metrics
        savant_sources = _dedupe_sources_by_id(
            metrics.get("sources") or [],
            score_keys=(
                "frames_seen_total",
                "pose_objects_total",
                "face_objects_total",
                "person_observations_exported_total",
                "face_observations_exported_total",
            ),
        )
        forwarder_sources = _dedupe_sources_by_id(
            forwarder.get("sources") or [],
            score_keys=(
                "frames_seen_total",
                "frames_forwarded_total",
                "frames_dropped_total",
                "savant_send_failures_total",
            ),
        )
        central_sources = _dedupe_sources_by_id(
            adaface_central.get("sources") or [],
            score_keys=(
                "frames_seen_total",
                "face_objects_total",
                "adaface_embeddings_total",
                "face_observations_exported_total",
            ),
        )
        adaface_forwarder_sources = _dedupe_sources_by_id(
            adaface_forwarder.get("sources") or [],
            score_keys=(
                "frames_seen_total",
                "frames_forwarded_total",
                "metadata_filtered_total",
                "savant_send_failures_total",
            ),
        )
        raw_forwarder_sources = _dedupe_sources_by_id(
            raw_forwarder.get("sources") or [],
            score_keys=(
                "raw_frames_forwarded_total",
                "raw_frames_dropped_total",
                "raw_send_failures_total",
            ),
        )
        sample_index = path.stem.rsplit("_", 1)[-1]
        stats = _read_docker_stats_sample(samples_dir / f"docker_stats_{sample_index}.json")
        effective_fps = []
        for source in savant_sources:
            window = ((source.get("windows") or {}).get("10s") or {})
            value = window.get("va_savant_effective_fps")
            if value is not None:
                effective_fps.append(float(value))
        queue_depth = float((forwarder.get("global") or {}).get("queue_depth") or 0.0)
        send_failures = sum(
            float(source.get("savant_send_failures_total") or 0.0)
            for source in forwarder_sources
        )
        if baseline_send_failures is None:
            baseline_send_failures = send_failures
        send_failures_delta = max(0.0, send_failures - baseline_send_failures)
        max_queue_depth = max(max_queue_depth, queue_depth)
        if queue_depth >= 2048:
            queue_full_samples += 1
        max_send_failures = max(max_send_failures, send_failures)
        max_send_failures_delta = max(max_send_failures_delta, send_failures_delta)
        max_forwarder_sources = max(max_forwarder_sources, len(forwarder_sources))
        max_savant_sources = max(max_savant_sources, len(savant_sources))
        max_adaface_central_sources = max(
            max_adaface_central_sources, len(central_sources)
        )
        max_adaface_forwarder_sources = max(
            max_adaface_forwarder_sources, len(adaface_forwarder_sources)
        )
        adaface_forwarder_eligible_source_ids = {
            str(source.get("source_id") or "")
            for source in adaface_forwarder_sources
            if float(source.get("frames_forwarded_total") or 0.0) > 0
        }
        adaface_central_source_ids = {
            str(source.get("source_id") or "") for source in central_sources
        }
        adaface_central_missing_eligible_source_ids = sorted(
            adaface_forwarder_eligible_source_ids - adaface_central_source_ids
        )
        max_adaface_forwarder_eligible_sources = max(
            max_adaface_forwarder_eligible_sources,
            len(adaface_forwarder_eligible_source_ids),
        )
        max_adaface_central_missing_eligible_sources = max(
            max_adaface_central_missing_eligible_sources,
            len(adaface_central_missing_eligible_source_ids),
        )
        final_adaface_central_missing_eligible_source_ids = (
            adaface_central_missing_eligible_source_ids
        )
        adaface_forwarder_queue_depth = float(
            (adaface_forwarder.get("global") or {}).get("queue_depth") or 0.0
        )
        max_adaface_forwarder_queue_depth = max(
            max_adaface_forwarder_queue_depth,
            adaface_forwarder_queue_depth,
        )
        raw_forwarder_queue_depth = float(
            (raw_forwarder.get("global") or {}).get("raw_queue_depth") or 0.0
        )
        raw_forwarder_frames_forwarded = sum(
            float(source.get("raw_frames_forwarded_total") or 0.0)
            for source in raw_forwarder_sources
        )
        raw_forwarder_frames_dropped = sum(
            float(source.get("raw_frames_dropped_total") or 0.0)
            for source in raw_forwarder_sources
        )
        raw_forwarder_send_failures = sum(
            float(source.get("raw_send_failures_total") or 0.0)
            for source in raw_forwarder_sources
        )
        max_raw_forwarder_queue_depth = max(
            max_raw_forwarder_queue_depth, raw_forwarder_queue_depth
        )
        max_raw_forwarder_frames_dropped = max(
            max_raw_forwarder_frames_dropped, raw_forwarder_frames_dropped
        )
        max_raw_forwarder_send_failures = max(
            max_raw_forwarder_send_failures, raw_forwarder_send_failures
        )
        final_raw_forwarder_frames_forwarded = raw_forwarder_frames_forwarded
        forwarder_seen = sum(float(source.get("frames_seen_total") or 0.0) for source in forwarder_sources)
        forwarder_forwarded = sum(
            float(source.get("frames_forwarded_total") or 0.0) for source in forwarder_sources
        )
        forwarder_dropped = sum(float(source.get("frames_dropped_total") or 0.0) for source in forwarder_sources)
        final_forwarder_seen = forwarder_seen
        final_forwarder_forwarded = forwarder_forwarded
        final_forwarder_dropped = forwarder_dropped
        adaface_forwarder_seen = sum(
            float(source.get("frames_seen_total") or 0.0)
            for source in adaface_forwarder_sources
        )
        adaface_forwarder_forwarded = sum(
            float(source.get("frames_forwarded_total") or 0.0)
            for source in adaface_forwarder_sources
        )
        adaface_forwarder_filtered = sum(
            float(source.get("metadata_filtered_total") or 0.0)
            for source in adaface_forwarder_sources
        )
        adaface_forwarder_send_failures = sum(
            float(source.get("savant_send_failures_total") or 0.0)
            for source in adaface_forwarder_sources
        )
        final_adaface_forwarder_seen = adaface_forwarder_seen
        final_adaface_forwarder_forwarded = adaface_forwarder_forwarded
        final_adaface_forwarder_filtered = adaface_forwarder_filtered
        final_adaface_forwarder_send_failures = adaface_forwarder_send_failures
        savant_pose_objects = sum(float(source.get("pose_objects_total") or 0.0) for source in savant_sources)
        savant_frames_seen = sum(
            float(source.get("frames_seen_total") or 0.0)
            for source in savant_sources
        )
        savant_face_objects = sum(float(source.get("face_objects_total") or 0.0) for source in savant_sources)
        savant_adaface_embeddings = sum(
            float(source.get("adaface_embeddings_total") or 0.0) for source in savant_sources
        )
        savant_person_observations = sum(
            float(source.get("person_observations_exported_total") or 0.0)
            for source in savant_sources
        )
        savant_face_observations = sum(
            float(source.get("face_observations_exported_total") or 0.0)
            for source in savant_sources
        )
        if cfg.adaface_decoupled:
            savant_adaface_embeddings = sum(
                float(source.get("adaface_embeddings_total") or 0.0)
                for source in central_sources
            )
            savant_face_observations = sum(
                float(source.get("face_observations_exported_total") or 0.0)
                for source in central_sources
            )
        final_savant_pose_objects = savant_pose_objects
        final_savant_face_objects = savant_face_objects
        final_savant_adaface_embeddings = savant_adaface_embeddings
        final_savant_person_observations = savant_person_observations
        final_savant_face_observations = savant_face_observations
        if cfg.dual_shard_same_gpu:
            forwarder_cpu = _stats_cpu_percent_sum(
                stats,
                [
                    "video-analytics-midterm-analysis-forwarder-a",
                    "video-analytics-midterm-analysis-forwarder-b",
                ],
            )
            savant_cpu = _stats_cpu_percent_sum(
                stats,
                [
                    "video-analytics-midterm-savant-a",
                    "video-analytics-midterm-savant-b",
                ],
            )
            adaface_central_cpu = _stats_cpu_percent_sum(
                stats,
                [
                    f"video-analytics-midterm-{service}"
                    for service in adaface_central_service_names(cfg)
                ],
            )
        else:
            forwarder_cpu = _stats_cpu_percent(stats, "video-analytics-midterm-analysis-forwarder")
            savant_cpu = _stats_cpu_percent(stats, "video-analytics-midterm-savant")
            adaface_central_cpu = 0.0
        source_cpu = max(
            (
                _parse_percent(str(item.get("CPUPerc") or "0"))
                for name, item in stats.items()
                if name.startswith(f"video-analytics-source-{cfg.run_id}_")
            ),
            default=0.0,
        )
        max_forwarder_cpu_percent = max(max_forwarder_cpu_percent, forwarder_cpu)
        max_savant_cpu_percent = max(max_savant_cpu_percent, savant_cpu)
        max_adaface_central_cpu_percent = max(
            max_adaface_central_cpu_percent, adaface_central_cpu
        )
        max_source_adapter_cpu_percent = max(max_source_adapter_cpu_percent, source_cpu)
        worker_cpu = {
            key: _stats_cpu_percent(stats, container)
            for key, container in WORKER_CONTAINER_NAMES.items()
        }
        for key, value in worker_cpu.items():
            max_worker_cpu_percent[key] = max(max_worker_cpu_percent[key], value)
        if (
            len(forwarder_sources) >= cfg.stream_count
            and queue_depth <= 0
            and send_failures_delta <= cfg.max_send_failures
        ):
            stable_samples += 1
        rows.append(
            {
                "sample": path.name,
                "observed_at": payload.get("_pressure_sample_observed_at"),
                "savant_sources": len(savant_sources),
                "adaface_central_sources": len(central_sources),
                "adaface_forwarder_sources": len(adaface_forwarder_sources),
                "adaface_forwarder_eligible_sources": len(
                    adaface_forwarder_eligible_source_ids
                ),
                "adaface_central_missing_eligible_source_ids": (
                    adaface_central_missing_eligible_source_ids
                ),
                "adaface_forwarder_queue_depth": adaface_forwarder_queue_depth,
                "adaface_forwarder_frames_seen_total": int(
                    adaface_forwarder_seen
                ),
                "adaface_forwarder_frames_forwarded_total": int(
                    adaface_forwarder_forwarded
                ),
                "adaface_forwarder_metadata_filtered_total": int(
                    adaface_forwarder_filtered
                ),
                "adaface_forwarder_send_failures_total": int(
                    adaface_forwarder_send_failures
                ),
                "raw_forwarder_queue_depth": raw_forwarder_queue_depth,
                "raw_forwarder_frames_forwarded_total": int(
                    raw_forwarder_frames_forwarded
                ),
                "raw_forwarder_frames_dropped_total": int(
                    raw_forwarder_frames_dropped
                ),
                "raw_forwarder_send_failures_total": int(
                    raw_forwarder_send_failures
                ),
                "forwarder_sources": len(forwarder_sources),
                "queue_depth": queue_depth,
                "savant_send_failures_total": send_failures,
                "savant_send_failures_delta": send_failures_delta,
                "forwarder_frames_seen_total": int(forwarder_seen),
                "forwarder_frames_forwarded_total": int(forwarder_forwarded),
                "forwarder_frames_dropped_total": int(forwarder_dropped),
                "savant_pose_objects_total": int(savant_pose_objects),
                "savant_frames_seen_total": int(savant_frames_seen),
                "savant_face_objects_total": int(savant_face_objects),
                "savant_adaface_embeddings_total": int(savant_adaface_embeddings),
                "savant_person_observations_exported_total": int(savant_person_observations),
                "savant_face_observations_exported_total": int(savant_face_observations),
                "forwarder_forwarded_seen_ratio": (
                    round(forwarder_forwarded / forwarder_seen, 4)
                    if forwarder_seen > 0
                    else None
                ),
                "forwarder_cpu_percent": forwarder_cpu,
                "savant_cpu_percent": savant_cpu,
                "adaface_central_cpu_percent": adaface_central_cpu,
                "worker_cpu_percent": worker_cpu,
                "max_source_adapter_cpu_percent": source_cpu,
                "avg_effective_fps_10s": (
                    round(sum(effective_fps) / len(effective_fps), 3)
                    if effective_fps
                    else None
                ),
                "min_effective_fps_10s": min(effective_fps) if effective_fps else None,
                "max_effective_fps_10s": max(effective_fps) if effective_fps else None,
                "savant_stage_metrics": savant_stage_metrics,
            }
        )
    steady_effective_fps_values = [
        float(row["avg_effective_fps_10s"])
        for row in rows[1:]
        if row.get("avg_effective_fps_10s") is not None
    ]
    steady_window_effective_fps_mean = (
        sum(steady_effective_fps_values) / len(steady_effective_fps_values)
        if steady_effective_fps_values
        else None
    )
    steady_counter_effective_fps = _savant_counter_delta_fps(
        rows,
        stream_count=cfg.stream_count,
    )
    steady_effective_fps_mean = (
        steady_counter_effective_fps
        if steady_counter_effective_fps is not None
        else steady_window_effective_fps_mean
    )
    minimum_effective_fps = _fps_to_float(cfg.min_fps)
    target_effective_fps = _fps_to_float(cfg.fps)
    postfill_adaface_visibility: dict[str, Any] = {}
    postfill_path = cfg.artifact_dir / "rolling_cache_postfill_summary.json"
    try:
        postfill_payload = json.loads(postfill_path.read_text(encoding="utf-8"))
        candidate = postfill_payload.get("adaface_visibility") or {}
        if isinstance(candidate, dict):
            postfill_adaface_visibility = candidate
    except (OSError, json.JSONDecodeError):
        pass
    if postfill_adaface_visibility:
        final_adaface_central_missing_eligible_source_ids = list(
            postfill_adaface_visibility.get(
                "missing_adaface_central_eligible_sources"
            )
            or []
        )
    summary = {
        "sample_count": len(rows),
        "max_queue_depth": max_queue_depth,
        "max_forwarder_queue_depth": max_queue_depth,
        "queue_full_samples": queue_full_samples,
        "max_savant_send_failures_total": int(max_send_failures),
        "baseline_savant_send_failures_total": int(baseline_send_failures or 0),
        "max_savant_send_failures_delta": int(max_send_failures_delta),
        "max_forwarder_sources": max_forwarder_sources,
        "max_savant_sources": max_savant_sources,
        "max_adaface_central_sources": max_adaface_central_sources,
        "max_adaface_forwarder_sources": max_adaface_forwarder_sources,
        "max_adaface_forwarder_eligible_sources": (
            max_adaface_forwarder_eligible_sources
        ),
        "max_adaface_central_missing_eligible_sources": (
            max_adaface_central_missing_eligible_sources
        ),
        "max_adaface_forwarder_queue_depth": max_adaface_forwarder_queue_depth,
        "max_raw_forwarder_queue_depth": max_raw_forwarder_queue_depth,
        "max_raw_forwarder_frames_dropped_total": int(
            max_raw_forwarder_frames_dropped
        ),
        "max_raw_forwarder_send_failures_total": int(
            max_raw_forwarder_send_failures
        ),
        "final_raw_forwarder_frames_forwarded_total": int(
            final_raw_forwarder_frames_forwarded
        ),
        "final_forwarder_frames_seen_total": int(final_forwarder_seen),
        "final_forwarder_frames_forwarded_total": int(final_forwarder_forwarded),
        "final_forwarder_frames_dropped_total": int(final_forwarder_dropped),
        "final_adaface_forwarder_frames_seen_total": int(
            final_adaface_forwarder_seen
        ),
        "final_adaface_forwarder_frames_forwarded_total": int(
            final_adaface_forwarder_forwarded
        ),
        "final_adaface_forwarder_metadata_filtered_total": int(
            final_adaface_forwarder_filtered
        ),
        "final_adaface_forwarder_send_failures_total": int(
            final_adaface_forwarder_send_failures
        ),
        "final_adaface_forwarder_send_attempts_total": int(
            final_adaface_forwarder_forwarded
            + final_adaface_forwarder_send_failures
        ),
        "final_adaface_forwarder_send_failure_ratio": (
            round(
                final_adaface_forwarder_send_failures
                / (
                    final_adaface_forwarder_forwarded
                    + final_adaface_forwarder_send_failures
                ),
                6,
            )
            if (
                final_adaface_forwarder_forwarded
                + final_adaface_forwarder_send_failures
            )
            > 0
            else 0.0
        ),
        "max_adaface_forwarder_send_failure_ratio": (
            cfg.max_adaface_forwarder_send_failure_ratio
        ),
        "final_adaface_central_missing_eligible_source_ids": (
            final_adaface_central_missing_eligible_source_ids
        ),
        "postfill_adaface_visibility": postfill_adaface_visibility,
        "final_savant_pose_objects_total": int(final_savant_pose_objects),
        "final_savant_face_objects_total": int(final_savant_face_objects),
        "final_savant_adaface_embeddings_total": int(final_savant_adaface_embeddings),
        "final_savant_person_observations_exported_total": int(final_savant_person_observations),
        "final_savant_face_observations_exported_total": int(final_savant_face_observations),
        "final_savant_stage_metrics": final_savant_stage_metrics,
        "steady_effective_fps_sample_count": len(steady_effective_fps_values),
        "steady_effective_fps_method": (
            "savant_frames_seen_counter_delta"
            if steady_counter_effective_fps is not None
            else "mean_10s_window_gauge"
        ),
        "steady_window_effective_fps_mean": (
            round(steady_window_effective_fps_mean, 4)
            if steady_window_effective_fps_mean is not None
            else None
        ),
        "steady_counter_effective_fps": (
            round(steady_counter_effective_fps, 4)
            if steady_counter_effective_fps is not None
            else None
        ),
        "steady_effective_fps_mean": (
            round(steady_effective_fps_mean, 4)
            if steady_effective_fps_mean is not None
            else None
        ),
        "minimum_effective_fps": minimum_effective_fps,
        "steady_effective_fps_meets_minimum": (
            steady_effective_fps_mean >= minimum_effective_fps
            if steady_effective_fps_mean is not None and minimum_effective_fps > 0
            else False
        ),
        "steady_effective_fps_target_ratio": (
            round(steady_effective_fps_mean / target_effective_fps, 4)
            if steady_effective_fps_mean is not None and target_effective_fps > 0
            else None
        ),
        "final_forwarder_forwarded_seen_ratio": (
            round(final_forwarder_forwarded / final_forwarder_seen, 4)
            if final_forwarder_seen > 0
            else None
        ),
        "target_forwarded_frames": int(
            round(cfg.stream_count * _fps_to_float(cfg.fps) * max(cfg.duration_s, 0))
        ),
        "final_forwarded_target_ratio": (
            round(
                final_forwarder_forwarded
                / max(cfg.stream_count * _fps_to_float(cfg.fps) * max(cfg.duration_s, 1), 1),
                4,
            )
            if final_forwarder_forwarded
            else 0.0
        ),
        "max_forwarder_cpu_percent": round(max_forwarder_cpu_percent, 3),
        "max_savant_cpu_percent": round(max_savant_cpu_percent, 3),
        "max_adaface_central_cpu_percent": round(
            max_adaface_central_cpu_percent, 3
        ),
        "max_source_adapter_cpu_percent": round(max_source_adapter_cpu_percent, 3),
        "max_worker_cpu_percent": {
            key: round(value, 3) for key, value in max_worker_cpu_percent.items()
        },
        "stable_samples": stable_samples,
        "samples": rows,
    }
    write_json(cfg.artifact_dir / "sample_summary.json", summary)
    return summary


def aggregate_savant_stage_metrics(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    shard_metrics = [
        shard.get("stage_metrics") or {}
        for shard in metrics.get("shards") or []
        if isinstance(shard, dict)
    ]
    if not shard_metrics:
        shard_metrics = [metrics.get("stage_metrics") or {}]
    merged: dict[str, dict[str, Any]] = {}
    for stages in shard_metrics:
        for stage, row in stages.items():
            if not isinstance(row, dict):
                continue
            target = merged.setdefault(
                stage,
                {
                    "duration_count": 0.0,
                    "duration_sum_s": 0.0,
                    "duration_last_s": None,
                    "duration_buckets": {},
                    "batch_occupancy": {},
                },
            )
            target["duration_count"] += float(row.get("duration_count") or 0.0)
            target["duration_sum_s"] += float(row.get("duration_sum_s") or 0.0)
            last = row.get("duration_last_s")
            if last is not None:
                target["duration_last_s"] = max(
                    float(target.get("duration_last_s") or 0.0), float(last)
                )
            for upper, value in (row.get("duration_buckets") or {}).items():
                buckets = target["duration_buckets"]
                buckets[str(upper)] = float(buckets.get(str(upper)) or 0.0) + float(value)
            for size, value in (row.get("batch_occupancy") or {}).items():
                occupancy = target["batch_occupancy"]
                occupancy[str(size)] = float(occupancy.get(str(size)) or 0.0) + float(value)
    return _finalize_stage_metrics(merged)


def _dedupe_sources_by_id(
    sources: list[dict[str, Any]],
    *,
    score_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    by_score: dict[str, float] = {}
    anonymous: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "")
        if not source_id:
            anonymous.append(source)
            continue
        score = 0.0
        for key in score_keys:
            try:
                score += float(source.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
        if source_id not in by_source or score >= by_score.get(source_id, -1.0):
            by_source[source_id] = source
            by_score[source_id] = score
    return [*by_source.values(), *anonymous]


def save_pressure_source_logs(cfg: PressureConfig, source_summary: dict[str, Any]) -> None:
    log_dir = cfg.artifact_dir / "source_adapter_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for item in source_summary.get("items") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        log_path = log_dir / f"{name}.log"
        run(
            ["docker", "logs", "--tail", "10000", name],
            log_path,
            check=False,
        )
        item["log_path"] = str(log_path)


def inspect_rtsp_republishers(cfg: PressureConfig) -> dict[str, Any]:
    manifest_path = cfg.artifact_dir / "rtsp_republish_manifest.json"
    if not manifest_path.exists():
        return {"enabled": False}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"enabled": True, "error": repr(exc)}
    stop_path = cfg.artifact_dir / "rtsp_republish_stop.json"
    stopped_pids: set[int] = set()
    if stop_path.exists():
        try:
            stop_rows = json.loads(stop_path.read_text(encoding="utf-8"))
        except Exception:
            stop_rows = []
        if isinstance(stop_rows, list):
            stopped_pids = {
                int(row.get("pid") or 0)
                for row in stop_rows
                if isinstance(row, dict) and int(row.get("pid") or 0) > 0
            }
    items: list[dict[str, Any]] = []
    for item in manifest:
        log_path = Path(str(item.get("log_path") or ""))
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        pid = int(item.get("pid") or 0)
        returncode = None
        if pid > 0:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid="],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if not completed.stdout.strip():
                returncode = "exited_or_missing"
        stopped_intentionally = pid in stopped_pids
        items.append(
            {
                "source_id": item.get("source_id"),
                "pid": pid,
                "returncode": returncode,
                "stopped_intentionally": stopped_intentionally,
                "log_path": str(log_path),
                "non_monotonic_dts": text.lower().count("non-monoton"),
                "negative_timestamp": text.lower().count("negative"),
                "connection_errors": text.lower().count("connection refused")
                + text.lower().count("connection timed out"),
            }
        )
    exited = [
        item
        for item in items
        if item.get("returncode") is not None
        and not bool(item.get("stopped_intentionally"))
    ]
    stopped = [item for item in items if bool(item.get("stopped_intentionally"))]
    summary = {
        "enabled": True,
        "total": len(items),
        "exited": len(exited),
        "stopped_intentionally": len(stopped),
        "non_monotonic_dts_total": sum(int(item.get("non_monotonic_dts") or 0) for item in items),
        "negative_timestamp_total": sum(int(item.get("negative_timestamp") or 0) for item in items),
        "connection_error_total": sum(int(item.get("connection_errors") or 0) for item in items),
        "exited_sources": exited[:20],
        "items": items,
    }
    return summary


def inspect_pressure_source_containers(run_id: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    prefix = f"video-analytics-source-{run_id}_"
    items: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(row.get("Names") or "")
        if not name.startswith(prefix):
            continue
        inspect = inspect_container(name)
        status = str(row.get("Status") or "")
        state = inspect.get("State") or {}
        restart_count = int(inspect.get("RestartCount") or 0)
        running = bool(state.get("Running"))
        items.append(
            {
                "name": name,
                "status": status,
                "running": running,
                "exit_code": state.get("ExitCode"),
                "restart_count": restart_count,
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "negative_pts_errors": count_docker_log_pattern(name, "OverflowError: -"),
                "processed_zero_frames": count_docker_log_pattern(name, "Processed 0 frames"),
            }
        )
    exited = [item for item in items if not item["running"]]
    restarted = [item for item in items if int(item.get("restart_count") or 0) > 0]
    summary = {
        "total": len(items),
        "running": len(items) - len(exited),
        "exited": len(exited),
        "restart_count_total": sum(int(item.get("restart_count") or 0) for item in items),
        "negative_pts_error_total": sum(int(item.get("negative_pts_errors") or 0) for item in items),
        "processed_zero_frames_total": sum(int(item.get("processed_zero_frames") or 0) for item in items),
        "exited_sources": exited[:20],
        "restarted_sources": restarted[:20],
        "items": items,
    }
    return summary


def inspect_container(name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "inspect", name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return {}
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return data[0] if data else {}


def count_docker_log_pattern(name: str, pattern: str) -> int:
    completed = subprocess.run(
        ["docker", "logs", "--tail", "5000", name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.count(pattern)


def _extract_metric_ints(text: str, name: str) -> list[int]:
    pattern = re.compile(rf"\b{re.escape(name)}=(\d+)")
    return [int(match.group(1)) for match in pattern.finditer(text or "")]


def _extract_metric_numbers(text: str, name: str) -> list[float]:
    pattern = re.compile(rf"\b{re.escape(name)}=(-?\d+(?:\.\d+)?)")
    return [float(match.group(1)) for match in pattern.finditer(text or "")]


def _metric_sum(values: list[int]) -> int:
    return int(sum(values))


def _numeric_distribution(values: list[float] | list[int]) -> dict[str, Any]:
    if not values:
        return {"status": "not_enough_data", "count": 0}
    sorted_values = sorted(float(value) for value in values)
    return {
        "status": "measured",
        "count": len(sorted_values),
        "min": round(sorted_values[0], 3),
        "p50": round(_percentile(sorted_values, 0.50), 3),
        "p95": round(_percentile(sorted_values, 0.95), 3),
        "p99": round(_percentile(sorted_values, 0.99), 3),
        "max": round(sorted_values[-1], 3),
    }


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def collect_rolling_cache_segment_visibility(
    cfg: PressureConfig,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Approximate segment close/index visibility lag from finalized metadata.

    The rolling-cache lookup is filesystem-backed: a segment becomes visible to
    lookup when its finalized ``metadata.json`` exists. The native frame PTS in
    this deployment is epoch-ns, so ``metadata.json`` mtime minus the last frame
    PTS is a practical close/index-visible lag estimate. If a future source
    emits stream-relative PTS, the lag distribution is intentionally omitted
    instead of mixing clock domains.
    """

    root = root or pressure_rolling_cache_root_host()
    source_prefix = f"{cfg.run_id}_"
    segment_durations_s: list[float] = []
    frame_counts: list[int] = []
    segment_frame_rates_fps: list[float] = []
    visibility_lags_s: list[float] = []
    source_ids: set[str] = set()
    sampled_paths: list[str] = []
    skipped_clock_domain = 0
    parse_errors = 0
    metadata_files_vanished = 0
    metadata_paths = sorted(root.rglob("metadata.json")) if root.exists() else []
    for metadata_path in metadata_paths:
        if "materialized" in metadata_path.parts:
            continue
        source_id = _source_id_from_segment_metadata_path(metadata_path)
        if not source_id.startswith(source_prefix):
            continue
        try:
            frames = _load_segment_metadata_frames(metadata_path)
        except Exception:
            parse_errors += 1
            continue
        if not frames:
            continue
        pts_values = [
            _int_or_none(row.get("pts") if isinstance(row, dict) else None)
            for row in frames
        ]
        pts_values = [value for value in pts_values if value is not None]
        if not pts_values:
            continue
        source_ids.add(source_id)
        frame_counts.append(len(pts_values))
        first_pts = min(pts_values)
        last_pts = max(pts_values)
        duration_s = max(0.0, (last_pts - first_pts) / 1_000_000_000.0)
        segment_durations_s.append(duration_s)
        if duration_s > 0:
            segment_frame_rates_fps.append(len(pts_values) / duration_s)
        # Treat only plausible epoch-ns PTS as comparable with filesystem mtime.
        last_pts_epoch_s = last_pts / 1_000_000_000.0
        if 946684800.0 <= last_pts_epoch_s <= 4102444800.0:
            try:
                metadata_mtime = metadata_path.stat().st_mtime
            except FileNotFoundError:
                metadata_files_vanished += 1
                continue
            visibility_lags_s.append(metadata_mtime - last_pts_epoch_s)
            if len(sampled_paths) < 20:
                sampled_paths.append(str(metadata_path))
        else:
            skipped_clock_domain += 1

    return {
        "root": str(root),
        "source_prefix": source_prefix,
        "metadata_files_scanned": len(metadata_paths),
        "segments_measured": len(frame_counts),
        "source_count": len(source_ids),
        "frame_count": _numeric_distribution(frame_counts),
        "segment_duration_s": _numeric_distribution(segment_durations_s),
        "segment_frame_rate_fps": _numeric_distribution(segment_frame_rates_fps),
        "metadata_visible_lag_s": _numeric_distribution(visibility_lags_s),
        "skipped_clock_domain": skipped_clock_domain,
        "parse_errors": parse_errors,
        "metadata_files_vanished": metadata_files_vanished,
        "sampled_metadata_paths": sampled_paths,
        "interpretation": (
            "metadata_visible_lag_s approximates segment close/index-visible lag "
            "because rolling-cache lookup scans finalized metadata.json files; "
            "it is omitted for non-epoch PTS."
        ),
    }


def _source_id_from_segment_metadata_path(path: Path) -> str:
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part == "epochs" and index + 2 < len(parts):
            return parts[index + 2].rstrip("%")
    return ""


def _load_segment_metadata_frames(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
    if isinstance(payload, dict):
        frames = payload.get("frames")
        return [row for row in frames if isinstance(row, dict)] if isinstance(frames, list) else []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_log_field(line: str, field: str) -> str:
    match = re.search(rf"\b{re.escape(field)}=([^\s]+)", line)
    return match.group(1) if match else ""


def _media_finalizer_line_metrics(text: str) -> dict[str, Any]:
    by_worker: dict[str, int] = {}
    by_source: dict[str, list[int]] = {}
    by_shard: dict[str, list[int]] = {}
    event_counts: dict[str, int] = {}
    claim_wait_ms: list[int] = []
    for line in text.splitlines():
        if "media_event_finalized" not in line:
            continue
        event_id = _extract_log_field(line, "event_id")
        if event_id:
            event_counts[event_id] = event_counts.get(event_id, 0) + 1
        worker_id = _extract_log_field(line, "worker_id") or "unknown"
        by_worker[worker_id] = by_worker.get(worker_id, 0) + 1
        source_id = _extract_log_field(line, "source_id") or "unknown"
        shard_id = _extract_log_field(line, "replay_shard_id") or "unknown"
        queue_wait = _extract_log_field(line, "queue_wait_ms")
        try:
            queue_wait_value = int(queue_wait)
        except ValueError:
            queue_wait_value = -1
        if queue_wait_value >= 0:
            by_source.setdefault(source_id, []).append(queue_wait_value)
            by_shard.setdefault(shard_id, []).append(queue_wait_value)
        claim_wait = _extract_log_field(line, "claim_wait_ms")
        try:
            claim_wait_ms.append(int(claim_wait))
        except ValueError:
            pass
    return {
        "worker_counts": by_worker,
        "worker_count": len(by_worker),
        "queue_wait_ms_by_source": {
            key: _numeric_distribution(values)
            for key, values in sorted(by_source.items())
        },
        "queue_wait_ms_by_shard": {
            key: _numeric_distribution(values)
            for key, values in sorted(by_shard.items())
        },
        "claim_wait_ms": _numeric_distribution(claim_wait_ms),
        "duplicate_materialization_count": sum(
            count - 1 for count in event_counts.values() if count > 1
        ),
    }


def _log_field_counts(text: str, *, marker: str, field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if marker not in line:
            continue
        value = _extract_log_field(line, field)
        if not value:
            value = "unknown"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _log_metric_numbers_for_lines(
    text: str,
    *,
    marker: str,
    field: str,
) -> list[float]:
    values: list[float] = []
    for line in text.splitlines():
        if marker not in line:
            continue
        value = _extract_log_field(line, field)
        if not value:
            continue
        try:
            values.append(float(value))
        except ValueError:
            continue
    return values


def summarize_logs(cfg: PressureConfig) -> dict[str, Any]:
    paths = {
        "savant": cfg.artifact_dir / "savant_logs_since_start.txt",
        "analysis_forwarder": cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt",
        "media_worker": cfg.artifact_dir / "media_worker_logs_since_start.txt",
        "clip_worker": cfg.artifact_dir / "clip_worker_logs_since_start.txt",
        "event_worker": cfg.artifact_dir / "event_worker_logs_since_start.txt",
        "face_worker": cfg.artifact_dir / "face_worker_logs_since_start.txt",
        "adaface_central": cfg.artifact_dir / "adaface_central_logs_since_start.txt",
        "adaface_forwarder": cfg.artifact_dir / "adaface_forwarder_logs_since_start.txt",
        "adaface_roi_worker": cfg.artifact_dir / "adaface_roi_worker_logs_since_start.txt",
        "replay_raw_fanout": cfg.artifact_dir / "replay_raw_fanout_logs_since_start.txt",
    }
    summary: dict[str, Any] = {}
    for key, path in paths.items():
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        media_finalization_ms = _extract_metric_ints(text, "finalization_duration_ms")
        media_queue_wait_ms = _extract_metric_ints(text, "queue_wait_ms")
        media_lifecycle_ms = _extract_metric_ints(text, "lifecycle_elapsed_ms")
        media_post_savant_finalization_ms = _extract_metric_ints(
            text,
            "post_savant_finalization_elapsed_ms",
        )
        media_ffprobe_ms = _extract_metric_ints(text, "ffprobe_duration_ms")
        media_ffmpeg_ms = _extract_metric_ints(text, "ffmpeg_duration_ms")
        media_imageio_ffmpeg_fallback_counts = _extract_metric_ints(
            text,
            "imageio_ffmpeg_fallback_count",
        )
        media_replay_to_sink_metadata_ms = _extract_metric_ints(
            text,
            "replay_to_sink_metadata_ms",
        )
        media_sink_metadata_to_video_ms = _extract_metric_ints(
            text,
            "sink_metadata_to_video_ms",
        )
        media_sink_video_to_stable_ms = _extract_metric_ints(
            text,
            "sink_video_to_stable_ms",
        )
        media_sink_stable_to_ffprobe_ready_ms = _extract_metric_ints(
            text,
            "sink_stable_to_ffprobe_ready_ms",
        )
        media_sink_ffprobe_ready_to_finalizer_start_ms = _extract_metric_ints(
            text,
            "sink_ffprobe_ready_to_finalizer_start_ms",
        )
        media_finalizer_pool_wait_ms = _extract_metric_ints(
            text,
            "finalizer_pool_wait_ms",
        )
        media_scheduler_tick_duration_ms = _log_metric_numbers_for_lines(
            text,
            marker="media_scheduler_tick",
            field="tick_duration_ms",
        )
        media_scheduler_tick_gap_ms = _log_metric_numbers_for_lines(
            text,
            marker="media_scheduler_tick",
            field="tick_gap_ms",
        )
        media_scheduler_remux_lane_depth = _log_metric_numbers_for_lines(
            text,
            marker="media_scheduler_tick",
            field="remux_lane_depth",
        )
        media_scheduler_permit_active = _log_metric_numbers_for_lines(
            text,
            marker="media_scheduler_tick",
            field="permit_active",
        )
        media_scheduler_permit_limit = _log_metric_numbers_for_lines(
            text,
            marker="media_scheduler_tick",
            field="permit_limit",
        )
        media_scheduler_capacity_metrics = {
            field: _log_metric_numbers_for_lines(
                text,
                marker="media_scheduler_tick",
                field=field,
            )
            for field in (
                "oldest_ready_age_ms",
                "image_lane_depth",
                "finalizer_lane_depth",
                "db_pool_in_use",
                "db_pool_limit",
                "db_pool_peak_in_use",
                "db_pool_checkout_count",
                "db_pool_checkout_wait_ms",
                "db_pool_checkout_timeouts",
                "db_pool_checkout_errors",
                "db_pool_resets",
                "db_pool_connections_lost",
                "lease_heartbeat_active",
                "lease_heartbeat_total",
                "lease_heartbeat_lost",
                "lease_heartbeat_expired",
                "lease_heartbeat_errors",
                "segment_index_hits",
                "segment_index_misses",
                "segment_index_refreshes",
                "segment_index_parses",
                "segment_index_stale_entries",
                "segment_index_fallback_scans",
                "segment_index_row_cache_entries",
                "segment_index_row_cache_evictions",
                "segment_index_active_read_pins",
                "segment_index_read_pins_created",
                "segment_index_read_pins_released",
                "segment_index_generation",
            )
        }
        media_resource_capacity_metrics = {
            field: _log_metric_numbers_for_lines(
                text,
                marker="media_worker_resources",
                field=field,
            )
            for field in (
                "max_active",
                "image_workers",
                "remux_workers",
                "finalizer_workers",
                "image_queue_capacity",
                "remux_queue_capacity",
                "finalizer_queue_capacity",
                "source_limit",
            )
        }
        media_resource_capacity_metrics["cpu_thread_limit"] = (
            _log_metric_numbers_for_lines(
                text,
                marker="media-worker started",
                field="materialization_cpu_thread_limit",
            )
        )
        media_db_index_metrics = {
            name: _log_metric_numbers_for_lines(
                text,
                marker="evidence_db_index_upserted",
                field=field,
            )
            for name, field in (
                ("duration_ms", "duration_ms"),
                ("sidecar_ms", "sidecar_ms"),
                ("bundle_ms", "bundle_ms"),
                ("artifact_ms", "artifact_ms"),
                ("timeline_ms", "timeline_ms"),
                ("overlay_ms", "overlay_ms"),
            )
        }
        media_sidecar_prune_duration_ms = _log_metric_numbers_for_lines(
            text,
            marker="evidence_sidecars_pruned",
            field="duration_ms",
        )
        media_throttle_sleep_s = _extract_metric_numbers(text, "throttle_sleep_s")
        media_deadline_slack_s = _extract_metric_numbers(text, "deadline_slack_s")
        clip_record_request_pending_ms = _extract_metric_ints(
            text,
            "record_request_pending_ms",
        )
        clip_proof_wait_ms = _extract_metric_ints(text, "proof_wait_ms")
        clip_replay_job_create_ms = _extract_metric_ints(
            text,
            "replay_job_create_ms",
        )
        clip_replay_slot_hold_ms = _extract_metric_ints(
            text,
            "replay_slot_hold_ms",
        )
        replay_duration_seconds_effective = _extract_metric_numbers(
            text,
            "replay_duration_seconds_effective",
        )
        replay_slot_timeout_budget_s = _extract_metric_numbers(
            text,
            "timeout_budget_s",
        )
        replay_slot_release_sink_stable_ms = _log_metric_numbers_for_lines(
            text,
            marker="replay_slot_released",
            field="sink_video_to_stable_ms",
        )
        face_gallery_query_ms = _extract_metric_ints(text, "gallery_query_duration_ms")
        qdrant_query_ms = _extract_metric_ints(text, "qdrant_query_duration_ms")
        qdrant_exact_rerank_ms = _extract_metric_ints(
            text,
            "qdrant_exact_rerank_duration_ms",
        )
        qdrant_fallback_counts = _extract_metric_ints(
            text,
            "qdrant_fallback_count",
        )
        qdrant_shadow_mismatch = _extract_metric_ints(
            text,
            "qdrant_shadow_mismatch",
        )
        face_roi_enqueued = _extract_metric_ints(text, "enqueued")
        face_roi_queue_dropped = _extract_metric_ints(text, "queue_dropped")
        face_worker_watchlist_emitted = _extract_metric_ints(
            text, "total_watchlist_emitted"
        )
        media_finalizer_metrics = (
            _media_finalizer_line_metrics(text)
            if key == "media_worker"
            else {}
        )
        summary[key] = {
            "line_count": len(text.splitlines()),
            "validate_seq_iq": text.count("validate_seq_iq"),
            "writer_send_timeout": text.count("WriterResultSendTimeout"),
            "raw_branch_queue_full": text.count("raw branch queue full"),
            "raw_branch_send_failed": (
                text.count("failed to send raw branch")
                + text.count("raw branch send was not successful")
            ),
            "frame_annotation_redis_write_error": text.count(
                "frame_annotation_redis_writer action=write_error"
            ),
            "frame_annotation_redis_timeout": text.count("TimeoutError:timed out"),
            "face_roi_enqueued_max": max(face_roi_enqueued, default=0),
            "face_roi_queue_dropped_max": max(face_roi_queue_dropped, default=0),
            "face_worker_watchlist_emitted_max": max(
                face_worker_watchlist_emitted, default=0
            ),
            "negative_pts_overflow": len(re.findall(r"OverflowError: -\\d+", text)),
            "ffprobe_missing": text.count("ffprobe not found"),
            "imageio_ffmpeg_fallback": _metric_sum(
                media_imageio_ffmpeg_fallback_counts
            ),
            "imageio_ffmpeg_fallback_count_distribution": _numeric_distribution(
                media_imageio_ffmpeg_fallback_counts
            ),
            "record_request_dedupe_reserved": text.count(
                "record_request_dedupe_reserved"
            ),
            "record_request_dedupe_duplicate": text.count(
                "record_request_dedupe_duplicate"
            ),
            "record_request_dedupe_reserve_failed": text.count(
                "record_request_dedupe_reserve_failed"
            ),
            "watchlist_hit_emitted": text.count("watchlist_hit_emitted"),
            "watchlist_hit_suppressed_cooldown": text.count(
                "watchlist_hit_suppressed_cooldown"
            ),
            "watchlist_emit_failed": text.count("watchlist emit failed"),
            "watchlist_gallery_query_completed": text.count(
                "watchlist_gallery_query_completed"
            ),
            "watchlist_gallery_query_failed": text.count(
                "watchlist_gallery_query_failed"
            ),
            "face_gallery_query_latency_ms": _numeric_distribution(
                face_gallery_query_ms
            ),
            "qdrant_query_latency_ms": _numeric_distribution(qdrant_query_ms),
            "qdrant_exact_rerank_latency_ms": _numeric_distribution(
                qdrant_exact_rerank_ms
            ),
            "qdrant_fallback_count": _metric_sum(qdrant_fallback_counts),
            "qdrant_shadow_mismatch_count": _metric_sum(qdrant_shadow_mismatch),
            "media_event_finalized": text.count("media_event_finalized"),
            "media_materialization_deferred": text.count(
                "media_materialization_deferred"
            ),
            "media_materialization_paced": text.count("media_materialization_paced"),
            "media_materialization_throttle_paced": text.count(
                "throttle_reason=paced"
            ),
            "media_materialization_throttle_deadline_guard": text.count(
                "throttle_reason=deadline_guard"
            ),
            "media_materialization_throttle_disabled": text.count(
                "throttle_reason=disabled"
            ),
            "media_finalization_claim_busy": text.count(
                "media_finalization_claim_busy"
            ),
            "media_finalization_claim_terminal": text.count(
                "media_finalization_claim_terminal"
            ),
            "post_savant_finalizer_failed": text.count(
                "post_savant_finalizer_failed"
            ),
            "media_finalization_duration_ms": _numeric_distribution(
                media_finalization_ms
            ),
            "media_queue_wait_ms": _numeric_distribution(media_queue_wait_ms),
            "media_lifecycle_elapsed_ms": _numeric_distribution(media_lifecycle_ms),
            "media_post_savant_finalization_elapsed_ms": _numeric_distribution(
                media_post_savant_finalization_ms
            ),
            "media_ffprobe_duration_ms": _numeric_distribution(media_ffprobe_ms),
            "media_ffmpeg_duration_ms": _numeric_distribution(media_ffmpeg_ms),
            "media_replay_to_sink_metadata_ms": _numeric_distribution(
                media_replay_to_sink_metadata_ms
            ),
            "media_sink_metadata_to_video_ms": _numeric_distribution(
                media_sink_metadata_to_video_ms
            ),
            "media_sink_video_to_stable_ms": _numeric_distribution(
                media_sink_video_to_stable_ms
            ),
            "media_sink_stable_to_ffprobe_ready_ms": _numeric_distribution(
                media_sink_stable_to_ffprobe_ready_ms
            ),
            "media_sink_ffprobe_ready_to_finalizer_start_ms": _numeric_distribution(
                media_sink_ffprobe_ready_to_finalizer_start_ms
            ),
            "media_finalizer_pool_wait_ms": _numeric_distribution(
                media_finalizer_pool_wait_ms
            ),
            "media_scheduler_tick_duration_ms": _numeric_distribution(
                media_scheduler_tick_duration_ms
            ),
            "media_scheduler_tick_gap_ms": _numeric_distribution(
                media_scheduler_tick_gap_ms
            ),
            "media_scheduler_remux_lane_depth": _numeric_distribution(
                media_scheduler_remux_lane_depth
            ),
            "media_scheduler_permit_active": _numeric_distribution(
                media_scheduler_permit_active
            ),
            "media_scheduler_permit_limit": _numeric_distribution(
                media_scheduler_permit_limit
            ),
            **{
                f"media_scheduler_{field}": _numeric_distribution(values)
                for field, values in media_scheduler_capacity_metrics.items()
            },
            **{
                f"media_resource_{field}": _numeric_distribution(values)
                for field, values in media_resource_capacity_metrics.items()
            },
            **{
                f"media_db_index_{field}": _numeric_distribution(values)
                for field, values in media_db_index_metrics.items()
            },
            "media_sidecar_prune_duration_ms": _numeric_distribution(
                media_sidecar_prune_duration_ms
            ),
            "media_scheduler_modes": _log_field_counts(
                text,
                marker="media_scheduler_tick",
                field="scheduler_mode",
            ),
            "media_segment_index_modes": _log_field_counts(
                text,
                marker="media_scheduler_tick",
                field="segment_index_mode",
            ),
            "clip_record_request_pending_ms": _numeric_distribution(
                clip_record_request_pending_ms
            ),
            "clip_proof_wait_ms": _numeric_distribution(clip_proof_wait_ms),
            "clip_replay_job_create_ms": _numeric_distribution(
                clip_replay_job_create_ms
            ),
            "clip_replay_slot_hold_ms": _numeric_distribution(
                clip_replay_slot_hold_ms
            ),
            "replay_slot_acquired": text.count("replay_slot_acquired"),
            "replay_slot_acquire_success": text.count("slot_acquired=True"),
            "replay_slot_released": text.count("replay_slot_released"),
            "replay_slot_release_reasons": _log_field_counts(
                text,
                marker="replay_slot_released",
                field="release_reason",
            ),
            "replay_slot_release_sink_video_to_stable_ms": _numeric_distribution(
                replay_slot_release_sink_stable_ms
            ),
            "replay_duration_seconds_effective": _numeric_distribution(
                replay_duration_seconds_effective
            ),
            "replay_slot_timeout_budget_s": _numeric_distribution(
                replay_slot_timeout_budget_s
            ),
            "media_throttle_sleep_s": _numeric_distribution(media_throttle_sleep_s),
            "media_deadline_slack_s": _numeric_distribution(media_deadline_slack_s),
            "media_finalizer_worker_counts": media_finalizer_metrics.get(
                "worker_counts",
                {},
            ),
            "media_finalizer_worker_count": media_finalizer_metrics.get(
                "worker_count",
                0,
            ),
            "media_queue_wait_ms_by_source": media_finalizer_metrics.get(
                "queue_wait_ms_by_source",
                {},
            ),
            "media_queue_wait_ms_by_shard": media_finalizer_metrics.get(
                "queue_wait_ms_by_shard",
                {},
            ),
            "media_claim_wait_ms": media_finalizer_metrics.get(
                "claim_wait_ms",
                _not_enough_data("media claim wait logs unavailable"),
            ),
            "media_duplicate_materialization_count": media_finalizer_metrics.get(
                "duplicate_materialization_count",
                0,
            ),
        }
    sink_instances: dict[str, dict[str, Any]] = {}
    for sink_instance, filename in VIDEO_FILE_SINK_LOG_PATHS.items():
        path = cfg.artifact_dir / filename
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        metrics = parse_video_file_sink_log(text, sink_instance=sink_instance)
        metrics["line_count"] = len(text.splitlines())
        metrics["log_path"] = str(path)
        sink_instances[sink_instance] = metrics
    summary["video_file_sink"] = {
        "instances": sink_instances,
        "aggregate": aggregate_video_file_sink_metrics(sink_instances),
    }
    return summary


def pressure_failure_reasons(
    cfg: PressureConfig,
    kept: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    db_before_cleanup: dict[str, Any] | None = None,
    event_quiescence: dict[str, Any] | None = None,
    post_sample_cleanup: dict[str, Any] | None = None,
) -> list[str]:
    reasons: list[str] = []
    sample_summary = diagnostics.get("sample_summary") or {}
    source_summary = diagnostics.get("source_containers") or {}
    republish_summary = diagnostics.get("rtsp_republishers") or {}
    log_summary = diagnostics.get("log_summary") or {}
    savant_logs = log_summary.get("savant") or {}
    mps_summary = diagnostics.get("cuda_mps") or {}
    roi_worker = diagnostics.get("adaface_roi_worker") or {}
    if not cfg.forwarder_null_sink and cfg.keep_evidence >= 0 and len(kept) < cfg.keep_evidence:
        reasons.append("insufficient_playable_evidence")
    if not cfg.forwarder_null_sink and cfg.keep_evidence < 0 and db_before_cleanup:
        event_count = int(db_before_cleanup.get("events") or 0)
        distinct_playable = int(
            db_before_cleanup.get("distinct_events_with_playable_evidence") or 0
        )
        blocking_count = int(db_before_cleanup.get("blocking_materialization_tasks") or 0)
        terminal_nonplayable = int(
            db_before_cleanup.get("distinct_events_with_terminal_nonplayable_outcome") or 0
        )
        if distinct_playable + terminal_nonplayable < event_count:
            reasons.append("event_outcomes_unaccounted")
        if blocking_count > 0:
            reasons.append("materialization_unresolved_present")
        expired_count = _summary_materialization_expired_count(db_before_cleanup)
        if expired_count:
            reasons.append("materialization_expired_present")
    if int(source_summary.get("exited") or 0) > cfg.max_exited_sources:
        reasons.append("source_containers_exited")
    if int(source_summary.get("restart_count_total") or 0) > 0:
        reasons.append("source_containers_restarted")
    if int(source_summary.get("negative_pts_error_total") or 0) > 0:
        reasons.append("source_adapter_negative_pts")
    if cfg.rolling_cache_evidence and cfg.keep_evidence >= 0:
        reasons.append("rolling_cache_acceptance_must_keep_all_evidence")
    if bool(republish_summary.get("enabled")):
        if int(republish_summary.get("exited") or 0) > 0:
            reasons.append("rtsp_republishers_exited")
        if int(republish_summary.get("connection_error_total") or 0) > 0:
            reasons.append("rtsp_republishers_connection_errors")
    if _sample_savant_send_failures_for_gate(sample_summary) > cfg.max_send_failures:
        reasons.append("savant_send_failures")
    if cfg.rolling_cache_evidence:
        raw_logs = log_summary.get("replay_raw_fanout") or {}
        if (
            int(sample_summary.get("max_raw_forwarder_frames_dropped_total") or 0)
            > 0
            or int(sample_summary.get("max_raw_forwarder_send_failures_total") or 0)
            > 0
            or int(raw_logs.get("raw_branch_queue_full") or 0) > 0
            or int(raw_logs.get("raw_branch_send_failed") or 0) > 0
        ):
            reasons.append("rolling_cache_raw_fanout_loss")
    if cfg.cuda_mps and (
        not bool(mps_summary.get("ready"))
        or not (mps_summary.get("server_list") or [])
    ):
        reasons.append("cuda_mps_client_unavailable")
    if int(sample_summary.get("steady_effective_fps_sample_count") or 0) <= 0:
        reasons.append("steady_effective_fps_unmeasured")
    elif not bool(sample_summary.get("steady_effective_fps_meets_minimum")):
        reasons.append("steady_effective_fps_below_minimum")
    if (
        cfg.keep_evidence > 0
        and not cfg.forwarder_null_sink
        and int(sample_summary.get("max_savant_sources") or 0) >= cfg.stream_count
    ):
        if int(sample_summary.get("final_savant_pose_objects_total") or 0) <= 0:
            reasons.append("savant_pose_objects_zero")
        if int(sample_summary.get("final_savant_person_observations_exported_total") or 0) <= 0:
            reasons.append("savant_person_observations_zero")
        if int(sample_summary.get("final_savant_face_observations_exported_total") or 0) <= 0:
            reasons.append("savant_face_observations_zero")
    if (
        cfg.savant_ablation_stage
        in {"pose-face-adaface", "full-exporter", "full-evidence"}
        and not cfg.adaface_roi_redis
        and int(sample_summary.get("max_savant_sources") or 0) >= cfg.stream_count
        and int(sample_summary.get("final_savant_adaface_embeddings_total") or 0) <= 0
    ):
        reasons.append("savant_adaface_embeddings_zero")
    if cfg.adaface_decoupled:
        if (
            int(sample_summary.get("max_adaface_forwarder_sources") or 0)
            < cfg.stream_count
        ):
            reasons.append("adaface_forwarder_did_not_see_all_sources")
        if (
            len(
                sample_summary.get(
                    "final_adaface_central_missing_eligible_source_ids"
                )
                or []
            )
            > 0
        ):
            reasons.append("adaface_central_missed_eligible_sources")
        if (
            float(
                sample_summary.get("max_adaface_forwarder_queue_depth") or 0
            )
            >= 512
        ):
            reasons.append("adaface_forwarder_queue_full")
        if _adaface_forwarder_send_failure_ratio(sample_summary) > (
            cfg.max_adaface_forwarder_send_failure_ratio
        ):
            reasons.append("adaface_forwarder_send_failures")
    if cfg.adaface_roi_redis:
        roi_db = (db_before_cleanup or {}).get("adaface_roi") or {}
        if not bool(roi_worker.get("reachable")):
            reasons.append("adaface_roi_worker_unreachable")
        metrics = roi_worker.get("metrics") or {}
        published = _prometheus_labeled_total(
            metrics, "va_adaface_roi_messages_total", 'outcome="published"'
        )
        invalid = _prometheus_labeled_total(
            metrics, "va_adaface_roi_messages_total", 'outcome="invalid"'
        )
        expired = _prometheus_labeled_total(
            metrics, "va_adaface_roi_messages_total", 'outcome="expired"'
        )
        if published <= 0:
            reasons.append("adaface_roi_embeddings_zero")
        total_outcomes = published + invalid + expired
        if total_outcomes > 0 and (invalid + expired) / total_outcomes > 0.005:
            reasons.append("adaface_roi_stream_loss_exceeded")
        pending = _prometheus_labeled_total(
            metrics, "va_adaface_roi_pending"
        )
        if pending > 0:
            reasons.append("adaface_roi_pending_present")
        cleanup_errors = _prometheus_labeled_total(
            metrics,
            "va_adaface_roi_stream_cleanup_total",
            'outcome="ack_error"',
        ) + _prometheus_labeled_total(
            metrics,
            "va_adaface_roi_stream_cleanup_total",
            'outcome="delete_error"',
        )
        if cleanup_errors > 0:
            reasons.append("adaface_roi_stream_cleanup_errors")
        if int(roi_db.get("source_count") or 0) < cfg.stream_count:
            reasons.append("adaface_roi_did_not_cover_all_sources")
        event_types = {
            str(item.get("event_type") or ""): int(item.get("count") or 0)
            for item in ((db_before_cleanup or {}).get("event_types") or [])
        }
        watchlist_emitted_in_logs = int(
            (log_summary.get("face_worker") or {}).get(
                "face_worker_watchlist_emitted_max"
            )
            or 0
        )
        if event_types.get("watchlist_hit", 0) <= 0 and (
            cfg.savant_ablation_stage == "full-evidence"
            or watchlist_emitted_in_logs <= 0
        ):
            reasons.append("adaface_roi_watchlist_events_zero")
        enqueued = int(savant_logs.get("face_roi_enqueued_max") or 0)
        dropped = int(savant_logs.get("face_roi_queue_dropped_max") or 0)
        if enqueued + dropped > 0 and dropped / (enqueued + dropped) > 0.005:
            reasons.append("adaface_roi_producer_loss_exceeded")
    if int(savant_logs.get("frame_annotation_redis_write_error") or 0) > 0:
        reasons.append("frame_annotation_redis_write_errors")
    if int(sample_summary.get("max_forwarder_sources") or 0) < cfg.stream_count:
        reasons.append("forwarder_did_not_see_all_sources")
    if int(sample_summary.get("max_forwarder_sources") or 0) > cfg.stream_count:
        reasons.append("forwarder_saw_extra_sources")
    if int(sample_summary.get("queue_full_samples") or 0) > 0:
        reasons.append("forwarder_queue_full")
    if float(sample_summary.get("max_forwarder_queue_depth") or 0.0) > 0.0:
        reasons.append("forwarder_queue_nonzero")
    if not cfg.forwarder_null_sink and int(sample_summary.get("max_savant_sources") or 0) < cfg.stream_count:
        reasons.append("savant_did_not_see_all_sources")
    if not cfg.forwarder_null_sink and int(sample_summary.get("max_savant_sources") or 0) > cfg.stream_count:
        reasons.append("savant_saw_extra_sources")
    if _pressure_observed_window_exceeded(
        cfg,
        event_quiescence,
        post_sample_cleanup=post_sample_cleanup,
    ):
        reasons.append("pressure_observed_window_exceeded")
    if not cfg.forwarder_null_sink and _validate_seq_iq_is_failure(cfg, diagnostics, reasons):
        reasons.append("validate_seq_iq_exceeded")
    return reasons


def _prometheus_labeled_total(
    metrics: dict[str, Any], metric_name: str, label_fragment: str = ""
) -> float:
    total = 0.0
    for key, value in metrics.items():
        text = str(key)
        if not (text == metric_name or text.startswith(metric_name + "{")):
            continue
        if label_fragment and label_fragment not in text:
            continue
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return total


def rolling_cache_full_rate_gate(
    cfg: PressureConfig,
    segment_visibility: dict[str, Any],
    sink_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_fps = (
        _float_or_none((sink_summary or {}).get("rolling_cache_expected_raw_fps"))
        if isinstance(sink_summary, dict)
        else None
    )
    if expected_fps is None:
        expected_fps = _fps_to_float(cfg.fps) or 0.0
    min_fps = expected_fps * ROLLING_CACHE_FULL_RATE_MIN_RATIO
    rate = segment_visibility.get("segment_frame_rate_fps") or {}
    p50 = _float_or_none(rate.get("p50")) if isinstance(rate, dict) else None
    source_count = int(segment_visibility.get("source_count") or 0)
    segments_measured = int(segment_visibility.get("segments_measured") or 0)
    return {
        "expected_fps": expected_fps,
        "expected_fps_source": "rolling_cache_raw_input"
        if isinstance(sink_summary, dict)
        and sink_summary.get("rolling_cache_expected_raw_fps") is not None
        else "analysis_fps_fallback",
        "min_full_rate_fps": min_fps,
        "min_full_rate_ratio": ROLLING_CACHE_FULL_RATE_MIN_RATIO,
        "observed_segment_frame_rate_p50": p50,
        "segments_measured": segments_measured,
        "source_count": source_count,
        "stream_count": cfg.stream_count,
        "full_rate_ok": bool(
            segments_measured > 0
            and source_count >= cfg.stream_count
            and expected_fps > 0
            and p50 is not None
            and p50 >= min_fps
        ),
    }


def rolling_cache_full_rate_failure_reasons(gate: dict[str, Any]) -> list[str]:
    if not gate:
        return ["rolling_cache_full_rate_unmeasured"]
    reasons: list[str] = []
    if int(gate.get("segments_measured") or 0) <= 0:
        reasons.append("rolling_cache_segments_unmeasured")
    if int(gate.get("source_count") or 0) < int(gate.get("stream_count") or 0):
        reasons.append("rolling_cache_missing_source_segments")
    if not bool(gate.get("full_rate_ok")):
        reasons.append("rolling_cache_segment_fps_below_full_rate")
    return sorted(set(reasons))


def evidence_window_validation_summary(
    cfg: PressureConfig,
    kept: list[dict[str, Any]],
) -> dict[str, Any]:
    allowed_windows = {
        (int(pre_seconds), int(post_seconds))
        for pre_seconds, post_seconds in cfg.evidence_policy_groups
    }
    video_rows = [
        row for row in kept
        if not bool(row.get("image_evidence"))
    ]
    unexpected_windows: list[dict[str, Any]] = []
    duration_mismatches: list[dict[str, Any]] = []
    missing_policy: list[dict[str, Any]] = []
    missing_duration: list[dict[str, Any]] = []
    by_window: dict[str, int] = {}

    for row in video_rows:
        event_id = str(row.get("event_id") or "")
        pair = _evidence_policy_pair_from_row(row)
        if pair is None:
            missing_policy.append({"event_id": event_id})
            continue
        pre_seconds, post_seconds = pair
        window_key = f"{pre_seconds}:{post_seconds}"
        by_window[window_key] = by_window.get(window_key, 0) + 1
        if pair not in allowed_windows:
            unexpected_windows.append(
                {
                    "event_id": event_id,
                    "source_id": row.get("source_id"),
                    "pre_seconds": pre_seconds,
                    "post_seconds": post_seconds,
                }
            )
        duration = _float_or_none(row.get("actual_raw_clip_duration_seconds"))
        duration_source = "ffprobe"
        if duration is None:
            duration = _float_or_none(row.get("raw_clip_duration_seconds"))
            duration_source = "database"
        if duration is None:
            missing_duration.append({"event_id": event_id, "window": window_key})
            continue
        expected_total = float(pre_seconds + post_seconds)
        delta = abs(duration - expected_total)
        if delta > EVIDENCE_WINDOW_DURATION_TOLERANCE_S:
            duration_mismatches.append(
                {
                    "event_id": event_id,
                    "source_id": row.get("source_id"),
                    "window": window_key,
                    "expected_duration_s": expected_total,
                    "observed_duration_s": duration,
                    "duration_source": duration_source,
                    "delta_s": delta,
                }
            )

    failures = {
        "unexpected_window_count": len(unexpected_windows),
        "duration_mismatch_count": len(duration_mismatches),
        "missing_policy_count": len(missing_policy),
        "missing_duration_count": len(missing_duration),
    }
    return {
        "status": "passed" if not any(failures.values()) else "failed",
        "video_count": len(video_rows),
        "allowed_windows": [
            f"{pre_seconds}:{post_seconds}"
            for pre_seconds, post_seconds in sorted(allowed_windows)
        ],
        "duration_tolerance_s": EVIDENCE_WINDOW_DURATION_TOLERANCE_S,
        "by_window": dict(sorted(by_window.items())),
        "failures": failures,
        "unexpected_windows": unexpected_windows[:20],
        "duration_mismatches": duration_mismatches[:20],
        "missing_policy": missing_policy[:20],
        "missing_duration": missing_duration[:20],
    }


def evidence_window_failure_reasons(summary: dict[str, Any]) -> list[str]:
    failures = summary.get("failures") if isinstance(summary, dict) else {}
    if not isinstance(failures, dict):
        return ["evidence_window_validation_unmeasured"]
    reasons: list[str] = []
    if int(failures.get("unexpected_window_count") or 0) > 0:
        reasons.append("evidence_unexpected_window_policy")
    if int(failures.get("duration_mismatch_count") or 0) > 0:
        reasons.append("evidence_clip_duration_mismatch")
    if int(failures.get("missing_policy_count") or 0) > 0:
        reasons.append("evidence_window_policy_missing")
    if int(failures.get("missing_duration_count") or 0) > 0:
        reasons.append("evidence_clip_duration_missing")
    return reasons


def _evidence_policy_pair_from_row(row: dict[str, Any]) -> tuple[int, int] | None:
    for pre_key, post_key in (
        ("expected_pre_seconds", "expected_post_seconds"),
        ("rule_pre_seconds", "rule_post_seconds"),
    ):
        pre_seconds = _int_or_none(row.get(pre_key))
        post_seconds = _int_or_none(row.get(post_key))
        if (
            pre_seconds is not None
            and post_seconds is not None
            and pre_seconds >= 0
            and post_seconds >= 0
        ):
            return pre_seconds, post_seconds
    return None


def evidence_annotation_failure_reasons(evidence_8090: dict[str, Any]) -> list[str]:
    retained = int(evidence_8090.get("retained_count") or 0)
    checked = int(evidence_8090.get("annotation_checked_count") or 0)
    ok = int(evidence_8090.get("annotation_ok_count") or 0)
    skipped = int(evidence_8090.get("annotation_skipped_zero_expected_count") or 0)
    if retained <= 0:
        return []
    reasons: list[str] = []
    if checked + skipped < retained:
        reasons.append("evidence_8090_annotations_unchecked_or_zero")
    if ok < checked:
        reasons.append("evidence_8090_annotations_incomplete")
    if int(evidence_8090.get("annotation_fallback_count") or 0) > 0:
        reasons.append("evidence_8090_annotations_filesystem_fallback")
    if int(evidence_8090.get("annotation_bbox_missing_count") or 0) > 0:
        reasons.append("evidence_8090_annotation_bbox_missing")
    if int(evidence_8090.get("annotation_person_context_missing_count") or 0) > 0:
        reasons.append("evidence_8090_person_context_missing")
    if int(evidence_8090.get("timeline_missing_count") or 0) > 0:
        reasons.append("evidence_8090_timeline_missing")
    if int(evidence_8090.get("timeline_fallback_count") or 0) > 0:
        reasons.append("evidence_8090_timeline_filesystem_fallback")
    return reasons


def _pressure_observed_window_exceeded(
    cfg: PressureConfig,
    event_quiescence: dict[str, Any] | None,
    *,
    post_sample_cleanup: dict[str, Any] | None = None,
) -> bool:
    db_ingest = None
    if isinstance(post_sample_cleanup, dict):
        candidate = post_sample_cleanup.get("post_cleanup_db_ingest")
        if isinstance(candidate, dict):
            db_ingest = candidate
    if db_ingest is None and event_quiescence:
        snapshot = event_quiescence.get("last_snapshot") or {}
        candidate = snapshot.get("db_ingest")
        if isinstance(candidate, dict):
            db_ingest = candidate
    if db_ingest is None:
        return False
    max_allowed_s = cfg.duration_s + PRESSURE_OBSERVED_WINDOW_SLACK_S
    try:
        event_ts_span_s = float(db_ingest.get("event_ts_span_s") or 0)
    except (TypeError, ValueError):
        event_ts_span_s = 0.0
    if event_ts_span_s > max_allowed_s:
        return True
    return False


def pressure_warnings(
    cfg: PressureConfig,
    diagnostics: dict[str, Any],
    failure_reasons: list[str],
) -> list[str]:
    warnings: list[str] = []
    log_summary = diagnostics.get("log_summary") or {}
    savant_logs = log_summary.get("savant") or {}
    validate_count = int(savant_logs.get("validate_seq_iq") or 0)
    if (
        not cfg.forwarder_null_sink
        and
        validate_count > cfg.max_validate_seq_iq
        and "validate_seq_iq_exceeded" not in failure_reasons
    ):
        warnings.append("validate_seq_iq_expected_sampling_gap")
    return warnings


def _validate_seq_iq_is_failure(
    cfg: PressureConfig,
    diagnostics: dict[str, Any],
    current_reasons: list[str],
) -> bool:
    log_summary = diagnostics.get("log_summary") or {}
    savant_logs = log_summary.get("savant") or {}
    validate_count = int(savant_logs.get("validate_seq_iq") or 0)
    if validate_count <= cfg.max_validate_seq_iq:
        return False

    sample_summary = diagnostics.get("sample_summary") or {}
    source_summary = diagnostics.get("source_containers") or {}
    ingress_unhealthy = (
        _sample_savant_send_failures_for_gate(sample_summary) > cfg.max_send_failures
        or int(sample_summary.get("queue_full_samples") or 0)
        > 0
        or int(source_summary.get("exited") or 0) > cfg.max_exited_sources
        or int(source_summary.get("restart_count_total") or 0) > 0
        or int(source_summary.get("negative_pts_error_total") or 0) > 0
    )
    if ingress_unhealthy:
        return True
    source_visibility_failed = {
        "forwarder_did_not_see_all_sources",
        "savant_did_not_see_all_sources",
    }
    return bool(source_visibility_failed.intersection(current_reasons))


def _sample_savant_send_failures_for_gate(sample_summary: dict[str, Any]) -> int:
    if "max_savant_send_failures_delta" in sample_summary:
        return int(sample_summary.get("max_savant_send_failures_delta") or 0)
    return int(sample_summary.get("max_savant_send_failures_total") or 0)


def _adaface_forwarder_send_failure_ratio(
    sample_summary: dict[str, Any],
) -> float:
    if "final_adaface_forwarder_send_failure_ratio" in sample_summary:
        return float(
            sample_summary.get("final_adaface_forwarder_send_failure_ratio")
            or 0.0
        )
    forwarded = int(
        sample_summary.get("final_adaface_forwarder_frames_forwarded_total")
        or 0
    )
    failures = int(
        sample_summary.get("final_adaface_forwarder_send_failures_total") or 0
    )
    attempted = forwarded + failures
    return failures / attempted if attempted > 0 else 0.0


def select_kept_evidence(conn, cfg: PressureConfig) -> list[dict[str, Any]]:
    limit_sql = "" if cfg.keep_evidence < 0 else "LIMIT %(limit)s"
    params: dict[str, Any] = {
        "prefix": f"{cfg.run_id}_%",
        "sampling_start_event_ts_ms": cfg.pressure_sampling_start_event_ts_ms,
    }
    if cfg.keep_evidence >= 0:
        params["limit"] = cfg.keep_evidence
    rows = conn.execute(
        f"""
        SELECT eb.event_id, eb.source_id, eb.camera_id, eb.camera_name, eb.event_type,
               eb.evidence_state, eb.media_status, eb.raw_clip_uri, eb.raw_clip_size_bytes,
               eb.summary->>'playback_kind' AS playback_kind,
               face_crop.uri AS face_crop_uri,
               full_frame.uri AS full_frame_uri,
               annotated_frame.uri AS annotated_frame_uri,
               eb.annotation_count,
               eb.raw_clip_duration_seconds, eb.created_at, e.created_at AS event_created_at,
               e.payload, NULL::jsonb AS camera_metadata,
               rule_policy.evidence_policy AS rule_evidence_policy
        FROM evidence_bundles eb
        JOIN events e ON e.id = eb.event_id
        LEFT JOIN cameras c ON c.id::text = eb.camera_id
        LEFT JOIN evidence_artifacts face_crop
          ON face_crop.event_id = eb.event_id
         AND face_crop.artifact_type = 'face_crop'
        LEFT JOIN evidence_artifacts full_frame
          ON full_frame.event_id = eb.event_id
         AND full_frame.artifact_type = 'full_frame'
        LEFT JOIN evidence_artifacts annotated_frame
          ON annotated_frame.event_id = eb.event_id
         AND annotated_frame.artifact_type = 'annotated_frame'
        LEFT JOIN LATERAL (
            SELECT cr.evidence_policy
            FROM camera_rules cr
            WHERE cr.camera_id::text = eb.camera_id
              AND (
                  cr.rule_type = eb.event_type
                  OR cr.algorithm_id = eb.event_type
                  OR (
                      eb.event_type = 'watchlist_hit'
                      AND cr.algorithm_id = 'face.watchlist'
                  )
                  OR (
                      eb.event_type = 'intrusion'
                      AND cr.algorithm_id = 'behavior.intrusion'
                  )
              )
            ORDER BY cr.updated_at DESC NULLS LAST, cr.created_at DESC NULLS LAST
            LIMIT 1
        ) rule_policy ON true
        WHERE eb.source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e")}
          AND (
              (
                eb.raw_clip_uri IS NOT NULL
                AND COALESCE(eb.raw_clip_size_bytes, 0) > 0
              )
              OR COALESCE(eb.media_status, eb.evidence_state, '') = 'image_ready'
              OR COALESCE(eb.summary->>'playback_kind', '') = 'image'
          )
        ORDER BY random()
        {limit_sql}
        """,
        params,
    ).fetchall()
    kept = []
    for row in rows:
        item = _row_json(row)
        raw_clip_path = raw_clip_uri_to_path(cfg.evidence_root, str(item.get("raw_clip_uri") or ""))
        item["raw_clip_path"] = str(raw_clip_path) if raw_clip_path else ""
        item["raw_clip_exists"] = bool(raw_clip_path and raw_clip_path.is_file())
        item["actual_raw_clip_duration_seconds"] = (
            probe_local_video_duration_seconds(raw_clip_path)
            if raw_clip_path and raw_clip_path.is_file()
            else None
        )
        item["image_evidence"] = (
            item.get("playback_kind") == "image"
            or item.get("media_status") == "image_ready"
        )
        item["image_artifact_exists"] = any(
            bool(str(item.get(key) or "").strip())
            for key in ("face_crop_uri", "full_frame_uri", "annotated_frame_uri")
        )
        camera_metadata = item.get("camera_metadata")
        if isinstance(camera_metadata, dict):
            item["pressure_group_index"] = camera_metadata.get("pressure_group_index")
            item["expected_pre_seconds"] = camera_metadata.get(
                "pressure_evidence_pre_seconds"
            )
            item["expected_post_seconds"] = camera_metadata.get(
                "pressure_evidence_post_seconds"
            )
            item["expected_total_seconds"] = camera_metadata.get(
                "pressure_evidence_total_seconds"
            )
        rule_policy = item.get("rule_evidence_policy")
        if isinstance(rule_policy, dict):
            item["rule_pre_seconds"] = rule_policy.get("pre_seconds")
            item["rule_post_seconds"] = rule_policy.get("post_seconds")
        kept.append(item)
    kept = [
        row
        for row in kept
        if row["raw_clip_exists"] or bool(row.get("image_evidence"))
    ]
    return kept if cfg.keep_evidence < 0 else kept[: cfg.keep_evidence]


def select_covered_event_ids_for_bundles(
    conn,
    cfg: PressureConfig,
    bundle_event_ids: set[str],
) -> set[str]:
    if not bundle_event_ids:
        return set()
    rows = conn.execute(
        f"""
        SELECT l.event_id
        FROM evidence_event_links l
        JOIN events e ON e.id = l.event_id
        WHERE l.bundle_event_id = ANY(%(bundle_event_ids)s::uuid[])
          AND l.relation = 'covered_by'
          AND e.source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e")}
        """,
        {
            "bundle_event_ids": list(bundle_event_ids),
            "prefix": f"{cfg.run_id}_%",
            "sampling_start_event_ts_ms": cfg.pressure_sampling_start_event_ts_ms,
        },
    ).fetchall()
    return {str(row["event_id"]) for row in rows}


def write_kept_csv(cfg: PressureConfig, kept: list[dict[str, Any]]) -> None:
    path = cfg.artifact_dir / "kept_50_evidence.csv"
    fields = [
        "event_id",
        "source_id",
        "camera_id",
        "camera_name",
        "event_type",
        "evidence_state",
        "media_status",
        "pressure_group_index",
        "expected_pre_seconds",
        "expected_post_seconds",
        "expected_total_seconds",
        "rule_pre_seconds",
        "rule_post_seconds",
        "raw_clip_uri",
        "raw_clip_path",
        "raw_clip_size_bytes",
        "raw_clip_duration_seconds",
        "actual_raw_clip_duration_seconds",
        "created_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in kept:
            writer.writerow({field: row.get(field, "") for field in fields})


def cleanup_pressure_data(
    conn,
    redis_client: Redis,
    cfg: PressureConfig,
    *,
    keep_event_ids: set[str],
    runtime_epoch_root: str | None,
) -> dict[str, Any]:
    prefix = f"{cfg.run_id}_%"
    deleted_event_ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM events WHERE source_id LIKE %s AND NOT (id = ANY(%s::uuid[]))",
            (prefix, list(keep_event_ids)),
        ).fetchall()
    ]
    kept_source_observation_ids = [
        str(row["source_observation_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT e.payload->'match'->>'source_observation_id' AS source_observation_id
            FROM events e
            WHERE e.id = ANY(%s::uuid[])
              AND COALESCE(e.payload->'match'->>'source_observation_id', '') <> ''
            """,
            (list(keep_event_ids),),
        ).fetchall()
    ]
    run_event_ids = sorted(set(deleted_event_ids) | set(keep_event_ids))
    cleanup: dict[str, Any] = {"deleted_event_count": len(deleted_event_ids)}
    for event_id in deleted_event_ids:
        shutil.rmtree(cfg.evidence_root / event_id, ignore_errors=True)
    with conn.transaction():
        cleanup["deleted_face_observations"] = conn.execute(
            """
            DELETE FROM face_observations
            WHERE source_id LIKE %s
              AND NOT (source_observation_id = ANY(%s::text[]))
            """,
            (prefix, kept_source_observation_ids),
        ).rowcount
        cleanup["deleted_person_bbox_observations"] = conn.execute(
            "DELETE FROM person_bbox_observations WHERE source_id LIKE %s", (prefix,)
        ).rowcount
        cleanup["deleted_events"] = conn.execute(
            "DELETE FROM events WHERE source_id LIKE %s AND NOT (id = ANY(%s::uuid[]))",
            (prefix, list(keep_event_ids)),
        ).rowcount
        cleanup["disabled_cameras"] = conn.execute(
            "UPDATE cameras SET enabled=false, updated_at=now() WHERE source_id LIKE %s",
            (prefix,),
        ).rowcount
        cleanup["deleted_rules"] = 0
        cleanup["deleted_zones"] = 0
        cleanup["deleted_cameras"] = 0
        cleanup["camera_configs_retained"] = True
    cleanup["runtime_sources_apply_after_camera_cleanup"] = apply_sources_only(
        cfg,
        "runtime_sources_apply_after_pressure_cleanup.json",
    )
    cleanup["redis_deleted"] = cleanup_redis_streams(redis_client, cfg.run_id)
    cleanup["removed_source_containers"] = remove_pressure_source_containers(cfg.run_id)
    cleanup["removed_rolling_cache"] = remove_pressure_rolling_cache_artifacts(
        conn,
        cfg,
        event_ids=run_event_ids,
    )
    if runtime_epoch_root:
        epoch_path = Path(runtime_epoch_root)
        try:
            epoch_path.relative_to(cfg.replay_epoch_root)
            is_under_epoch_root = True
        except ValueError:
            is_under_epoch_root = False
        if epoch_path.is_dir() and is_under_epoch_root:
            shutil.rmtree(epoch_path, ignore_errors=True)
            cleanup["removed_runtime_epoch_root"] = str(epoch_path)
    return cleanup


def cleanup_pressure_runtime_only(
    conn,
    redis_client: Redis,
    cfg: PressureConfig,
) -> dict[str, Any]:
    prefix = f"{cfg.run_id}_%"
    cleanup: dict[str, Any] = {}
    with conn.transaction():
        cleanup["disabled_cameras"] = conn.execute(
            "UPDATE cameras SET enabled=false, updated_at=now() WHERE source_id LIKE %s",
            (prefix,),
        ).rowcount
    cleanup.update(
        {
            "deleted_rules": 0,
            "deleted_zones": 0,
            "deleted_cameras": 0,
            "deleted_events": 0,
            "deleted_face_observations": 0,
            "deleted_person_bbox_observations": 0,
            "removed_evidence_paths": 0,
            "visual_results_retained": True,
        }
    )
    cleanup["runtime_sources_apply_after_camera_cleanup"] = apply_sources_only(
        cfg,
        "runtime_sources_apply_after_pressure_runtime_cleanup.json",
    )
    cleanup["redis_deleted"] = cleanup_redis_streams(redis_client, cfg.run_id)
    cleanup["removed_source_containers"] = remove_pressure_source_containers(cfg.run_id)
    cleanup["removed_rolling_cache"] = remove_pressure_rolling_cache_artifacts(
        conn,
        cfg,
    )
    return cleanup


def pressure_rolling_cache_root_host() -> Path:
    override = os.environ.get("PRESSURE_ROLLING_CACHE_ROOT_HOST")
    if override:
        return Path(override)
    resolved = resolve_container_bind_source(
        (
            "video-analytics-midterm-rolling-cache-sink-a",
            "video-analytics-midterm-rolling-cache-sink-b",
            "video-analytics-midterm-rolling-cache-sink",
        ),
        "/media/rolling-cache",
    )
    return resolved or Path("/data/video-analytics/media/rolling-cache")


def pressure_rolling_cache_materialized_root_host() -> Path:
    override = os.environ.get("PRESSURE_ROLLING_CACHE_MATERIALIZED_ROOT_HOST")
    if override:
        return Path(override)
    resolved = resolve_container_bind_source(
        ("video-analytics-midterm-media-worker",),
        "/media/rolling-cache-materialized",
    )
    return resolved or Path("/data/video-analytics/media/rolling-cache-materialized")


def resolve_container_bind_source(
    container_names: tuple[str, ...], destination: str
) -> Path | None:
    """Resolve a container path to its host bind source.

    Prefer the most-specific mount so production's fast-disk nested bind wins
    over the broad ``/data/.../media:/media`` evidence mount.
    """
    target = Path(destination)
    for container_name in container_names:
        completed = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .Mounts}}"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            continue
        try:
            mounts = json.loads(completed.stdout)
        except json.JSONDecodeError:
            continue
        candidates: list[tuple[int, Path]] = []
        for mount in mounts if isinstance(mounts, list) else []:
            mount_destination = Path(str(mount.get("Destination") or ""))
            source = str(mount.get("Source") or "")
            if not source:
                continue
            try:
                relative = target.relative_to(mount_destination)
            except ValueError:
                continue
            candidates.append(
                (len(mount_destination.parts), Path(source).joinpath(relative))
            )
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
    return None


def resolve_host_path_in_container(
    host_path: Path,
    *,
    container_names: tuple[str, ...] = (
        "video-analytics-midterm-media-worker",
        "video-analytics-midterm-rolling-cache-sink-a",
        "video-analytics-midterm-rolling-cache-sink-b",
    ),
) -> tuple[str, Path] | None:
    """Map a host bind path to a writable path inside a running container."""
    target = host_path.resolve(strict=False)
    for container_name in container_names:
        completed = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .Mounts}}"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            continue
        try:
            mounts = json.loads(completed.stdout)
        except json.JSONDecodeError:
            continue
        candidates: list[tuple[int, Path]] = []
        for mount in mounts if isinstance(mounts, list) else []:
            if not bool(mount.get("RW")):
                continue
            source = str(mount.get("Source") or "")
            destination = str(mount.get("Destination") or "")
            if not source or not destination:
                continue
            source_path = Path(source).resolve(strict=False)
            try:
                relative = target.relative_to(source_path)
            except ValueError:
                continue
            candidates.append(
                (len(source_path.parts), Path(destination).joinpath(relative))
            )
        if candidates:
            return container_name, max(candidates, key=lambda item: item[0])[1]
    return None


def remove_host_path_via_container(path: Path) -> tuple[bool, str | None]:
    mapping = resolve_host_path_in_container(path)
    if mapping is None:
        return False, "no_writable_container_bind"
    container_name, container_path = mapping
    completed = subprocess.run(
        ["docker", "exec", container_name, "rm", "-rf", "--", str(container_path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return False, (
            f"docker_exec_rc={completed.returncode} "
            f"stderr={completed.stderr.strip()[:500]}"
        )
    if path.exists():
        return False, "path_still_exists_after_container_delete"
    return True, None


def clear_directory_contents_strict(path: Path) -> dict[str, Any]:
    """Remove every child and fail if root-owned container output survives."""
    path.mkdir(parents=True, exist_ok=True)
    discovered = list(path.iterdir())
    host_failures: list[dict[str, str]] = []
    for child in discovered:
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError as exc:
            host_failures.append(
                {"path": str(child), "error": f"{type(exc).__name__}: {exc}"}
            )

    remaining = list(path.iterdir())
    container_fallback: dict[str, Any] = {"used": False}
    if remaining:
        mapping = resolve_host_path_in_container(path)
        if mapping is not None:
            container_name, container_path = mapping
            completed = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "sh",
                    "-c",
                    (
                        'for child in "$1"/* "$1"/.[!.]* "$1"/..?*; do '
                        '[ -e "$child" ] || continue; rm -rf -- "$child"; done'
                    ),
                    "sh",
                    str(container_path),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            container_fallback = {
                "used": True,
                "container": container_name,
                "container_path": str(container_path),
                "returncode": completed.returncode,
                "stderr": completed.stderr.strip()[:1000],
            }
        else:
            container_fallback = {
                "used": True,
                "error": "no_writable_container_bind",
            }

    remaining = list(path.iterdir())
    if remaining:
        raise RuntimeError(
            f"directory cleanup incomplete path={path} "
            f"remaining={len(remaining)} samples={[item.name for item in remaining[:20]]}"
        )
    return {
        "discovered_paths": len(discovered),
        "removed_paths": len(discovered),
        "remaining_paths": 0,
        "host_remove_failures": len(host_failures),
        "host_remove_failure_samples": host_failures[:20],
        "container_fallback": container_fallback,
    }


def evidence_database_counts(conn) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM events) AS events,
          (SELECT count(*) FROM evidence_tasks) AS evidence_tasks,
          (SELECT count(*) FROM evidence_bundles) AS evidence_bundles,
          (SELECT count(*) FROM evidence_artifacts) AS evidence_artifacts,
          (SELECT count(*) FROM evidence_frame_timeline) AS evidence_frame_timeline,
          (SELECT count(*) FROM evidence_overlay_segments) AS evidence_overlay_segments,
          (SELECT count(*) FROM evidence_event_links) AS evidence_event_links,
          (SELECT count(*) FROM face_observations) AS face_observations,
          (SELECT count(*) FROM person_bbox_observations)
            AS person_bbox_observations
        """
    ).fetchone() or {}
    return {key: int(value or 0) for key, value in row.items()}


def clear_existing_evidence_state(
    conn,
    *,
    evidence_root: Path = Path("/data/video-analytics/media/evidence"),
) -> dict[str, Any]:
    """Clear evidence while preserving observations, people, and trajectories."""
    cleanup: dict[str, Any] = {}
    cleanup["database_counts_before"] = evidence_database_counts(conn)
    evidence_cleanup = clear_directory_contents_strict(evidence_root)
    cleanup["removed_evidence_paths"] = evidence_cleanup["removed_paths"]
    cleanup["remaining_evidence_paths"] = evidence_cleanup["remaining_paths"]
    cleanup["evidence_filesystem_cleanup"] = evidence_cleanup

    for key, root in (
        ("removed_rolling_cache_root", pressure_rolling_cache_root_host()),
        (
            "removed_rolling_cache_materialized_root",
            pressure_rolling_cache_materialized_root_host(),
        ),
    ):
        root_cleanup = clear_directory_contents_strict(root)
        cleanup[key] = bool(root_cleanup["removed_paths"])
        cleanup[f"{key}_details"] = root_cleanup

    with conn.transaction():
        cleanup["deleted_events"] = conn.execute("DELETE FROM events").rowcount
    cleanup["deleted_face_observations"] = 0
    cleanup["deleted_person_bbox_observations"] = 0
    cleanup["database_counts_after"] = evidence_database_counts(conn)
    cleanup["trajectory_data_preserved"] = True
    return cleanup


def remove_pressure_rolling_cache_artifacts(
    conn,
    cfg: PressureConfig,
    *,
    event_ids: list[str] | None = None,
    materialized_root: Path | None = None,
) -> dict[str, Any]:
    source_prefix = f"{cfg.run_id}_"
    removed_source_dirs = 0
    root = pressure_rolling_cache_root_host()
    for path in list(root.rglob("*")) if root.exists() else []:
        if not path.is_dir():
            continue
        name = path.name.rstrip("%")
        if name.startswith(source_prefix):
            shutil.rmtree(path, ignore_errors=True)
            removed_source_dirs += 1

    discovered_event_ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM events WHERE source_id LIKE %s",
            (f"{cfg.run_id}_%",),
        ).fetchall()
    ]
    materialized_event_ids = sorted(set(event_ids or []) | set(discovered_event_ids))
    removed_materialized_dirs = 0
    materialized_root = (
        pressure_rolling_cache_materialized_root_host()
        if materialized_root is None
        else materialized_root
    )
    removed_paths: set[Path] = set()
    if materialized_root.exists():
        for event_id in materialized_event_ids:
            for path in materialized_root.rglob(event_id):
                resolved = path.resolve(strict=False)
                if path.is_dir() and resolved not in removed_paths:
                    shutil.rmtree(path, ignore_errors=True)
                    removed_paths.add(resolved)
                    removed_materialized_dirs += 1
        for metadata_path in list(materialized_root.rglob("metadata.json")):
            event_dir = metadata_path.parent
            resolved = event_dir.resolve(strict=False)
            if resolved in removed_paths or not event_dir.is_dir():
                continue
            if _rolling_cache_materialized_dir_belongs_to_run(
                metadata_path,
                source_prefix=source_prefix,
            ):
                shutil.rmtree(event_dir, ignore_errors=True)
                removed_paths.add(resolved)
                removed_materialized_dirs += 1
        _remove_empty_dirs_under(materialized_root)
    return {
        "source_dirs": removed_source_dirs,
        "materialized_dirs": removed_materialized_dirs,
    }


def _rolling_cache_materialized_dir_belongs_to_run(
    metadata_path: Path,
    *,
    source_prefix: str,
) -> bool:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    labels = metadata.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    candidates = (
        metadata.get("source_id"),
        metadata.get("replay_source_id"),
        labels.get("source_id"),
        labels.get("replay_source_id"),
    )
    return any(str(value or "").startswith(source_prefix) for value in candidates)


def _remove_empty_dirs_under(root: Path) -> None:
    if not root.exists():
        return
    dirs = [path for path in root.rglob("*") if path.is_dir()]
    for path in sorted(dirs, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def cleanup_after_aborted_run(
    conn,
    redis_client: Redis,
    cfg: PressureConfig,
    report: dict[str, Any],
    *,
    runtime_epoch_root: str | None,
) -> None:
    stop_summary: dict[str, Any] = {}
    try:
        stop_pressure_sources(conn, cfg)
        stop_summary["pressure_sources_stopped"] = True
    except Exception as exc:
        report["stop_pressure_sources_after_abort_error"] = repr(exc)
        stop_summary["pressure_sources_stopped"] = False
        stop_summary["error"] = repr(exc)
    if not cfg.cleanup:
        report["cleanup_after_abort"] = {
            **stop_summary,
            "skipped_data_cleanup": True,
            "reason": "no_cleanup_preserves_evidence_artifacts",
        }
        write_json(cfg.artifact_dir / "report.json", report)
        return
    try:
        if cfg.discard_pressure_results:
            report["cleanup_after_abort"] = cleanup_pressure_data(
                conn,
                redis_client,
                cfg,
                keep_event_ids=set(),
                runtime_epoch_root=runtime_epoch_root,
            )
        else:
            report["cleanup_after_abort"] = cleanup_pressure_runtime_only(
                conn,
                redis_client,
                cfg,
            )
    except Exception as exc:
        report["cleanup_after_abort_error"] = repr(exc)
    write_json(cfg.artifact_dir / "report.json", report)


def _is_pressure_source_id(source_id: str) -> bool:
    return bool(re.match(r"^(?:rc\d+_|pressure\d*_)", str(source_id or "")))


def _is_pressure_camera(camera: dict[str, Any]) -> bool:
    return (
        str(camera.get("site_id") or "") == "pressure"
        or _is_pressure_source_id(str(camera.get("source_id") or ""))
    )


def restore_cameras(conn, cameras: list[dict[str, Any]]) -> None:
    with conn.transaction():
        for camera in cameras:
            if _is_pressure_camera(camera):
                continue
            conn.execute(
                "UPDATE cameras SET enabled=%s, updated_at=now() WHERE id=%s",
                (bool(camera["enabled"]), camera["id"]),
            )


def restore_runtime(cfg: PressureConfig, original_perf: dict[str, Any]) -> None:
    if cfg.dual_shard_same_gpu:
        if cfg.dual_shard_api:
            api_json(cfg.api_base, "POST", "/runtime/control/dual/stop", timeout_s=360)
            stop_dual_shard_runtime(cfg)
        else:
            stop_dual_shard_runtime(cfg)
        if cfg.cuda_mps:
            stop_cuda_mps(cfg)
    env = os.environ.copy()
    env.update(
        {
            "BATCH_SIZE": str(original_perf.get("savant_batch_size", 4)),
            "POSE_BATCH_SIZE": str(original_perf.get("pose_batch_size", 4)),
            "FACE_DETECTOR_BATCH_SIZE": str(original_perf.get("face_detector_batch_size", 4)),
            "FACE_EMBEDDING_BATCH_SIZE": str(original_perf.get("face_embedding_batch_size", 16)),
            "FACE_INFER_INTERVAL": str(original_perf.get("face_infer_interval", 7)),
            "FACE_EMBEDDING_INFER_INTERVAL": str(
                original_perf.get("face_embedding_infer_interval", 7)
            ),
            "MAX_PARALLEL_STREAMS": str(original_perf.get("max_parallel_streams", 64)),
            "BATCHED_PUSH_TIMEOUT": str(original_perf.get("batched_push_timeout", 40000)),
            "ANALYSIS_FPS": str(original_perf.get("analysis_fps", "8/1")),
            "ANALYSIS_MIN_FPS": str(original_perf.get("analysis_min_fps", "2/1")),
            "FORWARDER_OUT_ENDPOINT": "dealer+connect:tcp://savant-security:5557",
            "MAX_FPS": str(original_perf.get("savant_max_fps", "8/1")),
            "MIN_FPS": str(original_perf.get("savant_min_fps", "2/1")),
        }
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "analysis-forwarder",
            "savant-security",
        ],
        cfg.artifact_dir / "compose_recreate_restore.log",
        env=env,
        check=False,
    )
    api_json(cfg.api_base, "PUT", "/runtime/performance-config", original_perf)
    api_json(cfg.api_base, "POST", "/runtime/performance-config/apply?force=true", timeout_s=360)
    api_json(cfg.api_base, "POST", "/cameras/runtime/restart?include_disabled=true&force=true", timeout_s=360)


def db_summary_connect(cfg: PressureConfig) -> dict[str, Any]:
    with psycopg.connect(cfg.db_url, row_factory=dict_row) as conn:
        return db_summary(
            conn,
            cfg.run_id,
            sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
        )


def db_summary(
    conn,
    run_id: str,
    *,
    sampling_start_event_ts_ms: int = 0,
) -> dict[str, Any]:
    prefix = f"{run_id}_%"
    row = conn.execute(
        f"""
        WITH run_events AS (
          SELECT id, status, event_type
          FROM events e
          WHERE source_id LIKE %(prefix)s
            AND {_formal_pressure_event_predicate("e")}
        ),
        run_tasks AS (
          SELECT event_id, materialization_status, status,
                 materialization_defer_reason, materialization_ready_at
          FROM evidence_tasks et
          WHERE source_id LIKE %(prefix)s
            AND {_formal_pressure_event_predicate("et")}
        ),
        parent_playable_events AS (
          SELECT DISTINCT eb.event_id
          FROM evidence_bundles eb
          JOIN run_events re ON re.id = eb.event_id
          WHERE (
              eb.raw_clip_uri IS NOT NULL
              AND COALESCE(eb.raw_clip_size_bytes, 0) > 0
            )
             OR COALESCE(eb.media_status, eb.evidence_state, '') = 'image_ready'
             OR COALESCE(eb.summary->>'playback_kind', '') = 'image'
        ),
        parent_video_playable_events AS (
          SELECT DISTINCT eb.event_id
          FROM evidence_bundles eb
          JOIN run_events re ON re.id = eb.event_id
          WHERE eb.raw_clip_uri IS NOT NULL
            AND COALESCE(eb.raw_clip_size_bytes, 0) > 0
        ),
        parent_image_ready_events AS (
          SELECT DISTINCT eb.event_id
          FROM evidence_bundles eb
          JOIN run_events re ON re.id = eb.event_id
          WHERE COALESCE(eb.media_status, eb.evidence_state, '') = 'image_ready'
             OR COALESCE(eb.summary->>'playback_kind', '') = 'image'
        ),
        parent_watchlist_image_ready_events AS (
          SELECT DISTINCT eb.event_id
          FROM evidence_bundles eb
          JOIN run_events re ON re.id = eb.event_id
          WHERE re.event_type = 'watchlist_hit'
            AND (
              COALESCE(eb.media_status, eb.evidence_state, '') = 'image_ready'
              OR COALESCE(eb.summary->>'playback_kind', '') = 'image'
            )
        ),
        covered_events_distinct AS (
          SELECT DISTINCT l.event_id
          FROM evidence_event_links l
          JOIN run_events re ON re.id = l.event_id
          WHERE l.relation = 'covered_by'
        ),
        covered_playable_events_distinct AS (
          SELECT DISTINCT l.event_id
          FROM evidence_event_links l
          JOIN run_events re ON re.id = l.event_id
          JOIN evidence_bundles eb ON eb.event_id = l.bundle_event_id
          WHERE l.relation = 'covered_by'
            AND eb.raw_clip_uri IS NOT NULL
            AND COALESCE(eb.raw_clip_size_bytes, 0) > 0
        ),
        distinct_events_with_playable_evidence AS (
          SELECT event_id FROM parent_playable_events
          UNION
          SELECT event_id FROM covered_playable_events_distinct
        ),
        distinct_events_with_terminal_nonplayable_outcome AS (
          SELECT DISTINCT rt.event_id
          FROM run_tasks rt
          WHERE rt.event_id NOT IN (
              SELECT event_id FROM distinct_events_with_playable_evidence
          )
            AND (
              rt.materialization_status IN (
                'materialization_expired',
                'materialization_failed',
                'materialization_skipped'
              )
              OR (
                rt.materialization_status = 'materialization_deferred'
                AND COALESCE(rt.materialization_defer_reason, '') <> ''
                AND rt.materialization_defer_reason <> 'covered_by_existing_evidence'
	              )
	            )
          UNION
          SELECT re.id
          FROM run_events re
          WHERE re.status = 'suppressed'
            AND re.id NOT IN (
                SELECT event_id FROM distinct_events_with_playable_evidence
            )
        ),
        blocking_materialization_events AS (
          SELECT DISTINCT rt.event_id
          FROM run_tasks rt
          WHERE rt.materialization_status IN (
              'manifest_ready',
              'materialization_pending',
              'materializing'
          )
             OR (
               rt.materialization_status = 'materialization_deferred'
               AND COALESCE(rt.materialization_defer_reason, '') = ''
             )
        )
        SELECT
          (SELECT count(*) FROM cameras WHERE source_id LIKE %(prefix)s) AS cameras,
          (SELECT count(*) FROM run_events) AS events,
          (SELECT count(*) FROM run_events WHERE status = 'suppressed') AS suppressed_events,
          (SELECT count(*) FROM run_events WHERE status <> 'suppressed') AS unsuppressed_events,
          (SELECT count(*) FROM run_tasks) AS tasks,
          (
            SELECT count(*)
            FROM evidence_bundles eb
            JOIN run_events re ON re.id = eb.event_id
          ) AS bundles,
          (SELECT count(*) FROM parent_playable_events) AS playable_bundles,
          (SELECT count(*) FROM parent_video_playable_events) AS behavior_video_playable_bundles,
          (SELECT count(*) FROM parent_image_ready_events) AS face_image_ready_bundles,
          (SELECT count(*) FROM parent_watchlist_image_ready_events) AS watchlist_image_ready_bundles,
          (SELECT count(*) FROM covered_events_distinct) AS covered_events,
          (SELECT count(*) FROM covered_playable_events_distinct) AS covered_playable_events,
          (
            SELECT count(*)
            FROM run_tasks rt
            WHERE materialization_status = ANY(%(active_materialization_states)s)
          ) AS active_materialization_tasks,
          (
            SELECT count(*)
            FROM run_tasks rt
            WHERE rt.materialization_status IN (
                'manifest_ready',
                'materialization_pending',
                'pending'
            )
              AND (
                rt.materialization_ready_at IS NULL
                OR rt.materialization_ready_at <= now()
              )
          ) AS ready_waiting_materialization_tasks,
          (
            SELECT count(*)
            FROM run_tasks rt
            WHERE rt.materialization_status IN (
                'manifest_ready',
                'materialization_pending',
                'pending'
            )
              AND rt.materialization_ready_at > now()
          ) AS unready_waiting_materialization_tasks,
          (SELECT count(*) FROM distinct_events_with_playable_evidence) AS distinct_events_with_playable_evidence,
          (
            SELECT count(*)
            FROM run_events re
            WHERE re.id NOT IN (SELECT event_id FROM distinct_events_with_playable_evidence)
          ) AS distinct_events_without_playable_evidence,
          (
            SELECT count(*)
            FROM run_events re
            WHERE re.id IN (SELECT event_id FROM covered_events_distinct)
              AND re.id NOT IN (SELECT event_id FROM covered_playable_events_distinct)
          ) AS covered_but_not_playable_distinct_events,
          (
            SELECT count(*)
            FROM distinct_events_with_terminal_nonplayable_outcome
          ) AS distinct_events_with_terminal_nonplayable_outcome,
          (
            SELECT count(*)
            FROM blocking_materialization_events
          ) AS blocking_materialization_tasks
        """,
        {
            "prefix": prefix,
            "active_materialization_states": sorted(ACTIVE_MATERIALIZATION_STATES),
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
        },
    ).fetchone()
    statuses = conn.execute(
        f"""
        SELECT et.status, et.materialization_status, count(*) AS count
        FROM evidence_tasks et
        WHERE source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("et")}
        GROUP BY status, materialization_status
        ORDER BY count DESC
        """,
        {
            "prefix": prefix,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
        },
    ).fetchall()
    event_types = conn.execute(
        f"""
        SELECT event_type, count(*) AS count
        FROM events e
        WHERE source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("e")}
        GROUP BY event_type
        ORDER BY count DESC
        """,
        {
            "prefix": prefix,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
        },
    ).fetchall()
    data = _row_json(row)
    data["task_statuses"] = [_row_json(item) for item in statuses]
    data["event_types"] = [_row_json(item) for item in event_types]
    roi_row = conn.execute(
        """
        SELECT
          count(*) AS observations,
          count(DISTINCT source_id) AS source_count,
          min(embedding_norm) AS embedding_norm_min,
          max(embedding_norm) AS embedding_norm_max
        FROM face_observations
        WHERE source_id LIKE %(prefix)s
          AND payload ? 'roi_transport'
          AND (
            %(sampling_start_event_ts_ms)s <= 0
            OR timestamp_ms >= %(sampling_start_event_ts_ms)s
          )
        """,
        {
            "prefix": prefix,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
        },
    ).fetchone()
    data["adaface_roi"] = _row_json(roi_row)
    return data


def non_materialized_task_details(
    conn,
    run_id: str,
    *,
    sampling_start_event_ts_ms: int = 0,
) -> list[dict[str, Any]]:
    prefix = f"{run_id}_%"
    rows = conn.execute(
        f"""
        SELECT
          et.task_id,
          et.event_id,
          et.source_event_id,
          et.source_id,
          et.event_type,
          et.event_ts_ms,
          et.pre_seconds,
          et.post_seconds,
          et.status,
          et.materialization_status,
          et.materialization_defer_reason,
          et.materialization_failure_reason,
          et.materialization_expired_reason,
          et.runtime_epoch_id,
          et.materialization_ready_at,
          et.materialization_deadline_at,
          et.materialization_attempt_count,
          et.created_at AS task_created_at,
          et.updated_at AS task_updated_at,
          e.status AS event_status,
          e.media_status AS event_media_status,
          e.created_at AS event_created_at,
          e.payload->'media' AS event_media,
          l.bundle_event_id,
          l.reason AS coverage_reason,
          l.metadata AS coverage_metadata
        FROM evidence_tasks et
        LEFT JOIN events e ON e.id = et.event_id
        LEFT JOIN evidence_event_links l ON l.event_id = et.event_id
        WHERE et.source_id LIKE %(prefix)s
          AND {_formal_pressure_event_predicate("et")}
          AND COALESCE(et.materialization_status, '') <> 'materialized'
        ORDER BY et.created_at, et.source_id, et.event_type
        """,
        {
            "prefix": prefix,
            "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
        },
    ).fetchall()
    return [_row_json(row) for row in rows]


def evidence_type_observability_summary(run_summary: dict[str, Any]) -> dict[str, Any]:
    """Expose the fixed video/image product split as an explicit contract."""

    behavior_video = _safe_int(
        run_summary.get("behavior_video_playable_bundles")
    )
    watchlist_image = _safe_int(
        run_summary.get("watchlist_image_ready_bundles")
    )
    all_image = _safe_int(run_summary.get("face_image_ready_bundles"))
    return {
        "schema_version": "evidence-type-counts-v1",
        "behavior_video": {
            "event_policy": "behavior_video_5_plus_5",
            "playable_bundle_count": behavior_video,
        },
        "watchlist_image": {
            "event_policy": "watchlist_image_only",
            "ready_bundle_count": watchlist_image,
        },
        "other_image_ready_bundle_count": max(0, all_image - watchlist_image),
        "playable_or_ready_total": behavior_video + all_image,
    }


def collect_downstream_observability(
    cfg: PressureConfig,
    conn,
    redis_client: Redis,
    *,
    kept: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    replay_topology = replay_topology_summary()
    video_file_sink = video_file_sink_observability_summary(diagnostics)
    postgresql = postgres_observability_summary(
        conn,
        cfg.run_id,
        sampling_start_event_ts_ms=cfg.pressure_sampling_start_event_ts_ms,
    )
    run_summary = postgresql.get("run_summary")
    run_summary = run_summary if isinstance(run_summary, dict) else {}
    summary = {
        "schema_version": DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "redis": redis_observability_summary(redis_client),
        "postgresql": postgresql,
        "qdrant": qdrant_observability_summary(conn, diagnostics),
        "event_worker": event_worker_observability_summary(diagnostics),
        "face_worker": face_worker_observability_summary(diagnostics),
        "media_worker": media_worker_observability_summary(diagnostics),
        "evidence_types": evidence_type_observability_summary(run_summary),
        "phase_latency_ms": evidence_phase_latency_summary(diagnostics),
        "replay_admission": replay_admission_observability_summary(diagnostics),
        "replay_topology": replay_topology,
        "video_file_sink": video_file_sink,
        "evidence_8090": evidence_8090_observability_summary(cfg, kept),
    }
    validate_downstream_observability_schema(summary)
    write_json(cfg.artifact_dir / "replay_topology_summary.json", replay_topology)
    write_json(
        cfg.artifact_dir / "video_file_sink_pressure_summary.json",
        video_file_sink,
    )
    write_json(cfg.artifact_dir / "downstream_observability_summary.json", summary)
    return summary


def redis_observability_summary(redis_client: Redis) -> dict[str, Any]:
    streams: dict[str, Any] = {}
    for stream in SECURITY_STREAMS:
        item: dict[str, Any] = {}
        try:
            item["length"] = int(redis_client.xlen(stream))
        except Exception as exc:
            item["length"] = _not_enough_data(f"xlen_failed:{type(exc).__name__}")
        try:
            groups = redis_client.xinfo_groups(stream)
            item["consumer_groups"] = [
                _decode_redis_mapping(group) for group in groups
            ]
        except Exception as exc:
            item["consumer_groups"] = []
            item["consumer_groups_status"] = _not_enough_data(
                f"xinfo_groups_failed:{type(exc).__name__}"
            )
        streams[stream] = item
    return {"streams": streams}


def postgres_observability_summary(
    conn,
    run_id: str,
    *,
    sampling_start_event_ts_ms: int = 0,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "run_summary": db_summary(
            conn,
            run_id,
            sampling_start_event_ts_ms=sampling_start_event_ts_ms,
        )
    }
    tables = [
        "events",
        "evidence_tasks",
        "evidence_bundles",
        "face_observations",
        "person_bbox_observations",
    ]
    try:
        rows = conn.execute(
            """
            SELECT relname, n_live_tup, n_dead_tup, seq_scan, seq_tup_read,
                   idx_scan, idx_tup_fetch
            FROM pg_stat_user_tables
            WHERE relname = ANY(%s)
            ORDER BY relname
            """,
            (tables,),
        ).fetchall()
        summary["table_stats"] = [_row_json(row) for row in rows]
    except Exception as exc:
        summary["table_stats"] = _not_enough_data(
            f"pg_stat_user_tables_failed:{type(exc).__name__}"
        )
    try:
        row = conn.execute(
            f"""
            SELECT count(*) AS count,
                   percentile_cont(0.50) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (COALESCE(last_materialization_at, updated_at) - created_at))
                   ) AS p50_seconds,
                   percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (COALESCE(last_materialization_at, updated_at) - created_at))
                   ) AS p95_seconds,
                   percentile_cont(0.99) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (COALESCE(last_materialization_at, updated_at) - created_at))
                   ) AS p99_seconds
            FROM evidence_tasks et
            WHERE source_id LIKE %(prefix)s
              AND {_formal_pressure_event_predicate("et")}
              AND created_at IS NOT NULL
              AND COALESCE(last_materialization_at, updated_at) IS NOT NULL
            """,
            {
                "prefix": f"{run_id}_%",
                "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            },
        ).fetchone()
        lifecycle = _row_json(row)
        lifecycle["status"] = (
            "measured" if int(lifecycle.get("count") or 0) > 0 else "not_enough_data"
        )
        summary["evidence_task_lifecycle_seconds"] = lifecycle
    except Exception as exc:
        summary["evidence_task_lifecycle_seconds"] = _not_enough_data(
            f"evidence_lifecycle_query_failed:{type(exc).__name__}"
        )
    try:
        row = conn.execute(
            f"""
            WITH measured AS (
                SELECT
                    et.materialization_ready_at,
                    NULLIF(
                        et.materialization_audit->'rolling_cache'->>'claimed_at',
                        ''
                    )::timestamptz AS claimed_at,
                    et.last_materialization_at
                FROM evidence_tasks et
                WHERE et.source_id LIKE %(prefix)s
                  AND {_formal_pressure_event_predicate("et")}
                  AND et.materialization_ready_at IS NOT NULL
                  AND et.materialization_audit ? 'rolling_cache'
            ),
            deltas AS (
                SELECT
                    EXTRACT(EPOCH FROM (claimed_at - materialization_ready_at)) AS ready_to_claim_s,
                    EXTRACT(EPOCH FROM (last_materialization_at - claimed_at)) AS claim_to_materialized_s
                FROM measured
                WHERE claimed_at IS NOT NULL
            )
            SELECT
                count(*) AS count,
                count(*) FILTER (WHERE ready_to_claim_s IS NOT NULL) AS ready_to_claim_count,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY ready_to_claim_s)
                    FILTER (WHERE ready_to_claim_s IS NOT NULL) AS ready_to_claim_p50_s,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY ready_to_claim_s)
                    FILTER (WHERE ready_to_claim_s IS NOT NULL) AS ready_to_claim_p95_s,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY ready_to_claim_s)
                    FILTER (WHERE ready_to_claim_s IS NOT NULL) AS ready_to_claim_max_s,
                count(*) FILTER (WHERE claim_to_materialized_s IS NOT NULL) AS claim_to_materialized_count,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY claim_to_materialized_s)
                    FILTER (WHERE claim_to_materialized_s IS NOT NULL) AS claim_to_materialized_p50_s,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY claim_to_materialized_s)
                    FILTER (WHERE claim_to_materialized_s IS NOT NULL) AS claim_to_materialized_p95_s,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY claim_to_materialized_s)
                    FILTER (WHERE claim_to_materialized_s IS NOT NULL) AS claim_to_materialized_max_s
            FROM deltas
            """,
            {
                "prefix": f"{run_id}_%",
                "sampling_start_event_ts_ms": sampling_start_event_ts_ms,
            },
        ).fetchone()
        ready_metrics = _row_json(row)
        ready_metrics["status"] = (
            "measured"
            if int(ready_metrics.get("ready_to_claim_count") or 0) > 0
            else "not_enough_data"
        )
        summary["rolling_cache_ready_schedule_seconds"] = ready_metrics
    except Exception as exc:
        summary["rolling_cache_ready_schedule_seconds"] = _not_enough_data(
            f"rolling_cache_ready_schedule_query_failed:{type(exc).__name__}"
        )
    return summary


def qdrant_observability_summary(conn, diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("face_worker") or {})
    summary: dict[str, Any] = {
        "query_latency_ms": logs.get("qdrant_query_latency_ms")
        or _not_enough_data("qdrant query logs unavailable"),
        "exact_rerank_latency_ms": logs.get("qdrant_exact_rerank_latency_ms")
        or _not_enough_data("qdrant exact-rerank logs unavailable"),
        "fallback_count": int(logs.get("qdrant_fallback_count") or 0),
        "shadow_mismatch_count": int(logs.get("qdrant_shadow_mismatch_count") or 0),
        "outbox": _not_enough_data("gallery_vector_sync_outbox not queried"),
    }
    try:
        row = conn.execute(
            """
            SELECT to_regclass('public.gallery_vector_sync_outbox') IS NOT NULL AS exists
            """
        ).fetchone()
        if row and row["exists"]:
            counts = conn.execute(
                """
                SELECT status, count(*) AS count
                FROM gallery_vector_sync_outbox
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            lag = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status IN ('pending', 'retry', 'processing')) AS active,
                    EXTRACT(EPOCH FROM (
                        now() - min(created_at)
                            FILTER (WHERE status IN ('pending', 'retry', 'processing'))
                    )) AS oldest_active_age_s,
                    EXTRACT(EPOCH FROM (
                        now() - max(processed_at)
                            FILTER (WHERE status = 'completed')
                    )) AS newest_completed_age_s
                FROM gallery_vector_sync_outbox
                """
            ).fetchone()
            outbox = _row_json(lag)
            outbox["status_counts"] = {
                str(item["status"]): int(item["count"]) for item in counts
            }
            summary["outbox"] = outbox
        else:
            summary["outbox"] = _not_enough_data("gallery_vector_sync_outbox_missing")
    except Exception as exc:
        summary["outbox"] = _not_enough_data(
            f"gallery_vector_sync_outbox_query_failed:{type(exc).__name__}"
        )
    return summary


def event_worker_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("event_worker") or {})
    return {
        "record_request_dedupe": {
            "reserved_count": int(logs.get("record_request_dedupe_reserved") or 0),
            "duplicate_count": int(logs.get("record_request_dedupe_duplicate") or 0),
            "reserve_failed_count": int(
                logs.get("record_request_dedupe_reserve_failed") or 0
            ),
            "latency_ms": _not_enough_data("dedupe path is O(1); latency timer not instrumented yet"),
        },
        "cpu_percent": (
            (diagnostics.get("sample_summary") or {})
            .get("max_worker_cpu_percent", {})
            .get("event_worker")
        ),
    }


def face_worker_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("face_worker") or {})
    return {
        "watchlist_hit_emitted_count": int(logs.get("watchlist_hit_emitted") or 0),
        "watchlist_emit_failed_count": int(logs.get("watchlist_emit_failed") or 0),
        "gallery_query_completed_count": int(
            logs.get("watchlist_gallery_query_completed") or 0
        ),
        "gallery_query_failed_count": int(
            logs.get("watchlist_gallery_query_failed") or 0
        ),
        "gallery_query_latency_ms": logs.get("face_gallery_query_latency_ms")
        or _not_enough_data("face-worker gallery query logs unavailable"),
        "cpu_percent": (
            (diagnostics.get("sample_summary") or {})
            .get("max_worker_cpu_percent", {})
            .get("face_worker")
        ),
    }


def media_worker_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("media_worker") or {})
    return {
        "finalized_count": int(logs.get("media_event_finalized") or 0),
        "deferred_count": int(logs.get("media_materialization_deferred") or 0),
        "paced_count": int(logs.get("media_materialization_paced") or 0),
        "throttle_paced_count": int(
            logs.get("media_materialization_throttle_paced") or 0
        ),
        "throttle_deadline_guard_count": int(
            logs.get("media_materialization_throttle_deadline_guard") or 0
        ),
        "throttle_disabled_count": int(
            logs.get("media_materialization_throttle_disabled") or 0
        ),
        "finalizer_failed_count": int(logs.get("post_savant_finalizer_failed") or 0),
        "claim_busy_count": int(logs.get("media_finalization_claim_busy") or 0),
        "claim_terminal_count": int(
            logs.get("media_finalization_claim_terminal") or 0
        ),
        "duplicate_materialization_count": int(
            logs.get("media_duplicate_materialization_count") or 0
        ),
        "finalizer_worker_counts": logs.get("media_finalizer_worker_counts") or {},
        "finalizer_worker_count": int(
            logs.get("media_finalizer_worker_count") or 0
        ),
        "imageio_ffmpeg_fallback_count": int(logs.get("imageio_ffmpeg_fallback") or 0),
        "claim_wait_ms": logs.get("media_claim_wait_ms")
        or _not_enough_data("media claim wait logs unavailable"),
        "queue_wait_ms_by_source": logs.get("media_queue_wait_ms_by_source") or {},
        "queue_wait_ms_by_shard": logs.get("media_queue_wait_ms_by_shard") or {},
        "throttle_sleep_s": logs.get("media_throttle_sleep_s")
        or _not_enough_data("media throttle logs unavailable"),
        "deadline_slack_s": logs.get("media_deadline_slack_s")
        or _not_enough_data("media deadline slack logs unavailable"),
        "finalization_duration_ms": logs.get("media_finalization_duration_ms")
        or _not_enough_data("media finalization logs unavailable"),
        "queue_wait_ms": logs.get("media_queue_wait_ms")
        or _not_enough_data("media queue wait logs unavailable"),
        "lifecycle_elapsed_ms": logs.get("media_lifecycle_elapsed_ms")
        or _not_enough_data("media lifecycle logs unavailable"),
        "post_savant_finalization_elapsed_ms": logs.get(
            "media_post_savant_finalization_elapsed_ms"
        )
        or _not_enough_data("post-Savant finalization logs unavailable"),
        "ffprobe_duration_ms": logs.get("media_ffprobe_duration_ms")
        or _not_enough_data("ffprobe logs unavailable"),
        "ffmpeg_duration_ms": logs.get("media_ffmpeg_duration_ms")
        or _not_enough_data("ffmpeg logs unavailable"),
        "replay_to_sink_metadata_ms": logs.get("media_replay_to_sink_metadata_ms")
        or _not_enough_data("Replay-to-sink metadata logs unavailable"),
        "sink_metadata_to_video_ms": logs.get("media_sink_metadata_to_video_ms")
        or _not_enough_data("sink metadata-to-video logs unavailable"),
        "sink_video_to_stable_ms": logs.get("media_sink_video_to_stable_ms")
        or _not_enough_data("sink video stability logs unavailable"),
        "sink_stable_to_ffprobe_ready_ms": logs.get(
            "media_sink_stable_to_ffprobe_ready_ms"
        )
        or _not_enough_data("sink ffprobe readiness logs unavailable"),
        "sink_ffprobe_ready_to_finalizer_start_ms": logs.get(
            "media_sink_ffprobe_ready_to_finalizer_start_ms"
        )
        or _not_enough_data("finalizer start readiness logs unavailable"),
        "finalizer_pool_wait_ms": logs.get("media_finalizer_pool_wait_ms")
        or _not_enough_data("finalizer pool wait logs unavailable"),
        "db_index": {
            "duration_ms": logs.get("media_db_index_duration_ms")
            or _not_enough_data("DB index timing logs unavailable"),
            "sidecar_ms": logs.get("media_db_index_sidecar_ms")
            or _not_enough_data("sidecar build timing logs unavailable"),
            "bundle_ms": logs.get("media_db_index_bundle_ms")
            or _not_enough_data("bundle index timing logs unavailable"),
            "artifact_ms": logs.get("media_db_index_artifact_ms")
            or _not_enough_data("artifact index timing logs unavailable"),
            "timeline_ms": logs.get("media_db_index_timeline_ms")
            or _not_enough_data("timeline index timing logs unavailable"),
            "overlay_ms": logs.get("media_db_index_overlay_ms")
            or _not_enough_data("overlay index timing logs unavailable"),
            "sidecar_prune_ms": logs.get("media_sidecar_prune_duration_ms")
            or _not_enough_data("sidecar prune timing logs unavailable"),
        },
        "scheduler": {
            "schema_version": "phase6-capacity-v1",
            "modes": logs.get("media_scheduler_modes") or {},
            "poll_duration_ms": logs.get("media_scheduler_tick_duration_ms")
            or _not_enough_data("scheduler tick logs unavailable"),
            "poll_gap_ms": logs.get("media_scheduler_tick_gap_ms")
            or _not_enough_data("scheduler tick logs unavailable"),
            "oldest_ready_age_ms": logs.get(
                "media_scheduler_oldest_ready_age_ms"
            )
            or _not_enough_data("scheduler oldest-ready age unavailable"),
            "lanes": {
                "image_depth": logs.get("media_scheduler_image_lane_depth")
                or _not_enough_data("image lane depth unavailable"),
                "remux_depth": logs.get("media_scheduler_remux_lane_depth")
                or _not_enough_data("rolling remux lane unavailable"),
                "finalizer_depth": logs.get(
                    "media_scheduler_finalizer_lane_depth"
                )
                or _not_enough_data("finalizer lane depth unavailable"),
            },
            "work_budget": {
                "active": logs.get("media_scheduler_permit_active")
                or _not_enough_data("legacy permit logs unavailable"),
                "limit": logs.get("media_scheduler_permit_limit")
                or _not_enough_data("legacy permit logs unavailable"),
            },
            "db_pool": {
                "in_use": logs.get("media_scheduler_db_pool_in_use")
                or _not_enough_data("DB pool in-use metrics unavailable"),
                "limit": logs.get("media_scheduler_db_pool_limit")
                or _not_enough_data("DB pool limit metrics unavailable"),
                "peak_in_use": logs.get("media_scheduler_db_pool_peak_in_use")
                or _not_enough_data("DB pool peak metrics unavailable"),
                "checkout_count": logs.get(
                    "media_scheduler_db_pool_checkout_count"
                )
                or _not_enough_data("DB pool checkout metrics unavailable"),
                "checkout_wait_ms": logs.get(
                    "media_scheduler_db_pool_checkout_wait_ms"
                )
                or _not_enough_data("DB pool wait metrics unavailable"),
                "checkout_timeouts": logs.get(
                    "media_scheduler_db_pool_checkout_timeouts"
                )
                or _not_enough_data("DB pool timeout metrics unavailable"),
                "checkout_errors": logs.get(
                    "media_scheduler_db_pool_checkout_errors"
                )
                or _not_enough_data("DB pool error metrics unavailable"),
                "resets": logs.get("media_scheduler_db_pool_resets")
                or _not_enough_data("DB pool reset metrics unavailable"),
                "connections_lost": logs.get(
                    "media_scheduler_db_pool_connections_lost"
                )
                or _not_enough_data("DB pool loss metrics unavailable"),
            },
            "segment_index": {
                "modes": logs.get("media_segment_index_modes") or {},
                "hits": logs.get("media_scheduler_segment_index_hits")
                or _not_enough_data("segment-index hits unavailable"),
                "misses": logs.get("media_scheduler_segment_index_misses")
                or _not_enough_data("segment-index misses unavailable"),
                "refreshes": logs.get(
                    "media_scheduler_segment_index_refreshes"
                )
                or _not_enough_data("segment-index refreshes unavailable"),
                "parses": logs.get("media_scheduler_segment_index_parses")
                or _not_enough_data("segment-index parses unavailable"),
                "stale_entries": logs.get(
                    "media_scheduler_segment_index_stale_entries"
                )
                or _not_enough_data("segment-index stale entries unavailable"),
                "fallback_scans": logs.get(
                    "media_scheduler_segment_index_fallback_scans"
                )
                or _not_enough_data("segment-index fallback scans unavailable"),
                "row_cache_entries": logs.get(
                    "media_scheduler_segment_index_row_cache_entries"
                )
                or _not_enough_data("segment-index row cache unavailable"),
                "row_cache_evictions": logs.get(
                    "media_scheduler_segment_index_row_cache_evictions"
                )
                or _not_enough_data("segment-index row cache evictions unavailable"),
                "active_read_pins": logs.get(
                    "media_scheduler_segment_index_active_read_pins"
                )
                or _not_enough_data("segment-index active pins unavailable"),
                "read_pins_created": logs.get(
                    "media_scheduler_segment_index_read_pins_created"
                )
                or _not_enough_data("segment-index created pins unavailable"),
                "read_pins_released": logs.get(
                    "media_scheduler_segment_index_read_pins_released"
                )
                or _not_enough_data("segment-index released pins unavailable"),
                "generation": logs.get(
                    "media_scheduler_segment_index_generation"
                )
                or _not_enough_data("segment-index generation unavailable"),
            },
            "capacity": {
                "max_active": logs.get("media_resource_max_active")
                or _not_enough_data("effective max_active unavailable"),
                "cpu_thread_limit": logs.get("media_resource_cpu_thread_limit")
                or _not_enough_data("CPU thread limit unavailable"),
                "image_workers": logs.get("media_resource_image_workers")
                or _not_enough_data("image worker count unavailable"),
                "remux_workers": logs.get("media_resource_remux_workers")
                or _not_enough_data("remux worker count unavailable"),
                "finalizer_workers": logs.get("media_resource_finalizer_workers")
                or _not_enough_data("finalizer worker count unavailable"),
                "image_queue_capacity": logs.get(
                    "media_resource_image_queue_capacity"
                )
                or _not_enough_data("image queue capacity unavailable"),
                "remux_queue_capacity": logs.get(
                    "media_resource_remux_queue_capacity"
                )
                or _not_enough_data("remux queue capacity unavailable"),
                "finalizer_queue_capacity": logs.get(
                    "media_resource_finalizer_queue_capacity"
                )
                or _not_enough_data("finalizer queue capacity unavailable"),
                "source_limit": logs.get("media_resource_source_limit")
                or _not_enough_data("source limit unavailable"),
            },
        },
        "cpu_percent": (
            (diagnostics.get("sample_summary") or {})
            .get("max_worker_cpu_percent", {})
            .get("media_worker")
        ),
    }


def video_file_sink_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("video_file_sink") or {})
    instances = logs.get("instances") if isinstance(logs, dict) else None
    aggregate = logs.get("aggregate") if isinstance(logs, dict) else None
    return {
        "instances": instances if isinstance(instances, dict) else {},
        "aggregate": aggregate
        if isinstance(aggregate, dict)
        else _not_enough_data("video-file-sink logs unavailable"),
    }


def replay_admission_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = diagnostics.get("log_summary") or {}
    clip_logs = logs.get("clip_worker") or {}
    media_logs = logs.get("media_worker") or {}
    return {
        "slot_acquired_count": int(clip_logs.get("replay_slot_acquired") or 0),
        "slot_acquire_success_count": int(
            clip_logs.get("replay_slot_acquire_success") or 0
        ),
        "slot_released_count": int(media_logs.get("replay_slot_released") or 0),
        "slot_release_reasons": media_logs.get("replay_slot_release_reasons") or {},
        "duration_seconds_effective": clip_logs.get(
            "replay_duration_seconds_effective"
        )
        or _not_enough_data("Replay effective duration logs unavailable"),
        "timeout_budget_s": clip_logs.get("replay_slot_timeout_budget_s")
        or _not_enough_data("Replay slot timeout budget logs unavailable"),
        "release_sink_video_to_stable_ms": media_logs.get(
            "replay_slot_release_sink_video_to_stable_ms"
        )
        or _not_enough_data("Replay slot release logs unavailable"),
    }


def evidence_phase_latency_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = diagnostics.get("log_summary") or {}
    clip_logs = logs.get("clip_worker") or {}
    media_logs = logs.get("media_worker") or {}
    return {
        "clip_worker": {
            "record_request_pending_ms": clip_logs.get(
                "clip_record_request_pending_ms"
            )
            or _not_enough_data("clip record request pending logs unavailable"),
            "proof_wait_ms": clip_logs.get("clip_proof_wait_ms")
            or _not_enough_data("clip proof wait logs unavailable"),
            "replay_job_create_ms": clip_logs.get("clip_replay_job_create_ms")
            or _not_enough_data("Replay job create logs unavailable"),
            "replay_slot_hold_ms": clip_logs.get("clip_replay_slot_hold_ms")
            or _not_enough_data("Replay slot hold logs unavailable"),
        },
        "media_worker": {
            "replay_to_sink_metadata_ms": media_logs.get(
                "media_replay_to_sink_metadata_ms"
            )
            or _not_enough_data("Replay-to-sink metadata logs unavailable"),
            "sink_metadata_to_video_ms": media_logs.get(
                "media_sink_metadata_to_video_ms"
            )
            or _not_enough_data("sink metadata-to-video logs unavailable"),
            "sink_video_to_stable_ms": media_logs.get(
                "media_sink_video_to_stable_ms"
            )
            or _not_enough_data("sink video stability logs unavailable"),
            "sink_stable_to_ffprobe_ready_ms": media_logs.get(
                "media_sink_stable_to_ffprobe_ready_ms"
            )
            or _not_enough_data("sink ffprobe readiness logs unavailable"),
            "sink_ffprobe_ready_to_finalizer_start_ms": media_logs.get(
                "media_sink_ffprobe_ready_to_finalizer_start_ms"
            )
            or _not_enough_data("finalizer start readiness logs unavailable"),
            "finalizer_pool_wait_ms": media_logs.get("media_finalizer_pool_wait_ms")
            or _not_enough_data("finalizer pool wait logs unavailable"),
        },
    }


def evidence_8090_observability_summary(
    cfg: PressureConfig,
    kept: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "retained_count": len(kept),
        "checked_count": 0,
        "ok_count": 0,
        "timeline_checked_count": 0,
        "timeline_ok_count": 0,
        "timeline_missing_count": 0,
        "timeline_fallback_count": 0,
        "annotation_checked_count": 0,
        "annotation_ok_count": 0,
        "annotation_skipped_zero_expected_count": 0,
        "annotation_fallback_count": 0,
        "annotation_bbox_missing_count": 0,
        "annotation_person_context_missing_count": 0,
        "annotation_count_mismatch_count": 0,
        "failed": [],
        "timeline_failed": [],
        "annotation_failed": [],
        "annotation_count_mismatches": [],
    }
    try:
        result["health"] = api_json(cfg.api_base, "GET", "/evidence/health", timeout_s=10)
    except Exception as exc:
        result["health"] = _not_enough_data(f"health_request_failed:{type(exc).__name__}")
    observability_limit = len(kept) if cfg.keep_evidence < 0 else max(0, cfg.keep_evidence)
    for row in kept[:observability_limit]:
        event_id = str(row.get("event_id") or "")
        if not event_id:
            continue
        result["checked_count"] += 1
        try:
            response = api_json(
                cfg.api_base,
                "GET",
                f"/evidence/bundles/{event_id}",
                timeout_s=10,
            )
        except Exception as exc:
            result["failed"].append(
                {"event_id": event_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if response.get("error"):
            result["failed"].append({"event_id": event_id, "error": response["error"]})
            continue
        data = response.get("data") or {}
        if str(data.get("event_id") or "") == event_id:
            result["ok_count"] += 1
        else:
            result["failed"].append(
                {
                    "event_id": event_id,
                    "error": "event_id_mismatch",
                    "observed_event_id": data.get("event_id"),
                }
            )
            continue
        summary_data = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        is_image_evidence = _evidence_row_is_image(row, data)
        if not is_image_evidence:
            result["timeline_checked_count"] += 1
            try:
                sink_response = api_json(
                    cfg.api_base,
                    "GET",
                    f"/evidence/bundles/{event_id}/sink-metadata",
                    timeout_s=10,
                )
            except Exception as exc:
                result["timeline_missing_count"] += 1
                result["timeline_failed"].append(
                    {
                        "event_id": event_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                sink_data = sink_response.get("data") or {}
                sink_count = _safe_int(sink_data.get("count"))
                if sink_response.get("error") or sink_count <= 0:
                    result["timeline_missing_count"] += 1
                    result["timeline_failed"].append(
                        {
                            "event_id": event_id,
                            "observed_timeline_rows": sink_count,
                            "error": (
                                sink_response.get("error")
                                or "sink_metadata_missing_from_8090_endpoint"
                            ),
                        }
                    )
                else:
                    result["timeline_ok_count"] += 1
                    if bool(sink_data.get("fallback_used")):
                        result["timeline_fallback_count"] += 1
        expected_annotations = _safe_int(
            row.get("annotation_count")
            or data.get("annotation_lines")
            or summary_data.get("annotation_lines")
        )
        if expected_annotations <= 0:
            if is_image_evidence:
                result["annotation_skipped_zero_expected_count"] += 1
            else:
                result["annotation_checked_count"] += 1
                result["annotation_failed"].append(
                    {
                        "event_id": event_id,
                        "error": "video_annotations_expected_zero",
                    }
                )
            continue
        result["annotation_checked_count"] += 1
        try:
            annotation_response = api_json(
                cfg.api_base,
                "GET",
                f"/evidence/bundles/{event_id}/annotations?include_records=true",
                timeout_s=10,
            )
        except Exception as exc:
            result["annotation_failed"].append(
                {
                    "event_id": event_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if annotation_response.get("error"):
            result["annotation_failed"].append(
                {
                    "event_id": event_id,
                    "error": annotation_response["error"],
                }
            )
            continue
        annotation_data = annotation_response.get("data") or {}
        observed_annotations = _safe_int(annotation_data.get("count"))
        records = annotation_data.get("records") or annotation_data.get("annotations") or []
        object_summary = _annotation_object_summary(records if isinstance(records, list) else [])
        annotation_ok = observed_annotations > 0
        if observed_annotations < expected_annotations:
            result["annotation_count_mismatch_count"] += 1
            result["annotation_count_mismatches"].append(
                {
                    "event_id": event_id,
                    "observed_annotations": observed_annotations,
                    "expected_annotations": expected_annotations,
                    "source": annotation_data.get("source"),
                    "reason": "db_overlay_rows_can_merge_clip_frame_index",
                }
            )
            if observed_annotations <= 0:
                annotation_ok = False
                result["annotation_failed"].append(
                    {
                        "event_id": event_id,
                        "observed_annotations": observed_annotations,
                        "expected_annotations": expected_annotations,
                        "error": "annotations_missing_from_8090_endpoint",
                    }
                )
        if bool(annotation_data.get("fallback_used")):
            result["annotation_fallback_count"] += 1
            annotation_ok = False
            result["annotation_failed"].append(
                {
                    "event_id": event_id,
                    "observed_annotations": observed_annotations,
                    "expected_annotations": expected_annotations,
                    "error": "annotations_database_overlay_missing_filesystem_fallback_used",
                }
            )
        if object_summary["bbox_object_count"] <= 0:
            result["annotation_bbox_missing_count"] += 1
            annotation_ok = False
            result["annotation_failed"].append(
                {
                    "event_id": event_id,
                    "observed_annotations": observed_annotations,
                    "expected_annotations": expected_annotations,
                    "error": "annotation_bbox_objects_missing",
                }
            )
        if (
            _evidence_video_requires_person_context(row, data)
            and object_summary["person_context_count"] <= 0
        ):
            result["annotation_person_context_missing_count"] += 1
            annotation_ok = False
            result["annotation_failed"].append(
                {
                    "event_id": event_id,
                    "observed_annotations": observed_annotations,
                    "expected_annotations": expected_annotations,
                    "error": "annotation_person_context_missing",
                }
            )
        if annotation_ok:
            result["annotation_ok_count"] += 1
    result["status"] = (
        "measured" if result["checked_count"] else "not_enough_data"
    )
    return result


def _evidence_row_is_image(row: dict[str, Any], manifest: dict[str, Any]) -> bool:
    if bool(row.get("image_evidence")):
        return True
    playback_kind = str(manifest.get("playback_kind") or "").lower()
    if playback_kind == "image":
        return True
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    return str(media.get("playback_kind") or "").lower() == "image"


def _evidence_video_requires_person_context(
    row: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    if _evidence_row_is_image(row, manifest):
        return False
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    event_type = str(
        row.get("event_type")
        or event.get("event_type")
        or manifest.get("event_type")
        or ""
    )
    return event_type in {"intrusion", "loitering", "running", "crowd_gathering", "fall", "chasing"}


def _annotation_object_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "record_count": len(records),
        "object_count": 0,
        "bbox_object_count": 0,
        "person_context_count": 0,
        "face_count": 0,
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        objects = record.get("objects")
        record_objects = list(objects) if isinstance(objects, list) else []
        if isinstance(record.get("object_type"), str) and isinstance(record.get("bbox"), dict):
            record_objects.append(record)
        for obj in record_objects:
            if not isinstance(obj, dict):
                continue
            summary["object_count"] += 1
            if isinstance(obj.get("bbox"), dict):
                summary["bbox_object_count"] += 1
            object_type = str(obj.get("object_type") or "")
            annotation_role = str(obj.get("annotation_role") or "")
            if object_type == "face":
                summary["face_count"] += 1
            if object_type == "person" or annotation_role == "person_context":
                summary["person_context_count"] += 1
    return summary


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def validate_downstream_observability_schema(summary: dict[str, Any]) -> bool:
    missing = [
        section
        for section in DOWNSTREAM_OBSERVABILITY_REQUIRED_SECTIONS
        if section not in summary
    ]
    if summary.get("schema_version") != DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION:
        missing.append("schema_version")
    nested_requirements = {
        "redis": ("streams",),
        "postgresql": (
            "run_summary",
            "table_stats",
            "evidence_task_lifecycle_seconds",
            "rolling_cache_ready_schedule_seconds",
        ),
        "event_worker": ("record_request_dedupe",),
        "face_worker": ("gallery_query_latency_ms",),
        "qdrant": ("query_latency_ms", "exact_rerank_latency_ms", "fallback_count", "outbox"),
        "media_worker": (
            "finalization_duration_ms",
            "ffprobe_duration_ms",
            "throttle_sleep_s",
            "deadline_slack_s",
            "claim_wait_ms",
            "replay_to_sink_metadata_ms",
            "sink_metadata_to_video_ms",
            "sink_video_to_stable_ms",
            "sink_stable_to_ffprobe_ready_ms",
            "sink_ffprobe_ready_to_finalizer_start_ms",
            "finalizer_pool_wait_ms",
            "queue_wait_ms_by_source",
            "queue_wait_ms_by_shard",
            "duplicate_materialization_count",
            "db_index",
            "scheduler",
        ),
        "evidence_types": (
            "behavior_video",
            "watchlist_image",
            "playable_or_ready_total",
        ),
        "phase_latency_ms": ("clip_worker", "media_worker"),
        "replay_admission": (
            "slot_acquired_count",
            "slot_released_count",
            "duration_seconds_effective",
            "timeout_budget_s",
            "release_sink_video_to_stable_ms",
        ),
        "replay_topology": ("replay_topology", "dual_replay_enabled", "containers"),
        "video_file_sink": ("instances", "aggregate"),
        "evidence_8090": ("retained_count", "checked_count", "ok_count"),
    }
    for section, keys in nested_requirements.items():
        value = summary.get(section)
        if not isinstance(value, dict):
            continue
        for key in keys:
            if key not in value:
                missing.append(f"{section}.{key}")
    if missing:
        raise ValueError(
            "downstream observability summary missing required fields: "
            + ", ".join(sorted(missing))
        )
    return True


def _not_enough_data(reason: str) -> dict[str, Any]:
    return {"status": "not_enough_data", "reason": reason}


def _decode_redis_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value": _decode_redis_value(value)}
    return {
        str(_decode_redis_value(key)): _decode_redis_value(item)
        for key, item in value.items()
    }


def _decode_redis_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [_decode_redis_value(item) for item in value]
    if isinstance(value, tuple):
        return [_decode_redis_value(item) for item in value]
    if isinstance(value, dict):
        return _decode_redis_mapping(value)
    return value


def cleanup_redis_streams(redis_client: Redis, run_id: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    token = run_id.encode("utf-8")
    for stream in SECURITY_STREAMS:
        ids: list[bytes] = []
        last_id = b"-"
        while True:
            rows = redis_client.xrange(stream, min=last_id, max=b"+", count=500)
            if not rows:
                break
            for entry_id, fields in rows:
                if entry_id == last_id:
                    continue
                haystack = b" ".join(
                    key + b"=" + value for key, value in fields.items()
                    if isinstance(key, bytes) and isinstance(value, bytes)
                )
                if token in haystack:
                    ids.append(entry_id)
                last_id = entry_id
            if len(rows) < 500:
                break
        deleted[stream] = int(redis_client.xdel(stream, *ids)) if ids else 0
    deleted_source_streams = 0
    deleted_source_stream_entries = 0
    pattern = f"security.frame_annotations.{run_id}_*"
    try:
        for key in redis_client.scan_iter(match=pattern, count=500):
            key_name = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            try:
                stream_len = int(redis_client.xlen(key) or 0)
            except Exception:
                stream_len = 0
            if redis_client.delete(key):
                deleted_source_streams += 1
                deleted_source_stream_entries += stream_len
            deleted[key_name] = stream_len
    except Exception:
        # Best-effort cleanup: the global streams above remain authoritative for
        # older pressure runs, and source-specific streams are bounded by maxlen.
        deleted["security.frame_annotations.source_stream_cleanup_error"] = -1
    if deleted_source_streams:
        deleted["security.frame_annotations.source_streams_deleted"] = deleted_source_streams
        deleted["security.frame_annotations.source_stream_entries_deleted"] = (
            deleted_source_stream_entries
        )
    roi_stream = f"security.face_rois.{safe_run_token(run_id)}"
    try:
        roi_len = int(redis_client.xlen(roi_stream) or 0)
        redis_client.delete(roi_stream)
        deleted[roi_stream] = roi_len
    except Exception:
        deleted[roi_stream] = -1
    return deleted


def remove_pressure_source_containers(run_id: str) -> list[str]:
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    names = [
        name
        for name in completed.stdout.splitlines()
        if name.startswith(f"video-analytics-source-{run_id}_")
    ]
    if names:
        subprocess.run(["docker", "rm", "-f", *names], check=False)
    return names


def remove_pressure_source_containers_until_stable(
    run_id: str,
    *,
    stable_checks: int = 3,
    max_checks: int = 12,
    sleep_s: float = 2.0,
) -> dict[str, Any]:
    removed: list[str] = []
    consecutive_empty = 0
    checks = 0
    while checks < max_checks and consecutive_empty < stable_checks:
        checks += 1
        names = remove_pressure_source_containers(run_id)
        if names:
            removed.extend(names)
            consecutive_empty = 0
        else:
            consecutive_empty += 1
        if consecutive_empty < stable_checks:
            time.sleep(sleep_s)
    return {
        "checks": checks,
        "stable": consecutive_empty >= stable_checks,
        "removed_count": len(removed),
        "removed": removed,
    }


def raw_clip_uri_to_path(evidence_root: Path, uri: str) -> Path | None:
    prefix = "/media/evidence/"
    if not uri.startswith(prefix):
        return None
    rel = uri[len(prefix):]
    if "/" not in rel:
        return None
    return evidence_root / rel


def probe_local_video_duration_seconds(path: Path, *, timeout_s: float = 10.0) -> float | None:
    if not path.is_file():
        return None
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return _float_or_none((completed.stdout or "").strip())


def api_json(
    api_base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_s: float = 60,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(
        api_base + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            body = {"error": {"message": str(exc), "code": exc.code}}
        return body
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"api request failed {method} {path}: {exc}") from exc


def api_text(api_base: str, path: str, *, timeout_s: float = 60) -> str:
    return fetch_text_url(api_base + path, timeout_s=timeout_s)


def fetch_text_url(url: str, *, timeout_s: float = 10) -> str:
    with urlopen(url, timeout=timeout_s) as response:
        return response.read().decode("utf-8", errors="replace")


def collect_adaface_roi_worker_metrics(cfg: PressureConfig) -> dict[str, Any]:
    if not cfg.adaface_roi_redis:
        return {"enabled": False}
    try:
        body = fetch_text_url(ADAFACE_ROI_METRICS_URL, timeout_s=5)
    except Exception as exc:
        return {"enabled": True, "reachable": False, "error": repr(exc)}
    metrics: dict[str, float] = {}
    wanted = {
        "va_adaface_roi_messages_total",
        "va_adaface_roi_batches_total",
        "va_adaface_roi_batch_size_count",
        "va_adaface_roi_batch_size_sum",
        "va_adaface_roi_inference_seconds_count",
        "va_adaface_roi_inference_seconds_sum",
        "va_adaface_roi_stream_cleanup_total",
        "va_adaface_roi_pending",
        "va_adaface_roi_last_success_unixtime",
    }
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        name_and_labels, separator, value_text = text.rpartition(" ")
        if not separator:
            continue
        metric_name = name_and_labels.split("{", 1)[0]
        if metric_name not in wanted:
            continue
        try:
            value = float(value_text)
        except ValueError:
            continue
        metrics[name_and_labels] = value
    batch_count = _prometheus_labeled_total(
        metrics, "va_adaface_roi_batch_size_count"
    )
    batch_sum = _prometheus_labeled_total(
        metrics, "va_adaface_roi_batch_size_sum"
    )
    full_batches = _prometheus_labeled_total(
        metrics,
        "va_adaface_roi_batches_total",
        f'size="{cfg.face_embedding_batch_size}"',
    )
    return {
        "enabled": True,
        "reachable": True,
        "url": ADAFACE_ROI_METRICS_URL,
        "metrics": metrics,
        "batch_occupancy": {
            "max_batch_size": cfg.face_embedding_batch_size,
            "batch_count": int(batch_count),
            "sample_count": int(batch_sum),
            "mean": round(batch_sum / batch_count, 4) if batch_count else 0.0,
            "full_batches": int(full_batches),
            "full_ratio": round(full_batches / batch_count, 6)
            if batch_count
            else 0.0,
        },
    }


def dual_shard_runtime_overview(cfg: PressureConfig) -> dict[str, Any]:
    forwarder_shards: list[dict[str, Any]] = []
    forwarder_sources: list[dict[str, Any]] = []
    forwarder_queue_depth = 0.0
    forwarder_running = 0.0
    for shard_id, url in DUAL_SHARD_FORWARDER_METRICS.items():
        try:
            parsed = parse_forwarder_metrics_text(fetch_text_url(url, timeout_s=5))
        except Exception as exc:
            parsed = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "global": {},
                "sources": [],
            }
        for source in parsed.get("sources") or []:
            source["replay_shard_id"] = shard_id
        forwarder_sources.extend(parsed.get("sources") or [])
        shard_global = parsed.get("global") or {}
        forwarder_queue_depth += float(shard_global.get("queue_depth") or 0.0)
        forwarder_running += float(shard_global.get("running") or 0.0)
        forwarder_shards.append({"shard_id": shard_id, "url": url, **parsed})

    savant_shards: list[dict[str, Any]] = []
    savant_sources: list[dict[str, Any]] = []
    savant_sources_active = 0.0
    for shard_id, url in DUAL_SHARD_SAVANT_METRICS.items():
        try:
            parsed = parse_savant_metrics_text(fetch_text_url(url, timeout_s=5))
        except Exception as exc:
            parsed = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "global": {},
                "sources": [],
            }
        for source in parsed.get("sources") or []:
            source["replay_shard_id"] = shard_id
        savant_sources.extend(parsed.get("sources") or [])
        savant_sources_active += float(parsed.get("sources_active") or 0.0)
        savant_shards.append({"shard_id": shard_id, "url": url, **parsed})

    raw_forwarder_shards: list[dict[str, Any]] = []
    raw_forwarder_sources: list[dict[str, Any]] = []
    raw_forwarder_queue_depth = 0.0
    raw_forwarder_running = 0.0
    for shard_id, url in DUAL_SHARD_RAW_FORWARDER_METRICS.items():
        try:
            parsed = parse_forwarder_metrics_text(fetch_text_url(url, timeout_s=5))
        except Exception as exc:
            parsed = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "global": {},
                "sources": [],
            }
        for source in parsed.get("sources") or []:
            source["replay_shard_id"] = shard_id
        raw_forwarder_sources.extend(parsed.get("sources") or [])
        shard_global = parsed.get("global") or {}
        raw_forwarder_queue_depth += float(
            shard_global.get("raw_queue_depth") or 0.0
        )
        raw_forwarder_running += float(shard_global.get("running") or 0.0)
        raw_forwarder_shards.append({"shard_id": shard_id, "url": url, **parsed})

    adaface_central: dict[str, Any] = {"enabled": False}
    adaface_forwarder: dict[str, Any] = {"enabled": False}
    if cfg.adaface_decoupled:
        adaface_forwarder_shards: list[dict[str, Any]] = []
        adaface_forwarder_sources: list[dict[str, Any]] = []
        adaface_forwarder_queue_depth = 0.0
        adaface_forwarder_running = 0.0
        for shard_id, url in ADAFACE_FORWARDER_METRICS.items():
            try:
                parsed = parse_forwarder_metrics_text(
                    fetch_text_url(url, timeout_s=5)
                )
            except Exception as exc:
                parsed = {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "global": {},
                    "sources": [],
                }
            for source in parsed.get("sources") or []:
                source["replay_shard_id"] = shard_id
            adaface_forwarder_sources.extend(parsed.get("sources") or [])
            shard_global = parsed.get("global") or {}
            adaface_forwarder_queue_depth += float(
                shard_global.get("queue_depth") or 0.0
            )
            adaface_forwarder_running += float(
                shard_global.get("running") or 0.0
            )
            adaface_forwarder_shards.append(
                {"shard_id": shard_id, "url": url, **parsed}
            )
        adaface_forwarder = {
            "enabled": True,
            "available": any(
                bool(item.get("available"))
                for item in adaface_forwarder_shards
            ),
            "global": {
                "queue_depth": adaface_forwarder_queue_depth,
                "running": adaface_forwarder_running,
            },
            "sources": adaface_forwarder_sources,
            "shards": adaface_forwarder_shards,
        }
        central_shards: list[dict[str, Any]] = []
        central_sources: list[dict[str, Any]] = []
        central_sources_active = 0.0
        for shard_id, url in adaface_central_metrics_urls(cfg).items():
            try:
                parsed = parse_savant_metrics_text(
                    fetch_text_url(url, timeout_s=5)
                )
            except Exception as exc:
                parsed = {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "global": {},
                    "sources": [],
                }
            for source in parsed.get("sources") or []:
                source["replay_shard_id"] = shard_id
            central_sources.extend(parsed.get("sources") or [])
            central_sources_active += float(parsed.get("sources_active") or 0.0)
            central_shards.append({"shard_id": shard_id, "url": url, **parsed})
        adaface_central = {
            "enabled": True,
            "available": any(bool(item.get("available")) for item in central_shards),
            "sources_active": central_sources_active,
            "global": {"va_savant_sources_active": central_sources_active},
            "sources": central_sources,
            "shards": central_shards,
        }

    return {
        "dual_shard_same_gpu": True,
        "dual_shard_gpu": cfg.dual_shard_gpu,
        "metrics_url": "dual-shard://savant-a,savant-b",
        "forwarder_metrics_url": "dual-shard://analysis-forwarder-a,analysis-forwarder-b",
        "metrics": {
            "available": any(bool(item.get("available")) for item in savant_shards),
            "sources_active": savant_sources_active,
            "global": {"va_savant_sources_active": savant_sources_active},
            "sources": savant_sources,
            "shards": savant_shards,
        },
        "forwarder": {
            "available": any(bool(item.get("available")) for item in forwarder_shards),
            "global": {
                "queue_depth": forwarder_queue_depth,
                "running": forwarder_running,
            },
            "sources": forwarder_sources,
            "shards": forwarder_shards,
        },
        "raw_forwarder": {
            "available": any(
                bool(item.get("available")) for item in raw_forwarder_shards
            ),
            "global": {
                "raw_queue_depth": raw_forwarder_queue_depth,
                "running": raw_forwarder_running,
            },
            "sources": raw_forwarder_sources,
            "shards": raw_forwarder_shards,
        },
        "adaface_forwarder": adaface_forwarder,
        "adaface_central": adaface_central,
    }


def parse_forwarder_metrics_text(text: str) -> dict[str, Any]:
    samples = _parse_prometheus_samples(text, prefix="va_forwarder_")
    by_source: dict[str, dict[str, float]] = {}
    global_metrics: dict[str, float] = {}
    for sample in samples:
        name = str(sample["name"])
        value = float(sample["value"])
        source_id = str(sample["labels"].get("source_id") or "")
        if source_id:
            by_source.setdefault(source_id, {})[name] = value
        else:
            global_metrics[name] = value
    sources = [
        {
            "source_id": source_id,
            "frames_seen_total": values.get("va_forwarder_frames_seen_total"),
            "frames_forwarded_total": values.get("va_forwarder_frames_forwarded_total"),
            "frames_dropped_total": values.get("va_forwarder_frames_dropped_total"),
            "metadata_filtered_total": values.get(
                "va_forwarder_metadata_filtered_total"
            ),
            "savant_send_failures_total": values.get(
                "va_forwarder_savant_send_failures_total"
            ),
            "raw_frames_forwarded_total": values.get(
                "va_forwarder_raw_frames_forwarded_total"
            ),
            "raw_frames_dropped_total": values.get(
                "va_forwarder_raw_frames_dropped_total"
            ),
            "raw_send_failures_total": values.get(
                "va_forwarder_raw_send_failures_total"
            ),
        }
        for source_id, values in sorted(by_source.items())
    ]
    return {
        "available": bool(samples),
        "sample_count": len(samples),
        "global": {
            "queue_depth": global_metrics.get("va_forwarder_queue_depth"),
            "raw_queue_depth": global_metrics.get(
                "va_forwarder_raw_queue_depth"
            ),
            "running": global_metrics.get("va_forwarder_running"),
        },
        "sources": sources,
    }


def parse_savant_metrics_text(text: str) -> dict[str, Any]:
    samples = _parse_prometheus_samples(text, prefix="va_savant_")
    by_source: dict[str, dict[str, Any]] = {}
    global_metrics: dict[str, float] = {}
    stage_metrics: dict[str, dict[str, Any]] = {}
    required_seen: set[str] = set()
    for sample in samples:
        name = str(sample["name"])
        value = float(sample["value"])
        labels = sample["labels"]
        stage = str(labels.get("stage") or "")
        if stage:
            stage_row = stage_metrics.setdefault(
                stage,
                {
                    "duration_count": 0.0,
                    "duration_sum_s": 0.0,
                    "duration_last_s": None,
                    "duration_buckets": {},
                    "batch_occupancy": {},
                },
            )
            if name == "va_savant_stage_duration_seconds_count":
                stage_row["duration_count"] = value
            elif name == "va_savant_stage_duration_seconds_sum":
                stage_row["duration_sum_s"] = value
            elif name == "va_savant_stage_duration_seconds_last":
                stage_row["duration_last_s"] = value
            elif name == "va_savant_stage_duration_seconds_bucket":
                stage_row["duration_buckets"][str(labels.get("le") or "")] = value
            elif name == "va_savant_batch_occupancy_total":
                stage_row["batch_occupancy"][str(labels.get("batch_size") or "0")] = value
            continue
        source_id = str(labels.get("source_id") or "")
        if source_id:
            row = by_source.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "counters": {},
                    "gauges": {},
                    "windows": {},
                },
            )
            window = str(labels.get("window") or "")
            if window:
                row["windows"].setdefault(window, {})[name] = value
            elif name in SAVANT_GAUGE_METRICS:
                row["gauges"][name] = value
            else:
                row["counters"][name] = value
            if name in SAVANT_COUNTER_METRICS or name in SAVANT_GAUGE_METRICS:
                required_seen.add(name)
        else:
            global_metrics[name] = value
            if name == "va_savant_sources_active":
                required_seen.add(name)
    sources = []
    for source_id, row in sorted(by_source.items()):
        gauges = row["gauges"]
        counters = row["counters"]
        sources.append(
            {
                "source_id": source_id,
                "effective_fps": gauges.get("va_savant_effective_fps"),
                "last_frame_age_seconds": gauges.get("va_savant_last_frame_age_seconds"),
                "frames_seen_total": counters.get("va_savant_frames_seen_total"),
                "frame_annotations_exported_total": counters.get(
                    "va_savant_frame_annotations_exported_total"
                ),
                "pose_objects_total": counters.get("va_savant_pose_objects_total"),
                "face_objects_total": counters.get("va_savant_face_objects_total"),
                "adaface_embeddings_total": counters.get("va_savant_adaface_embeddings_total"),
                "person_observations_exported_total": counters.get(
                    "va_savant_person_observations_exported_total"
                ),
                "face_observations_exported_total": counters.get(
                    "va_savant_face_observations_exported_total"
                ),
                "counters": counters,
                "gauges": gauges,
                "windows": row["windows"],
            }
        )
    return {
        "available": bool(samples),
        "sample_count": len(samples),
        "required_metric_names_seen": sorted(required_seen),
        "sources_active": global_metrics.get("va_savant_sources_active"),
        "global": global_metrics,
        "sources": sources,
        "stage_metrics": _finalize_stage_metrics(stage_metrics),
    }


def _finalize_stage_metrics(stage_metrics: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for stage, row in sorted(stage_metrics.items()):
        count = float(row.get("duration_count") or 0.0)
        duration_sum = float(row.get("duration_sum_s") or 0.0)
        occupancy = {
            str(size): int(value)
            for size, value in sorted(
                (row.get("batch_occupancy") or {}).items(),
                key=lambda item: int(item[0]),
            )
        }
        occupancy_total = sum(occupancy.values())
        finalized[stage] = {
            **row,
            "duration_count": int(count),
            "duration_mean_ms": round(duration_sum * 1000.0 / count, 3) if count else None,
            "duration_p50_upper_ms": _histogram_quantile_upper_ms(
                row.get("duration_buckets") or {}, count, 0.50
            ),
            "duration_p95_upper_ms": _histogram_quantile_upper_ms(
                row.get("duration_buckets") or {}, count, 0.95
            ),
            "batch_occupancy": occupancy,
            "batch_occupancy_total": occupancy_total,
            "batch_full_ratio": (
                round(occupancy.get("4", 0) / occupancy_total, 4)
                if occupancy_total
                else None
            ),
        }
    return finalized


def _histogram_quantile_upper_ms(
    buckets: dict[str, Any],
    count: float,
    quantile: float,
) -> float | None:
    if count <= 0:
        return None
    target = count * quantile
    finite: list[tuple[float, float]] = []
    for upper, value in buckets.items():
        if upper == "+Inf":
            continue
        try:
            finite.append((float(upper), float(value)))
        except (TypeError, ValueError):
            continue
    for upper, value in sorted(finite):
        if value >= target:
            return round(upper * 1000.0, 3)
    return None


def _parse_prometheus_samples(text: str, *, prefix: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_SAMPLE_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if not name.startswith(prefix):
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        samples.append(
            {
                "name": name,
                "labels": _parse_prom_labels(match.group("labels") or ""),
                "value": value,
            }
        )
    return samples


def _parse_prom_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for key, value in PROM_LABEL_RE.findall(raw or ""):
        labels[key] = value.replace(r"\"", '"').replace(r"\\", "\\")
    return labels


def write_combined_docker_logs(containers: list[str], path: Path, *, since: str) -> None:
    chunks: list[str] = []
    for name in containers:
        completed = subprocess.run(
            ["docker", "logs", "--since", since, name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        chunks.append(f"===== {name} rc={completed.returncode} =====\n{completed.stdout}\n")
    write_text(path, "".join(chunks))


def nvidia_smi_csv() -> str:
    query = ",".join(
        (
            "timestamp",
            "index",
            "name",
            "utilization.gpu",
            "utilization.decoder",
            "memory.used",
            "memory.total",
            "pstate",
            "temperature.gpu",
            "power.draw",
            "power.limit",
            "clocks.current.sm",
            "clocks.current.memory",
            "clocks.max.sm",
            "clocks_throttle_reasons.active",
        )
    )
    try:
        return subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
    except Exception as exc:
        return f"nvidia-smi failed: {exc}\n"


def docker_stats_json(cfg: PressureConfig) -> dict[str, Any]:
    if cfg.dual_shard_same_gpu:
        desired = [
            "video-analytics-midterm-analysis-forwarder-a",
            "video-analytics-midterm-analysis-forwarder-b",
            "video-analytics-midterm-savant-a",
            "video-analytics-midterm-savant-b",
            *WORKER_CONTAINER_NAMES.values(),
            *pressure_source_container_names(cfg.run_id),
        ]
    else:
        desired = [
            "video-analytics-midterm-analysis-forwarder",
            "video-analytics-midterm-savant",
            *WORKER_CONTAINER_NAMES.values(),
            *pressure_source_container_names(cfg.run_id),
        ]
    if not desired:
        return {"_meta": {"error": "no_containers"}}
    completed = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *desired],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rows: dict[str, Any] = {}
    errors: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                errors.append(line.strip())
            continue
        name = str(item.get("Name") or item.get("Container") or item.get("ID") or "")
        if name:
            rows[name] = item
    rows["_meta"] = {"returncode": completed.returncode, "errors": errors[:20]}
    return rows


def pressure_source_container_names(run_id: str) -> list[str]:
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    prefix = f"video-analytics-source-{run_id}_"
    return sorted(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith(prefix)
    )


def _read_docker_stats_sample(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if not str(key).startswith("_") and isinstance(value, dict)
    }


def _stats_cpu_percent(stats: dict[str, dict[str, Any]], name: str) -> float:
    return _parse_percent(str((stats.get(name) or {}).get("CPUPerc") or "0"))


def _stats_cpu_percent_sum(stats: dict[str, dict[str, Any]], names: list[str]) -> float:
    return sum(_stats_cpu_percent(stats, name) for name in names)


def _parse_percent(value: str) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except Exception:
        return 0.0


def probe_video_input_fps(uri: str) -> dict[str, Any]:
    """Probe the raw media cadence entering Replay.

    ``cfg.fps`` is the analysis/resampler target. Rolling cache is intentionally
    tapped before that resampler, so its segment sizing and full-rate gate must
    use the raw input FPS instead.
    """

    result: dict[str, Any] = {
        "uri": uri,
        "status": "unmeasured",
        "fps": None,
        "rate": "",
    }
    if not uri:
        result["reason"] = "empty_uri"
        return result
    command = [
        "ffprobe",
        "-v",
        "error",
    ]
    if str(uri).startswith("rtsp://"):
        command.extend(["-rtsp_transport", "tcp"])
    command.extend(
        [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            uri,
        ]
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except FileNotFoundError:
        result["status"] = "failed"
        result["reason"] = "ffprobe_missing"
        return result
    except subprocess.TimeoutExpired:
        result["status"] = "failed"
        result["reason"] = "ffprobe_timeout"
        return result
    if completed.returncode != 0:
        result["status"] = "failed"
        result["reason"] = completed.stderr.strip()[-500:] or "ffprobe_failed"
        return result
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        result["status"] = "failed"
        result["reason"] = "ffprobe_invalid_json"
        return result
    streams = payload.get("streams") if isinstance(payload, dict) else None
    stream = streams[0] if isinstance(streams, list) and streams else {}
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = str(stream.get(key) or "")
        fps = _fps_to_float(rate)
        if fps > 0:
            result.update({"status": "measured", "fps": fps, "rate": rate, "field": key})
            return result
    result["status"] = "failed"
    result["reason"] = "no_valid_frame_rate"
    return result


def _format_fps_float(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _fps_to_float(value: str) -> float:
    try:
        return float(Fraction(str(value).strip()))
    except Exception:
        try:
            return float(value)
        except Exception:
            return 0.0


def run(
    cmd: list[str],
    log_path: Path,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    write_text(log_path, completed.stdout)
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed rc={completed.returncode}: {' '.join(cmd)}")
    return completed


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _row_json(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    out = {}
    for key, value in dict(row).items():
        if isinstance(value, (datetime, uuid.UUID)):
            out[key] = str(value)
        else:
            out[key] = value
    return out


if __name__ == "__main__":
    raise SystemExit(main())
