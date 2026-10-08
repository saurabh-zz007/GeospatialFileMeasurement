import io
import math
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
import shapely.wkt

from main import app
from src.utils.helpers import (
    ERROR_EMPTY_FILE,
    ERROR_INVALID_ZIP,
    ERROR_MISSING_PRJ,
    ERROR_NO_FEATURES,
    ERROR_NO_SHP,
    ERROR_UNEXPECTED,
    choose_measurement_crs,
    clean_feature_properties,
    extract_features_and_measurements,
    read_geospatial_file,
    round_geometry_coordinates,
    sanitize_for_json,
    utm_epsg,
)

TEST_FILES_DIR = Path(__file__).resolve().parent.parent / "Docs" / "Test Files"
client = TestClient(app)


# ==============================================================================
# Unit Tests: CRS Selection & Projections
# ==============================================================================

def test_utm_epsg_formula():
    """Verifies UTM EPSG calculation for northern and southern hemispheres."""
    # Equator (lon=0, lat=0) -> Zone 31 North -> EPSG:32631
    assert utm_epsg(0.0, 0.0) == 32631
    # Equator South (lon=0, lat=-1) -> Zone 31 South -> EPSG:32731
    assert utm_epsg(0.0, -1.0) == 32731
    # Norway / 60N (lon=10.5, lat=60.5) -> Zone 32 North -> EPSG:32632
    assert utm_epsg(10.5, 60.5) == 32632
    # West hemisphere (lon=-122.4, lat=37.7) -> Zone 10 North -> EPSG:32610
    assert utm_epsg(-122.4, 37.7) == 32610


def test_choose_measurement_crs_polar_fallback():
    """Verifies polar CRS fallback outside -80° to 84°."""
    # North pole area (> 84N)
    assert choose_measurement_crs(10.0, 85.0) == "EPSG:3413"
    assert choose_measurement_crs(0.0, 84.1) == "EPSG:3413"
    # South pole area (< -80S)
    assert choose_measurement_crs(0.0, -80.5) == "EPSG:3031"
    assert choose_measurement_crs(45.0, -85.0) == "EPSG:3031"
    # Within UTM range (-80 to 84)
    assert choose_measurement_crs(10.5, 60.5) == "EPSG:32632"
    assert choose_measurement_crs(0.5, 0.5) == "EPSG:32631"


# ==============================================================================
# Unit Tests: Coordinate Rounding to 9 Decimals
# ==============================================================================

def test_coordinate_rounding_9_decimals():
    """Verifies that geometry coordinates are rounded to at most 9 decimals."""
    poly = Polygon([
        (10.123456789123456, 60.987654321987654),
        (11.123456789123456, 60.987654321987654),
        (11.123456789123456, 61.987654321987654),
        (10.123456789123456, 60.987654321987654),
    ])
    gdf = gpd.GeoDataFrame({"geometry": [poly]}, crs="EPSG:4326")
    records = extract_features_and_measurements(gdf)

    coords = records[0]["geometry"]["coordinates"][0]
    for pt in coords:
        lon_str = str(pt[0])
        lat_str = str(pt[1])
        if "." in lon_str:
            assert len(lon_str.split(".")[1]) <= 9
        if "." in lat_str:
            assert len(lat_str.split(".")[1]) <= 9
        assert pt[0] == 10.123456789 or pt[0] == 11.123456789
        assert pt[1] == 60.987654322 or pt[1] == 61.987654322


# ==============================================================================
# Unit Tests: Geometry Types & Measurements
# ==============================================================================

def test_box_60n_measurement():
    """Verifies that 1°x1° box at 60°N produces ~6.12 billion m², NOT ~25.17 billion m² (Mercator bug)."""
    kml_path = TEST_FILES_DIR / "box_60n.kml"
    assert kml_path.exists()
    gdf, crs_str = read_geospatial_file(kml_path, "box_60n.kml")
    assert crs_str == "EPSG:4326"
    records = extract_features_and_measurements(gdf, is_kml=True)

    assert len(records) == 1
    rec = records[0]
    assert rec["geometry_type"] == "Polygon"
    assert rec["measurement_crs"] == "EPSG:32632"
    assert rec["measurement_unit"] == "sq meters"

    # Expect ~6.12 billion m²
    assert 6.0e9 < rec["measurement"] < 6.2e9
    assert rec["measurement"] != pytest.approx(25.17e9, rel=0.1)
    assert rec["note"] is None


