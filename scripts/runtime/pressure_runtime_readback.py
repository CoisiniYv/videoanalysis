"""Read actual pressure-runtime configuration without creating containers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


FORWARDER_ENV_KEYS = (
    "FORWARDER_MAX_QUEUE_AGE_MS", "FORWARDER_STRICT_FPS_BUDGET", "FORWARDER_QUEUE_MAX_SIZE",
)
ROI_ENV_KEYS = (
    "FACE_ROI_EXPORT_ENABLED", "FACE_OBSERVATION_EXPORT_ENABLED", "ADAFACE_INPUT_OBJECT",
)
FORWARDER_CODE_FILES = ("main.py", "queueing.py", "sampler.py")
CONTAINER_PREFIX = "video-analytics-midterm-"


class RuntimeReadbackError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report if report is not None else {"status": "failed", "errors": [message]}


def runtime_readback_services(cfg: Any) -> dict[str, Any]:
    direct = bool(cfg.rolling_cache_evidence and cfg.dual_shard_same_gpu)
    dual = bool(cfg.dual_shard_same_gpu or cfg.dual_shard_api)
    if direct:
        forwarders = ["replay-raw-fanout-a", "replay-raw-fanout-b"]
    elif dual:
        forwarders = ["analysis-forwarder-a", "analysis-forwarder-b"]
    else:
        forwarders = ["analysis-forwarder"]
    return {
        "mode": "direct_rolling_cache" if direct else "dual" if dual else "single",
        "forwarders": forwarders,
        "savant": ["savant-a", "savant-b"] if dual else ["savant"],
    }


def _env_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def expected_forwarder_environment(
    config: Mapping[str, Any], services: Sequence[str]
) -> dict[str, dict[str, str]]:
    """Extract expectations from the exact pressure compose rendering."""
    result: dict[str, dict[str, str]] = {}
    for service in services:
        environment = ((config.get("services") or {}).get(service) or {}).get("environment") or {}
        if not isinstance(environment, Mapping):
            raise RuntimeReadbackError(f"expected normalized compose environment for {service}")
        missing = [key for key in FORWARDER_ENV_KEYS if environment.get(key) is None]
        if missing:
            raise RuntimeReadbackError(f"missing expected forwarder settings for {service}: {missing}")
        result[service] = {key: _env_string(environment[key]) for key in FORWARDER_ENV_KEYS}
    return result


def validate_rendered_pressure_config(cfg: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the configuration that this pressure run would render.

    This is deliberately independent of the containers left by an earlier
    pressure run.  A pure preflight must answer whether the requested Compose
    files and generated pressure override contain the values that will be
    applied when the run starts.
    """
    selection = runtime_readback_services(cfg)
    services = config.get("services") or {}
    report: dict[str, Any] = {
        "status": "checking",
        "mode": selection["mode"],
        "forwarders": {},
        "savant": {},
        "roi": {},
        "code": {
            "status": "unknown",
            "reason": "pure_preflight_does_not_create_a_running_container",
        },
        "errors": [],
    }

    direct = bool(cfg.rolling_cache_evidence and cfg.dual_shard_same_gpu)
    expected_queue_size = "8192" if direct else "2048"
    expected_forwarder = {
        "FORWARDER_MAX_QUEUE_AGE_MS": str(cfg.analysis_max_queue_age_ms),
        "FORWARDER_STRICT_FPS_BUDGET": "true",
        "FORWARDER_QUEUE_MAX_SIZE": expected_queue_size,
    }

    def environment_for(service: str) -> Mapping[str, Any]:
        spec = services.get(service) or {}
        environment = spec.get("environment") or {}
        if not isinstance(environment, Mapping):
            raise RuntimeReadbackError(f"rendered environment for {service} is not a mapping")
        return environment

    for service in selection["forwarders"]:
        environment = environment_for(service)
        observed = {key: _env_string(environment[key]) if key in environment else None
                    for key in FORWARDER_ENV_KEYS}
        mismatches = {
            key: {"expected": value, "observed": observed[key]}
            for key, value in expected_forwarder.items()
            if observed[key] != value
        }
        report["forwarders"][service] = {
            "expected": expected_forwarder.copy(),
            "observed": observed,
            "mismatches": mismatches,
        }
        if mismatches:
            report["errors"].append(
                f"rendered forwarder settings mismatch for {service}: {mismatches}"
            )

    expected_module = (
        str((Path(str(getattr(cfg, "artifact_dir", ""))) / "module.pressure.yml").resolve())
        if cfg.dual_shard_same_gpu
        else "module.yml"
    )
    expected_savant = {
        "FACE_INFER_INTERVAL": str(cfg.face_infer_interval),
        "FACE_EMBEDDING_INFER_INTERVAL": str(cfg.face_embedding_infer_interval),
        "SAVANT_MODULE_FILE": expected_module,
    }
    for service in selection["savant"]:
        environment = environment_for(service)
        observed = {key: _env_string(environment[key]) if key in environment else None
                    for key in expected_savant}
        mismatches = {
            key: {"expected": value, "observed": observed[key]}
            for key, value in expected_savant.items()
            if observed[key] != value
        }
        report["savant"][service] = {
            "expected": expected_savant.copy(),
            "observed": observed,
            "mismatches": mismatches,
        }
        if mismatches:
            report["errors"].append(
                f"rendered Savant settings mismatch for {service}: {mismatches}"
            )

    expected_roi = {
        "FACE_ROI_EXPORT_ENABLED": "true" if cfg.adaface_roi_redis else "false",
        "FACE_OBSERVATION_EXPORT_ENABLED": "false" if cfg.adaface_roi_redis else "true",
        "ADAFACE_INPUT_OBJECT": "disabled.face" if cfg.adaface_roi_redis else "yolov8_face.face",
    }
    for service in selection["savant"]:
        environment = environment_for(service)
        observed = {key: _env_string(environment[key]) if key in environment else None
                    for key in ROI_ENV_KEYS}
        mismatches = {
            key: {"expected": value, "observed": observed[key]}
            for key, value in expected_roi.items()
            if observed[key] != value
        }
        report["roi"][service] = {
            "expected": expected_roi.copy(),
            "observed": observed,
            "mismatches": mismatches,
        }
        if mismatches:
            report["errors"].append(
                f"rendered ROI settings mismatch for {service}: {mismatches}"
            )

    if report["errors"]:
        report["status"] = "failed"
        raise RuntimeReadbackError("; ".join(report["errors"]), report)
    report["status"] = "passed"
    return report


