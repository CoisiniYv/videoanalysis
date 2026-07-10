from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[2] / "services" / "analysis-forwarder" / "app"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sampler_mod = _load_module("sampler")
queueing_mod = _load_module("queueing")


def _load_main_module():
    _install_fake_savant_rs()
    package_name = "analysis_forwarder_test_app"
    package = types.ModuleType(package_name)
    package.__path__ = [str(APP_DIR)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", APP_DIR / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_name}.main"] = module
    spec.loader.exec_module(module)
    return module


def _install_fake_savant_rs() -> None:
    savant_rs = types.ModuleType("savant_rs")
    py_mod = types.ModuleType("savant_rs.py")
    utils_mod = types.ModuleType("savant_rs.py.utils")
    zeromq_mod = types.ModuleType("savant_rs.py.utils.zeromq")
    zmq_mod = types.ModuleType("savant_rs.zmq")

    class FakeZeroMQSource:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def next_message(self):
            return None

        def shutdown(self) -> None:
            pass

    class FakeWriterConfigBuilder:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint

        def with_send_timeout(self, _value: int) -> None:
            pass

        def with_send_retries(self, _value: int) -> None:
            pass

        def with_send_hwm(self, _value: int) -> None:
            pass

        def build(self) -> dict[str, str]:
            return {"endpoint": self.endpoint}

    class FakeBlockingWriter:
        def __init__(self, config) -> None:
            self.config = config
            self.sent: list[tuple[str, object, bytes]] = []

        def start(self) -> None:
            pass

        def send_message(self, topic: str, message, content: bytes):
            self.sent.append((topic, message, content))
            class WriterResultSuccess:
                pass

            return WriterResultSuccess()

        def shutdown(self) -> None:
            pass

    zeromq_mod.ZeroMQSource = FakeZeroMQSource
    zmq_mod.BlockingWriter = FakeBlockingWriter
    zmq_mod.WriterConfigBuilder = FakeWriterConfigBuilder
    sys.modules.update(
        {
            "savant_rs": savant_rs,
            "savant_rs.py": py_mod,
            "savant_rs.py.utils": utils_mod,
            "savant_rs.py.utils.zeromq": zeromq_mod,
            "savant_rs.zmq": zmq_mod,
        }
    )


@dataclass
class _Content:
    none: bool = False

    def is_none(self) -> bool:
        return self.none


@dataclass
class _Frame:
    source_id: str
    pts: int
    keyframe: bool = False
    time_base: tuple[int, int] = (1, 1_000_000_000)
    content: _Content = field(default_factory=_Content)


@dataclass
class _Object:
    namespace: str
    label: str
    attributes: dict[tuple[str, str], object] = field(default_factory=dict)
    confidence: float = 1.0
    detection_box: object = field(
        default_factory=lambda: types.SimpleNamespace(width=100.0, height=100.0)
    )

    def get_attribute(self, namespace: str, name: str):
        return self.attributes.get((namespace, name))


@dataclass
class _MetadataFrame:
    objects: list[_Object]

    def get_all_objects(self) -> list[_Object]:
        return self.objects


def test_sampler_admits_keyframes_and_limits_by_pts() -> None:
    sampler = sampler_mod.AnalysisFrameSampler(enabled=True, max_fps="2/1")

    assert sampler.admit(_Frame("cam", pts=0, keyframe=True)) is True
    assert sampler.admit(_Frame("cam", pts=100_000_000)) is False
    assert sampler.admit(_Frame("cam", pts=500_000_000)) is True
    assert sampler.admit(_Frame("cam", pts=600_000_000, keyframe=True)) is True


def test_sampler_tracks_sources_independently() -> None:
    sampler = sampler_mod.AnalysisFrameSampler(enabled=True, max_fps="1/1")

    assert sampler.admit(_Frame("a", pts=0)) is True
    assert sampler.admit(_Frame("a", pts=100_000_000)) is False
    assert sampler.admit(_Frame("b", pts=100_000_000)) is True


def test_sampler_drops_empty_content() -> None:
    sampler = sampler_mod.AnalysisFrameSampler(enabled=True, max_fps="8/1")

    assert sampler.admit(_Frame("cam", pts=0, content=_Content(none=True))) is False