def test_equator_box_measurement():
    """Verifies 1°x1° box at equator."""
    kml_path = TEST_FILES_DIR / "equator_box.kml"
    gdf, _ = read_geospatial_file(kml_path, "equator_box.kml")
    records = extract_features_and_measurements(gdf, is_kml=True)

    assert len(records) == 1
    rec = records[0]
    assert rec["geometry_type"] == "Polygon"
    assert rec["measurement_crs"] == "EPSG:32631"
    assert rec["measurement_unit"] == "sq meters"
    assert 12.0e9 < rec["measurement"] < 12.5e9
    assert rec["note"] is None


def test_polygon_with_hole():
    """Verifies that polygon inner ring hole is subtracted from total area."""
    kml_path = TEST_FILES_DIR / "polygon_with_hole.kml"
    gdf, _ = read_geospatial_file(kml_path, "polygon_with_hole.kml")
    records = extract_features_and_measurements(gdf, is_kml=True)

    assert len(records) == 1
    rec = records[0]
    assert rec["geometry_type"] == "Polygon"
    assert rec["measurement_crs"] == "EPSG:32631"
    assert 1.15e8 < rec["measurement"] < 1.20e8
    assert rec["note"] is None


def test_utm_zone_crossing():
    """Verifies that features crossing UTM zone border are projected using their centroid UTM zone."""
    kml_path = TEST_FILES_DIR / "utm_zone_crossing.kml"
    gdf, _ = read_geospatial_file(kml_path, "utm_zone_crossing.kml")
    records = extract_features_and_measurements(gdf, is_kml=True)

    assert len(records) == 1
    rec = records[0]
    assert rec["geometry_type"] == "Polygon"
    assert rec["measurement_crs"] == "EPSG:32632"
    assert 1.55e8 < rec["measurement"] < 1.65e8
    assert rec["note"] is None


def test_mixed_geometries():
    """Verifies mixed geometries (Point, LineString, Polygon), property cleanup, and NaT handling."""
    kml_path = TEST_FILES_DIR / "mixed_geometries.kml"
    gdf, _ = read_geospatial_file(kml_path, "mixed_geometries.kml")
    records = extract_features_and_measurements(gdf, is_kml=True)

    assert len(records) == 3

    # Feature 0: Point
    pt_rec = records[0]
    assert pt_rec["geometry_type"] == "Point"
    assert pt_rec["measurement"] is None
    assert pt_rec["measurement_unit"] is None
    assert pt_rec["measurement_crs"] is None
    assert pt_rec["note"] is None
    # ExtendedData preserved
    assert pt_rec["properties"].get("owner") == "survey_team"
    assert pt_rec["properties"].get("Name") == "my_point"
    # NaT converted to None
    assert pt_rec["properties"].get("timestamp") is None
    assert pt_rec["properties"].get("begin") is None
    assert pt_rec["properties"].get("end") is None

    # Feature 1: LineString
    line_rec = records[1]
    assert line_rec["geometry_type"] == "LineString"
    assert line_rec["measurement_unit"] == "meters"
    assert line_rec["measurement_crs"] == "EPSG:32631"
    assert 110_000 < line_rec["measurement"] < 112_000
    assert line_rec["properties"].get("owner") is None

    # Feature 2: Polygon
    poly_rec = records[2]
    assert poly_rec["geometry_type"] == "Polygon"
    assert poly_rec["measurement_unit"] == "sq meters"
    assert poly_rec["measurement_crs"] == "EPSG:32631"
    assert 12.0e9 < poly_rec["measurement"] < 12.5e9

    # Check dropped fields across all records
    for rec in records:
        props = rec["properties"]
        for dropped_field in ["id", "icon", "drawOrder", "altitudeMode", "tessellate", "visibility", "extrude"]:
            assert dropped_field not in props, f"Dropped field {dropped_field} still in properties"


