"""Tests for the savant_security rule registry.

Midterm mainline entrypoint:
- intrusion is the only registered rule.
- build_rules instantiates concrete rules from a CameraConfig.
- The rules tree imports only pure Python (no ``savant`` package).
- Multi-camera cooldown keys do not collide even when track_ids match.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _isolate_savant_security_modules():
    """Purge every cross-runtime ``custom.*`` cache and pin sys.path to
    ``modules/savant_security`` only.

    Other harness test files inject sibling ``archived or alternate module``
    paths and import ``custom.models`` from there. Python caches the
    first hit in ``sys.modules`` and reuses it for our tests, so the
    rules tree would silently bind to sibling's copies. We purge both
    caches before every test to keep this entrypoint suite isolated.
    """
    # Drop any runtime sibling modules from sys.path.
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)

    # Drop every cached custom.* module so re-import picks our copy.
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


def _reset_registry_module():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.rules")


@pytest.fixture()
def rules_pkg():
    return _reset_registry_module()


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------

def test_registry_exposes_intrusion(rules_pkg):
    assert rules_pkg.REGISTRY.has("intrusion")
    assert "intrusion" in rules_pkg.REGISTRY.known_rule_types()
    assert rules_pkg.REGISTRY.has("loitering")
    assert rules_pkg.REGISTRY.has("running")


def test_register_rule_rejects_duplicate(rules_pkg):
    with pytest.raises(ValueError):
        rules_pkg.REGISTRY.register("intrusion", lambda *a, **k: None)


def test_register_rule_rejects_empty_type(rules_pkg):
    with pytest.raises(ValueError):
        rules_pkg.REGISTRY.register("", lambda *a, **k: None)


def test_get_factory_unknown_raises(rules_pkg):
    with pytest.raises(KeyError):
        rules_pkg.REGISTRY.get_factory("wall_climb")


# ---------------------------------------------------------------------------
# build_rules from CameraConfig
# ---------------------------------------------------------------------------

def _zone(name="perimeter"):
    from custom.models.camera_config import ZoneConfig
    return ZoneConfig(name=name, polygon=[(0, 0), (100, 0), (100, 100), (0, 100)])


def _intrusion_cfg(name="intrusion_perimeter", zone="perimeter", enabled=True):
    from custom.models.camera_config import RuleConfig
    return RuleConfig(
        name=name,
        rule_type="intrusion",
        zone=zone,
        enabled=enabled,
        min_inside_ms=500,
        cooldown_s=60,
    )


def test_build_rules_returns_intrusion_instance(rules_pkg):
    from custom.models.camera_config import CameraConfig
    from custom.services.cooldown import CooldownTracker

    cfg = CameraConfig(
        camera_id="cam_01",
        zones={"perimeter": _zone()},
        rules={"intrusion_perimeter": _intrusion_cfg()},
    )
    rules = rules_pkg.build_rules(cfg, CooldownTracker())
    assert len(rules) == 1
    assert rules[0].rule_type == "intrusion"
    assert rules[0].config.name == "intrusion_perimeter"
    assert rules[0].zone.name == "perimeter"


def test_build_rules_skips_disabled(rules_pkg):
    from custom.models.camera_config import CameraConfig
    from custom.services.cooldown import CooldownTracker

    cfg = CameraConfig(
        camera_id="cam_01",
        zones={"perimeter": _zone()},
        rules={"intrusion_perimeter": _intrusion_cfg(enabled=False)},
    )
    rules = rules_pkg.build_rules(cfg, CooldownTracker())
    assert rules == []


def test_build_rules_skips_unknown_rule_type(rules_pkg, capsys):
    from custom.models.camera_config import CameraConfig, RuleConfig
    from custom.services.cooldown import CooldownTracker

    cfg = CameraConfig(
        camera_id="cam_01",
        zones={"perimeter": _zone()},
        rules={
            "intrusion_perimeter": _intrusion_cfg(),
            "wall_climb_perimeter": RuleConfig(
                name="wall_climb_perimeter",
                rule_type="wall_climb",
                zone="perimeter",
                enabled=True,
            ),
        },
    )
    rules = rules_pkg.build_rules(cfg, CooldownTracker())
    # Wall-climb is still deferred because it needs line/墙体 configuration.
    assert [r.rule_type for r in rules] == ["intrusion"]
    captured = capsys.readouterr().out
    assert "stage=savant_security_rule_registry_unknown" in captured
    assert "rule_type=wall_climb" in captured


# ---------------------------------------------------------------------------
# Multi-camera cooldown key isolation
# ---------------------------------------------------------------------------

def test_cooldown_key_includes_camera_id(rules_pkg):
    """Same track_id from two different cameras must not share a cooldown slot."""
    from custom.models.camera_config import CameraConfig
    from custom.models.pose import BBox, PersonPoseObservation
    from custom.models.tracks import TrackState
    from custom.services.cooldown import CooldownTracker

    cfg = CameraConfig(
        camera_id="cam_01",
        zones={"perimeter": _zone()},
        rules={"intrusion_perimeter": _intrusion_cfg()},
    )
    cooldown = CooldownTracker()
    rule = rules_pkg.build_rules(cfg, cooldown)[0]

    def _track(camera_id: str, ts_ms: int) -> TrackState:
        t = TrackState(track_id=7)
        t.add_observation(
            PersonPoseObservation(
                source_id=camera_id,
                camera_id=camera_id,
                frame_id=0,
                timestamp_ms=ts_ms,
                bbox=BBox(x=40, y=40, width=10, height=10),
                confidence=0.9,
                track_id=7,
            ),
            window_s=10.0,
        )
        return t

    key_a = rule.cooldown_key(_track("cam_01", 0))
    key_b = rule.cooldown_key(_track("cam_02", 0))
    assert key_a != key_b
    assert key_a == "cam_01:7:perimeter"
    assert key_b == "cam_02:7:perimeter"


# ---------------------------------------------------------------------------
# Architectural guard: rules layer must not import Savant
# ---------------------------------------------------------------------------

def test_rules_tree_does_not_import_savant():
    """Walk every .py under custom/rules, custom/models, custom/services,
    custom/geometry, custom/adapters and assert no module imports
    ``savant`` or ``pyds`` at module scope.

    The pyfunc layer (``custom/pyfuncs/behavior_rules.py``) is allowed
    to import Savant — it runs inside the container only.
    """
    _isolate_savant_security_modules()
    forbidden = ("savant", "pyds")
    pure_python_subtrees = [
        Path(MODULE_DIR) / "custom" / "rules",
        Path(MODULE_DIR) / "custom" / "models",
        Path(MODULE_DIR) / "custom" / "services",
        Path(MODULE_DIR) / "custom" / "geometry",
        Path(MODULE_DIR) / "custom" / "adapters",
    ]
    offenders = []
    for tree in pure_python_subtrees:
        for path in tree.rglob("*.py"):
            text = path.read_text()
            for token in forbidden:
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if stripped.startswith(("import ", "from ")) and (
                        f" {token}" in f" {stripped} "
                        or stripped.startswith(f"import {token}")
                        or stripped.startswith(f"from {token}")
                    ):
                        offenders.append(f"{path}: {stripped}")
    assert not offenders, "Rules layer must not import Savant/pyds: " + ", ".join(offenders)


def test_rules_package_imports_without_savant_installed():
    """Spawn a subprocess with ``savant`` masked to ImportError and verify
    that ``import custom.rules`` still succeeds. Belt-and-braces guard
    that complements ``test_rules_tree_does_not_import_savant``.
    """
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {MODULE_DIR!r})
        # Mask the savant package so any latent import explodes loudly.
        import builtins
        _orig = builtins.__import__
        def _blocked(name, *a, **kw):
            if name == "savant" or name.startswith("savant."):
                raise ImportError("savant import is forbidden in rules layer")
            return _orig(name, *a, **kw)
        builtins.__import__ = _blocked

        import custom.rules
        assert custom.rules.REGISTRY.has("intrusion")
        print("OK")
        """
    )
    env = os.environ.copy()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout
