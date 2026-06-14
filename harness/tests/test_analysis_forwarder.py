from __future__ import annotations

import importlib.util
import sys
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


def test_phase05_passthrough_probe_is_available() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "spikes" / (
        "check_phase05_savant_rs_passthrough.sh"
    )
    text = script.read_text(encoding="utf-8")

    assert "PASS_PHASE05_S2_MINIMAL_PASSTHROUGH" in text
    assert "savant-deepstream:0.6.0-7.1" in text
    assert "outbound_bytes == inbound_bytes" in text
