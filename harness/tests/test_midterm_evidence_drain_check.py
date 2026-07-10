from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT.parent / "scripts" / "tools" / "check_midterm_evidence_drain.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_midterm_evidence_drain",
        SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_drain_complete_requires_no_active_db_or_stream_lag() -> None:
    module = _load_module()

    assert module.drain_complete(
        {
            "active_evidence_tasks": 0,
            "active_replay_slots": 0,
            "active_event_media": 9,
            "record_request_group_pending": 0,
            "record_request_group_lag": 0,
        }
    )
    assert not module.drain_complete(
        {
            "active_evidence_tasks": 0,
            "active_replay_slots": 1,
            "active_event_media": 0,
            "record_request_group_pending": 0,
            "record_request_group_lag": 0,
        }
    )
    assert not module.drain_complete(
        {
            "active_evidence_tasks": 0,
            "active_replay_slots": 0,
            "active_event_media": 0,
            "record_request_group_pending": 0,
            "record_request_group_lag": 3,
        }
    )
