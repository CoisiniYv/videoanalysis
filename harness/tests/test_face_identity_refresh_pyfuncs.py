"""The face gates apply the identity refresh policy before AdaFace.

Drives the real Savant pyfuncs with stubbed Savant modules and fake face
objects through the same sequence: an unclear first face is recognized,
a clear face upgrades it after the minimum interval, and the identified
track is then only recognized again after the refresh interval.
"""

from __future__ import annotations

import importlib
import math
import queue
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = str(REPO_ROOT / "modules" / "savant_security")
MODULES_ROOT = str(REPO_ROOT / "modules")

FRONTAL = [100.0, 200.0, 140.0, 200.0, 120.0, 222.0, 104.0, 240.0, 136.0, 240.0]
UNIT_FEATURE = [1.0 / math.sqrt(512.0)] * 512
# (timestamp ms, face size px): small first face, then clear faces.
SEQUENCE = [(1000, 48.0), (1500, 80.0), (2200, 80.0), (4000, 80.0), (7300, 80.0)]
EXPECTED_SENT = {1000: "first_sighting", 2200: "clear_upgrade", 7300: "refresh"}


class _ObjectMeta:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self.attrs: dict[tuple[str, str], object] = {}

    def add_attr_meta(self, namespace, name, value, confidence=1.0) -> None:
        self.attrs[(namespace, name)] = value

    def get_attr_meta(self, namespace, name):
        if (namespace, name) not in self.attrs:
            return None
        return SimpleNamespace(value=self.attrs[(namespace, name)], confidence=1.0)


def _install_savant_stubs() -> None:
    savant = ModuleType("savant")
    deepstream = ModuleType("savant.deepstream")
    pyfunc = ModuleType("savant.deepstream.pyfunc")
    opencv_utils = ModuleType("savant.deepstream.opencv_utils")
    meta = ModuleType("savant.meta")
    meta_object = ModuleType("savant.meta.object")
    utils = ModuleType("savant.utils")
    image = ModuleType("savant.utils.image")

    class NvDsPyFuncPlugin:
        def __init__(self, **kwargs) -> None:
            pass

    pyfunc.NvDsPyFuncPlugin = NvDsPyFuncPlugin
    opencv_utils.nvds_to_gpu_mat = None
    meta_object.ObjectMeta = _ObjectMeta
    image.GPUImage = None
    savant.deepstream = deepstream
    deepstream.pyfunc = pyfunc
    deepstream.opencv_utils = opencv_utils
    savant.meta = meta
    meta.object = meta_object
    savant.utils = utils
    utils.image = image
    for name, module in {
        "savant": savant,
        "savant.deepstream": deepstream,
        "savant.deepstream.pyfunc": pyfunc,
        "savant.deepstream.opencv_utils": opencv_utils,
        "savant.meta": meta,
        "savant.meta.object": meta_object,
        "savant.utils": utils,
        "savant.utils.image": image,
    }.items():
        sys.modules[name] = module


@pytest.fixture()
def pyfuncs(monkeypatch):
    for name in (
        "FACE_REID_MIN_INTERVAL_MS",
        "FACE_IDENTITY_REFRESH_MS",
        "FACE_CLEAR_MIN_SIZE_PX",
        "FACE_CLEAR_MIN_CONFIDENCE",
        "FACE_CLEAR_MAX_YAW_RATIO",
        "FACE_IDENTITY_STATE_TTL_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    sys.path[:] = [
        p for p in sys.path if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)
    _install_savant_stubs()
    return {
        "roi": importlib.import_module("custom.pyfuncs.face_roi_exporter"),
        "candidate": importlib.import_module("custom.pyfuncs.face_reid_candidate_gate"),
        "gate": importlib.import_module("custom.pyfuncs.face_reid_gate"),
    }


def _face(size: float, *, track_id: int = 7, element: str = "yolov8_face", feature=None):
    face = _ObjectMeta(
        label="face",
        element_name=element,
        confidence=0.85,
        bbox=SimpleNamespace(xc=120.0, yc=220.0, width=size, height=size * 1.2),
        uid=11,
        track_id=0,
    )
    face.add_attr_meta("yolov8_face", "landmarks", list(FRONTAL))
    face.add_attr_meta("face_person_associator", "person_track_id", track_id)
    face.add_attr_meta("face_person_associator", "association_method", "inside_person")
    face.add_attr_meta("face_person_associator", "association_score", 0.9)
    if feature is not None:
        face.add_attr_meta("adaface", "feature", list(feature))
    return face


