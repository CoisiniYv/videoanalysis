"""C2.15 replay-first single-stream evidence baseline contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "docker-compose.c2-replay-first-dev.yml"
ENV_FILE = ROOT / "infra" / "env" / "c2-replay-first-dev.env"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.c2_replay_first_dev.json"
SAVANT_MODULE = ROOT / "modules" / "savant_security" / "module.yml"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.c1e_replay.yml"
DOC = ROOT / "docs" / "phase_c2_15_replay_first_single_stream_evidence.md"
FIXED_RTSP = "rtsp://10.37.57.112:8554/live/1080movie"
CLIP_WORKER_ROOT = ROOT / "services" / "clip-worker"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_text(COMPOSE))


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in _text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _replay_config() -> dict:
    return json.loads(_text(REPLAY_CONFIG))


def _activate_clip_worker_path() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_root = str(CLIP_WORKER_ROOT)
    if service_root in sys.path:
        sys.path.remove(service_root)
    sys.path.insert(0, service_root)


def test_files_exist() -> None:
    assert COMPOSE.exists()
    assert ENV_FILE.exists()
    assert REPLAY_CONFIG.exists()
    assert SAVANT_MODULE.exists()
    assert CAMERA_CONFIG.exists()
    assert DOC.exists()


def test_replay_first_runtime_uses_c2_env_not_c1_env() -> None:
    compose = _compose()
    expected = ["./env/c2-replay-first-dev.env"]
    for service_name in (
        "savant-security",
        "source-adapter",
        "event-worker",
        "media-worker",
    ):
        assert compose["services"][service_name]["env_file"] == expected

    rendered = _text(COMPOSE)
    assert "./env/c1-official-replay-dev.env" not in rendered
    assert "c2-replay-first-dev" in _text(ENV_FILE)


def test_topology_records_rtsp_in_replay_before_savant() -> None:
    compose = _compose()
    replay = _replay_config()
    services = compose["services"]

    assert services["source-adapter"]["environment"]["ZMQ_ENDPOINT"] == (
        "dealer+connect:tcp://replay-service:5555"
    )
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"
    assert services["savant-security"]["environment"]["ZMQ_SRC_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:5557"
    )
    assert services["clip-worker"]["environment"]["REPLAY_JOB_SINK_URL"] == (
        "dealer+connect:tcp://video-file-sink:6666"
    )
    assert services["video-file-sink"]["environment"]["ZMQ_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:6666"
    )


def test_replay_cache_is_bounded_to_30_seconds() -> None:
    replay = _replay_config()
    storage = replay["storage"]["rocksdb"]
    assert storage["data_expiration_ttl"] == {"secs": 30, "nanos": 0}
    assert storage["compaction_period"] == {"secs": 30, "nanos": 0}


def test_replay_to_savant_out_stream_has_backpressure_headroom() -> None:
    replay = _replay_config()
    out_options = replay["out_stream"]["options"]

    assert out_options["send_timeout"] == {"secs": 5, "nanos": 0}
    assert out_options["send_retries"] >= 10
    assert out_options["send_hwm"] >= 10000
    assert out_options["receive_hwm"] >= 10000
    assert out_options["inflight_ops"] >= 1000


def test_savant_ingress_fps_gate_is_parameterized_and_default_on() -> None:
    compose = _compose()
    env = compose["services"]["savant-security"]["environment"]
    module = yaml.safe_load(_text(SAVANT_MODULE))
    source = module["pipeline"]["source"]
    ingress_filter = source["ingress_frame_filter"]

    assert env["MAX_FPS_CONTROL"] == "${MAX_FPS_CONTROL:-true}"
    assert env["MAX_FPS"] == "${MAX_FPS:-8/1}"
    assert env["MIN_FPS"] == "${MIN_FPS:-2/1}"
    assert module["parameters"]["max_fps_control"] == (
        "${oc.decode:${oc.env:MAX_FPS_CONTROL, false}}"
    )
    assert module["parameters"]["max_fps"] == "${oc.env:MAX_FPS, 8/1}"
    assert module["parameters"]["min_fps"] == "${oc.env:MIN_FPS, 2/1}"
    assert ingress_filter["module"] == "custom.filters.pts_fps_gate"
    assert ingress_filter["class_name"] == "PtsFpsGate"
    assert ingress_filter["kwargs"]["enabled"] == "${parameters.max_fps_control}"
    assert ingress_filter["kwargs"]["max_fps"] == "${parameters.max_fps}"
    assert ingress_filter["kwargs"]["min_fps"] == "${parameters.min_fps}"


def test_pose_and_face_runtime_calibration_is_c2_only_over_shared_defaults() -> None:
    compose = _compose()
    env_file = _env(ENV_FILE)
    env = compose["services"]["savant-security"]["environment"]
    module = yaml.safe_load(_text(SAVANT_MODULE))
    pose = next(
        element
        for element in module["pipeline"]["elements"]
        if element.get("name") == "yolo26_pose"
    )

    assert env["POSE_INFER_INTERVAL"] == "${POSE_INFER_INTERVAL:-1}"
    assert env["POSE_CONFIDENCE_THRESHOLD"] == "${POSE_CONFIDENCE_THRESHOLD:-0.50}"
    assert env["POSE_KEYPOINT_THRESHOLD"] == "${POSE_KEYPOINT_THRESHOLD:-0.35}"
    assert env["POSE_SELECTOR_CONFIDENCE_THRESHOLD"] == (
        "${POSE_SELECTOR_CONFIDENCE_THRESHOLD:-0.50}"
    )
    assert env["POSE_SELECTOR_NMS_IOU_THRESHOLD"] == (
        "${POSE_SELECTOR_NMS_IOU_THRESHOLD:-0.50}"
    )
    assert env["POSE_MIN_WIDTH"] == "${POSE_MIN_WIDTH:-60}"
    assert env["POSE_MIN_HEIGHT"] == "${POSE_MIN_HEIGHT:-100}"
    assert env["FACE_CONFIDENCE_THRESHOLD"] == "${FACE_CONFIDENCE_THRESHOLD:-0.50}"
    assert env_file["MAX_FPS_CONTROL"] == "true"
    assert env_file["POSE_INFER_INTERVAL"] == "1"
    assert env_file["POSE_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["POSE_SELECTOR_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["FACE_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["WATCHLIST_THRESHOLD"] == "0.60"
    assert module["parameters"]["max_fps_control"] == (
        "${oc.decode:${oc.env:MAX_FPS_CONTROL, false}}"
    )
    assert module["parameters"]["pose_infer_interval"] == (
        "${oc.decode:${oc.env:POSE_INFER_INTERVAL, 0}}"
    )
    assert module["parameters"]["pose_confidence_threshold"] == (
        "${oc.decode:${oc.env:POSE_CONFIDENCE_THRESHOLD, 0.25}}"
    )
    assert module["parameters"]["face_confidence_threshold"] == (
        "${oc.decode:${oc.env:FACE_CONFIDENCE_THRESHOLD, 0.25}}"
    )
    assert pose["properties"]["interval"] == "${parameters.pose_infer_interval}"


def test_existing_yolo_pose_face_adaface_chain_is_preserved() -> None:
    module = _text(SAVANT_MODULE)
    assert "yolo26_pose" in module
    assert "face_detector" in module or "yolov8_face" in module or "scrfd" in module
    assert "adaface" in module.lower()
    assert "face_observation_exporter" in module


def test_watchlist_targets_are_registered_reese_and_finch() -> None:
    compose = _compose()
    env = compose["services"]["face-worker"]["environment"]
    assert env["WATCHLIST_MATCH_ENABLED"] == "true"
    assert env["WATCHLIST_THRESHOLD"] == "${WATCHLIST_THRESHOLD:-0.60}"
    assert env["WATCHLIST_TARGET_EXTERNAL_PERSON_IDS"] == (
        "${WATCHLIST_TARGET_EXTERNAL_PERSON_IDS:-demo:f4_3:reese,demo:f4_3:finch}"
    )
    assert env["WATCHLIST_TARGET_NAMES"] == "${WATCHLIST_TARGET_NAMES:-Reese,Finch}"


def test_workers_default_to_existing_gallery_postgres() -> None:
    compose = _compose()
    services = compose["services"]
    expected = (
        "${C2_REPLAY_FIRST_DATABASE_URL:-"
        "postgresql://video:video@host.docker.internal:5432/video_analytics}"
    )

    assert services["postgres"]["profiles"] == ["c2-local-postgres"]
    for service_name in ("event-worker", "face-worker", "clip-worker", "media-worker"):
        service = services[service_name]
        assert service["environment"]["DATABASE_URL"] == expected
        assert "host.docker.internal:host-gateway" in service["extra_hosts"]
        assert "postgres" not in service.get("depends_on", {})


def test_camera_config_includes_replay_first_source() -> None:
    doc = yaml.safe_load(_text(CAMERA_CONFIG))
    camera = doc["cameras"]["c2_replay_first_rtsp"]
    assert camera["enabled"] is True
    assert camera["source_id"] == "c2_replay_first_rtsp"
    assert camera["rtsp_url"] == FIXED_RTSP
    assert camera["rules"]["intrusion"]["clip_required"] is True
    assert camera["rules"]["intrusion"]["snapshot_required"] is False


def test_evidence_uses_raw_clip_and_jsonl_not_annotated_video() -> None:
    compose = _compose()
    media_env = compose["services"]["media-worker"]["environment"]
    video_sink_env = compose["services"]["video-file-sink"]["environment"]
    doc = _text(DOC)

    assert media_env["EVIDENCE_TOPOLOGY"] == "post_savant_replay"
    assert media_env["RAW_CLIP_SANITIZE_MODE"] == "off"
    assert video_sink_env["CHUNK_SIZE"] == "0"
    assert video_sink_env["METADATA_JSON_FORMAT"] == "native"
    assert "raw_clip.mov" in doc
    assert "annotations.frame_cache.identity.jsonl" in doc
    assert "annotated video clips are not generated" in doc


def test_replay_job_uses_original_stream_cadence_not_inference_fps() -> None:
    compose = _compose()
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert clip_env["REPLAY_STOP_CONDITION_MODE"] == "ts_delta_sec"
    assert clip_env["REPLAY_FPS"] == "${REPLAY_FPS:-24}"
    assert clip_env["REPLAY_DURATION_EXTRA_SLACK_S"] == (
        "${REPLAY_DURATION_EXTRA_SLACK_S:-15}"
    )
    assert clip_env["REPLAY_FORCE_CONSTANT_CADENCE"] == "false"
    assert clip_env["REPLAY_ANCHOR_STRATEGY"] == "event_keyframe"
    assert clip_env["KEYFRAME_LOOKUP_WINDOW_S"] == "${KEYFRAME_LOOKUP_WINDOW_S:-15}"
    assert clip_env["KEYFRAME_LOOKUP_RETRIES"] == "${KEYFRAME_LOOKUP_RETRIES:-8}"
    assert clip_env["KEYFRAME_LOOKUP_RETRY_SLEEP_S"] == (
        "${KEYFRAME_LOOKUP_RETRY_SLEEP_S:-1.0}"
    )


def test_clip_worker_uses_fresh_frame_annotation_pts_anchor() -> None:
    compose = _compose()
    clip_env = compose["services"]["clip-worker"]["environment"]
    doc = _text(DOC)

    assert clip_env["FRAME_ANNOTATION_STREAM"] == "security.frame_annotations"
    assert clip_env["FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT"] == (
        "${FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT:-20000}"
    )
    assert clip_env["FRAME_ANNOTATION_ANCHOR_WALL_CLOCK_SLACK_S"] == (
        "${FRAME_ANNOTATION_ANCHOR_WALL_CLOCK_SLACK_S:-1.0}"
    )
    assert clip_env["FRAME_ANNOTATION_ANCHOR_PTS_TOLERANCE_S"] == (
        "${FRAME_ANNOTATION_ANCHOR_PTS_TOLERANCE_S:-1.0}"
    )
    assert "same-source, same-camera frame-domain proofs" in doc
    assert "`requested_end_pts`" in doc
    assert "two fresh" in doc
    assert "real keyframe at or before" in doc
    assert "post-window candidate" in doc
    assert "at or after the event frame UUID timestamp" in doc
    assert "stale Redis rows" in doc
    assert "cross-loop frame UUIDs" in doc


def test_replay_job_stop_condition_can_cover_decodable_gop_before_final_crop() -> None:
    _activate_clip_worker_path()
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c2_replay_first_rtsp",
        keyframe_uuid="start-keyframe-anchor",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={
            "event_id": "ev-c2-15",
            "event_frame_uuid": "alarm-frame-uuid",
            "event_frame_pts": "10000000000",
            "requested_start_pts": "5000000000",
            "requested_end_pts": "15000000000",
            "anchor_keyframe_uuid": "start-keyframe-anchor",
            "anchor_keyframe_pts": "4000000000",
            "post_window_frame_pts": "15000000000",
            "post_window_frame_uuid": "post-window-frame",
            "start_window_frame_uuid": "start-window-frame",
        },
        stop_condition_mode="ts_delta_sec",
        fps=24,
        offset_seconds_override=7.0,
        duration_seconds_override=11.0,
    )

    assert payload["anchor_keyframe"] == "start-keyframe-anchor"
    assert payload["offset"]["seconds"] == 7.0
    assert payload["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 11.0}}
    assert payload["configuration"]["labels"]["requested_start_pts"] == "5000000000"
    assert payload["configuration"]["labels"]["requested_end_pts"] == "15000000000"
    assert payload["configuration"]["labels"]["post_window_frame_uuid"] == (
        "post-window-frame"
    )
    assert payload["configuration"]["labels"]["anchor_keyframe_uuid"] == (
        "start-keyframe-anchor"
    )


def test_replay_uuid_contract_keeps_event_frame_and_anchor_keyframe_distinct() -> None:
    _activate_clip_worker_path()
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c2_replay_first_rtsp",
        keyframe_uuid="replay-keyframe-uuid",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={
            "event_id": "ev-c2-15-uuid",
            "event_frame_uuid": "alarm-frame-uuid",
            "event_frame_pts": "10000000000",
            "requested_start_pts": "5000000000",
            "requested_end_pts": "15000000000",
            "anchor_keyframe_uuid": "replay-keyframe-uuid",
            "anchor_keyframe_pts": "4000000000",
            "post_window_frame_uuid": "post-window-frame-uuid",
            "post_window_frame_pts": "15000000000",
            "start_window_frame_uuid": "start-window-frame-uuid",
            "start_window_frame_pts": "5000000000",
        },
        stop_condition_mode="ts_delta_sec",
        fps=24,
        offset_seconds_override=7.0,
        duration_seconds_override=11.0,
    )

    labels = payload["configuration"]["labels"]
    assert payload["anchor_keyframe"] == "replay-keyframe-uuid"
    assert payload["anchor_keyframe"] == labels["anchor_keyframe_uuid"]
    assert payload["anchor_keyframe"] != labels["event_frame_uuid"]
    assert payload["anchor_keyframe"] != labels["post_window_frame_uuid"]
    assert payload["anchor_keyframe"] != labels["start_window_frame_uuid"]
    assert labels["event_frame_uuid"] == "alarm-frame-uuid"
    assert labels["post_window_frame_uuid"] == "post-window-frame-uuid"
    assert labels["event_frame_pts"] == "10000000000"
    assert labels["requested_start_pts"] == "5000000000"
    assert labels["requested_end_pts"] == "15000000000"


def test_replay_alignment_doc_requires_event_pts_window_and_fail_closed_binding() -> None:
    doc = _text(DOC)

    assert "`frame_pts`" in doc
    assert "`event_frame_pts`" in doc
    assert "`event_frame_uuid`" in doc
    assert "`anchor_keyframe_uuid`" in doc
    assert "`requested_start_pts`" in doc
    assert "`requested_end_pts`" in doc
    assert "UUID remains the primary frame identity" in doc
    assert "The Savant alarm frame `frame_uuid`" in doc
    assert "identifies the event frame" in doc
    assert "not automatically a valid Replay `anchor_keyframe`" in doc
    assert "`previous_keyframe_uuid` first, otherwise" in doc
    assert "they must not\n  replace UUID identity" in doc
    assert "Neither proof may replace `anchor_keyframe_uuid`" in doc
    assert "Replay `anchor_keyframe` is the event/record_request" in doc
    assert "that exact UUID and PTS" in doc
    assert "proof keyframe is rejected" in doc
    assert "fails closed with `missing_anchor_keyframe_pts`" in doc
    assert "`offset.seconds` is the PTS" in doc
    assert "delta from `anchor_keyframe_pts` back to `requested_start_pts`" in doc
    assert "coverage duration from the earliest" in doc
    assert "`REPLAY_DURATION_EXTRA_SLACK_S` extends this raw Replay/video-file-sink" in doc
    assert "it does not change the requested evidence window" in doc
    assert "`video-file-sink` is only the ZMQ file sink" in doc
    assert "Extending the window therefore belongs in the\n  clip-worker Replay job" in doc
    assert "Media-worker then crops raw video and sink metadata" in doc
    assert "Do not bypass the\nviewer `production_ready` gate" in doc
    assert "event frame appears at `pre_seconds`" in doc
    assert "raw video, sink metadata, sidecar JSONL, and the 8090" in doc
    assert "broad DB window fallback" in doc
    assert "legacy visual binding" in doc
    assert "fails closed" in doc


def test_replay_first_uses_frame_cache_for_annotations() -> None:
    compose = _compose()
    savant_env = compose["services"]["savant-security"]["environment"]
    media_env = compose["services"]["media-worker"]["environment"]

    assert savant_env["FRAME_ANNOTATION_EXPORT_ENABLED"] == "true"
    assert savant_env["FRAME_ANNOTATION_STREAM"] == "security.frame_annotations"
    assert savant_env["FRAME_ANNOTATION_INCLUDE_EMBEDDING"] == "false"
    assert media_env["FRAME_CACHE_SIDECAR_ENABLED"] == "true"
    assert media_env["FRAME_CACHE_SIDECAR_EVENT_TYPES"] == "intrusion,watchlist_hit"
    assert media_env["FRAME_CACHE_SIDECAR_REQUIRE_TRIGGER_FACE"] == "false"
    assert media_env["FRAME_CACHE_SIDECAR_STREAM"] == "security.frame_annotations"
    assert media_env["FRAME_CACHE_FRESHNESS_GUARD_MODE"] == "metadata_pts"
    assert media_env["FRAME_CACHE_REQUIRE_EVENT_CENTERED"] == "true"
    assert media_env["FRAME_CACHE_CANONICAL_MIN_DURATION_SECONDS"] == (
        "${FRAME_CACHE_CANONICAL_MIN_DURATION_SECONDS:-8.0}"
    )
    assert media_env["FRAME_CACHE_CANONICAL_MAX_DURATION_SECONDS"] == (
        "${FRAME_CACHE_CANONICAL_MAX_DURATION_SECONDS:-12.5}"
    )
    assert media_env["FRAME_CACHE_CANONICAL_EVENT_CENTER_TOLERANCE_SECONDS"] == (
        "${FRAME_CACHE_CANONICAL_EVENT_CENTER_TOLERANCE_SECONDS:-0.75}"
    )
    assert media_env["C2_FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED"] == "true"


def test_intrusion_sidecar_can_render_bbox_without_watchlist_identity() -> None:
    compose = _compose()
    media_env = compose["services"]["media-worker"]["environment"]

    assert "intrusion" in media_env["FRAME_CACHE_SIDECAR_EVENT_TYPES"].split(",")
    assert media_env["FRAME_CACHE_SIDECAR_REQUIRE_TRIGGER_FACE"] == "false"
    assert media_env["FRAME_CACHE_SIDECAR_WRITE_MODE"] == "sidecar_only"
    assert media_env["FRAME_CACHE_SIDECAR_OUTPUT_ANNOTATIONS"] == (
        "annotations.frame_cache.identity.jsonl"
    )


def test_evidence_viewer_serves_port_8090() -> None:
    compose = _compose()
    viewer = compose["services"]["evidence-viewer"]
    assert viewer["environment"]["EVIDENCE_VIEWER_PORT"] == "8090"
    assert "8090:8090" in viewer["ports"]
    assert "/data/video-analytics/media/evidence:/evidence:ro" in viewer["volumes"]


def test_source_and_clip_lengths_are_configurable() -> None:
    compose = _compose()
    source_env = compose["services"]["source-adapter"]["environment"]
    event_env = compose["services"]["event-worker"]["environment"]
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert source_env["RTSP_URI"] == (
        "${C2_REPLAY_FIRST_RTSP_URI:-rtsp://10.37.57.112:8554/live/1080movie}"
    )
    assert source_env["LOCATION"] == (
        "${C2_REPLAY_FIRST_RTSP_URI:-rtsp://10.37.57.112:8554/live/1080movie}"
    )
    assert FIXED_RTSP in _text(DOC)
    assert event_env["RECORDING_PRE_SECONDS"] == "${RECORDING_PRE_SECONDS:-5}"
    assert event_env["RECORDING_POST_SECONDS"] == "${RECORDING_POST_SECONDS:-5}"
    assert event_env["RECORDING_MAX_REQUESTS_PER_RUN"] == (
        "${RECORDING_MAX_REQUESTS_PER_RUN:-0}"
    )
    assert clip_env["DEFAULT_PRE_SECONDS"] == "${DEFAULT_PRE_SECONDS:-5}"
    assert clip_env["DEFAULT_POST_SECONDS"] == "${DEFAULT_POST_SECONDS:-5}"
