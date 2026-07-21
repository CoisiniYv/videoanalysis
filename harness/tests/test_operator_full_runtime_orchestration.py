from __future__ import annotations

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
API_DIR = str(ROOT / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

api_root = Path(API_DIR).resolve()
loaded_app = sys.modules.get("app")
loaded_app_path = Path(getattr(loaded_app, "__file__", "") or "/").resolve()
if loaded_app is not None and not loaded_app_path.is_relative_to(api_root):
    for module_name in [
        name for name in list(sys.modules) if name == "app" or name.startswith("app.")
    ]:
        sys.modules.pop(module_name, None)

from app.services.runtime_performance import _merge_env_list  # noqa: E402
from app.services.runtime_topology import (  # noqa: E402
    _forwarder_env,
    _savant_env,
)


def _compose() -> dict:
    return yaml.safe_load(
        (ROOT / "infra" / "docker-compose.midterm.yml").read_text(encoding="utf-8")
    )


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node)
)


def test_operator_profile_precreates_every_full_pipeline_dependency() -> None:
    compose = _compose()
    services = compose["services"]
    required = {
        "cuda-mps-operator",
        "savant-a",
        "savant-b",
        "replay-raw-fanout-a",
        "replay-raw-fanout-b",
        "replay-a",
        "replay-b",
        "video-file-sink-a",
        "video-file-sink-b",
        "rolling-cache-sink-a",
        "rolling-cache-sink-b",
        "adaface-roi-worker",
    }

    assert required <= set(services)
    for service in required:
        assert "operator-dual-runtime" in services[service].get("profiles", [])

    helper = (
        ROOT / "scripts" / "runtime" / "precreate_operator_dual_runtime.sh"
    ).read_text(encoding="utf-8")
    for service in required:
        assert service in helper
    assert "--no-start" in helper
    assert "--no-deps" in helper
    assert "--no-recreate" in helper
    assert "--force-recreate" in helper
    assert "operator-dual-runtime.override.yml" in helper
    assert "prepare_dual_4090_savant_b_model_cache.sh" in helper

    operator_override = yaml.load(
        (ROOT / "infra" / "operator-dual-runtime.override.yml").read_text(
            encoding="utf-8"
        ),
        Loader=_ComposeLoader,
    )
    savant_b = operator_override["services"]["savant-b"]
    assert savant_b["environment"]["NVIDIA_VISIBLE_DEVICES"] == "0"
    assert savant_b["environment"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert savant_b["deploy"]["resources"]["reservations"]["devices"][0][
        "device_ids"
    ] == ["0"]


def test_production_storage_override_keeps_cache_off_evidence_disk() -> None:
    override = yaml.safe_load(
        (ROOT / "infra" / "midterm-storage.override.yml").read_text(
            encoding="utf-8"
        )
    )
    services = override["services"]

    for service in ("rolling-cache-sink-a", "rolling-cache-sink-b"):
        assert any(
            "/home/user/video-analytics-fast/rolling-cache" in volume
            and volume.endswith(":/media/rolling-cache:rw")
            for volume in services[service]["volumes"]
        )
    media_volumes = services["media-worker"]["volumes"]
    assert any("rolling-cache-materialized" in volume for volume in media_volumes)


def test_full_pipeline_env_uses_raw_fanout_and_roi_instead_of_savant_video() -> None:
    branch = {
        "gpu_id": 0,
        "savant_batch_size": 4,
        "pose_batch_size": 4,
        "face_detector_batch_size": 4,
        "face_embedding_batch_size": 16,
        "face_infer_interval": 3,
        "max_parallel_streams": 64,
        "analysis_fps": "4/1",
        "analysis_min_fps": "99/25",
        "savant_max_fps": "4/1",
        "savant_min_fps": "99/25",
        "batched_push_timeout": 10000,
    }

    savant = _savant_env(
        branch,
        mode="dual_same_gpu",
        full_pipeline=True,
        cuda_mps_enabled=True,
        mps_percentage=45,
    )
    forwarder = _forwarder_env(
        branch, branch_id="a", full_pipeline=True
    )

    assert savant["OUTPUT_FRAME"] == "null"
    assert savant["FACE_ROI_EXPORT_ENABLED"] == "true"
    assert savant["FACE_OBSERVATION_EXPORT_ENABLED"] == "false"
    assert savant["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"] == "45"
    assert forwarder["FORWARDER_OUT_ENDPOINT"] == (
        "dealer+connect:tcp://savant-a:5557"
    )
    assert forwarder["FORWARDER_RAW_OUT_ENDPOINT"] == (
        "pub+bind:tcp://0.0.0.0:5560"
    )
    assert forwarder["FORWARDER_SAMPLER_ENABLED"] == "true"


def test_runtime_recreate_can_remove_stale_mps_environment() -> None:
    merged = _merge_env_list(
        ["KEEP=value", "CUDA_MPS_PIPE_DIRECTORY=/tmp/stale"],
        {"CUDA_MPS_PIPE_DIRECTORY": None},
    )

    assert merged == ["KEEP=value"]


def test_8090_form_exposes_named_full_runtime_profiles() -> None:
    html = (
        ROOT / "services" / "evidence-viewer" / "app" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    js = (
        ROOT / "services" / "evidence-viewer" / "app" / "static" / "operator.js"
    ).read_text(encoding="utf-8")
    viewer_main = (
        ROOT / "services" / "evidence-viewer" / "app" / "main.py"
    ).read_text(encoding="utf-8")

    assert 'name="runtime_profile"' in html
    assert 'value="production_t4_40"' in html
    assert 'value="local_4090_60"' in html
    assert 'name="pipeline_mode"' in html
    assert "识别、人员轨迹和证据录像服务" in js
    assert 'id="quick-runtime-start"' in html
    assert 'id="quick-runtime-shard-strategy"' in html
    assert "管理摄像头与算法" in html
    assert "quickStartFullRuntime" in js
    assert "系统自动均分（推荐）" in html
    assert 'id="quick-runtime-select-required"' in html
    assert 'id="quick-runtime-clear-selection"' in html
    assert "选择本次参与运行的摄像头" in html
    assert "quickRuntimeSelectedSourceIds" in js
    assert "source_ids: selected.map" in js
    assert "disable_unselected: true" in js
    assert "JSON.stringify({ source_ids: sourceIds, disable_unselected: disableUnselected })" in js
    assert "点击“启动全部分析”后才会统一启用" in html
    assert 'id="open-full-runtime-from-cameras"' not in html
    assert "openFullRuntimeFromCamerasBtn" not in js
    assert "enabled: false" in js
    assert 'id="open-full-runtime-primary"' not in html
    assert "系统启动入口" not in html
    assert "openFullRuntimePrimaryBtn" not in js
    assert "openQuickRuntimeStart" not in js
    assert "运维控制" in html
    assert 'id="quick-runtime-progress"' in html
    assert 'id="quick-runtime-progress-bar"' in html
    assert "录像缓存准备" in html
    assert "runtime/topology-config/apply-async" in js
    assert "runtime/topology-config/apply-status" in js
    assert "renderRuntimeTopologyApplyProgress" in js
    assert "remaining_seconds" in js
    assert "await asyncio.to_thread(" in viewer_main
    assert viewer_main.count("await asyncio.to_thread(") >= 2
    assert "dualReady" in js
    assert "分析服务就绪" in js
    assert "处理组 A / 处理组 B" in js
    assert 'id="quick-runtime-stop"' in html
    assert "停止采集与推理" in html
    assert "runtime/control/dual/stop" in js
    assert 'id="runtime-latency-summary"' in html
    assert "`${API}/runtime/latency`" in js
    assert "window.setInterval" in js
    assert "}, 5000);" in js
