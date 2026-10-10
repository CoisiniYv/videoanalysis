#!/usr/bin/env python3
"""Measure a stream's keyframe/PTS distribution and replay it through the analysis sampler.

Answers, for the real pushed stream rather than a synthetic one:

- how many keyframes per second arrive, and how many fall off the regular GOP
  cadence (scene cuts, encoder decisions);
- how regular the PTS is (deltas, non-monotonic steps, missing PTS);
- how many frames the analysis sampler would admit at a given ANALYSIS_FPS,
  both with the legacy budget (keyframes on top of the budget) and with the
  strict budget (keyframes borrow from it), using the forwarder's own
  ``services/analysis-forwarder/app/sampler.py``.

Uses ``ffprobe`` packet metadata only (no decoding). Packet ``K`` flags can
differ from the keyframe flag Savant's VideoFrame carries into the forwarder;
the forwarder metric ``va_forwarder_keyframes_seen_total`` is the sampler's
own view and should be compared with this probe.

Examples:
  python scripts/tools/probe_stream_keyframes.py \\
      --source rtsp://192.168.1.105:8554/live/1080movie --duration-s 300 \\
      --output /tmp/keyframes_1080movie.json
  python scripts/tools/probe_stream_keyframes.py --packets-json saved_ffprobe.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
SAMPLER_PATH = ROOT / "services" / "analysis-forwarder" / "app" / "sampler.py"


@dataclass(frozen=True)
class Packet:
    pts_s: float | None
    keyframe: bool


def build_ffprobe_command(source: str, duration_s: float) -> list[str]:
    command = ["ffprobe", "-v", "error"]
    if source.startswith("rtsp://"):
        command += ["-rtsp_transport", "tcp"]
    command += [
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,flags",
        "-read_intervals",
        f"%+{float(duration_s):g}",
        "-of",
        "json",
        source,
    ]
    return command


def parse_ffprobe_packets(doc: dict[str, Any]) -> list[Packet]:
    packets: list[Packet] = []
    for raw in doc.get("packets") or []:
        pts_text = str(raw.get("pts_time", "") or "")
        try:
            pts_s = float(pts_text)
            if not math.isfinite(pts_s):
                pts_s = None
        except ValueError:
            pts_s = None
        flags = str(raw.get("flags", "") or "")
        packets.append(Packet(pts_s=pts_s, keyframe="K" in flags))
    return packets


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarize_packets(packets: list[Packet]) -> dict[str, Any]:
    timed = [p.pts_s for p in packets if p.pts_s is not None]
    deltas_ms: list[float] = []
    non_monotonic = 0
    previous = None
    for pts in timed:
        if previous is not None:
            if pts <= previous:
                non_monotonic += 1
            else:
                deltas_ms.append((pts - previous) * 1000.0)
        previous = pts
    span_s = (max(timed) - min(timed)) if len(timed) >= 2 else 0.0

    key_indexes = [index for index, p in enumerate(packets) if p.keyframe]
    intervals = [b - a for a, b in zip(key_indexes, key_indexes[1:])]
    interval_counts = Counter(intervals)
    regular_gop = interval_counts.most_common(1)[0][0] if interval_counts else None
    off_cadence = sum(
        count for interval, count in interval_counts.items() if interval != regular_gop
    )
    median_delta = statistics.median(deltas_ms) if deltas_ms else None
    return {
        "packets": len(packets),
        "packets_without_pts": len(packets) - len(timed),
        "pts_span_s": round(span_s, 3),
        "fps_estimate": round(1000.0 / median_delta, 3) if median_delta else None,
        "pts_delta_ms": {
            "min": round(min(deltas_ms), 3) if deltas_ms else None,
            "median": round(median_delta, 3) if median_delta else None,
            "p99": round(_percentile(deltas_ms, 0.99), 3) if deltas_ms else None,
            "max": round(max(deltas_ms), 3) if deltas_ms else None,
        },
        "non_monotonic_pts": non_monotonic,
        "keyframes": len(key_indexes),
        "keyframes_per_s": round(len(key_indexes) / span_s, 4) if span_s else None,
        "regular_gop_frames": regular_gop,
        "keyframe_intervals_off_regular_gop": off_cadence,
        "keyframe_interval_histogram": {
            str(interval): count for interval, count in sorted(interval_counts.items())
        },
    }


def _load_sampler_module():
    spec = importlib.util.spec_from_file_location("analysis_forwarder_sampler", SAMPLER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Frame:
    __slots__ = ("source_id", "pts", "keyframe", "time_base", "content")

    def __init__(self, pts_ns: int | None, keyframe: bool) -> None:
        self.source_id = "probe"
        self.pts = pts_ns
        self.keyframe = keyframe
        self.time_base = (1, 1_000_000_000)
        self.content = None


def replay_sampler(
    packets: Iterable[Packet],
    *,
    analysis_fps: str,
    strict_budget: bool,
    keyframe_debt_s: float = 60.0,
) -> dict[str, Any]:
    sampler_mod = _load_sampler_module()
    sampler = sampler_mod.AnalysisFrameSampler(
        enabled=True,
        max_fps=analysis_fps,
        strict_budget=strict_budget,
        keyframe_debt_s=keyframe_debt_s,
    )
    admitted = keyframes_admitted = over_budget = 0
    first = last = None
    for packet in packets:
        pts_ns = None if packet.pts_s is None else int(round(packet.pts_s * 1e9))
        if pts_ns is not None:
            first = pts_ns if first is None else min(first, pts_ns)
            last = pts_ns if last is None else max(last, pts_ns)
        if sampler.admit(_Frame(pts_ns, packet.keyframe)):
            admitted += 1
            keyframes_admitted += packet.keyframe
            over_budget += sampler.last_decision == "keyframe_over_budget"
    span_s = (last - first) / 1e9 if first is not None and last is not None else 0.0
    budget = float(Fraction(str(analysis_fps)))
    rate = admitted / span_s if span_s else None
    return {
        "strict_budget": strict_budget,
        "keyframe_debt_s": keyframe_debt_s if strict_budget else None,
        "admitted": admitted,
        "admitted_fps": round(rate, 4) if rate is not None else None,
        "admitted_over_budget_ratio": round(rate / budget, 4) if rate else None,
        "keyframes_admitted": keyframes_admitted,
        "keyframes_over_budget": over_budget,
    }


def analyze(packets: list[Packet], *, analysis_fps: str, keyframe_debt_s: float) -> dict[str, Any]:
    return {
        "stream": summarize_packets(packets),
        "analysis_fps": analysis_fps,
        "sampler_legacy": replay_sampler(
            packets, analysis_fps=analysis_fps, strict_budget=False
        ),
        "sampler_strict": replay_sampler(
            packets,
            analysis_fps=analysis_fps,
            strict_budget=True,
            keyframe_debt_s=keyframe_debt_s,
        ),
        "note": (
            "ffprobe packet K flags; compare keyframes_per_s with the forwarder "
            "metric va_forwarder_keyframes_seen_total over the same window."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", help="RTSP URL or media file to probe with ffprobe")
    group.add_argument("--packets-json", help="Saved `ffprobe -show_entries packet=pts_time,flags -of json` output")
    parser.add_argument("--duration-s", type=float, default=300.0)
    parser.add_argument("--analysis-fps", default="4/1")
    parser.add_argument("--keyframe-debt-s", type=float, default=60.0)
    parser.add_argument("--output", help="Write the JSON report here as well as stdout")
    args = parser.parse_args(argv)

    if args.packets_json:
        doc = json.loads(Path(args.packets_json).read_text(encoding="utf-8"))
    else:
        completed = subprocess.run(
            build_ffprobe_command(args.source, args.duration_s),
            check=True,
            capture_output=True,
            text=True,
            timeout=max(60.0, args.duration_s * 2 + 30.0),
        )
        doc = json.loads(completed.stdout or "{}")

    report = analyze(
        parse_ffprobe_packets(doc),
        analysis_fps=args.analysis_fps,
        keyframe_debt_s=args.keyframe_debt_s,
    )
    if args.source:
        report["source"] = args.source
        report["duration_s"] = args.duration_s
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
