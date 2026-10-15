"""Read-only storage guards for the pressure runner."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts/runtime/pressure_storage_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("pressure_storage_guard", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sources():
    return {
        name: {
            "/media/rolling-cache": "/fast/rolling-cache",
            "/media/rolling-cache-materialized": "/fast/materialized",
        }
        for name in ("rolling-cache-sink-a", "rolling-cache-sink-b", "media-worker")
    }


def _probe(path, *, rotational=False, kind="xfs", free=900_000, total=1_000_000):
    return {
        "path": str(path), "existing_path": str(path), "realpath": str(path),
        "device": "259:3" if str(path).startswith("/fast") else str(path),
        "filesystem_type": kind, "rotational": rotational,
        "total_bytes": total, "available_bytes": free,
        "device_chain": [{"name": "nvme0n1", "rotational": rotational}],
        "is_tmpfs": kind == "tmpfs",
    }


def _preflight(module, **kwargs):
    values = dict(
        sources=_sources(), retention_s=100, camera_count=2,
        bytes_per_second_per_camera=100, materialized_estimate_bytes=2_000,
        evidence_root="/data/evidence", evidence_estimate_bytes=1_000,
        inspector=_probe,
    )
    values.update(kwargs)
    return module.storage_preflight(**values)


def test_nvme_passes_and_aggregates_shared_filesystem_budget():
    module = _load()
    report = _preflight(module)
    assert report["status"] == "passed"
    assert report["estimated_cache_bytes"] == 26_000
    nvme = next(item for item in report["filesystems"] if item["device"] == "259:3")
    assert nvme["estimated_additional_bytes"] == 28_000
    assert nvme["minimum_remaining_fraction"] == 0.2
    assert len(report["services"]) == 3
    assert {item["path"] for item in report["paths"]} >= {"/data", "/dev/shm"}
    json.dumps(report)


def test_rotational_fails_closed_and_preserves_report():
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="rotational") as error:
        _preflight(module, inspector=lambda path: _probe(path, rotational=True))
    assert error.value.report["status"] == "failed"


def test_rotational_override_is_explicit_and_reported():
    module = _load()
    report = _preflight(
        module, allow_rotational=True,
        inspector=lambda path: _probe(path, rotational=True),
    )
    assert report["status"] == "passed"
    assert report["allow_rotational"] is True
    assert report["warnings"]


def test_unknown_storage_cannot_be_bypassed_by_rotational_override():
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="unresolved"):
        _preflight(
            module, allow_rotational=True,
            inspector=lambda path: _probe(path, rotational=None),
        )


def test_tmpfs_passes_without_block_device_metadata():
    module = _load()
    report = _preflight(
        module, inspector=lambda path: _probe(path, rotational=None, kind="tmpfs")
    )
    assert report["status"] == "passed"
    assert all(path["is_tmpfs"] for path in report["paths"])


def test_sink_worker_path_mismatch_fails_before_inspection():
    module = _load()
    sources = _sources()
    sources["media-worker"]["/media/rolling-cache"] = "/other/cache"
    with pytest.raises(module.StoragePreflightError, match="inconsistent"):
        _preflight(module, sources=sources)


def test_missing_service_mount_fails():
    module = _load()
    sources = _sources()
    del sources["rolling-cache-sink-b"]["/media/rolling-cache"]
    with pytest.raises(module.StoragePreflightError, match="missing"):
        _preflight(module, sources=sources)


def test_post_recreation_mounts_must_match_preflight():
    module = _load()
    expected = _sources()
    actual = _sources()
    for mounts in actual.values():
        mounts["/media/rolling-cache"] = "/data/cache"
    with pytest.raises(module.StoragePreflightError, match="expected"):
        module.validate_cache_sources(actual, expected=expected)


def test_shared_filesystem_combined_budget_keeps_twenty_percent():
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="space") as error:
        _preflight(module, inspector=lambda path: _probe(path, free=227_000))
    filesystem = next(item for item in error.value.report["filesystems"] if item["device"] == "259:3")
    assert filesystem["estimated_remaining_bytes"] == 199_000


@pytest.mark.parametrize("role", ["/data/evidence", "/dev/shm"])
def test_data_and_shm_are_checked_separately(role):
    module = _load()
    def inspect(path):
        return _probe(path, free=0 if str(path) == role else 900_000)
    kwargs = {"inspector": inspect}
    if role == "/dev/shm":
        kwargs["shm_estimate_bytes"] = 1
    with pytest.raises(module.StoragePreflightError, match="space"):
        _preflight(module, **kwargs)


@pytest.mark.parametrize("bitrate", [0, -1, float("nan"), float("inf")])
def test_bitrate_must_be_known_and_positive(bitrate):
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="bitrate"):
        _preflight(module, bytes_per_second_per_camera=bitrate)


def test_compose_sources_resolve_parent_mount_and_specific_child():
    module = _load()
    config = {"services": {
        name: {"volumes": [
            {"type": "bind", "source": "/data/media", "target": "/media"},
            {"type": "bind", "source": "/fast/cache", "target": "/media/rolling-cache"},
        ]}
        for name in ("rolling-cache-sink-a", "rolling-cache-sink-b", "media-worker")
    }}
    sources = module.parse_compose_cache_sources(config, ("rolling-cache-sink-a", "rolling-cache-sink-b"))
    assert sources["media-worker"] == {
        "/media/rolling-cache": "/fast/cache",
        "/media/rolling-cache-materialized": "/data/media/rolling-cache-materialized",
    }


def test_compose_named_volume_or_absent_service_fails():
    module = _load()
    config = {"services": {"media-worker": {"volumes": [
        {"type": "volume", "source": "media", "target": "/media"},
    ]}}}
    with pytest.raises(module.StoragePreflightError, match="missing|bind"):
        module.parse_compose_cache_sources(config, ("rolling-cache-sink",))


def test_ffprobe_prefers_format_bitrate_and_can_sum_streams():
    module = _load()
    assert module.ffprobe_bytes_per_second({"format": {"bit_rate": "8000"}}) == 1000
    assert module.ffprobe_bytes_per_second({"streams": [{"bit_rate": "7000"}, {"bit_rate": "1000"}]}) == 1000
    with pytest.raises(module.StoragePreflightError, match="bitrate"):
        module.ffprobe_bytes_per_second({"streams": [{"codec_type": "video"}]})


def _sysfs(tmp_path, rotational="0", dm=False):
    sys = tmp_path / "sys"
    disk = sys / "devices/pci/block/nvme0n1"
    (disk / "queue").mkdir(parents=True)
    (disk / "queue/rotational").write_text(rotational)
    part = disk / "nvme0n1p3"
    part.mkdir()
    (part / "partition").write_text("3")
    (sys / "dev/block").mkdir(parents=True)
    dev = os.makedev(259, 3)
    (sys / f"dev/block/{os.major(dev)}:{os.minor(dev)}").symlink_to(part)
    if dm:
        mapper = sys / "devices/virtual/block/dm-2"
        (mapper / "slaves").mkdir(parents=True)
        (mapper / "slaves/nvme0n1p3").symlink_to(part)
        (sys / "dev/block/253:2").symlink_to(mapper)
    return sys


@pytest.mark.parametrize("dm", [False, True])
def test_inspection_traces_partition_and_lvm_to_physical_leaf(tmp_path, dm):
    module = _load()
    sys = _sysfs(tmp_path, dm=dm)
    dev = os.makedev(253, 2) if dm else os.makedev(259, 3)
    mountinfo = f"1 0 {os.major(dev)}:{os.minor(dev)} / /fast rw - xfs /dev/root rw\n"
    result = module.inspect_path_storage(
        "/fast/cache", sys_root=sys, mountinfo_text=mountinfo,
        stat_fn=lambda path: SimpleNamespace(st_dev=dev),
        statvfs_fn=lambda path: SimpleNamespace(f_frsize=4096, f_blocks=100, f_bavail=80),
        realpath_fn=lambda path: str(path),
    )
    assert result["rotational"] is False
    assert result["filesystem_type"] == "xfs"
    assert result["leaf_devices"] == ["nvme0n1"]
    assert result["available_bytes"] == 80 * 4096
    assert result["device_chain"][0]["name"] == ("dm-2" if dm else "nvme0n1p3")


def test_dm_with_any_rotational_leaf_is_rotational(tmp_path):
    module = _load()
    sys = _sysfs(tmp_path, dm=True)
    disk = sys / "devices/pci/block/sda"
    (disk / "queue").mkdir(parents=True)
    (disk / "queue/rotational").write_text("1")
    (sys / "devices/virtual/block/dm-2/slaves/sda").symlink_to(disk)
    result = module.inspect_path_storage(
        "/fast", sys_root=sys,
        mountinfo_text="1 0 253:2 / /fast rw - xfs /dev/mapper/fast rw\n",
        stat_fn=lambda path: SimpleNamespace(st_dev=os.makedev(253, 2)),
        statvfs_fn=lambda path: SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=800),
        realpath_fn=lambda path: str(path),
    )
    assert result["rotational"] is True
    assert result["leaf_devices"] == ["nvme0n1", "sda"]


def test_inspection_tmpfs_uses_mountinfo_without_sysfs(tmp_path):
    module = _load()
    result = module.inspect_path_storage(
        "/dev/shm/new/cache", sys_root=tmp_path / "missing",
        mountinfo_text="1 0 0:7 / /dev/shm rw - tmpfs shm rw\n",
        stat_fn=lambda path: SimpleNamespace(st_dev=os.makedev(0, 7)),
        statvfs_fn=lambda path: SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=800),
        realpath_fn=lambda path: str(path),
    )
    assert result["is_tmpfs"] is True
    assert result["rotational"] is None


def test_missing_path_uses_existing_parent_without_creating(tmp_path):
    module = _load()
    missing = tmp_path / "new/run/cache"
    result = module.inspect_path_storage(
        missing, mountinfo_text=f"1 0 {os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)} / / rw - tmpfs shm rw\n",
    )
    assert result["existing_path"] == str(tmp_path)
    assert not missing.exists()


def test_inspection_unknown_sysfs_fails_closed(tmp_path):
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="unresolved"):
        module.inspect_path_storage(
            "/fast", sys_root=tmp_path,
            mountinfo_text="1 0 259:3 / /fast rw - xfs /dev/root rw\n",
            stat_fn=lambda path: SimpleNamespace(st_dev=os.makedev(259, 3)),
            statvfs_fn=lambda path: SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=800),
            realpath_fn=lambda path: str(path),
        )


def test_unknown_filesystem_metadata_fails_closed(tmp_path):
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="unresolved filesystem"):
        module.inspect_path_storage(
            "/fast", sys_root=tmp_path, mountinfo_text="",
            stat_fn=lambda path: SimpleNamespace(st_dev=os.makedev(1, 2)),
            realpath_fn=lambda path: str(path),
        )


def test_rotational_metadata_must_be_exact_zero_or_one(tmp_path):
    module = _load()
    sys = _sysfs(tmp_path, rotational="unknown", dm=True)
    with pytest.raises(module.StoragePreflightError, match="unresolved rotational"):
        module.inspect_path_storage(
            "/fast", sys_root=sys,
            mountinfo_text="1 0 253:2 / /fast rw - xfs /dev/mapper/fast rw\n",
            stat_fn=lambda path: SimpleNamespace(st_dev=os.makedev(253, 2)),
            statvfs_fn=lambda path: SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=800),
            realpath_fn=lambda path: str(path),
        )


def test_failed_report_is_strict_json_serializable():
    module = _load()
    with pytest.raises(module.StoragePreflightError) as error:
        _preflight(module, bytes_per_second_per_camera=float("nan"))
    json.dumps(error.value.report, allow_nan=False)


def test_data_demand_is_aggregated_when_evidence_shares_cache_disk():
    module = _load()
    report = _preflight(module, evidence_root="/fast/evidence")
    filesystem = next(item for item in report["filesystems"] if item["device"] == "259:3")
    assert filesystem["estimated_additional_bytes"] == 29_000


def test_standard_storage_override_only_requires_sink_cache_child():
    module = _load()
    config = {"services": {
        service: {"volumes": [
            {"type": "bind", "source": "/data/media", "target": "/media"},
            {"type": "bind", "source": "/fast/rolling-cache", "target": "/media/rolling-cache"},
        ]}
        for service in ("rolling-cache-sink-a", "rolling-cache-sink-b", "media-worker")
    }}
    config["services"]["media-worker"]["volumes"].append({
        "type": "bind", "source": "/fast/materialized", "target": "/media/rolling-cache-materialized",
    })
    sources = module.parse_compose_cache_sources(config, ("rolling-cache-sink-a", "rolling-cache-sink-b"))
    assert sources["rolling-cache-sink-a"] == {"/media/rolling-cache": "/fast/rolling-cache"}
    inspected = []
    def inspect(path):
        inspected.append(str(path))
        return _probe(path, rotational=str(path).startswith("/data"))
    report = _preflight(module, sources=sources, inspector=inspect)
    assert report["status"] == "passed"
    assert "/data/media/rolling-cache-materialized" not in inspected


def test_unused_sink_materialized_mount_does_not_gate_storage():
    module = _load()
    sources = _sources()
    sources["rolling-cache-sink-a"]["/media/rolling-cache-materialized"] = "/data/unused"
    sources["rolling-cache-sink-b"]["/media/rolling-cache-materialized"] = "/other/unused"
    report = _preflight(module, sources=sources)
    assert report["services"]["rolling-cache-sink-a"] == {"/media/rolling-cache": "/fast/rolling-cache"}
    assert not any("unused" in item["path"] for item in report["paths"])


def test_worker_materialized_mount_remains_mandatory():
    module = _load()
    sources = _sources()
    del sources["media-worker"]["/media/rolling-cache-materialized"]
    with pytest.raises(module.StoragePreflightError, match="missing cache mount"):
        _preflight(module, sources=sources)


def test_worker_materialized_disk_is_still_checked():
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="rotational"):
        _preflight(module, inspector=lambda path: _probe(path, rotational=str(path) == "/fast/materialized"))


def test_materialization_reader_cannot_be_omitted():
    module = _load()
    sources = _sources()
    del sources["media-worker"]
    with pytest.raises(module.StoragePreflightError, match="media-worker"):
        _preflight(module, sources=sources)
