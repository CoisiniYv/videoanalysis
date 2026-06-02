"""C1G.1b operator frontend static asset contract tests.

Verifies the enhanced operator page at /operator serves correct HTML/JS
with real API endpoint references, algorithm templates, and no 8090 references.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---- /operator HTML tests ----

def test_operator_page_returns_200(client: TestClient):
    resp = client.get("/operator")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_operator_page_contains_camera_list_area(client: TestClient):
    resp = client.get("/operator")
    html = resp.text
    assert "cameras" in html.lower()
    assert "camera-list" in html or "camera_list" in html or "Cameras" in html


def test_operator_page_contains_algorithm_rule_templates(client: TestClient):
    resp = client.get("/operator")
    html = resp.text
    # Must have template buttons for algorithms
    assert "data-template" in html
    assert "face.observation" in html
    assert "behavior.intrusion" in html


def test_operator_page_contains_face_watchlist(client: TestClient):
    resp = client.get("/operator")
    html = resp.text
    assert "face.watchlist" in html


def test_operator_page_contains_global_alert_cooldown(client: TestClient):
    resp = client.get("/operator")
    html = resp.text
    assert "global_alert_cooldown_s" in html


def test_operator_page_does_not_reference_8090(client: TestClient):
    resp = client.get("/operator")
    html = resp.text
    assert "8090" not in html
    assert "evidence-viewer" not in html.lower()
    assert "evidence_viewer" not in html.lower()


def test_operator_page_contains_behavior_running_and_wall_climb(client: TestClient):
    resp = client.get("/operator")
    html = resp.text
    assert "behavior.running" in html
    assert "behavior.wall_climb_suspicious" in html


# ---- /operator/static/app.js tests ----

def test_app_js_returns_200(client: TestClient):
    resp = client.get("/operator/static/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "") or resp.headers.get("content-type", "").startswith("text/")


def test_app_js_contains_real_api_endpoints(client: TestClient):
    resp = client.get("/operator/static/app.js")
    js = resp.text
    # Must reference real API endpoints, not mock data
    # JS uses template literals: `${API}/cameras` where API = "/api/v1"
    assert "/api/v1" in js
    assert "cameras" in js
    assert "/zones" in js or "zones" in js
    assert "/rules" in js or "rules" in js
    # alert_policy is saved via camera update; config endpoint returns it
    assert "alert_policy" in js or "alert-policy" in js
    assert "config" in js


def test_app_js_contains_all_nine_templates(client: TestClient):
    resp = client.get("/operator/static/app.js")
    js = resp.text
    expected = [
        "face.observation",
        "face.watchlist",
        "face.live_search",
        "behavior.intrusion",
        "behavior.loitering",
        "behavior.crowd_gathering",
        "behavior.fall",
        "behavior.running",
        "behavior.wall_climb_suspicious",
    ]
    for algo in expected:
        assert algo in js, f"Missing template for {algo}"


def test_app_js_has_delete_and_enable_disable(client: TestClient):
    resp = client.get("/operator/static/app.js")
    js = resp.text
    assert "deleteZone" in js or "DELETE" in js
    assert "deleteRule" in js or "DELETE" in js
    # JS uses template literals: `"enable"` and `"disable"` as path segments
    assert "enable" in js
    assert "disable" in js


# ---- /operator/static/style.css test ----

def test_style_css_returns_200(client: TestClient):
    resp = client.get("/operator/static/style.css")
    assert resp.status_code == 200
