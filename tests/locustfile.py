"""Locust load and accuracy test suite for Geospatial File Measurement API.

Endpoints tested:
  1. POST /api/files/ (file upload)
  2. GET /api/files/{file_id}/ (status polling)
  3. GET /api/files/{file_id}/measurements/ (retrieval and error calculation against expected.json)

Usage:
  # Start Locust with Web UI on http://localhost:8089
  locust -f tests/locustfile.py --host http://127.0.0.1:8000

  # Or run headless:
  locust -f tests/locustfile.py --host http://127.0.0.1:8000 --headless -u 5 -r 1 -t 30s
"""

import json
import logging
import mimetypes
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from flask import jsonify, render_template_string
from locust import HttpUser, between, events, task

logger = logging.getLogger("locust.geospatial")

BASE_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BASE_DIR / "Docs" / "test_fixtures" / "fixtures"
EXPECTED_FILE = FIXTURES_DIR / "expected.json"

# Load expected reference data
EXPECTED_DATA: dict[str, Any] = {}
if EXPECTED_FILE.exists():
    try:
        with open(EXPECTED_FILE, "r", encoding="utf-8") as f:
            EXPECTED_DATA = json.load(f)
    except Exception as err:
        logger.error(f"Failed to load expected.json: {err}")

# In-memory store for tracking real-time accuracy and error statistics across runs
ACCURACY_STATS = {
    "total_checks": 0,
    "successful_checks": 0,
    "failed_checks": 0,
    "records": [],  # List of {fixture, feature_index, type, actual, expected, abs_error, rel_error_pct, status}
}


def compute_measurement_error(
    actual: Optional[float],
    expected: Optional[float],
) -> tuple[float, float]:
    """Calculates absolute error and relative percentage error between actual and reference."""
    if actual is None and expected is None:
        return 0.0, 0.0
    if actual is None or expected is None:
        return float("inf"), float("inf")

    abs_err = abs(actual - expected)
    rel_err_pct = (abs_err / expected * 100.0) if expected != 0 else (0.0 if abs_err == 0 else float("inf"))
    return abs_err, rel_err_pct


