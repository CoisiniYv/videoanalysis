"""Face detection data models (pure Python, no Savant / DB / GPU imports).

All bbox coordinates are in full-frame pixel space.  Image data is never
carried in these models — only path references for snapshots / crops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FaceROI:
    """A candidate head region extracted from a person detection.

    Produced by the head-ROI selector *before* SCRFD runs.  The region is
    a sub-crop of the full frame; coordinates are absolute frame pixels.
    """

    source_id: str = ""
    camera_id: str = ""
    track_id: int = 0
    timestamp_ms: int = 0
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    confidence: float = 0.0
    method: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FaceDetection:
    """A single face detection produced by SCRFD (or another face detector).

    ``person_bbox`` and ``face_bbox`` are in full-frame pixel coordinates.
    ``landmarks`` is a list of (x, y) pairs or (x, y, confidence) triples.
    """

    source_id: str = ""
    camera_id: str = ""
    track_id: int = 0
    timestamp_ms: int = 0
    person_bbox: Optional[List[float]] = None
    face_bbox: Optional[List[float]] = None
    landmarks: Optional[List[List[float]]] = None
    confidence: float = 0.0
    model_name: str = ""
    model_version: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FaceQualityResult:
    """Face quality evaluation result.

    ``quality`` is in [0.0, 1.0].  ``passed`` is ``True`` when quality
    meets or exceeds the configured threshold.
    """

    passed: bool = False
    quality: float = 0.0
    confidence_score: float = 0.0
    size_score: float = 0.0
    landmark_score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FaceObservationDraft:
    """Draft face observation ready for Redis Stream emission.

    Does **not** carry image bytes.  ``snapshot_path`` and ``crop_path``
    are optional filesystem path references filled in by downstream
    media / snapshot workers.
    """

    source_observation_id: str = ""
    source_id: str = ""
    camera_id: str = ""
    track_id: int = 0
    timestamp_ms: int = 0
    person_bbox: Optional[List[float]] = None
    face_bbox: Optional[List[float]] = None
    landmarks: Optional[List[List[float]]] = None
    quality: float = 0.0
    model_name: str = ""
    model_version: Optional[str] = None
    snapshot_path: Optional[str] = None
    crop_path: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