def test_multipolygon_and_multilinestring():
    """Verifies MultiPolygon sums areas and MultiLineString sums lengths."""
    poly1 = Polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01), (0, 0)])
    poly2 = Polygon([(0.02, 0.02), (0.03, 0.02), (0.03, 0.03), (0.02, 0.03), (0.02, 0.02)])
    mp = MultiPolygon([poly1, poly2])

    line1 = LineString([(0, 0), (0.01, 0)])
    line2 = LineString([(0, 0.01), (0.01, 0.01)])
    mls = MultiLineString([line1, line2])

    gdf = gpd.GeoDataFrame({"geometry": [mp, mls]}, crs="EPSG:4326")
    records = extract_features_and_measurements(gdf)

    # MultiPolygon
    assert records[0]["geometry_type"] == "MultiPolygon"
    assert records[0]["measurement_unit"] == "sq meters"
    assert records[0]["measurement"] > 0
    assert records[0]["measurement_crs"] == "EPSG:32631"

    # MultiLineString
    assert records[1]["geometry_type"] == "MultiLineString"
    assert records[1]["measurement_unit"] == "meters"
    assert records[1]["measurement"] > 0
    assert records[1]["measurement_crs"] == "EPSG:32631"


def test_unsupported_and_invalid_geometries():
    """Verifies that unsupported, empty, and invalid geometries do not crash and report notes."""
    geom_col = GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])
    empty_poly = Polygon()
    invalid_poly = shapely.wkt.loads("POLYGON ((0 0, 0 2, 2 0, 2 2, 0 0))")
    multi_point = MultiPoint([(0, 0), (1, 1)])

    gdf = gpd.GeoDataFrame({"geometry": [geom_col, empty_poly, invalid_poly, multi_point]}, crs="EPSG:4326")
    records = extract_features_and_measurements(gdf)

    assert len(records) == 4

    # GeometryCollection
    assert records[0]["measurement"] is None
    assert "Unsupported geometry type" in records[0]["note"]

    # Empty Polygon
    assert records[1]["measurement"] is None
    assert "empty" in records[1]["note"]

    # Invalid Polygon (Self-intersection)
    assert records[2]["measurement"] is None
    assert "Invalid geometry" in records[2]["note"]

    # MultiPoint
    assert records[3]["measurement"] is None
    assert records[3]["note"] is None


def test_projected_source_crs_transformation():
    """Verifies shapefile in projected CRS (e.g. EPSG:3857) transforms to 4326 and measures via UTM."""
    geom_4326 = Polygon([(10.0, 50.0), (10.1, 50.0), (10.1, 50.1), (10.0, 50.1), (10.0, 50.0)])
    gdf_4326 = gpd.GeoDataFrame({"geometry": [geom_4326]}, crs="EPSG:4326")
    gdf_3857 = gdf_4326.to_crs(epsg=3857)

    records = extract_features_and_measurements(gdf_3857)
    assert len(records) == 1
    rec = records[0]
    assert rec["measurement_crs"] == "EPSG:32632"
    coords = rec["geometry"]["coordinates"][0]
    assert 9.9 < coords[0][0] < 10.2
    assert 49.9 < coords[0][1] < 50.2


def test_sanitize_for_json_no_nan_or_nat():
    """Verifies that sanitize_for_json replaces NaT, NaN, inf with None."""
    assert sanitize_for_json(float("nan")) is None
    assert sanitize_for_json(float("inf")) is None
    assert sanitize_for_json(float("-inf")) is None
    assert sanitize_for_json("NaT") is None
    assert sanitize_for_json("NaN") is None
    assert sanitize_for_json("nan") is None

    nested = {
        "val": float("nan"),
        "list": [1.0, float("inf"), "NaT"],
        "normal": "hello",
    }
    cleaned = sanitize_for_json(nested)
    assert cleaned["val"] is None
    assert cleaned["list"] == [1.0, None, None]
    assert cleaned["normal"] == "hello"


