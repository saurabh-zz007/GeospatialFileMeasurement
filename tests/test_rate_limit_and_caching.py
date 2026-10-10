import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.logs.data_transfer_objects import (
    FeatureItemResponse,
    FileMeasurementsResponse,
    FileStatusResponse,
)
from src.logs.models import FileStatus
from src.utils.cache import clear_cache, get_ttl_cache
from src.utils.limiter import limiter
from src.utils.settings import settings


@pytest.fixture(autouse=True)
def clean_environment():
    """Resets limiter storage, clears cache, and restores default settings for each test."""
    orig_rl_enabled = settings.RATE_LIMIT_ENABLED
    orig_rl_post = settings.RATE_LIMIT_POST_FILES
    orig_rl_get = settings.RATE_LIMIT_GET_FILES
    orig_cache_enabled = settings.CACHE_ENABLED
    orig_cache_ttl = settings.CACHE_TTL_SECONDS
    orig_cache_max = settings.CACHE_MAX_ENTRIES

    settings.RATE_LIMIT_ENABLED = True
    limiter.reset()
    clear_cache()

    yield

    settings.RATE_LIMIT_ENABLED = orig_rl_enabled
    settings.RATE_LIMIT_POST_FILES = orig_rl_post
    settings.RATE_LIMIT_GET_FILES = orig_rl_get
    settings.CACHE_ENABLED = orig_cache_enabled
    settings.CACHE_TTL_SECONDS = orig_cache_ttl
    settings.CACHE_MAX_ENTRIES = orig_cache_max
    limiter.reset()
    clear_cache()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


# ==============================================================================
# Rate Limiting Tests (slowapi)
# ==============================================================================

def test_rate_limit_post_exceeded_returns_429(client):
    """
    Verifies that POST /api/files/ enforces rate limit per client IP,
    returning HTTP 429, a Retry-After header, and a JSON error payload.
    """
    settings.RATE_LIMIT_POST_FILES = "2/minute"
    limiter.reset()

    # Request 1
    resp1 = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
    assert resp1.status_code == 400  # rejected for empty file, but counted against limit

    # Request 2
    resp2 = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
    assert resp2.status_code == 400

    # Request 3 -> Exceeded limit
    resp3 = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
    assert resp3.status_code == 429
    assert "Retry-After" in resp3.headers
    retry_after = int(resp3.headers["Retry-After"])
    assert retry_after >= 1

    data = resp3.json()
    assert "detail" in data or "error" in data
    error_msg = data.get("detail") or data.get("error")
    assert "Rate limit exceeded" in error_msg


