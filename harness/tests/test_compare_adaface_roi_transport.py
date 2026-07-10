from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "tools"
    / "compare_adaface_roi_transport.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("compare_adaface_roi_transport", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_gallery_normalizes_vectors_and_preserves_identity(tmp_path: Path) -> None:
    vector = [0.0] * 512
    vector[17] = 2.0
    gallery_file = tmp_path / "gallery.psv"
    gallery_file.write_text(
        f"7|3|Reese|{vector!r}\n",
        encoding="utf-8",
    )

    rows, embeddings = _module().load_gallery(gallery_file)

    assert rows == [
        {"gallery_embedding_id": 7, "person_id": 3, "person_name": "Reese"}
    ]
    assert embeddings.shape == (1, 512)
    assert np.linalg.norm(embeddings[0]) == 1.0
    assert embeddings[0, 17] == 1.0