# ==============================================================================
# Failure Pipeline & Specific Error Message Tests
# ==============================================================================

def test_reject_zero_byte_upload():
    """Verifies that zero-byte uploads are rejected with HTTP 400 at upload time."""
    # Zero-byte KML
    resp_kml = client.post(
        "/api/files/",
        files={"file": ("zero.kml", b"", "application/vnd.google-earth.kml+xml")},
    )
    assert resp_kml.status_code == 400
    assert "empty" in resp_kml.json()["detail"].lower()

    # Zero-byte Zip
    resp_zip = client.post(
        "/api/files/",
        files={"file": ("zero.zip", b"", "application/zip")},
    )
    assert resp_zip.status_code == 400
    assert "empty" in resp_zip.json()["detail"].lower()


def test_error_not_a_valid_zip(tmp_path):
    """Verifies error 'not a valid zip' when non-empty corrupt zip is uploaded."""
    corrupt_zip = tmp_path / "corrupt.zip"
    corrupt_zip.write_bytes(b"This is completely invalid zip file content.")

    with open(corrupt_zip, "rb") as f:
        resp = client.post(
            "/api/files/",
            files={"file": ("corrupt.zip", f, "application/zip")},
        )
    assert resp.status_code == 202
    file_id = resp.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        if status_resp.json()["status"] in ("FAILED", "COMPLETED"):
            break
        time.sleep(0.5)

    data = status_resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] == ERROR_INVALID_ZIP

    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.status_code == 200
    meas_data = meas_resp.json()
    assert meas_data["status"] == "FAILED"
    assert meas_data["error"] == ERROR_INVALID_ZIP
    assert len(meas_data["features"]) == 0


def test_error_no_shp_found_in_archive(tmp_path):
    """Verifies error 'no .shp found in archive' when zip contains files but no .shp."""
    zip_path = tmp_path / "no_shp.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "Some text file without any shapefile.")

    with open(zip_path, "rb") as f:
        resp = client.post(
            "/api/files/",
            files={"file": ("no_shp.zip", f, "application/zip")},
        )
    assert resp.status_code == 202
    file_id = resp.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        if status_resp.json()["status"] in ("FAILED", "COMPLETED"):
            break
        time.sleep(0.5)

    data = status_resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] == ERROR_NO_SHP

    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.json()["error"] == ERROR_NO_SHP


def test_error_missing_prj(tmp_path):
    """Verifies error 'missing .prj (coordinate system unknown)' when shapefile lacks .prj."""
    shp_path = tmp_path / "test.shp"
    gdf = gpd.GeoDataFrame({"geometry": [Point(0, 0)]}, crs="EPSG:4326")
    gdf.to_file(shp_path)
    prj_file = tmp_path / "test.prj"
    if prj_file.exists():
        prj_file.unlink()

    zip_file = tmp_path / "no_prj.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        for f in tmp_path.glob("test.*"):
            zf.write(f, f.name)

    with open(zip_file, "rb") as f:
        resp = client.post(
            "/api/files/",
            files={"file": ("no_prj.zip", f, "application/zip")},
        )
    assert resp.status_code == 202
    file_id = resp.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        if status_resp.json()["status"] in ("FAILED", "COMPLETED"):
            break
        time.sleep(0.5)

    data = status_resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] == ERROR_MISSING_PRJ

    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.json()["error"] == ERROR_MISSING_PRJ


def test_error_empty_file_in_archive(tmp_path):
    """Verifies error 'empty file' when shapefile in archive is 0 bytes."""
    zip_path = tmp_path / "empty_shp.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("test.shp", b"")

    with open(zip_path, "rb") as f:
        resp = client.post(
            "/api/files/",
            files={"file": ("empty_shp.zip", f, "application/zip")},
        )
    assert resp.status_code == 202
    file_id = resp.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        if status_resp.json()["status"] in ("FAILED", "COMPLETED"):
            break
        time.sleep(0.5)

    data = status_resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] == ERROR_EMPTY_FILE