def test_metadata_filter_requires_matching_face_person_association() -> None:
    metadata_filter = sampler_mod.MetadataObjectFilter(
        object_namespace="yolov8_face",
        object_label="face",
        attribute_namespace="face_person_associator",
        attribute_name="person_track_id",
    )
    associated_face = _Object(
        namespace="yolov8_face",
        label="face",
        attributes={("face_person_associator", "person_track_id"): object()},
    )
    unassociated_face = _Object(namespace="yolov8_face", label="face")
    associated_person = _Object(
        namespace="yolo26_pose",
        label="person",
        attributes={("face_person_associator", "person_track_id"): object()},
    )

    assert metadata_filter.admit(_MetadataFrame([associated_face])) is True
    assert metadata_filter.admit(_MetadataFrame([unassociated_face])) is False
    assert metadata_filter.admit(_MetadataFrame([associated_person])) is False
    assert metadata_filter.admit(_MetadataFrame([])) is False


def test_metadata_filter_is_passthrough_when_unconfigured() -> None:
    metadata_filter = sampler_mod.MetadataObjectFilter()

    assert metadata_filter.admit(object()) is True


def test_metadata_filter_rejects_faces_below_embedding_quality_floor() -> None:
    metadata_filter = sampler_mod.MetadataObjectFilter(
        object_namespace="yolov8_face",
        object_label="face",
        attribute_namespace="face_person_associator",
        attribute_name="person_track_id",
        min_confidence=0.45,
        min_width=40,
        min_height=40,
    )
    attributes = {("face_person_associator", "person_track_id"): object()}

    assert metadata_filter.admit(
        _MetadataFrame(
            [_Object("yolov8_face", "face", attributes, 0.45)]
        )
    ) is True
    assert metadata_filter.admit(
        _MetadataFrame(
            [_Object("yolov8_face", "face", attributes, 0.44)]
        )
    ) is False
    assert metadata_filter.admit(
        _MetadataFrame(
            [
                _Object(
                    "yolov8_face",
                    "face",
                    attributes,
                    0.9,
                    types.SimpleNamespace(width=39.0, height=80.0),
                )
            ]
        )
    ) is False


def test_bounded_queue_drops_non_keyframes_and_preserves_keyframes() -> None:
    queue = queueing_mod.BoundedDropQueue(max_size=2)
    old_key = queueing_mod.ForwarderMessage("cam", "k0", b"k0", "cam", keyframe=True)
    old_delta = queueing_mod.ForwarderMessage("cam", "d0", b"d0", "cam", keyframe=False)
    new_delta = queueing_mod.ForwarderMessage("cam", "d1", b"d1", "cam", keyframe=False)
    new_key = queueing_mod.ForwarderMessage("cam", "k1", b"k1", "cam", keyframe=True)

    assert queue.push(old_key).accepted is True
    assert queue.push(old_delta).accepted is True

    dropped_delta = queue.push(new_delta)
    assert dropped_delta.accepted is False
    assert dropped_delta.dropped == new_delta
    assert dropped_delta.reason == "queue_full"

    evicted = queue.push(new_key)
    assert evicted.accepted is True
    assert evicted.dropped == old_delta
    assert evicted.reason == "evicted_non_keyframe"

    assert queue.pop(timeout_s=0) == old_key
    assert queue.pop(timeout_s=0) == new_key


def test_null_sink_counts_forwarded_without_savant_writer() -> None:
    main_mod = _load_main_module()
    config = main_mod.ForwarderConfig(
        in_endpoint="router+bind:tcp://0.0.0.0:5557",
        out_endpoint="null://diagnostic",
        raw_out_endpoint="",
        analysis_fps="8/1",
        min_fps="2/1",
        sampler_enabled=True,
        queue_max_size=8,
        receive_timeout_ms=100,
        receive_hwm=10,
        send_timeout_ms=100,
        send_retries=0,
        send_hwm=10,
        metrics_port=8081,
    )

    forwarder = main_mod.AnalysisForwarder(config)

    assert isinstance(forwarder.writer, main_mod.NullWriter)
    assert forwarder.metrics.null_sink_enabled == 1
    assert type(forwarder.writer.send_message("cam", object(), b"")).__name__ == (
        "WriterResultSuccess"
    )
    assert "va_forwarder_null_sink_enabled 1" in forwarder.metrics.render_prometheus()


