"""Apply video-analytics overlay patches to the installed Savant framework.

Savant v0.6.0 can remove a source registry entry after a PTS-reset-triggered
source reset while stale buffers for that source are still in flight. Those
buffers can later hit unguarded ``self._sources.get_source(...)`` calls and
raise ``KeyError``, which stops the whole module. This script copies narrowly
scoped patched framework files over the installed Savant package before the
module starts.

The overlay is intentionally pinned to known v0.6.0 md5 baselines. Unknown
framework files fail loudly unless ``SAVANT_PATCH_ENFORCE=false`` is set.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
from pathlib import Path

PATCH_DIR = Path(__file__).resolve().parent / "v0.6.0"

# (relative path inside the installed savant package,
#  patch file name in PATCH_DIR,
#  md5 of the pristine v0.6.0 file,
#  md5 of the patched file)
PATCHES = (
    (
        "deepstream/buffer_processor.py",
        "buffer_processor.py",
        "ab13b915a8a7fc7acd6265d054054036",
        "556af89b356401efa1dc9d5c2c4d3c68",
    ),
    (
        "deepstream/nvinfer/processor.py",
        "nvinfer_processor.py",
        "e6a05fb0e0eb7dd04c9d01e4fc2bb80f",
        "9615f7cd134f623a3950b65e5ad71fb1",
    ),
    (
        "deepstream/pipeline.py",
        "pipeline.py",
        "5c418afd69e9d478a43cc1a123513837",
        "2c52ffc83caa10e07bac0bbff94850c0",
    ),
)


def _boolish(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fobj:
        for chunk in iter(lambda: fobj.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locate_savant_package() -> Path:
    spec = importlib.util.find_spec("savant")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("cannot locate installed savant package")
    return Path(list(spec.submodule_search_locations)[0])


def _drop_stale_pyc(target: Path) -> None:
    pycache = target.parent / "__pycache__"
    if not pycache.is_dir():
        return
    stem = target.stem + "."
    for cached in pycache.glob("*.pyc"):
        if cached.name.startswith(stem):
            try:
                cached.unlink()
            except OSError:
                pass


def main() -> int:
    enabled = _boolish(os.environ.get("SAVANT_PATCH_ENABLED"), True)
    enforce = _boolish(os.environ.get("SAVANT_PATCH_ENFORCE"), True)
    if not enabled:
        print("savant_patches: disabled via SAVANT_PATCH_ENABLED", flush=True)
        return 0

    try:
        package_root = _locate_savant_package()
    except Exception as exc:  # noqa: BLE001
        print(f"savant_patches: ERROR locating savant package: {exc}", flush=True)
        return 1 if enforce else 0

    print(f"savant_patches: savant package at {package_root}", flush=True)

    failures = 0
    for rel_target, patch_name, original_md5, patched_md5 in PATCHES:
        target = package_root / rel_target
        patch_file = PATCH_DIR / patch_name

        if not patch_file.is_file():
            print(f"savant_patches: ERROR missing patch file {patch_file}", flush=True)
            failures += 1
            continue
        if _md5(patch_file) != patched_md5:
            print(
                f"savant_patches: ERROR patch file {patch_file} md5 mismatch "
                "(corrupted checkout?)",
                flush=True,
            )
            failures += 1
            continue
        if not target.is_file():
            print(f"savant_patches: ERROR target not found: {target}", flush=True)
            failures += 1
            continue

        current = _md5(target)
        if current == patched_md5:
            print(f"savant_patches: already patched: {rel_target}", flush=True)
            continue
        if current != original_md5:
            print(
                f"savant_patches: ERROR {rel_target} md5={current} does not "
                f"match v0.6.0 baseline {original_md5}; refusing to patch an "
                "unknown framework version",
                flush=True,
            )
            failures += 1
            continue

        shutil.copyfile(patch_file, target)
        _drop_stale_pyc(target)
        if _md5(target) != patched_md5:
            print(f"savant_patches: ERROR verification failed for {rel_target}", flush=True)
            failures += 1
            continue
        print(f"savant_patches: patched {rel_target}", flush=True)

    if failures:
        msg = f"savant_patches: {failures} patch step(s) failed"
        if enforce:
            print(msg + " (SAVANT_PATCH_ENFORCE=true -> aborting start)", flush=True)
            return 1
        print(msg + " (SAVANT_PATCH_ENFORCE=false -> continuing unpatched)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
