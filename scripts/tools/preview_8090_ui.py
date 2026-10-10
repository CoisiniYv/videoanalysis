#!/usr/bin/env python3
"""Preview the actual 8090 static UI with synthetic, read-only API fixtures.

Uses only the standard library. Never contacts a real API, database, or server.
Run from any directory: python scripts/tools/preview_8090_ui.py --port 18090
Optionally supply a local synthetic MP4 via --video to check browser playback.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "services/evidence-viewer/app/static"
CAMERAS = [
    {"id": "camera-a", "source_id": "source_a", "name": "园区入口", "enabled": False},
    {"id": "camera-b", "source_id": "source_b", "name": "教学楼走廊", "enabled": False},
    {"id": "camera-c", "source_id": "source_c", "name": "超长摄像头名称_" + "north_gate_" * 10, "enabled": False},
]
PEOPLE = [
    {"person_id": 7, "name": "模拟人员", "external_person_id": "DEMO-007",
     "primary_registered_crop_url": "/fixtures/face.svg", "gallery_count": 1},
    {"person_id": 8, "name": "超长人员姓名_" + "demo_name_" * 12,
     "external_person_id": "DEMO-LONG", "description": "离线长名称与缺图验证", "gallery_count": 0},
]
NOW = int(datetime.now(timezone.utc).timestamp() * 1000)


def trajectory_rows(person_id: int) -> list[dict]:
    if person_id not in (7, 8):
        return []
    rows = []
    for index in range(58):
        camera = CAMERAS[(index // 3) % len(CAMERAS)]
        missing = index % 7 == 3
        broken = index % 7 == 2
        rows.append({
            "person_id": person_id, "person_name": PEOPLE[person_id - 7]["name"],
            "external_person_id": PEOPLE[person_id - 7]["external_person_id"],
            "camera_id": camera["id"], "camera_name": camera["name"], "source_id": camera["source_id"],
            "event_ts_ms": NOW - index * 40 * 60 * 1000,
            "similarity": 0.92 - (index % 5) * 0.06,
            "trajectory_source": ("watchlist_event", "gallery_observation", "live_search_hit")[index % 3],
            "full_frame_url": None if missing else ("/fixtures/missing.svg" if broken else "/fixtures/frame.svg"),
            "trajectory_thumbnail_url": None if missing else "/fixtures/face.svg",
            "face_crop_url": None if missing else "/fixtures/face.svg",
        })
    return rows


def evidence_rows() -> list[dict]:
    rows = []
    for index in range(68):
        camera = CAMERAS[index % len(CAMERAS)]
        event_type = ("intrusion", "running", "watchlist_hit", "crowd_gathering", "loitering")[index % 5]
        image = event_type == "watchlist_hit"
        status = "image_ready" if image else ("ready", "generated_unverified", "pending", "generated_corrupt")[index % 4]
        ready = status in {"ready", "generated_unverified", "generated_corrupt"}
        rows.append({
            "event_id": f"demo-{index:03}", "event_type": event_type,
            "camera_id": camera["id"], "camera_name": camera["name"], "source_id": camera["source_id"],
            "alarm_machine_time": datetime.fromtimestamp((NOW - index * 30 * 60 * 1000) / 1000, timezone.utc).isoformat(),
            "playback_kind": "image" if image else "video", "clip_status": status,
            "materialization_status": status, "evidence_state": status,
            "visual_evidence_status": "unverified" if status == "generated_unverified" else "verified",
            "raw_clip_available": ready, "raw_clip_url": "/fixtures/sample.mp4" if ready else None,
            "full_frame_url": None if index % 4 == 3 else "/fixtures/frame.svg",
            "face_crop_url": "/fixtures/face.svg" if image else None,
            "image_available": image, "matched_objects": 1 if image else 0,
            # One identity row deliberately lacks a person ID, like a legacy bundle.
            "person_id": None if index == 7 else (7 if image else None),
            "person_name": "模拟人员" if image else None,
        })
    return rows


BUNDLES = evidence_rows()


class PreviewHandler(BaseHTTPRequestHandler):
    video: Path | None = None

    def reply(self, data: bytes, mime: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json_reply(self, data: object, status: int = 200) -> None:
        self.reply(json.dumps({"data": data}, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        route = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if route == "/":
            page = (STATIC / "index.html").read_text()
            page = page.replace("<title>视频分析操作台</title>", "<title>8090 离线模拟预览</title>")
            page = page.replace("<h2>视频分析管理台</h2>", "<h2>8090 离线验证 · 模拟数据</h2>")
            self.reply(page.encode(), "text/html; charset=utf-8")
        elif route.startswith("/static/"):
            target = STATIC / route.removeprefix("/static/")
            if target.parent == STATIC and target.is_file():
                self.reply(target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream")
            else:
                self.reply(b"not found", "text/plain", 404)
        elif route in {"/fixtures/face.svg", "/fixtures/frame.svg"}:
            face = route.endswith("face.svg")
            label = html.escape("MOCK FACE" if face else "OFFLINE MOCK FRAME - NO REAL CAMERA")
            width, height = (160, 160) if face else (960, 540)
            svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#183d47"/><rect x="20" y="20" width="{width - 40}" height="{height - 40}" rx="20" fill="#286474"/><circle cx="{width // 2}" cy="{height // 2 - 20}" r="30" fill="#78bac3"/><rect x="{width // 2 - 35}" y="{height // 2 + 15}" width="70" height="65" rx="12" fill="#78bac3"/><text x="50%" y="85%" text-anchor="middle" font-family="sans-serif" font-size="{12 if face else 22}" fill="white">{label}</text></svg>'
            self.reply(svg.encode(), "image/svg+xml")
        elif route == "/fixtures/sample.mp4" and self.video:
            self.reply(self.video.read_bytes(), "video/mp4")
        elif route == "/api/v1/cameras":
            self.json_reply({"cameras": CAMERAS})
        elif route.startswith("/api/v1/cameras/"):
            camera_id = route.split("/")[4]
            camera = next((row for row in CAMERAS if row["id"] == camera_id), CAMERAS[0])
            self.json_reply({"camera": camera, "rules": [], "zones": [], "config": {}, "sources": []})
        elif route == "/api/v1/people":
            self.json_reply({"people": PEOPLE})
        elif route.startswith("/api/v1/people/") and route.endswith("/trajectory"):
            rows = trajectory_rows(int(route.split("/")[4]))
            if query.get("camera_id"):
                rows = [row for row in rows if row["camera_id"] == query["camera_id"]]
            for key, predicate in (("start_ts_ms", lambda value, limit: value >= limit), ("end_ts_ms", lambda value, limit: value <= limit)):
                if query.get(key):
                    rows = [row for row in rows if predicate(row["event_ts_ms"], int(query[key]))]
            offset, limit = int(query.get("offset", "0")), int(query.get("limit", "50"))
            self.json_reply({"trajectory": rows[offset:offset + limit], "has_more": offset + limit < len(rows)})
        elif route == "/api/v1/evidence/health":
            self.json_reply({"status": "ok"})
        elif route == "/api/v1/evidence/bundles":
            rows = BUNDLES
            category_types = {"perimeter": {"intrusion"}, "behavior": {"running", "loitering"}, "crowd": {"crowd_gathering"}, "identity": {"watchlist_hit"}}
            if query.get("event_category") == "evidence":
                rows = [row for row in rows if row["event_type"] != "watchlist_hit"]
            if query.get("event_category") in category_types:
                rows = [row for row in rows if row["event_type"] in category_types[query["event_category"]]]
            for key in ("event_type", "source_id", "camera_id", "clip_status", "event_id"):
                if query.get(key):
                    rows = [row for row in rows if str(row.get(key)) == query[key]]
            if query.get("person"):
                rows = [row for row in rows if query["person"] in str(row.get("person_id")) or query["person"] in str(row.get("person_name"))]
            offset, limit = int(query.get("offset", "0")), int(query.get("limit", "50"))
            self.json_reply({"bundles": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit})
        elif route.startswith("/api/v1/evidence/bundles/"):
            event_id = route.split("/")[5]
            bundle = next((row for row in BUNDLES if row["event_id"] == event_id), None)
            if not bundle:
                self.json_reply({"error": "unknown fixture"}, 404)
            elif route.endswith("/annotations"):
                self.json_reply({"records": [], "warnings": [], "annotation_source": "database", "annotation_source_kind": "database_overlay_segments"})
            elif route.endswith("/sink-metadata"):
                self.json_reply({"records": [{"pts": 0, "width": 960, "height": 540}], "warnings": []})
            else:
                self.json_reply({**bundle, "metadata": {"event": bundle}, "summary": bundle, "warnings": []})
        elif route.startswith("/api/v1/algorithms"):
            self.json_reply({"algorithms": []})
        else:
            self.reply(b"no such offline fixture", "text/plain", 404)

    def do_POST(self) -> None:
        self.json_reply({"error": {"message": "离线预览只读，不执行配置或运行操作"}}, 405)

    do_PUT = do_POST
    do_DELETE = do_POST

    def log_message(self, format: str, *args: object) -> None:
        # Keep fixture output compact; broken images are deliberate scenarios.
        if args and str(args[1]) not in {"200", "304"}:
            super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18090)
    parser.add_argument("--video", type=Path)
    args = parser.parse_args()
    if args.video and not args.video.is_file():
        parser.error("--video must name an existing local file")
    PreviewHandler.video = args.video
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Offline mock UI: http://127.0.0.1:{server.server_port}/#evidence", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
