"""R3.2E local-video debug visual evidence exporter.

This module is intentionally separate from production event evidence. It only
works when observations and visual export use the same local MP4 timeline.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import psycopg
from psycopg.rows import dict_row


DEFAULT_TARGET_EXTERNAL_IDS = ("demo:f4_3:finch", "demo:f4_3:reese")
DEFAULT_OUTPUT_ROOT = "/data/video-analytics/media/debug/local_video_evidence"


MATCH_SQL = """
SELECT
  fo.source_observation_id,
  fo.camera_id,
  fo.source_id,
  fo.track_id,
  fo.timestamp_ms,
  fo.face_bbox,
  fo.landmarks,
  fo.quality,
  fo.embedding_model,
  fo.embedding_dim,
  fo.embedding_norm,
  fo.created_at,
  p.id AS person_id,
  p.external_person_id,
  p.name AS person_name,
  pge.id AS gallery_embedding_id,
  1 - (fo.embedding <=> pge.embedding) AS similarity
FROM face_observations fo
JOIN person_gallery_embeddings pge
  ON pge.is_active = true
JOIN persons p
  ON p.id = pge.person_id
WHERE fo.source_id = %(source_id)s
  AND fo.embedding IS NOT NULL
  AND p.external_person_id = ANY(%(external_person_ids)s)
ORDER BY fo.timestamp_ms ASC, similarity DESC;
"""


OBS_COUNT_SQL = """
SELECT COUNT(*) AS count
FROM face_observations
WHERE source_id = %(source_id)s
  AND embedding IS NOT NULL;