class GeospatialMeasurementUser(HttpUser):
    """Locust user simulating file uploads, measurement polling, and accuracy verification."""

    wait_time = between(1.0, 2.5)

    def on_start(self):
        """Discovers available fixture files for testing."""
        self.kml_files = sorted((FIXTURES_DIR / "kml").glob("*.kml")) if (FIXTURES_DIR / "kml").exists() else []
        self.shp_files = sorted((FIXTURES_DIR / "shapefile").glob("*.zip")) if (FIXTURES_DIR / "shapefile").exists() else []

    def _get_fixture_key(self, file_path: Path) -> str:
        """Constructs key matching expected.json format (e.g. 'kml/box_60n.kml' or 'shapefile/box_60n.zip')."""
        category = file_path.parent.name
        return f"{category}/{file_path.name}"

    def _poll_file_status(self, file_id: str, timeout: float = 30.0, poll_interval: float = 0.5) -> dict[str, Any]:
        """Polls GET /api/files/{file_id}/ until processing completes or fails."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self.client.get(
                f"/api/files/{file_id}/",
                name="/api/files/[id]/",
                catch_response=True,
            ) as response:
                if response.status_code != 200:
                    response.failure(f"Status check failed: HTTP {response.status_code}")
                    return {}

                data = response.json()
                status = data.get("status")
                if status in ("COMPLETED", "FAILED"):
                    return data

            time.sleep(poll_interval)

        return {"status": "TIMEOUT", "error": f"Polling timed out after {timeout}s"}

    def _upload_and_verify_measurements(self, file_path: Path):
        """Uploads a geospatial file, retrieves its measurements, and checks errors against expected.json."""
        if not file_path.exists():
            return

        fixture_key = self._get_fixture_key(file_path)
        expected_meta = EXPECTED_DATA.get(fixture_key, {})
        is_expected_to_fail = "behaviour" in expected_meta or file_path.stat().st_size == 0

        # Step 1: Upload file via POST /api/files/
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix.lower() == ".kml":
            content_type = "application/vnd.google-earth.kml+xml"
        elif file_path.suffix.lower() == ".zip":
            content_type = "application/zip"

        with open(file_path, "rb") as f_obj:
            file_bytes = f_obj.read()

        with self.client.post(
            "/api/files/",
            files={"file": (file_path.name, file_bytes, content_type)},
            name="/api/files/ [upload]",
            catch_response=True,
        ) as upload_resp:

            # Handle files expected to be rejected at upload time (e.g. 0-byte files, not_a_zip.txt)
            if file_path.stat().st_size == 0 or file_path.suffix.lower() not in (".kml", ".zip"):
                if upload_resp.status_code == 400:
                    upload_resp.success()
                else:
                    upload_resp.failure(f"Expected 400 for empty/invalid file, got {upload_resp.status_code}")
                return

            if upload_resp.status_code != 202:
                upload_resp.failure(f"Upload failed: HTTP {upload_resp.status_code} - {upload_resp.text}")
                return

            file_id = upload_resp.json().get("id")
            if not file_id:
                upload_resp.failure("Upload response missing file 'id'")
                return
            upload_resp.success()

        # Step 2: Poll file status until terminal state
        status_data = self._poll_file_status(file_id)
        file_status = status_data.get("status")

        # Step 3: Call GET /api/files/{file_id}/measurements/
        with self.client.get(
            f"/api/files/{file_id}/measurements/",
            name="/api/files/[id]/measurements/",
            catch_response=True,
        ) as meas_resp:
            if meas_resp.status_code != 200:
                meas_resp.failure(f"Measurements endpoint failed: HTTP {meas_resp.status_code}")
                return

            meas_data = meas_resp.json()

            # Handle files expected to fail processing
            if is_expected_to_fail:
                if file_status == "FAILED" and meas_data.get("status") == "FAILED":
                    error_msg = meas_data.get("error") or ""
                    meas_resp.success()
                    logger.info(f"[{fixture_key}] Expected failure verified: error='{error_msg}'")
                else:
                    meas_resp.failure(f"Expected FAILED status for {fixture_key}, got {file_status}")
                return

            # Verification of successful measurements against expected.json
            if file_status != "COMPLETED":
                meas_resp.failure(f"File {fixture_key} did not complete: status={file_status}")
                return

            actual_features = meas_data.get("features", [])
            expected_features = expected_meta.get("features", [])

            # Check metadata
            if "feature_count" in expected_meta:
                if len(actual_features) != expected_meta["feature_count"]:
                    meas_resp.failure(f"Feature count mismatch: expected {expected_meta['feature_count']}, got {len(actual_features)}")
                    return

            if "crs" in expected_meta and meas_data.get("source_crs") != expected_meta["crs"]:
                meas_resp.failure(f"source_crs mismatch: expected {expected_meta['crs']}, got {meas_data.get('source_crs')}")
                return

            # Verify geometry_crs is EPSG:4326
            if meas_data.get("geometry_crs") != "EPSG:4326":
                meas_resp.failure(f"geometry_crs expected 'EPSG:4326', got {meas_data.get('geometry_crs')}")
                return

            # Feature-level measurement error calculation
            max_rel_error = 0.0
            tolerance_exceeded = False

            for idx, exp_feat in enumerate(expected_features):
                if idx >= len(actual_features):
                    break

                act_feat = actual_features[idx]
                act_meas = act_feat.get("measurement")
                exp_type = exp_feat.get("type")

                # Determine expected numerical value
                if "area_m2" in exp_feat:
                    exp_meas = exp_feat["area_m2"]
                elif "length_m" in exp_feat:
                    exp_meas = exp_feat["length_m"]
                else:
                    exp_meas = None

                abs_err, rel_err_pct = compute_measurement_error(act_meas, exp_meas)
                max_rel_error = max(max_rel_error, rel_err_pct if rel_err_pct != float("inf") else 0.0)

                # Tolerance: 0.5% - 1.0% between geodesic ellipsoid reference and UTM planar projection
                status_ok = True
                if exp_meas is not None:
                    if rel_err_pct > 1.0:
                        status_ok = False
                        tolerance_exceeded = True
                else:
                    if act_meas is not None:
                        status_ok = False
                        tolerance_exceeded = True

                ACCURACY_STATS["total_checks"] += 1
                if status_ok:
                    ACCURACY_STATS["successful_checks"] += 1
                else:
                    ACCURACY_STATS["failed_checks"] += 1

                record_entry = {
                    "timestamp": time.strftime("%H:%M:%S"),
                    "fixture": fixture_key,
                    "feature_index": idx,
                    "geometry_type": act_feat.get("geometry_type") or exp_type,
                    "actual": round(act_meas, 4) if act_meas is not None else None,
                    "expected": round(exp_meas, 4) if exp_meas is not None else None,
                    "abs_error": round(abs_err, 4) if abs_err != float("inf") else "N/A",
                    "rel_error_pct": f"{rel_err_pct:.4f}%" if rel_err_pct != float("inf") else "N/A",
                    "passed": status_ok,
                }
                ACCURACY_STATS["records"].append(record_entry)
                # Keep latest 200 records in memory
                if len(ACCURACY_STATS["records"]) > 200:
                    ACCURACY_STATS["records"].pop(0)

                logger.info(
                    f"[{fixture_key}] Feature #{idx} ({exp_type}): "
                    f"Actual={act_meas}, Expected={exp_meas}, "
                    f"AbsErr={abs_err:.2f}, RelErr={rel_err_pct:.4f}% | Passed={status_ok}"
                )

            if tolerance_exceeded:
                meas_resp.failure(f"Measurement error exceeded tolerance (max rel error: {max_rel_error:.2f}%)")
            else:
                meas_resp.success()

    @task(3)
    def test_kml_fixtures(self):
        """Tests KML files from Docs/test_fixtures/fixtures/kml/."""
        if not self.kml_files:
            return
        # Cycle through or pick fixtures
        for kml_file in self.kml_files:
            self._upload_and_verify_measurements(kml_file)

    @task(3)
    def test_shapefile_fixtures(self):
        """Tests zipped Shapefiles from Docs/test_fixtures/fixtures/shapefile/."""
        if not self.shp_files:
            return
        # Test valid shapefiles with measurements
        valid_shps = [
            f for f in self.shp_files
            if f.name in (
                "equator_box.zip",
                "box_60n.zip",
                "polygon_with_hole.zip",
                "multipolygon.zip",
                "lines.zip",
                "points.zip",
                "utm_projected_rect.zip",
                "webmercator_box_60n.zip",
                "nested_folder.zip",
            )
        ]
        for shp_file in valid_shps:
            self._upload_and_verify_measurements(shp_file)

    @task(1)
    def test_error_and_edge_case_fixtures(self):
        """Tests error and edge-case archives (corrupt, empty, no_shp, missing_prj)."""
        if not self.shp_files:
            return
        error_shps = [
            f for f in self.shp_files
            if f.name in (
                "empty.zip",
                "corrupt.zip",
                "no_shp_inside.zip",
                "no_prj.zip",
                "missing_shx.zip",
            )
        ]
        for shp_file in error_shps:
            self._upload_and_verify_measurements(shp_file)


# ==============================================================================
# Custom Web UI Extension: Accuracy & Error Calculation Dashboard
# ==============================================================================

ACCURACY_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Geospatial Accuracy & Error Report</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; background: #f8f9fa; color: #212529; }
        .card { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 20px; }
        h1, h2 { margin-top: 0; color: #1a73e8; }
        .summary { display: flex; gap: 20px; margin-bottom: 15px; }
        .stat-box { flex: 1; padding: 15px; border-radius: 6px; background: #e8f0fe; text-align: center; }
        .stat-box.success { background: #e6f4ea; color: #137333; }
        .stat-box.danger { background: #fce8e6; color: #c5221f; }
        .stat-num { font-size: 24px; font-weight: bold; margin-top: 5px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #dee2e6; }
        th { background: #f1f3f4; font-weight: 600; }
        tr:hover { background: #f8f9fa; }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
        .badge.pass { background: #ceead6; color: #137333; }
        .badge.fail { background: #fad2cf; color: #c5221f; }
        .nav-link { display: inline-block; margin-bottom: 15px; color: #1a73e8; text-decoration: none; font-weight: bold; }
    </style>
</head>
<body>
    <a href="/" class="nav-link">&larr; Back to Locust Web UI</a>
    <div class="card">
        <h1>📐 Geospatial Measurement Accuracy & Error Report</h1>
        <p>Ground truth verification against <code>Docs/test_fixtures/fixtures/expected.json</code>.</p>
        
        <div class="summary">
            <div class="stat-box">
                <div>Total Checks</div>
                <div class="stat-num">{{ stats.total_checks }}</div>
            </div>
            <div class="stat-box success">
                <div>Passed (within &le;1% tolerance)</div>
                <div class="stat-num">{{ stats.successful_checks }}</div>
            </div>
            <div class="stat-box danger">
                <div>Failed</div>
                <div class="stat-num">{{ stats.failed_checks }}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>Latest Feature Comparisons & Calculated Errors</h2>
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Fixture File</th>
                    <th>Idx</th>
                    <th>Geometry</th>
                    <th>Actual Measurement</th>
                    <th>Expected Measurement</th>
                    <th>Absolute Error</th>
                    <th>Relative Error (%)</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {% for r in stats.records|reverse %}
                <tr>
                    <td>{{ r.timestamp }}</td>
                    <td><strong>{{ r.fixture }}</strong></td>
                    <td>#{{ r.feature_index }}</td>
                    <td>{{ r.geometry_type }}</td>
                    <td>{{ r.actual if r.actual is not none else "null" }}</td>
                    <td>{{ r.expected if r.expected is not none else "null" }}</td>
                    <td>{{ r.abs_error }}</td>
                    <td>{{ r.rel_error_pct }}</td>
                    <td>
                        {% if r.passed %}
                        <span class="badge pass">PASSED</span>
                        {% else %}
                        <span class="badge fail">FAILED</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</body>
</html>
"""


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Extends the Locust Web UI with an Accuracy & Error Report tab and API."""
    if environment.web_ui:
        app = environment.web_ui.app

        @app.route("/accuracy")
        def accuracy_dashboard():
            return render_template_string(ACCURACY_HTML_TEMPLATE, stats=ACCURACY_STATS)

        @app.route("/api/accuracy")
        def accuracy_json():
            return jsonify(ACCURACY_STATS)

        logger.info("Accuracy Report dashboard registered at: /accuracy")