def test_error_no_features_could_be_read(tmp_path):
    """Verifies error 'no features could be read (possibly missing/corrupt .shx)'."""
    # Write a shapefile with no rows
    shp_path = tmp_path / "empty_layer.shp"
    gdf = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
    gdf.to_file(shp_path)

    zip_file = tmp_path / "empty_layer.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        for f in tmp_path.glob("empty_layer.*"):
            zf.write(f, f.name)

    with open(zip_file, "rb") as f:
        resp = client.post(
            "/api/files/",
            files={"file": ("empty_layer.zip", f, "application/zip")},
        )
    assert resp.status_code == 202
    file_id = resp.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        if status_resp.json()["status"] in ("FAILED", "COMPLETED"):
            break
        time.sleep(0.5)

    data = status_resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] == ERROR_NO_FEATURES


def test_error_unexpected_processing_fallback(tmp_path):
    """Verifies fallback message 'unexpected processing error' without leaking stacktrace."""
    # Upload valid file but mock an unexpected runtime exception in extract_features
    kml_path = TEST_FILES_DIR / "box_60n.kml"

    with patch("src.logs.controllers.extract_features_and_measurements", side_effect=RuntimeError("Secret database crash line 99")):
        with open(kml_path, "rb") as f:
            resp = client.post(
                "/api/files/",
                files={"file": ("box_60n.kml", f, "application/vnd.google-earth.kml+xml")},
            )
        assert resp.status_code == 202
        file_id = resp.json()["id"]

        for _ in range(30):
            status_resp = client.get(f"/api/files/{file_id}/")
            if status_resp.json()["status"] in ("FAILED", "COMPLETED"):
                break
            time.sleep(0.5)

    data = status_resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] == ERROR_UNEXPECTED
    # Ensure sensitive internal trace is NOT leaked to client
    assert "Secret database crash" not in str(data)

    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.json()["error"] == ERROR_UNEXPECTED


# ==============================================================================
# End-to-End API Integration Tests (source_crs, geometry_crs, coordinate rounding)
# ==============================================================================

def test_api_upload_and_measurements_box_60n():
    """E2E Test: Upload box_60n.kml, verify source_crs, geometry_crs, and 9-decimal coordinates."""
    file_path = TEST_FILES_DIR / "box_60n.kml"
    with open(file_path, "rb") as f:
        response = client.post(
            "/api/files/",
            files={"file": ("box_60n.kml", f, "application/vnd.google-earth.kml+xml")},
        )
    assert response.status_code == 202
    file_id = response.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        assert status_resp.status_code == 200
        if status_resp.json()["status"] == "COMPLETED":
            break
        time.sleep(0.5)

    status_data = status_resp.json()
    assert status_data["status"] == "COMPLETED"
    assert status_data["feature_count"] == 1
    assert status_data["source_crs"] == "EPSG:4326"
    assert status_data["geometry_crs"] == "EPSG:4326"
    assert "crs" not in status_data
    assert status_data["error"] is None

    # Get measurements
    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.status_code == 200
    data = meas_resp.json()
    assert data["source_crs"] == "EPSG:4326"
    assert data["geometry_crs"] == "EPSG:4326"
    assert "crs" not in data
    assert data["error"] is None

    feat = data["features"][0]
    assert feat["measurement_crs"] == "EPSG:32632"
    assert 6.0e9 < feat["measurement"] < 6.2e9
    assert feat["note"] is None

    # Coordinates rounded to at most 9 decimals
    coords = feat["geometry"]["coordinates"][0]
    for pt in coords:
        for c in pt:
            s = str(c)
            if "." in s:
                assert len(s.split(".")[1]) <= 9


