#!/usr/bin/env python3
"""Phase 2 entry gate for the single-T4 30-stream pressure test."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import yaml


PASS_TOKEN = "PASS_PHASE2_SINGLE_T4_READY"
DEFAULT_MIN_SOURCES = 30
DEFAULT_GPU_NAME_SUBSTRING = "T4"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def evaluate_phase2_readiness(
    *,
    root: Path,
    sources_config: Path,
    compose_file: Path,
    replay_config: Path,
    phase1_doc: Path,
    gpu_names: list[str],
    min_sources: int = DEFAULT_MIN_SOURCES,
    gpu_name_substring: str = DEFAULT_GPU_NAME_SUBSTRING,
    runtime_overview: dict[str, Any] | None = None,
) -> list[CheckResult]:
    phase1_text = _read_text(phase1_doc)
    sources_doc = _read_yaml(sources_config)
    compose_doc = _read_yaml(compose_file)
    replay_doc = _read_json(replay_config)
    enabled_sources = enabled_rtsp_sources(sources_doc)
    services = compose_doc.get("services") if isinstance(compose_doc, dict) else {}
    services = services if isinstance(services, dict) else {}

    results = [
        CheckResult(
            "phase1_forwarder_passed",
            "PASS_PHASE1_FORWARDER" in phase1_text,
            f"doc={_rel(root, phase1_doc)}",
        ),
        CheckResult(
            "enabled_rtsp_sources",
            len(enabled_sources) >= min_sources,
            f"actual={len(enabled_sources)} required>={min_sources}",
        ),
        CheckResult(
            "gpu_t4_count",
            _matching_gpu_count(gpu_names, gpu_name_substring) >= 1,
            "actual=%d required>=1 names=%s"
            % (_matching_gpu_count(gpu_names, gpu_name_substring), ",".join(gpu_names) or "<none>"),
        ),
        CheckResult(
            "replay_out_stream_targets_raw_fanout",
            _nested_get(replay_doc, "out_stream", "url") == "dealer+connect:tcp://replay-raw-fanout:5557",
            f"url={_nested_get(replay_doc, 'out_stream', 'url')!r}",
        ),
        CheckResult(
            "analysis_forwarder_service_present",
            "analysis-forwarder" in services,
            "service=analysis-forwarder",
        ),
        CheckResult(
            "analysis_forwarder_targets_savant",
            _nested_get(services, "analysis-forwarder", "environment", "FORWARDER_OUT_ENDPOINT")
            == "dealer+connect:tcp://savant-security:5557",
            "endpoint=%r"
            % _nested_get(services, "analysis-forwarder", "environment", "FORWARDER_OUT_ENDPOINT"),
        ),
        CheckResult(
            "source_adapters_still_send_full_rate_to_replay",
            _nested_get(services, "source-adapter", "environment", "ZMQ_ENDPOINT")
            == "dealer+connect:tcp://replay-service:5555",
            "endpoint=%r" % _nested_get(services, "source-adapter", "environment", "ZMQ_ENDPOINT"),
        ),
        CheckResult(
            "savant_depends_on_forwarder",
            _depends_on_service(services.get("savant-security"), "analysis-forwarder"),
            "service=savant-security dependency=analysis-forwarder",
        ),
    ]
    if runtime_overview is not None:
        results.extend(runtime_checks(runtime_overview, min_sources=min_sources))
    return results


def runtime_checks(runtime_overview: dict[str, Any], *, min_sources: int) -> list[CheckResult]:
    data = runtime_overview.get("data") if isinstance(runtime_overview.get("data"), dict) else runtime_overview
    if not isinstance(data, dict):
        data = {}
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    forwarder = data.get("forwarder") if isinstance(data.get("forwarder"), dict) else {}
    containers = data.get("containers") if isinstance(data.get("containers"), dict) else {}
    fixed = containers.get("fixed") if isinstance(containers.get("fixed"), dict) else {}
    analysis_forwarder = (
        fixed.get("analysis_forwarder") if isinstance(fixed.get("analysis_forwarder"), dict) else {}
    )
    return [
        CheckResult(
            "runtime_health_ok",
            health.get("ok") is True,
            f"issues={health.get('issues')!r}",
        ),
        CheckResult(
            "runtime_source_count",
            _int_or_zero(health.get("source_count")) >= min_sources,
            f"actual={_int_or_zero(health.get('source_count'))} required>={min_sources}",
        ),
        CheckResult(
            "runtime_forwarder_metrics_available",
            forwarder.get("available") is True,
            f"available={forwarder.get('available')!r}",
        ),
        CheckResult(
            "runtime_forwarder_container_running",
            analysis_forwarder.get("running") is True,
            f"running={analysis_forwarder.get('running')!r} health={analysis_forwarder.get('health')!r}",
        ),
    ]


def enabled_rtsp_sources(sources_doc: dict[str, Any]) -> list[str]:
    sources = sources_doc.get("sources") if isinstance(sources_doc, dict) else {}
    rows = sources.values() if isinstance(sources, dict) else []
    source_ids: list[str] = []
    for source in rows:
        if not isinstance(source, dict):
            continue
        uri = str(source.get("uri") or "")
        source_id = str(source.get("source_id") or "")
        if (
            source.get("enabled") is True
            and source.get("adapter_type") == "gstreamer"
            and uri.startswith(("rtsp://", "rtsps://"))
            and source_id
        ):
            source_ids.append(source_id)
    return sorted(set(source_ids))


def gpu_names_from_nvidia_smi() -> list[str]:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_gpu_names(completed.stdout)


def parse_gpu_names(text: str) -> list[str]:
    names: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        names.append(line.split(",", 1)[0].strip())
    return names


def load_runtime_overview(*, path: Path | None, url: str | None, timeout_s: float) -> dict[str, Any] | None:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    if not url:
        return None
    try:
        with urlopen(url, timeout=max(timeout_s, 0.1)) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"data": {"health": {"ok": False, "issues": [f"runtime_overview_unavailable:{exc}"]}}}


def _depends_on_service(service: Any, dependency: str) -> bool:
    if not isinstance(service, dict):
        return False
    depends_on = service.get("depends_on")
    if isinstance(depends_on, dict):
        return dependency in depends_on
    if isinstance(depends_on, list):
        return dependency in depends_on
    return False


def _matching_gpu_count(gpu_names: list[str], needle: str) -> int:
    lowered = needle.lower()
    return sum(1 for name in gpu_names if lowered in name.lower())


def _nested_get(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--sources-config", type=Path, default=root / "infra/generated/sources.generated.yml")
    parser.add_argument("--compose-file", type=Path, default=root / "infra/docker-compose.midterm.yml")
    parser.add_argument("--replay-config", type=Path, default=root / "modules/savant_replay/config.midterm.json")
    parser.add_argument(
        "--phase1-doc",
        type=Path,
        default=root / "docs/repair_goal/midterm_phase1_analysis_forwarder_2026-06-15.md",
    )
    parser.add_argument("--min-sources", type=int, default=DEFAULT_MIN_SOURCES)
    parser.add_argument("--gpu-name-substring", default=DEFAULT_GPU_NAME_SUBSTRING)
    parser.add_argument("--gpu-csv-file", type=Path)
    parser.add_argument("--runtime-overview-file", type=Path)
    parser.add_argument("--runtime-overview-url", default="")
    parser.add_argument("--runtime-timeout-s", type=float, default=2.0)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gpu_names = (
        parse_gpu_names(args.gpu_csv_file.read_text(encoding="utf-8"))
        if args.gpu_csv_file is not None
        else gpu_names_from_nvidia_smi()
    )
    runtime_overview = load_runtime_overview(
        path=args.runtime_overview_file,
        url=args.runtime_overview_url,
        timeout_s=args.runtime_timeout_s,
    )
    results = evaluate_phase2_readiness(
        root=args.root,
        sources_config=args.sources_config,
        compose_file=args.compose_file,
        replay_config=args.replay_config,
        phase1_doc=args.phase1_doc,
        gpu_names=gpu_names,
        min_sources=args.min_sources,
        gpu_name_substring=args.gpu_name_substring,
        runtime_overview=runtime_overview,
    )
    ok = all(result.ok for result in results)
    if args.json:
        print(
            json.dumps(
                {
                    "ready": ok,
                    "pass_token": PASS_TOKEN if ok else None,
                    "checks": [
                        {"name": result.name, "ok": result.ok, "detail": result.detail}
                        for result in results
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if ok else 1

    print("check=phase2_single_t4_readiness")
    for result in results:
        prefix = "PASS" if result.ok else "FAIL"
        print(f"{prefix} {result.name} {result.detail}")
    if ok:
        print(PASS_TOKEN)
        return 0
    print("PHASE2_SINGLE_T4_READY=false")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