"""


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value).strip("_") or "local_video"


def _is_verified_local_source_id(source_id: str) -> bool:
    lowered = source_id.lower()
    if any(token in lowered for token in ("rtsp", "live")):
        return False
    return any(token in lowered for token in ("local", "video", "mp4", "f4_3", "r3_2e"))


def _bbox_to_rect(face_bbox: Any, width: int, height: int) -> tuple[int, int, int, int]:
    """Convert [xc, yc, w, h] bbox into clipped x1/y1/x2/y2 pixels."""
    if isinstance(face_bbox, dict):
        xc = float(face_bbox.get("xc", face_bbox.get("x", 0.0)))
        yc = float(face_bbox.get("yc", face_bbox.get("y", 0.0)))
        bw = float(face_bbox.get("width", face_bbox.get("w", 0.0)))
        bh = float(face_bbox.get("height", face_bbox.get("h", 0.0)))
    elif isinstance(face_bbox, (list, tuple)) and len(face_bbox) >= 4:
        xc, yc, bw, bh = [float(v) for v in face_bbox[:4]]
    else:
        raise ValueError(f"invalid face_bbox: {face_bbox!r}")

    x1 = max(0, min(width - 1, int(round(xc - bw / 2.0))))
    y1 = max(0, min(height - 1, int(round(yc - bh / 2.0))))
    x2 = max(0, min(width - 1, int(round(xc + bw / 2.0))))
    y2 = max(0, min(height - 1, int(round(yc + bh / 2.0))))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"invalid clipped face_bbox: {face_bbox!r}")
    return x1, y1, x2, y2


def _landmark_points(landmarks: Any) -> list[tuple[int, int]]:
    raw = landmarks.get("points", []) if isinstance(landmarks, dict) else landmarks
    points: list[tuple[int, int]] = []
    if isinstance(raw, (list, tuple)) and raw:
        if all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in raw):
            points = [(int(round(float(p[0]))), int(round(float(p[1])))) for p in raw]
        else:
            vals = [float(v) for v in raw]
            points = [
                (int(round(vals[i])), int(round(vals[i + 1])))
                for i in range(0, min(len(vals), 10), 2)
            ]
    return points[:5]


@dataclass
class LocalVideoMatch:
    rank: int
    source_observation_id: str
    camera_id: str
    source_id: str
    track_id: str
    timestamp_ms: int
    face_bbox: Any
    landmarks: Any
    quality: float
    embedding_model: str
    embedding_dim: int
    embedding_norm: float
    created_at: Any
    person_id: int
    external_person_id: str
    person_name: str
    gallery_embedding_id: int
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "source_observation_id": self.source_observation_id,
            "camera_id": self.camera_id,
            "source_id": self.source_id,
            "track_id": self.track_id,
            "timestamp_ms": self.timestamp_ms,
            "timestamp_sec": self.timestamp_ms / 1000.0,
            "face_bbox": self.face_bbox,
            "landmarks": self.landmarks,
            "quality": self.quality,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "embedding_norm": self.embedding_norm,
            "created_at": self.created_at,
            "person_id": self.person_id,
            "external_person_id": self.external_person_id,
            "person_name": self.person_name,
            "gallery_embedding_id": self.gallery_embedding_id,
            "similarity": self.similarity,
        }


def _draw_label_block(frame: Any, lines: list[str], color: tuple[int, int, int]) -> None:
    x, y = 18, 30
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.62
    thickness = 1
    line_h = 24
    max_w = 0
    for line in lines:
        (tw, _th), _ = cv2.getTextSize(line, font, font_scale, thickness)
        max_w = max(max_w, tw)
    cv2.rectangle(frame, (10, 8), (max_w + 28, 18 + line_h * len(lines)), (0, 0, 0), -1)
    for idx, line in enumerate(lines):
        yy = y + idx * line_h
        cv2.putText(
            frame,
            line,
            (x, yy),
            font,
            font_scale,
            (255, 255, 255),
            thickness + 2,
            cv2.LINE_AA,
        )
        cv2.putText(frame, line, (x, yy), font, font_scale, color, thickness, cv2.LINE_AA)


def _color_for(row: LocalVideoMatch) -> tuple[int, int, int]:
    if row.external_person_id.endswith(":finch"):
        return (0, 220, 255)
    if row.external_person_id.endswith(":reese"):
        return (0, 255, 0)
    return (255, 180, 0)


def _draw_match(frame: Any, row: LocalVideoMatch, *, persistent: bool = False) -> None:
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = _bbox_to_rect(row.face_bbox, width, height)
    color = _color_for(row)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
    for idx, (x, y) in enumerate(_landmark_points(row.landmarks), start=1):
        cv2.circle(frame, (x, y), 5, (0, 80, 255), -1)
        cv2.putText(
            frame,
            str(idx),
            (x + 6, y - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 80, 255),
            1,
            cv2.LINE_AA,
        )
    lines = [
        "LOCAL VIDEO DEBUG - NOT PRODUCTION RTSP EVIDENCE",
        f"{row.person_name} sim={row.similarity:.3f}",
        f"{row.external_person_id}",
        f"ts={row.timestamp_ms}ms obs={row.source_observation_id}",
    ]
    if persistent:
        lines.append("nearest local-video timestamp overlay")
    _draw_label_block(frame, lines, color)


def _read_frame(source_mp4_path: Path, timestamp_ms: int) -> Any:
    cap = cv2.VideoCapture(str(source_mp4_path))
    if not cap.isOpened():
        raise RuntimeError(f"FRAME_READ_FAILED: cv2 could not open {source_mp4_path}")
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(float(timestamp_ms), 0.0))
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame

        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if fps > 0 and math.isfinite(fps):
            frame_idx = max(int(round((timestamp_ms / 1000.0) * fps)), 0)
            for delta in (0, -1, 1, -2, 2, -5, 5):
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(frame_idx + delta, 0))
                ok, frame = cap.read()
                if ok and frame is not None:
                    return frame
        raise RuntimeError(f"FRAME_READ_FAILED timestamp_ms={timestamp_ms}")
    finally:
        cap.release()


def fetch_local_video_matches(
    conn: psycopg.Connection,
    *,
    source_id: str,
    external_person_ids: list[str],
) -> tuple[int, list[LocalVideoMatch]]:
    """Fetch face observations and gallery candidates for one local source."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(OBS_COUNT_SQL, {"source_id": source_id})
        obs_count = int(cur.fetchone()["count"])
        cur.execute(
            MATCH_SQL,
            {
                "source_id": source_id,
                "external_person_ids": external_person_ids,
            },
        )
        raw_rows = list(cur.fetchall())

    best_by_obs: dict[str, LocalVideoMatch] = {}
    for raw in raw_rows:
        obs_id = str(raw["source_observation_id"])
        row = LocalVideoMatch(
            rank=0,
            source_observation_id=obs_id,
            camera_id=str(raw["camera_id"]),
            source_id=str(raw["source_id"]),
            track_id=str(raw["track_id"]),
            timestamp_ms=int(raw["timestamp_ms"]),
            face_bbox=raw["face_bbox"],
            landmarks=raw["landmarks"],
            quality=float(raw["quality"]),
            embedding_model=str(raw["embedding_model"]),
            embedding_dim=int(raw["embedding_dim"]),
            embedding_norm=float(raw["embedding_norm"]),
            created_at=raw["created_at"],
            person_id=int(raw["person_id"]),
            external_person_id=str(raw["external_person_id"]),
            person_name=str(raw["person_name"]),
            gallery_embedding_id=int(raw["gallery_embedding_id"]),
            similarity=float(raw["similarity"]),
        )
        current = best_by_obs.get(obs_id)
        if current is None or row.similarity > current.similarity:
            best_by_obs[obs_id] = row

    rows = sorted(best_by_obs.values(), key=lambda r: (-r.similarity, r.timestamp_ms))
    for rank, row in enumerate(rows, start=1):
        row.rank = rank
    return obs_count, rows