def test_api_upload_and_measurements_mixed_geometries():
    """E2E Test: Upload mixed_geometries.kml, verify properties, units, and notes."""
    file_path = TEST_FILES_DIR / "mixed_geometries.kml"
    with open(file_path, "rb") as f:
        response = client.post(
            "/api/files/",
            files={"file": ("mixed_geometries.kml", f, "application/vnd.google-earth.kml+xml")},
        )
    assert response.status_code == 202
    file_id = response.json()["id"]

    for _ in range(30):
        status_resp = client.get(f"/api/files/{file_id}/")
        if status_resp.json()["status"] == "COMPLETED":
            break
        time.sleep(0.5)

    assert status_resp.json()["status"] == "COMPLETED"
    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.status_code == 200
    meas_data = meas_resp.json()
    assert meas_data["source_crs"] == "EPSG:4326"
    assert meas_data["geometry_crs"] == "EPSG:4326"
    assert "crs" not in meas_data
    assert meas_data["error"] is None

    features = meas_data["features"]
    assert len(features) == 3

    # Feature 0: Point
    assert features[0]["geometry_type"] == "Point"
    assert features[0]["measurement"] is None
    assert features[0]["measurement_unit"] is None
    assert features[0]["measurement_crs"] is None
    assert features[0]["properties"].get("owner") == "survey_team"

    # Feature 1: LineString
    assert features[1]["geometry_type"] == "LineString"
    assert features[1]["measurement_unit"] == "meters"
    assert features[1]["measurement_crs"] == "EPSG:32631"
    assert features[1]["measurement"] > 100_000

    # Feature 2: Polygon
    assert features[2]["geometry_type"] == "Polygon"
    assert features[2]["measurement_unit"] == "sq meters"
    assert features[2]["measurement_crs"] == "EPSG:32631"
    assert features[2]["measurement"] > 10e9


def test_concurrent_polling_during_20k_feature_processing():
    """
    Verifies that processing a 20k-feature file does NOT starve the DB connection pool.
    While Phase (b) CPU-bound parsing/measuring runs in the background thread,
    concurrent GET /api/files/{id}/ polling stays fast (asserted < 500 ms per request).
    """
    from src.utils.db import engine

    # Pre-warm pool connections so TCP/TLS handshake to remote DB does not skew first-request latency
    c1 = engine.connect()
    c2 = engine.connect()
    c1.close()
    c2.close()

    load_file = Path(__file__).resolve().parent.parent / "Docs" / "test_fixtures" / "fixtures" / "load" / "large_20000.zip"
    assert load_file.exists(), f"Load test file not found at {load_file}"

    with open(load_file, "rb") as f:
        upload_resp = client.post(
            "/api/files/",
            files={"file": ("large_20000.zip", f, "application/zip")},
        )
    assert upload_resp.status_code == 202
    file_id = upload_resp.json()["id"]

    latencies = []
    statuses = []
    max_wait_seconds = 60
    start_time = time.time()

    # Poll status concurrently while background thread executes Phase (b) & (c)
    while time.time() - start_time < max_wait_seconds:
        req_start = time.perf_counter()
        resp = client.get(f"/api/files/{file_id}/")
        req_duration = time.perf_counter() - req_start

        latencies.append(req_duration)
        status = resp.json()["status"]
        statuses.append(status)

        assert resp.status_code == 200

        if status in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.2)

    assert "PROCESSING" in statuses or "PENDING" in statuses
    final_resp = client.get(f"/api/files/{file_id}/")
    assert final_resp.status_code == 200
    final_data = final_resp.json()
    assert final_data["status"] == "COMPLETED"
    assert final_data["feature_count"] == 20000
    assert len(latencies) >= 5, f"Expected multiple polling requests during 20k processing, got {len(latencies)}"

    # Assert GET stays fast under 500 ms (median and p90 stay under 500ms, proving no pool starvation)
    median_latency = sorted(latencies)[len(latencies) // 2]
    p90_idx = int(len(latencies) * 0.90)
    p90_latency = sorted(latencies)[p90_idx]

    assert median_latency < 0.5, f"Median GET latency {median_latency * 1000:.1f}ms exceeded 500ms threshold!"
    assert p90_latency < 0.5, f"P90 GET latency {p90_latency * 1000:.1f}ms exceeded 500ms threshold!"


