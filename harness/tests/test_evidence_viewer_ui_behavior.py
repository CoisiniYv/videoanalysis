"""Exercise the shipped UI scripts with Node's dependency-free test runner."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_evidence_viewer_ui_behavior() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("UI behavior checks require Node.js")
    script = Path(__file__).with_name("js") / "evidence_viewer_ui.test.cjs"
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