class _Frame:
    def __init__(self, ts_ms: int, objects: list) -> None:
        self.source_id = "src1"
        self.pts = ts_ms * 1_000_000
        self.frame_num = ts_ms // 100
        self.objects = list(objects)
        self.added: list = []
        self.frame_meta = None

    def add_obj_meta(self, obj) -> None:
        self.added.append(obj)
        self.objects.append(obj)


def test_roi_exporter_sends_first_upgrade_and_refresh_only(pyfuncs) -> None:
    plugin = pyfuncs["roi"].FaceRoiExporterPyFunc(
        enabled=False, min_interval_ms=1000, identity_refresh_ms=5000
    )
    plugin._enabled = True
    plugin._crop_queue = queue.Queue()
    sent: dict[int, str] = {}

    def fake_export(buffer, frame_meta, eligible, **context):
        # Mirrors the real path: record each face after it is queued.
        for _, _, inp, verdict in eligible:
            sent[inp.timestamp_ms] = verdict.identity_reason
            plugin._identity.record(verdict.throttle_key, inp.timestamp_ms, bool(verdict.clear))

    plugin._export_eligible = fake_export
    for ts_ms, size in SEQUENCE:
        untracked = _face(size, track_id=0)
        plugin.process_frame(None, _Frame(ts_ms, [_face(size), untracked]))

    assert sent == EXPECTED_SENT
    counters = plugin._counters
    assert counters["throttled"] == 2
    assert counters["gate_rejected"] == len(SEQUENCE)
    assert counters["identity_first_sighting"] == 1
    assert counters["identity_clear_upgrade"] == 1
    assert counters["identity_refresh"] == 1
    assert counters["identity_min_interval"] == 1
    assert counters["identity_refresh_wait"] == 1


def test_roi_exporter_backs_off_dropped_faces_as_unclear(pyfuncs) -> None:
    plugin = pyfuncs["roi"].FaceRoiExporterPyFunc(enabled=False, identity_refresh_ms=5000)
    plugin._enabled = True
    plugin._crop_queue = queue.Queue(maxsize=1)
    plugin._crop_queue.put_nowait(object())

    plugin.process_frame(None, _Frame(1000, [_face(80.0)]))
    assert plugin._counters["crop_queue_dropped"] == 1
    # A dropped face was never recognized: a clear face may retry after the
    # minimum interval instead of waiting for the full refresh interval.
    plugin.process_frame(None, _Frame(2100, [_face(80.0)]))
    assert plugin._counters["identity_clear_upgrade"] == 1


def test_roi_exporter_refresh_zero_restores_one_second_cadence(pyfuncs) -> None:
    plugin = pyfuncs["roi"].FaceRoiExporterPyFunc(
        enabled=False, min_interval_ms=1000, identity_refresh_ms=0
    )
    plugin._enabled = True
    plugin._crop_queue = queue.Queue()
    sent: list[int] = []

    def fake_export(buffer, frame_meta, eligible, **context):
        for _, _, inp, verdict in eligible:
            sent.append(inp.timestamp_ms)
            plugin._identity.record(verdict.throttle_key, inp.timestamp_ms, bool(verdict.clear))

    plugin._export_eligible = fake_export
    for ts_ms in (1000, 1500, 2000, 3000, 4000):
        plugin.process_frame(None, _Frame(ts_ms, [_face(80.0)]))
    assert sent == [1000, 2000, 3000, 4000]


def test_candidate_gate_clones_only_policy_admitted_faces(pyfuncs) -> None:
    plugin = pyfuncs["candidate"].FaceReidCandidateGatePyFunc(
        face_reid_min_interval_ms=1000, identity_refresh_ms=5000
    )
    created: dict[int, str] = {}
    for ts_ms, size in SEQUENCE:
        frame = _Frame(ts_ms, [_face(size)])
        plugin.process_frame(None, frame)
        for candidate in frame.added:
            created[ts_ms] = candidate.attrs[("face_reid_candidate", "identity_refresh_reason")]
    assert created == EXPECTED_SENT


def test_post_embedding_gate_exports_only_policy_admitted_faces(pyfuncs) -> None:
    plugin = pyfuncs["gate"].FaceReidGatePyFunc(
        face_reid_min_interval_ms=1000, identity_refresh_ms=5000
    )
    allowed: dict[int, bool] = {}
    for ts_ms, size in SEQUENCE:
        face = _face(size, feature=UNIT_FEATURE)
        plugin.process_frame(None, _Frame(ts_ms, [face]))
        allowed[ts_ms] = face.attrs[("face_reid_gate", "reid_allowed")]
        if not allowed[ts_ms]:
            assert face.attrs[("face_reid_gate", "reid_skip_reason")] == "throttled"
    assert {ts for ts, ok in allowed.items() if ok} == set(EXPECTED_SENT)
    assert plugin._counters["identity_refresh"] == 1