def write_debug_overlay_snapshot(
    *,
    source_mp4_path: Path,
    output_path: Path,
    row: LocalVideoMatch,
) -> str:
    """Seek the local MP4 at row.timestamp_ms and draw the matched face."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame = _read_frame(source_mp4_path, row.timestamp_ms)
    _draw_match(frame, row)
    if not cv2.imwrite(str(output_path), frame):
        raise RuntimeError(f"SNAPSHOT_WRITE_FAILED: {output_path}")
    return str(output_path)


def write_debug_annotated_clip(
    *,
    source_mp4_path: Path,
    output_path: Path,
    rows: list[LocalVideoMatch],
    pre_ms: int,
    post_ms: int,
    window_ms: int,
) -> str:
    """Write a debug annotated clip by reading the same local MP4 timeline."""
    if not rows:
        return ""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    first_ts = min(row.timestamp_ms for row in rows)
    start_ms = max(first_ts - pre_ms, 0)
    end_ms = first_ts + post_ms

    cap = cv2.VideoCapture(str(source_mp4_path))
    if not cap.isOpened():
        raise RuntimeError(f"CLIP_READ_FAILED: cv2 could not open {source_mp4_path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        if not math.isfinite(fps) or fps <= 0:
            fps = 24.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
        writer = cv2.VideoWriter(
            str(output_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            fps,
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"CLIP_WRITE_FAILED: {output_path}")
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                if pos_ms > end_ms:
                    break
                near = [row for row in rows if abs(row.timestamp_ms - pos_ms) <= window_ms]
                if near:
                    for row in near[:3]:
                        _draw_match(frame, row)
                else:
                    nearest = min(rows, key=lambda row: abs(row.timestamp_ms - pos_ms))
                    _draw_label_block(
                        frame,
                        [
                            "LOCAL VIDEO DEBUG - NOT PRODUCTION RTSP EVIDENCE",
                            f"nearest: {nearest.person_name} sim={nearest.similarity:.3f}",
                            f"hit_ts={nearest.timestamp_ms}ms frame_ts={int(pos_ms)}ms",
                        ],
                        _color_for(nearest),
                    )
                writer.write(frame)
        finally:
            writer.release()
    finally:
        cap.release()
    return str(output_path)


def _write_hits_csv(path: Path, rows: list[LocalVideoMatch]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "rank",
                "person_name",
                "external_person_id",
                "similarity",
                "timestamp_ms",
                "source_observation_id",
                "camera_id",
                "source_id",
                "track_id",
                "face_bbox",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "rank": row.rank,
                    "person_name": row.person_name,
                    "external_person_id": row.external_person_id,
                    "similarity": f"{row.similarity:.6f}",
                    "timestamp_ms": row.timestamp_ms,
                    "source_observation_id": row.source_observation_id,
                    "camera_id": row.camera_id,
                    "source_id": row.source_id,
                    "track_id": row.track_id,
                    "face_bbox": json.dumps(row.face_bbox, separators=(",", ":")),
                }
            )
    return str(path)


def export_local_video_debug_evidence(
    conn: psycopg.Connection,
    *,
    source_mp4_path: str,
    source_id: str,
    external_person_ids: list[str] | None = None,
    threshold: float = 0.35,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    limit: int = 50,
    pre_ms: int = 5000,
    post_ms: int = 5000,
    window_ms: int = 600,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Export debug-only local-video visual evidence for face matches."""
    source_path = Path(source_mp4_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SOURCE_MP4_NOT_FOUND: {source_path}")

    targets = external_person_ids or list(DEFAULT_TARGET_EXTERNAL_IDS)
    obs_count, candidates = fetch_local_video_matches(
        conn,
        source_id=source_id,
        external_person_ids=targets,
    )
    hits = [row for row in candidates if row.similarity >= threshold]
    selected = hits[:limit] if hits else []

    run_dir = (
        Path(output_root)
        / _safe_part(source_id)
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    snapshot_path = run_dir / "debug_overlay_snapshot.jpg"
    clip_path = run_dir / "debug_annotated_clip.mp4"
    metadata_path = run_dir / "debug_visual_metadata.json"
    hits_csv_path = run_dir / "hits.csv"

    source_verified = _is_verified_local_source_id(source_id)
    alignment_warning = None
    if not source_verified:
        alignment_warning = "source_id is not verified as local-video source"

    if dry_run or not selected:
        snapshot_out = None
        clip_out = None
        hits_csv_out = None
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot_out = write_debug_overlay_snapshot(
            source_mp4_path=source_path,
            output_path=snapshot_path,
            row=selected[0],
        )
        clip_out = write_debug_annotated_clip(
            source_mp4_path=source_path,
            output_path=clip_path,
            rows=selected[:limit],
            pre_ms=pre_ms,
            post_ms=post_ms,
            window_ms=window_ms,
        )
        hits_csv_out = _write_hits_csv(hits_csv_path, selected)

    metadata: dict[str, Any] = {
        "debug_only": True,
        "evidence_mode": "local_video_debug",
        "source_mp4_path": str(source_path),
        "source_id": source_id,
        "source_timeline": "local_mp4",
        "timeline_assumption": "timestamp_ms is local video offset in milliseconds",
        "not_production_rtsp_evidence": True,
        "snapshot_path": snapshot_out,
        "annotated_clip_path": clip_out,
        "debug_overlay_snapshot_path": snapshot_out,
        "debug_annotated_clip_path": clip_out,
        "debug_visual_metadata_path": str(metadata_path) if selected and not dry_run else None,
        "hits_csv_path": hits_csv_out,
        "exact_event_frame": bool(selected) and not dry_run,
        "exact_event_clip": bool(selected) and not dry_run,
        "alignment_reason": "same local mp4 used for inference and evidence export",
        "alignment_warning": alignment_warning,
        "threshold": threshold,
        "pre_ms": pre_ms,
        "post_ms": post_ms,
        "window_ms": window_ms,
        "observations_count": obs_count,
        "hit_count": len(hits),
        "target_external_person_ids": targets,
        "top_candidates": [row.to_dict() for row in candidates[: max(limit, 10)]],
        "hits": [row.to_dict() for row in hits[:limit]],
        "dry_run": dry_run,
        "warning": "local-video debug output is not production RTSP evidence",
    }

    if selected and not dry_run:
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False, default=_json_default) + "\n",
            encoding="utf-8",
        )
    return metadata