def _parse_code_hashes(output: str) -> dict[str, str]:
    result = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(/app/app/(?:main|queueing|sampler)\.py)", line.strip())
        if match is None or match[2] in result:
            raise RuntimeReadbackError("invalid or duplicate forwarder code hash output")
        result[match[2]] = match[1].lower()
    expected_paths = {f"/app/app/{name}" for name in FORWARDER_CODE_FILES}
    if set(result) != expected_paths:
        raise RuntimeReadbackError(f"incomplete forwarder code hashes: {sorted(result)}")
    return result


def collect_runtime_readback(
    cfg: Any, *, expected_forwarder_env: Mapping[str, Mapping[str, str]],
    docker_env: Callable[[str], Mapping[str, str]],
    run_command: Callable[[list[str]], Any], source_root: str | Path,
    docker_image_id: Callable[[str], str | None] | None = None,
    running_container_for_image: Callable[[str], str | None] | None = None,
    roi_mode: bool | None = None,
    read_savant: bool = True,
) -> dict[str, Any]:
    """Fail closed on environment or source mismatches; callbacks only read Docker."""
    selection = runtime_readback_services(cfg)
    roi_mode = bool(cfg.adaface_roi_redis) if roi_mode is None else roi_mode
    report: dict[str, Any] = {
        "status": "checking", "mode": selection["mode"], "forwarders": {},
        "savant": {}, "source_root": str(Path(source_root).resolve()),
        "roi_mode": roi_mode, "errors": [],
    }
    try:
        expectations: dict[str, dict[str, str]] = {}
        for service in selection["forwarders"]:
            values = expected_forwarder_env.get(service) or {}
            missing = [key for key in FORWARDER_ENV_KEYS if values.get(key) is None]
            if missing:
                raise RuntimeReadbackError(f"missing expected forwarder settings for {service}: {missing}")
            expectations[service] = {key: _env_string(values[key]) for key in FORWARDER_ENV_KEYS}
            if expectations[service]["FORWARDER_MAX_QUEUE_AGE_MS"] != str(cfg.analysis_max_queue_age_ms):
                raise RuntimeReadbackError(f"rendered queue age differs from request for {service}: expected={expectations[service]['FORWARDER_MAX_QUEUE_AGE_MS']}, requested={cfg.analysis_max_queue_age_ms}")
        local_root = Path(source_root) / "services/analysis-forwarder/app"
        expected_hashes = {
            f"/app/app/{name}": hashlib.sha256((local_root / name).read_bytes()).hexdigest()
            for name in FORWARDER_CODE_FILES
        }
        report["expected_code_sha256"] = expected_hashes
        for service in selection["forwarders"]:
            container = CONTAINER_PREFIX + service
            record: dict[str, Any] = {
                "container": container, "expected_env": expectations[service],
                "observed_env": {}, "mismatches": {}, "code": {"status": "unavailable"},
            }
            report["forwarders"][service] = record
            try:
                actual = docker_env(container)
                record["observed_env"] = {key: actual.get(key) for key in FORWARDER_ENV_KEYS}
                record["mismatches"] = {
                    key: {"expected": expectations[service][key], "observed": actual.get(key)}
                    for key in FORWARDER_ENV_KEYS
                    if actual.get(key) != expectations[service][key]
                }
                if record["mismatches"]:
                    report["errors"].append(f"forwarder environment mismatch for {service}: {record['mismatches']}")
            except Exception as exc:
                report["errors"].append(f"forwarder environment readback failed for {service}: {exc}")
            code_container = container
            if docker_image_id is not None or running_container_for_image is not None:
                record["image_id"] = None
                record["code"]["status"] = "unknown"
                try:
                    if docker_image_id is None or running_container_for_image is None:
                        raise RuntimeReadbackError("image identity callbacks are incomplete")
                    image_id = docker_image_id(container)
                    record["image_id"] = image_id
                    if not image_id:
                        record["code"]["reason"] = "target_container_image_id_unavailable"
                        continue
                    code_container = running_container_for_image(image_id)
                    if not code_container:
                        record["code"]["reason"] = "no_running_container_with_target_image_id"
                        continue
                    record["code_container"] = code_container
                except Exception as exc:
                    record["code"]["reason"] = f"image_identity_readback_failed: {exc}"
                    continue
            command = ["docker", "exec", code_container, "sha256sum", *expected_hashes]
            record["code"]["command"] = command
            try:
                completed = run_command(command)
                record["code"]["returncode"] = completed.returncode
                if completed.returncode != 0:
                    raise RuntimeReadbackError(f"docker exec code hash readback failed for {service}: rc={completed.returncode}")
                output = completed.stdout.decode("utf-8") if isinstance(completed.stdout, bytes) else str(completed.stdout or "")
                hashes = _parse_code_hashes(output)
                record["code"]["observed_sha256"] = hashes
                record["code"]["mismatches"] = {
                    path: {"expected": digest, "observed": hashes[path]}
                    for path, digest in expected_hashes.items() if hashes[path] != digest
                }
                record["code"]["status"] = "mismatch" if record["code"]["mismatches"] else "matched"
                if record["code"]["mismatches"]:
                    report["errors"].append(f"forwarder code mismatch for {service}: {record['code']['mismatches']}")
            except Exception as exc:
                record["code"]["error"] = str(exc)
                report["errors"].append(str(exc))
        if read_savant:
            savant_expected = {
                "FACE_INFER_INTERVAL": str(cfg.face_infer_interval),
                "FACE_EMBEDDING_INFER_INTERVAL": str(cfg.face_embedding_infer_interval),
            }
            for service in selection["savant"]:
                container = CONTAINER_PREFIX + service
                record = {
                    "container": container, "expected_env": savant_expected.copy(),
                    "observed_env": {}, "mismatches": {},
                    "embedding_interval_applicability": "not_applicable_roi_mode" if roi_mode else "applicable",
                }
                report["savant"][service] = record
                try:
                    actual = docker_env(container)
                    record["observed_env"] = {key: actual.get(key) for key in savant_expected}
                    record["mismatches"] = {
                        key: {"expected": value, "observed": actual.get(key)}
                        for key, value in savant_expected.items()
                        if not (roi_mode and key == "FACE_EMBEDDING_INFER_INTERVAL")
                        and actual.get(key) != value
                    }
                    if record["mismatches"]:
                        report["errors"].append(f"Savant environment mismatch for {service}: {record['mismatches']}")
                except Exception as exc:
                    report["errors"].append(f"Savant environment readback failed for {service}: {exc}")
        else:
            report["savant_readback"] = "not_applicable_savant_not_rebuilt"
        if report["errors"]:
            raise RuntimeReadbackError("; ".join(report["errors"]))
        report["status"] = "passed"
        return report
    except (RuntimeReadbackError, OSError, ValueError, TypeError) as exc:
        report["status"] = "failed"
        if str(exc) not in report["errors"] and not report["errors"]:
            report["errors"].append(str(exc))
        raise RuntimeReadbackError(str(exc), report) from exc