def test_rate_limit_get_exceeded_returns_429(client):
    """
    Verifies that GET endpoints enforce separate rate limits per client IP,
    returning HTTP 429, a Retry-After header, and a JSON error payload.
    """
    settings.RATE_LIMIT_GET_FILES = "3/minute"
    limiter.reset()

    fake_id = uuid.uuid4()
    dummy_status = FileStatusResponse(
        id=fake_id,
        filename="test.zip",
        status=FileStatus.PENDING,
        source_crs="EPSG:4326",
        geometry_crs="EPSG:4326",
        feature_count=0,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    with patch("src.logs.router.get_file_status_controller", new_callable=AsyncMock, return_value=dummy_status):
        for _ in range(3):
            resp = client.get(f"/api/files/{fake_id}/")
            assert resp.status_code == 200

        # 4th request exceeds GET limit
        resp_exceeded = client.get(f"/api/files/{fake_id}/")
        assert resp_exceeded.status_code == 429
        assert "Retry-After" in resp_exceeded.headers
        data = resp_exceeded.json()
        assert "Rate limit exceeded" in (data.get("detail") or data.get("error", ""))


def test_rate_limit_recovery_after_window(client):
    """Verifies that client can make requests again after rate limit window passes."""
    settings.RATE_LIMIT_POST_FILES = "1/second"
    limiter.reset()

    # First request
    r1 = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
    assert r1.status_code == 400

    # Second immediate request hits rate limit
    r2 = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
    assert r2.status_code == 429

    # Wait for the 1-second window to reset
    time.sleep(1.1)

    # Recovered
    r3 = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
    assert r3.status_code != 429


def test_health_endpoint_excluded_from_limits(client):
    """Verifies that /health is completely excluded from rate limits."""
    settings.RATE_LIMIT_GET_FILES = "1/minute"
    limiter.reset()

    for _ in range(10):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_rate_limits_can_be_disabled_via_env(client):
    """Verifies that setting RATE_LIMIT_ENABLED=False completely disables rate limiting."""
    settings.RATE_LIMIT_ENABLED = False
    settings.RATE_LIMIT_POST_FILES = "1/minute"
    settings.RATE_LIMIT_GET_FILES = "1/minute"
    limiter.reset()

    fake_id = uuid.uuid4()
    dummy_status = FileStatusResponse(
        id=fake_id,
        filename="test.zip",
        status=FileStatus.PENDING,
        source_crs="EPSG:4326",
        geometry_crs="EPSG:4326",
        feature_count=0,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    with patch("src.logs.router.get_file_status_controller", new_callable=AsyncMock, return_value=dummy_status):
        for _ in range(5):
            resp_post = client.post("/api/files/", files={"file": ("zero.zip", b"", "application/zip")})
            assert resp_post.status_code != 429

            resp_get = client.get(f"/api/files/{fake_id}/")
            assert resp_get.status_code != 429


# ==============================================================================
# Caching Tests (cachetools TTLCache, Cache-Control, ETag, 304, X-Cache)
# ==============================================================================

def test_pending_and_processing_responses_never_cached(client):
    """
    Verifies that GET /api/files/{id}/ and GET /api/files/{id}/measurements/
    are NEVER cached when the status is PENDING or PROCESSING.
    Must always emit X-Cache: MISS and omit Cache-Control/ETag headers.
    """
    test_id = uuid.uuid4()
    mock_pending_dto = FileStatusResponse(
        id=test_id,
        filename="pending_file.zip",
        status=FileStatus.PENDING,
        source_crs=None,
        geometry_crs="EPSG:4326",
        feature_count=0,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    with patch(
        "src.logs.router.get_file_status_controller",
        new=AsyncMock(return_value=mock_pending_dto),
    ) as mock_ctrl:
        # First call
        resp1 = client.get(f"/api/files/{test_id}/")
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache") == "MISS"
        assert "ETag" not in resp1.headers
        assert "Cache-Control" not in resp1.headers
        assert resp1.json()["status"] == "PENDING"

        # Second call
        resp2 = client.get(f"/api/files/{test_id}/")
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Cache") == "MISS"
        assert "ETag" not in resp2.headers

        # Controller was invoked both times because response was never cached
        assert mock_ctrl.call_count == 2

    # Now verify PROCESSING status is also never cached
    mock_processing_dto = FileStatusResponse(
        id=test_id,
        filename="pending_file.zip",
        status=FileStatus.PROCESSING,
        source_crs=None,
        geometry_crs="EPSG:4326",
        feature_count=0,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    with patch(
        "src.logs.router.get_file_status_controller",
        new=AsyncMock(return_value=mock_processing_dto),
    ) as mock_ctrl:
        resp3 = client.get(f"/api/files/{test_id}/")
        assert resp3.status_code == 200
        assert resp3.headers.get("X-Cache") == "MISS"
        assert "ETag" not in resp3.headers
        assert resp3.json()["status"] == "PROCESSING"

        resp4 = client.get(f"/api/files/{test_id}/")
        assert resp4.status_code == 200
        assert resp4.headers.get("X-Cache") == "MISS"
        assert mock_ctrl.call_count == 2


def test_completed_response_served_from_cache_and_identical_json(client):
    """
    Verifies that a COMPLETED file response:
    1. Returns X-Cache: MISS on the 1st call.
    2. Sends Cache-Control and ETag headers.
    3. Serves from cache on the 2nd call (X-Cache: HIT) without hitting the DB controller.
    4. Returns identical JSON to the uncached path.
    """
    test_id = uuid.uuid4()
    mock_completed_dto = FileStatusResponse(
        id=test_id,
        filename="polygons.zip",
        status=FileStatus.COMPLETED,
        source_crs="EPSG:4326",
        geometry_crs="EPSG:4326",
        feature_count=42,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    with patch(
        "src.logs.router.get_file_status_controller",
        new=AsyncMock(return_value=mock_completed_dto),
    ) as mock_ctrl:
        # First call: cache MISS
        resp1 = client.get(f"/api/files/{test_id}/")
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache") == "MISS"
        assert "ETag" in resp1.headers
        assert "Cache-Control" in resp1.headers
        etag1 = resp1.headers["ETag"]
        json1 = resp1.json()

        assert mock_ctrl.call_count == 1

        # Second call: cache HIT
        resp2 = client.get(f"/api/files/{test_id}/")
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Cache") == "HIT"
        assert resp2.headers["ETag"] == etag1
        assert resp2.headers["Cache-Control"] == resp1.headers["Cache-Control"]
        json2 = resp2.json()

        # Controller was NOT called a second time
        assert mock_ctrl.call_count == 1

        # Verify cached JSON is byte-for-byte identical to the uncached response
        assert json1 == json2
        assert json2["status"] == "COMPLETED"
        assert json2["feature_count"] == 42


def test_completed_measurements_served_from_cache(client):
    """
    Verifies that GET /api/files/{id}/measurements/ caches COMPLETED results,
    emits X-Cache: HIT on second call, and returns identical JSON.
    """
    test_id = uuid.uuid4()
    mock_measurements_dto = FileMeasurementsResponse(
        file_id=test_id,
        filename="polygons.zip",
        status=FileStatus.COMPLETED,
        source_crs="EPSG:4326",
        geometry_crs="EPSG:4326",
        feature_count=1,
        error=None,
        features=[
            FeatureItemResponse(
                id=uuid.uuid4(),
                feature_index=0,
                geometry_type="Polygon",
                geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                properties={"name": "field_a"},
                measurement=10000.0,
                measurement_unit="sq meters",
                measurement_crs="EPSG:32631",
                note=None,
            )
        ],
    )

    with patch(
        "src.logs.router.get_file_measurements_controller",
        new=AsyncMock(return_value=mock_measurements_dto),
    ) as mock_ctrl:
        resp1 = client.get(f"/api/files/{test_id}/measurements/")
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache") == "MISS"
        assert "ETag" in resp1.headers
        json1 = resp1.json()

        resp2 = client.get(f"/api/files/{test_id}/measurements/")
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Cache") == "HIT"
        json2 = resp2.json()

        assert mock_ctrl.call_count == 1
        assert json1 == json2
        assert len(json2["features"]) == 1


def test_failed_response_is_cached(client):
    """Verifies that FAILED status is considered final and is cached."""
    test_id = uuid.uuid4()
    mock_failed_dto = FileStatusResponse(
        id=test_id,
        filename="corrupt.zip",
        status=FileStatus.FAILED,
        source_crs=None,
        geometry_crs="EPSG:4326",
        feature_count=0,
        error="not a valid zip",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    with patch(
        "src.logs.router.get_file_status_controller",
        new=AsyncMock(return_value=mock_failed_dto),
    ) as mock_ctrl:
        resp1 = client.get(f"/api/files/{test_id}/")
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache") == "MISS"
        assert resp1.json()["status"] == "FAILED"

        resp2 = client.get(f"/api/files/{test_id}/")
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Cache") == "HIT"
        assert mock_ctrl.call_count == 1
        assert resp1.json() == resp2.json()


def test_honour_if_none_match_with_304(client):
    """
    Verifies that sending If-None-Match with a matching ETag
    returns HTTP 304 Not Modified with X-Cache: HIT and empty body.
    """
    test_id = uuid.uuid4()
    mock_dto = FileStatusResponse(
        id=test_id,
        filename="polygons.zip",
        status=FileStatus.COMPLETED,
        source_crs="EPSG:4326",
        geometry_crs="EPSG:4326",
        feature_count=10,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    with patch(
        "src.logs.router.get_file_status_controller",
        new=AsyncMock(return_value=mock_dto),
    ):
        # Initial request to obtain ETag
        resp1 = client.get(f"/api/files/{test_id}/")
        assert resp1.status_code == 200
        etag = resp1.headers["ETag"]

        # Request with exact If-None-Match
        resp_304 = client.get(f"/api/files/{test_id}/", headers={"If-None-Match": etag})
        assert resp_304.status_code == 304
        assert resp_304.headers.get("X-Cache") == "HIT"
        assert resp_304.headers.get("ETag") == etag
        assert "Cache-Control" in resp_304.headers
        assert resp_304.text == ""

        # Request with weak ETag prefix W/
        weak_etag = f"W/{etag}"
        resp_weak_304 = client.get(f"/api/files/{test_id}/", headers={"If-None-Match": weak_etag})
        assert resp_weak_304.status_code == 304
        assert resp_weak_304.text == ""

        # Request with non-matching ETag returns 200
        resp_mismatch = client.get(f"/api/files/{test_id}/", headers={"If-None-Match": '"different-etag"'})
        assert resp_mismatch.status_code == 200


def test_caching_can_be_disabled_via_env(client):
    """Verifies that setting CACHE_ENABLED=False disables caching completely."""
    settings.CACHE_ENABLED = False
    test_id = uuid.uuid4()
    mock_dto = FileStatusResponse(
        id=test_id,
        filename="polygons.zip",
        status=FileStatus.COMPLETED,
        source_crs="EPSG:4326",
        geometry_crs="EPSG:4326",
        feature_count=10,
        error=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    with patch(
        "src.logs.router.get_file_status_controller",
        new=AsyncMock(return_value=mock_dto),
    ) as mock_ctrl:
        resp1 = client.get(f"/api/files/{test_id}/")
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache") == "MISS"

        resp2 = client.get(f"/api/files/{test_id}/")
        assert resp2.status_code == 200
        # Remains MISS because cache is disabled
        assert resp2.headers.get("X-Cache") == "MISS"
        assert mock_ctrl.call_count == 2
        assert len(get_ttl_cache()) == 0
