"""Stream keyframe/PTS probe: parsing, statistics and sampler replay."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "probe_stream_keyframes.py"
spec = importlib.util.spec_from_file_location("probe_stream_keyframes", SCRIPT)
probe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["probe_stream_keyframes"] = probe
spec.loader.exec_module(probe)

FRAME_S = 1001 / 24000


def _doc(seconds: int, *, gop: int = 12, extra_every: int = 0) -> dict:
    packets = []
    for index in range(int(seconds / FRAME_S)):
        keyframe = index % gop == 0 or (extra_every and index % extra_every == 5)
        packets.append(
            {"pts_time": f"{index * FRAME_S:.6f}", "flags": "K__" if keyframe else "___"}
        )
    return {"packets": packets}


def test_ffprobe_command_reads_packets_only() -> None:
    rtsp = probe.build_ffprobe_command("rtsp://camera/live", 120)
    assert rtsp[:3] == ["ffprobe", "-v", "error"]
    assert "-rtsp_transport" in rtsp and "packet=pts_time,flags" in rtsp
    assert rtsp[rtsp.index("-read_intervals") + 1] == "%+120"
    assert "-rtsp_transport" not in probe.build_ffprobe_command("/media/movie.mp4", 60)


def test_parse_and_summarize_regular_stream() -> None:
    packets = probe.parse_ffprobe_packets(_doc(60))
    stream = probe.summarize_packets(packets)
    assert stream["regular_gop_frames"] == 12
    assert stream["keyframe_intervals_off_regular_gop"] == 0
    assert abs(stream["keyframes_per_s"] - 2.0) < 0.05
    assert abs(stream["fps_estimate"] - 23.976) < 0.01
    assert stream["non_monotonic_pts"] == 0


def test_summary_counts_off_cadence_keyframes_and_bad_pts() -> None:
    doc = _doc(30, extra_every=70)
    doc["packets"].insert(10, {"pts_time": "0.000000", "flags": "___"})
    doc["packets"].append({"pts_time": "N/A", "flags": "___"})
    stream = probe.summarize_packets(probe.parse_ffprobe_packets(doc))
    assert stream["keyframe_intervals_off_regular_gop"] > 0
    assert stream["non_monotonic_pts"] == 1
    assert stream["packets_without_pts"] == 1


def test_sampler_replay_compares_legacy_and_strict_budget() -> None:
    report = probe.analyze(
        probe.parse_ffprobe_packets(_doc(300, extra_every=70)),
        analysis_fps="4/1",
        keyframe_debt_s=60.0,
    )
    legacy = report["sampler_legacy"]
    strict = report["sampler_strict"]
    assert legacy["admitted_fps"] > 4.15
    assert strict["admitted_fps"] <= 4.01
    assert legacy["keyframes_admitted"] == strict["keyframes_admitted"] == report["stream"]["keyframes"]
    assert strict["keyframes_over_budget"] > 0


def test_main_reads_saved_ffprobe_json(tmp_path, capsys) -> None:
    import json

    saved = tmp_path / "packets.json"
    saved.write_text(json.dumps(_doc(20)), encoding="utf-8")
    out = tmp_path / "report.json"
    assert probe.main(["--packets-json", str(saved), "--output", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["stream"]["regular_gop_frames"] == 12
    assert "sampler_strict" in capsys.readouterr().out
