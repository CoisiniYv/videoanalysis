"""Tests for DryRunEventExporter — no external dependencies, correct output."""

import json
import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_phase2c")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from custom.models.events import SecurityEvent
from custom.services.event_exporter import DryRunEventExporter


# ===========================================================================
# 13. DryRunEventExporter.export() does not raise
# ===========================================================================

def test_dry_run_export_no_exception():
    exporter = DryRunEventExporter()
    event = SecurityEvent(
        event_type="intrusion",
        camera_id="cam_01",
        track_id=3,
        start_ts_ms=1000,
        end_ts_ms=2000,
        source_event_id="test:cam_01:3:intrusion:1000",
    )
    try:
        exporter.export(event)
    except Exception as exc:
        assert False, f"DryRunEventExporter.export() raised: {exc}"


# ===========================================================================
# 14. DryRunEventExporter does not import redis
# ===========================================================================

def test_dry_run_no_redis_import():
    import ast
    import inspect
    source = inspect.getsource(DryRunEventExporter)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            for name in names:
                assert "redis" not in name.lower(), \
                    f"DryRunEventExporter must not import redis (found import {name})"


# ===========================================================================
# 15. DryRunEventExporter does not connect to Redis / PostgreSQL
# ===========================================================================

def test_dry_run_no_external_connection():
    import ast
    import inspect
    source = inspect.getsource(DryRunEventExporter)
    tree = ast.parse(source)
    forbidden_imports = {"redis", "postgres", "psycopg", "asyncpg",
                         "aioredis", "httpx", "aiohttp"}

    import_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                top_module = alias.name.split(".")[0].lower()
                import_nodes.add(top_module)

    forbidden_found = forbidden_imports & import_nodes
    assert len(forbidden_found) == 0, \
        f"DryRunEventExporter must not import forbidden modules: {forbidden_found}"


# ===========================================================================
# 16. DryRunEventExporter output contains valid JSON
# ===========================================================================

def test_dry_run_output_contains_valid_json(capsys):
    exporter = DryRunEventExporter()
    event = SecurityEvent(
        event_type="intrusion",
        camera_id="cam_01",
        track_id=3,
        start_ts_ms=1000,
        end_ts_ms=2000,
        source_event_id="test:cam_01:3:intrusion:1000",
        schema_version="1.0",
    )
    exporter.export(event)
    captured = capsys.readouterr()
    assert "stage=phase2c_security_event_dry_run" in captured.out
    assert "security_event_json=" in captured.out

    # Extract JSON portion and validate
    prefix = "security_event_json="
    idx = captured.out.find(prefix)
    assert idx >= 0
    json_str = captured.out[idx + len(prefix):].strip()
    parsed = json.loads(json_str)
    assert parsed["event_type"] == "intrusion"
    assert parsed["schema_version"] == "1.0"
    assert parsed["source_event_id"] == "test:cam_01:3:intrusion:1000"
    assert parsed["track_id"] == 3