def test_raw_branch_fanout_happens_before_sampling_drop() -> None:
    main_mod = _load_main_module()
    config = main_mod.ForwarderConfig(
        in_endpoint="router+bind:tcp://0.0.0.0:5557",
        out_endpoint="null://diagnostic",
        raw_out_endpoint="pub+bind:tcp://0.0.0.0:5560",
        analysis_fps="1/1",
        min_fps="1/1",
        sampler_enabled=True,
        queue_max_size=8,
        receive_timeout_ms=100,
        receive_hwm=10,
        send_timeout_ms=100,
        send_retries=0,
        send_hwm=10,
        metrics_port=8081,
    )

    forwarder = main_mod.AnalysisForwarder(config)
    frame0 = _Frame("cam", pts=0, keyframe=False)
    frame1 = _Frame("cam", pts=100_000_000, keyframe=False)
    def video_message(frame):
        message = types.SimpleNamespace()
        message.is_video_frame = lambda: True
        message.as_video_frame = lambda: frame
        message.is_end_of_stream = lambda: False
        message.is_shutdown = lambda: False
        return types.SimpleNamespace(message=message, content=b"x")

    item0 = forwarder._build_queue_item(video_message(frame0))
    item1 = forwarder._build_queue_item(video_message(frame1))

    assert item0 is not None
    assert item1 is None
    metrics_text = forwarder.metrics.render_prometheus()
    assert 'va_forwarder_raw_frames_forwarded_total{source_id="cam"} 2' in metrics_text
    assert 'va_forwarder_frames_dropped_total{source_id="cam"} 1' in metrics_text
    raw_writer = forwarder.raw_writer
    assert raw_writer.config["endpoint"] == "pub+bind:tcp://0.0.0.0:5560"
    assert len(raw_writer.sent) == 2


def test_forwarder_filters_frames_without_associated_faces() -> None:
    main_mod = _load_main_module()
    config = main_mod.ForwarderConfig(
        in_endpoint="router+bind:tcp://0.0.0.0:5557",
        out_endpoint="null://diagnostic",
        raw_out_endpoint="",
        analysis_fps="4/1",
        min_fps="1/1",
        sampler_enabled=False,
        queue_max_size=8,
        receive_timeout_ms=100,
        receive_hwm=10,
        send_timeout_ms=100,
        send_retries=0,
        send_hwm=10,
        metrics_port=8081,
        require_object_namespace="yolov8_face",
        require_object_label="face",
        require_attribute_namespace="face_person_associator",
        require_attribute_name="person_track_id",
    )
    forwarder = main_mod.AnalysisForwarder(config)
    unassociated = _Frame("cam", pts=0)
    unassociated.get_all_objects = lambda: [  # type: ignore[attr-defined]
        _Object(namespace="yolov8_face", label="face")
    ]
    associated = _Frame("cam", pts=1)
    associated.get_all_objects = lambda: [  # type: ignore[attr-defined]
        _Object(
            namespace="yolov8_face",
            label="face",
            attributes={("face_person_associator", "person_track_id"): object()},
        )
    ]

    def video_message(frame):
        message = types.SimpleNamespace()
        message.is_video_frame = lambda: True
        message.as_video_frame = lambda: frame
        message.is_end_of_stream = lambda: False
        message.is_shutdown = lambda: False
        return types.SimpleNamespace(message=message, content=b"x")

    assert forwarder._build_queue_item(video_message(unassociated)) is None
    assert forwarder._build_queue_item(video_message(associated)) is not None
    metrics_text = forwarder.metrics.render_prometheus()
    assert 'va_forwarder_metadata_filtered_total{source_id="cam"} 1' in metrics_text
    assert 'va_forwarder_frames_seen_total{source_id="cam"} 2' in metrics_text
    assert 'va_forwarder_frames_dropped_total{source_id="cam"} 1' in metrics_text


def test_phase05_passthrough_probe_is_available() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "spikes" / (
        "check_phase05_savant_rs_passthrough.sh"
    )
    text = script.read_text(encoding="utf-8")

    assert "PASS_PHASE05_S2_MINIMAL_PASSTHROUGH" in text
    assert "savant-deepstream:0.6.0-7.1" in text
    assert "outbound_bytes == inbound_bytes" in text


def test_analysis_forwarder_branch_pressure_probe_is_available() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "spikes" / (
        "check_analysis_forwarder_branch_pressure.py"
    )
    text = script.read_text(encoding="utf-8")

    assert "PASS_ANALYSIS_FORWARDER_BRANCH_30_STREAM_PRESSURE" in text
    assert 'parser.add_argument("--streams", type=int, default=30)' in text
    assert '"--payload-bytes"' in text
    assert "docker" in text
    assert "AnalysisForwarder(config)" in text
