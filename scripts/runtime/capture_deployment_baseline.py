#!/usr/bin/env python3
"""Capture a reproducible, secret-free school deployment baseline.

The manifest records source revision, configuration digest, image identities,
model hashes, migration digest, and GPU/runtime versions. Resolved compose
content is hashed but never written to the manifest because it may contain
credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return 127, ""
    return int(proc.returncode), proc.stdout.strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "present": False}
    stat = path.stat()
    return {
        "path": str(path),
        "present": True,
        "size_bytes": int(stat.st_size),
        "sha256": _sha256_file(path),
    }


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _model_host_path(container_path: str, data_root: Path) -> Path | None:
    text = str(container_path or "").strip()
    if not text:
        return None
    if text.startswith("/models/"):
        return data_root / "models" / text[len("/models/") :]
    if text.startswith("/"):
        return Path(text)
    return None


def _git_state(repo_root: Path) -> dict[str, Any]:
    _, sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    _, status = _run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=repo_root)
    dirty_paths = [line[3:] if len(line) > 3 else line for line in status.splitlines() if line]
    return {
        "commit": sha or None,
        "branch": branch or None,
        "dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
    }


def _compose_args(repo_root: Path, env_file: Path) -> list[str]:
    args = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(repo_root / "infra/docker-compose.midterm.yml"),
    ]
    storage = repo_root / "infra/midterm-storage.override.yml"
    if storage.is_file():
        args.extend(["-f", str(storage)])
    profiles = os.getenv("MIDTERM_COMPOSE_PROFILES", "")
    for profile in (part.strip() for part in profiles.split(",")):
        if profile:
            args.extend(["--profile", profile])
    return args


def _compose_state(repo_root: Path, env_file: Path) -> dict[str, Any]:
    args = _compose_args(repo_root, env_file)
    rc, rendered = _run([*args, "config"], cwd=repo_root, env=os.environ.copy())
    images_rc, images_text = _run(
        [*args, "config", "--images"],
        cwd=repo_root,
        env=os.environ.copy(),
    )
    images = sorted({line.strip() for line in images_text.splitlines() if line.strip()})

    containers: list[dict[str, Any]] = []
    ps_rc, container_ids = _run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=video-analytics-midterm",
            "--format",
            "{{.ID}}",
        ]
    )
    if ps_rc == 0:
        for container_id in (line.strip() for line in container_ids.splitlines()):
            if not container_id:
                continue
            inspect_rc, inspect_text = _run(["docker", "inspect", container_id])
            if inspect_rc != 0 or not inspect_text:
                continue
            try:
                doc = json.loads(inspect_text)
                row = doc[0] if isinstance(doc, list) and doc else {}
            except json.JSONDecodeError:
                continue
            config = row.get("Config") if isinstance(row, dict) else {}
            containers.append(
                {
                    "name": str(row.get("Name") or "").lstrip("/"),
                    "image_ref": str((config or {}).get("Image") or ""),
                    "image_id": str(row.get("Image") or ""),
                }
            )

    return {
        "config_resolved": rc == 0,
        "config_sha256": _sha256_bytes(rendered.encode("utf-8")) if rc == 0 else None,
        "declared_images": images if images_rc == 0 else [],
        "running_containers": sorted(containers, key=lambda item: item.get("name", "")),
    }


def _migration_state(repo_root: Path) -> dict[str, Any]:
    migration_dir = repo_root / "db/migrations"
    rows: list[dict[str, str]] = []
    if migration_dir.is_dir():
        for path in sorted(migration_dir.glob("*.sql")):
            rows.append({"name": path.name, "sha256": _sha256_file(path)})
    digest_input = "\n".join(f"{row['name']} {row['sha256']}" for row in rows).encode("utf-8")
    return {
        "latest_file": rows[-1]["name"] if rows else None,
        "file_count": len(rows),
        "set_sha256": _sha256_bytes(digest_input),
    }


def _runtime_versions() -> dict[str, Any]:
    _, docker_version = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    _, compose_version = _run(["docker", "compose", "version", "--short"])
    gpu_rc, gpu_text = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus: list[dict[str, str]] = []
    if gpu_rc == 0:
        for line in gpu_text.splitlines():
            parts = [part.strip() for part in line.split(",", 3)]
            if len(parts) == 4:
                gpus.append(
                    {
                        "index": parts[0],
                        "name": parts[1],
                        "driver_version": parts[2],
                        "memory_mib": parts[3],
                    }
                )
    return {
        "docker_server": docker_version or None,
        "docker_compose": compose_version or None,
        "gpus": gpus,
    }


def _model_state(data_root: Path, env_file: Path) -> list[dict[str, Any]]:
    env_values = _parse_env_file(env_file)
    candidates: list[Path] = []
    configured = (
        os.getenv("POSE_MODEL_FILE") or env_values.get("POSE_MODEL_FILE", ""),
        os.getenv("FACE_DETECTOR_MODEL_FILE") or env_values.get("FACE_DETECTOR_MODEL_FILE", ""),
    )
    for value in configured:
        path = _model_host_path(value, data_root)
        if path is not None:
            candidates.append(path)
    candidates.extend(
        [
            data_root / "models/adaface/adaface_ir50_webface4m.onnx",
            data_root / "models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return [_file_identity(path) for path in unique]


def capture_baseline(
    *,
    repo_root: Path,
    data_root: Path,
    output: Path,
    phase: str,
    require_clean: bool,
) -> dict[str, Any]:
    env_file = repo_root / "infra/env/midterm.env"
    git_state = _git_state(repo_root)
    if not git_state.get("commit"):
        raise RuntimeError("cannot resolve git commit for deployment baseline")
    if require_clean and git_state.get("dirty"):
        dirty = ", ".join(git_state.get("dirty_paths") or [])
        raise RuntimeError(f"refusing school deployment from a dirty worktree: {dirty}")

    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "phase": phase,
        "hostname": socket.gethostname(),
        "source": git_state,
        "compose": _compose_state(repo_root, env_file),
        "models": _model_state(data_root, env_file),
        "migrations": _migration_state(repo_root),
        "runtime": _runtime_versions(),
        "profiles": [
            item.strip()
            for item in os.getenv("MIDTERM_COMPOSE_PROFILES", "").split(",")
            if item.strip()
        ],
        "reproducible_source": not bool(git_state.get("dirty")),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o644)
    os.replace(temp, output)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--data-root", default=os.getenv("VIDEO_ANALYTICS_DATA_ROOT", "/data/video-analytics"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--phase", default="running")
    parser.add_argument("--require-clean", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[2]
    data_root = Path(args.data_root).resolve()
    output = Path(args.output).resolve() if args.output else data_root / "media/evidence/.deployment-baseline.json"
    try:
        payload = capture_baseline(
            repo_root=repo_root,
            data_root=data_root,
            output=output,
            phase=str(args.phase),
            require_clean=bool(args.require_clean),
        )
    except RuntimeError as exc:
        print(f"deployment baseline error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "commit": payload["source"]["commit"],
                "dirty": payload["source"]["dirty"],
                "config_sha256": payload["compose"]["config_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
