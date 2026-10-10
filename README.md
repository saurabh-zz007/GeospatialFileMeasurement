# Geospatial File Measurement API

A high-performance asynchronous REST API built with FastAPI and PostgreSQL that ingests geospatial files (ESRI Shapefile archives and KML), extracts geographic features, projects them to local metric coordinate systems, calculates surface areas and lengths, and provides cached, rate-limited query endpoints.

---

## 1. Overview

The Geospatial File Measurement API accepts uploads of geospatial vectors in either `.kml` or zipped `.zip` Shapefile formats, executes processing asynchronously in the background, calculates metric measurements (polygon area in square meters and linestring length in meters) using localized Universal Transverse Mercator (UTM) projections, and exposes query endpoints for tracking status and inspecting computed geometries.

### Technology Stack
- **Web Framework**: FastAPI 0.142+ (ASGI, Python 3.12)
- **ASGI Server**: Uvicorn 0.54+
- **Asynchronous Database ORM**: SQLAlchemy 2.1+ with `asyncpg` 0.32+
- **Database**: PostgreSQL 15+ (with native JSONB storage)
- **Geospatial Processing Engine**: GeoPandas 1.2+, Shapely 2.2+, PyProj 3.8+, Fiona 1.10+
- **Data Validation & Settings**: Pydantic 2.13+ and `pydantic-settings` 2.15+
- **Rate Limiting**: SlowAPI 0.1.10+ (token-bucket per client IP)
- **In-Memory Caching**: `cachetools` 7.2+ (TTLCache with HTTP ETag & 304 Not Modified support)
- **Load Testing**: Locust 2.46+
- **Test Runner**: Pytest 9.1+ with AnyIO

---

## 2. Setup and Installation

### Prerequisites
- Python 3.12 (Tested on Python 3.12.10, compatible with Windows, Linux, and macOS)

### 1. Create and Activate Virtual Environment
Clone or navigate to the repository root directory:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

**Runtime Dependencies:**
```bash
pip install -r requirements.txt
```

**Development & Test Dependencies (includes pytest, httpx, locust):**
```bash
pip install -r requirements-dev.txt
```

Verify dependency tree integrity:
```bash
pip check
```

### 3. Environment Configuration
Copy the sample environment file to `.env`:

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**Linux / macOS:**
```bash
cp .env.example .env
```

Ensure your `DATABASE_URL` in `.env` points to a reachable PostgreSQL database (e.g. `postgresql://postgres:password@localhost:5432/geospatial_db`). Database tables and indexes are initialized automatically on application startup via SQLAlchemy's lifespan handler.

### 4. Running the Application Server
Start the ASGI server with Uvicorn:

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

- Interactive OpenAPI Documentation: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- Alternative ReDoc Documentation: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- Health Check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

### 5. Running the Test Suite
Execute the full test suite (35 automated tests covering geospatial geometry validation, UTM projections, background concurrency, bounded thread pools, rate limiting, and caching):

```bash
pytest -v
```

Execute load testing using Locust:
```bash
locust -f tests/locustfile.py --headless -u 10 -r 2 -t 30s --host http://127.0.0.1:8000
```

Database tables are automatically created on application startup via SQLAlchemy's metadata initialization inside the FastAPI lifespan handler. No manual migration step is required for initial boot.

### Configuration Reference

All settings are managed via Pydantic BaseSettings in `src/utils/settings.py` and can be overridden via environment variables or `.env`:

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PROJECT_NAME` | `Geospatial File Measurement API` | Application display title for OpenAPI documentation |
| `VERSION` | `1.0.0` | API version string |
| `API_PREFIX` | `/api` | Root routing prefix for API endpoints |
| `DATABASE_URL` | *Required* | PostgreSQL connection string (auto-adapts to `postgresql+asyncpg://`) |
| `UPLOAD_DIR` | `uploads` | Directory for temporary raw upload files on disk |
| `TARGET_PROJECTED_CRS` | `EPSG:3857` | Default fallback target CRS identifier |
| `LOG_LEVEL` | `INFO` | Application logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `POOL_SIZE` | `5` | SQLAlchemy QueuePool steady-state persistent connection count |
| `MAX_OVERFLOW` | `5` | Maximum number of transient connections permitted beyond `POOL_SIZE` |
| `POOL_TIMEOUT` | `10.0` | Maximum seconds to block waiting for an available pool connection |
| `POOL_PRE_PING` | `false` | Tests connection liveness before checkout (`false` reduces remote DB latency) |
| `POOL_RECYCLE` | `1800` | Recycles idle connections after 30 minutes to prevent backend timeouts |
| `MAX_CONCURRENT_WORKERS` | `4` | Worker threads in the internal CPU ThreadPoolExecutor |
| `MAX_CONCURRENT_JOBS` | `2` | Module-level semaphore bounding concurrent background processing jobs |
| `RATE_LIMIT_ENABLED` | `true` | Master toggle enabling or bypassing SlowAPI IP rate limiting |
| `RATE_LIMIT_POST_FILES` | `30/minute` | Rate limit window applied to `POST /api/files/` |
| `RATE_LIMIT_GET_FILES` | `300/minute` | Rate limit window applied to GET polling and measurement endpoints |
| `CACHE_ENABLED` | `true` | Master toggle enabling in-memory TTLCache for terminal file states |
| `CACHE_MAX_ENTRIES` | `1000` | Maximum number of cached JSON responses in memory |
| `CACHE_TTL_SECONDS` | `300` | Expiration time for cached responses in seconds (5 minutes) |

### 4. Start the Application
Run the ASGI development server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Interactive Swagger documentation is available at `http://localhost:8000/docs`. ReDoc documentation is available at `http://localhost:8000/redoc`.

---

## 3. API Reference

### File Processing Status Lifecycle
- `PENDING`: Upload received and registered in database. Queued for worker processing.
- `PROCESSING`: Worker acquired concurrency semaphore; parsing and metric calculations in progress.
- `COMPLETED`: Features extracted, projected, measured, and persisted successfully.
- `FAILED`: Processing failed due to validation or formatting errors (e.g., missing `.prj`, corrupt zip). The `error` field contains a descriptive message.

---

### Endpoint 1: Health Check
`GET /health`

Verifies server liveness. This endpoint is completely exempt from rate limiting.

#### Example Request
```bash
curl -X GET "http://localhost:8000/health"
```

#### Example Response (HTTP 200 OK)
```json
{
  "status": "ok"
}
```

---

### Endpoint 2: Upload Geospatial File
`POST /api/files/`

Accepts a multipart file upload containing either a `.kml` file or a zipped Shapefile (`.zip`). Validates file non-emptiness, saves the payload asynchronously to `uploads/`, registers an initial `PENDING` tracking record in PostgreSQL, and enqueues background processing.

- **Status Code**: `HTTP 202 Accepted`
- **Rate Limit**: Default `30/minute` per client IP (returns `HTTP 429` if exceeded)

#### Example Request
```bash
curl -X POST "http://localhost:8000/api/files/" \
  -H "Accept: application/json" \
  -F "file=@box_60n.zip;type=application/zip"
```

#### Example Response (HTTP 202 Accepted)
```json
{
  "id": "e45f3c11-92b4-4b47-975a-c603a1d3550e",
  "filename": "box_60n.zip",
  "status": "PENDING",
  "message": "File uploaded successfully. Processing started in background."
}
```

---

### Endpoint 3: Get File Status
`GET /api/files/{id}/`

Queries current processing state, detected coordinate reference system, and feature count.
- **Status Code**: `HTTP 200 OK`
- **Caching**: When file status is `COMPLETED` or `FAILED`, the response is cached in memory, returning `X-Cache: HIT`, an `ETag` header, and `Cache-Control: public, max-age=300`. Supports conditional requests via `If-None-Match` returning `HTTP 304 Not Modified`.
- **Rate Limit**: Default `300/minute` per client IP.

#### Example Request
```bash
curl -X GET "http://localhost:8000/api/files/e45f3c11-92b4-4b47-975a-c603a1d3550e/" \
  -H "Accept: application/json"
```

#### Example Response (HTTP 200 OK)
```json
{
  "id": "e45f3c11-92b4-4b47-975a-c603a1d3550e",
  "filename": "box_60n.zip",
  "status": "COMPLETED",
  "source_crs": "EPSG:4326",
  "geometry_crs": "EPSG:4326",
  "feature_count": 1,
  "error": null,
  "created_at": "2026-10-10T16:35:10.123456Z",
  "updated_at": "2026-10-10T16:35:12.789012Z"
}
```

---

### Endpoint 4: Get File Measurements
`GET /api/files/{id}/measurements/`

Returns all extracted features with GeoJSON geometries (coordinates rounded to 9 decimal places), original properties (sanitized of null/NaN/NaT values), and computed metric measurements.

#### Example Request
```bash
curl -X GET "http://localhost:8000/api/files/e45f3c11-92b4-4b47-975a-c603a1d3550e/measurements/" \
  -H "Accept: application/json"
```

#### Example Response (HTTP 200 OK)
```json
{
  "file_id": "e45f3c11-92b4-4b47-975a-c603a1d3550e",
  "filename": "box_60n.zip",
  "status": "COMPLETED",
  "source_crs": "EPSG:4326",
  "geometry_crs": "EPSG:4326",
  "feature_count": 1,
  "error": null,
  "features": [
    {
      "id": "3bb87067-ea31-412f-90e8-091f06a19fef",
      "feature_index": 0,
      "geometry_type": "Polygon",
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [10.0, 60.0],
            [11.0, 60.0],
            [11.0, 61.0],
            [10.0, 61.0],
            [10.0, 60.0]
          ]
        ]
      },
      "properties": {
        "name": "box_60n",
        "owner": "survey_team"
      },
      "measurement": 6118946987.8411,
      "measurement_unit": "sq meters",
      "measurement_crs": "EPSG:32632",
      "note": null
    }
  ]
}
```

---

### HTTP Status Codes and Error Handling

| Code | Reason | Example Condition |
| :--- | :--- | :--- |
| `200 OK` | Request succeeded | Status or measurements query returned |
| `202 Accepted` | Asynchronous task enqueued | Upload accepted; background job queued |
| `304 Not Modified` | Cached content unchanged | Request header `If-None-Match` matches cached `ETag` |
| `400 Bad Request` | Invalid client input | Empty file (0 bytes), unsupported format, invalid zip archive |
| `404 Not Found` | Entity not found | File ID does not exist in `geospatial_files` |
| `429 Too Many Requests` | Rate limit window exceeded | More than 30 uploads/min or 300 queries/min. Includes `Retry-After: <seconds>` |
| `503 Service Unavailable` | DB connection pool timeout | Pool exhausted (`QueuePool limit reached`). Includes `Retry-After: 5` |

---

## 4. Architecture and Processing Pipeline

```
GeospatialFileMeasurement/
├── main.py                     # ASGI application setup, lifespan, CORS, and exception handlers
├── requirements.txt            # Pinned dependencies
├── .env.example                # Configuration template
├── src/
│   ├── logs/                   # Core application domain module
│   │   ├── models.py           # SQLAlchemy declarative models (GeospatialFile, Feature, FileStatus)
│   │   ├── router.py           # FastAPI endpoints with SlowAPI rate limiting & ETag caching
│   │   ├── controllers.py      # Background worker orchestration, 3-phase pipeline, DB controllers
│   │   └── data_transfer_objects.py # Pydantic response models and schemas
│   └── utils/                  # Shared infrastructural utilities
│       ├── settings.py         # Pydantic BaseSettings loading from .env
│       ├── db.py               # Async/sync database engines, session makers, transaction pooler config
│       ├── helpers.py          # GIS file parsing, UTM projection, measurement math, coordinate rounding
│       ├── limiter.py          # SlowAPI instance, rate limit callbacks, and 429 handler
│       └── cache.py            # Thread-safe TTLCache, strong ETag generation, If-None-Match evaluator
└── tests/                      # Verification and performance test suite
    ├── test_geospatial.py      # Accuracy, CRS handling, failure cases, and concurrency tests
    ├── test_rate_limit_and_caching.py # Rate limiter, ETag, 304, and cache isolation tests
    └── locustfile.py           # Headless/Web load testing suite with automated accuracy auditing
```

### End-to-End Processing Flow

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant Router as API Router
    participant Disk as Local Storage
    participant DB as PostgreSQL
    participant Worker as Background Task
    participant GIS as GIS Engine (Thread)

    Client->>Router: POST /api/files/ (Multipart)
    Router->>Router: Validate extension (.zip/.kml) & size > 0
    Router->>Disk: Stream upload to uploads/{uuid}_{filename}
    Router->>DB: Open short session -> INSERT GeospatialFile (status=PENDING)
    Router->>Worker: Dispatch process_geospatial_file_task via BackgroundTasks
    Router-->>Client: HTTP 202 Accepted (file_id, status=PENDING)

    Worker->>Worker: Acquire job_semaphore (Bounded concurrency: MAX_CONCURRENT_JOBS=2)
    Note over Worker,DB: Phase (a): Mark Processing
    Worker->>DB: Open session -> UPDATE GeospatialFile status=PROCESSING -> Commit & Close

    Note over Worker,GIS: Phase (b): Offloaded Parsing & Measurement (No DB connection held)
    Worker->>GIS: asyncio.to_thread(_cpu_bound_parse_and_measure)
    GIS->>Disk: Read KML or unzip Shapefile (find .shp & verify .prj)
    GIS->>GIS: Extract geometries & reproject to EPSG:4326 for GeoJSON output
    GIS->>GIS: Compute centroid per feature -> Select UTM EPSG -> Project -> Calculate Area/Length
    GIS->>GIS: Construct Feature database dictionaries with UUIDs
    GIS->>Disk: Delete temporary upload file from disk
    GIS-->>Worker: Return (feature_dicts, crs_str, error_msg, count)

    Note over Worker,DB: Phase (c): Bulk Persistence
    Worker->>DB: Open session -> Bulk INSERT Feature records in chunks of 2000
    Worker->>DB: UPDATE GeospatialFile status=COMPLETED (or FAILED) -> Commit & Close
    Worker->>Worker: Release job_semaphore
```

### Coordinate Reference System (CRS) Architecture
1. **Source CRS Identification (`source_crs`)**:
   - For Shapefiles, read directly from the accompanying `.prj` file. If missing, processing terminates with status `FAILED` (`"missing .prj (coordinate system unknown)"`).
   - For KML files, defaults to `EPSG:4326` (WGS84) per the OGC KML standard.
2. **Export CRS (`geometry_crs`)**:
   - Geometries returned via the API are standardized to `EPSG:4326` (WGS84 longitude/latitude) with coordinates rounded to 9 decimal places (~0.1 mm precision).
3. **Measurement CRS (`measurement_crs`)**:
   - Projected metric coordinate reference system chosen dynamically per feature based on its centroid:
     - **Latitudes between 80°S and 84°N**: Mapped to standard 6-degree UTM zones:
       $$\text{Zone} = \left\lfloor \frac{\text{longitude} + 180}{6} \right\rfloor + 1$$
       - Northern Hemisphere ($\text{lat} \ge 0$): $\text{EPSG} = 32600 + \text{Zone}$
       - Southern Hemisphere ($\text{lat} < 0$): $\text{EPSG} = 32700 + \text{Zone}$
     - **Polar Fallback**:
       - Latitudes $< -80^\circ$: `EPSG:3031` (Antarctic Polar Stereographic)
       - Latitudes $> +84^\circ$: `EPSG:3413` (NSIDC Sea Ice Polar Stereographic North)
   - Coordinate transformation uses `pyproj.Transformer` with `always_xy=True` to eliminate GIS axis-ordering ambiguities (ensuring input coordinates are consistently passed as `(longitude, latitude)` / `(easting, northing)`).
   - **Why Web Mercator (EPSG:3857) is Rejected**: Web Mercator is a conformal cylindrical projection designed for square tile web maps. Its areal scale factor is proportional to $1/\cos^2(\text{latitude})$. At latitude 60°N, Web Mercator inflates surface areas by a factor of $1/\cos^2(60^\circ) = 4.0\times$ (a 300% error). Localized UTM projections bound scale distortion to within $\pm 0.1\%$ to $0.2\%$.

---

## 5. Design Decisions and Trade-offs

### 1. Metric UTM Projection vs. Geodesic vs. Equal-Area
- **Chosen Approach**: Projected metric calculation using localized UTM zones chosen per feature centroid.
- **Trade-off**: UTM introduces a minor scale distortion bounded by $\approx 0.04\%$ at the central meridian up to $\approx 0.2\%$ at zone edges. Geodesic calculations (`pyproj.Geod` on the WGS84 ellipsoid) are mathematically exact for spherical/ellipsoidal area but require specialized geodesic algorithms. UTM was chosen because it produces standard planar geometries compatible with standard GIS engines and provides sub-0.2% accuracy across all benchmarks.

### 2. In-Process BackgroundTasks vs. Distributed Message Queue
- **Chosen Approach**: FastAPI `BackgroundTasks` bounded by an `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` and execution offloaded to worker threads via `asyncio.to_thread`.
- **Alternatives Considered**: Celery with Redis/RabbitMQ, Arq, or a database polling table using `SELECT FOR UPDATE SKIP LOCKED`.
- **Rationale**: For an evaluated single-service deployment, in-process tasks eliminate external message broker dependencies while the semaphore strictly prevents server CPU and connection pool saturation. In a multi-replica production environment, an external distributed queue (e.g., Celery or Arq) would be preferred.

### 3. Three-Phase Pipeline with Discrete Database Sessions
- **Problem**: Holding an open database session across file parsing and geometry calculations causes connection pool exhaustion under concurrent uploads.
- **Solution**: The background worker splits execution into three decoupled phases:
  1. *Phase (a)*: Acquire semaphore, open short session, mark `PROCESSING`, commit, and close.
  2. *Phase (b)*: No database session open. Parse archive, reproject, compute measurements, and build feature dictionaries in a background worker thread (`asyncio.to_thread`).
  3. *Phase (c)*: Open short session, bulk insert features in batches of 2,000, mark `COMPLETED`, commit, and close.

### 4. Connection Pooling and Transaction Poolers
- Even when connecting to a remote transaction pooler (such as Supabase Supavisor or PgBouncer in transaction mode on port 6543), an application-side pool (`POOL_SIZE=5, MAX_OVERFLOW=5`) is maintained to avoid local socket exhaustion and regulate outgoing TLS handshakes.
- Prepared statement caching is explicitly disabled (`statement_cache_size=0, prepared_statement_cache_size=0`) to ensure full compatibility with transaction pooling modes.

### 5. Caching and Rate Limiting Architecture
- **Terminal-State Caching Only**: Responses are cached in memory via `cachetools.TTLCache` exclusively when status is `COMPLETED` or `FAILED`. Active states (`PENDING`, `PROCESSING`) are never cached to guarantee callers observe live processing transitions.
- **Conditional HTTP Headers**: Cached responses emit strong `ETag` hashes and `Cache-Control: public, max-age=300`. Requests with matching `If-None-Match` headers receive `304 Not Modified`, saving serialization and network overhead.
- **Process Scope**: The current rate limiter and cache reside in application process memory. In a multi-node horizontal deployment, Redis would act as the shared cache and rate limiter store.

### 6. Strict Validation over Guessing
- **Missing `.prj`**: Marks file as `FAILED` rather than guessing a coordinate reference system. Guessing CRS (e.g., assuming WGS84 when data is projected in state plane meters) causes catastrophic measurement errors.
- **Zero Features / Empty Files**: Immediately rejected with `400 Bad Request` or marked `FAILED`.
- **Invalid / Unsupported Geometries**: Points and unsupported types (such as `GeometryCollection`) return `measurement: null` and descriptive `note` fields without crashing the pipeline.

---

## 6. Verification and Testing

### Running the Test Suite
Execute the entire test suite using `pytest`:
```bash
pytest tests/test_rate_limit_and_caching.py tests/test_geospatial.py -v
```

### Test Coverage Highlights
- **Geodesic Accuracy**: Verifies calculated areas and lengths against reference values derived from ellipsoidal geodesic calculations (`expected.json`).
- **CRS Transformations**: Validates source CRS preservation, UTM zone assignment, zone border crossings, and reprojection from projected systems (`EPSG:3857`, `EPSG:32632`).
- **Resilience and Error Handling**: Validates rejection of empty archives, corrupt zips, missing `.prj`, missing `.shx`, nested zip folders, and unexpected parser exceptions.
- **Rate Limiting & Caching**: Validates 429 enforcement, retry recovery after window expiration, cache bypass for active states, ETag generation, and 304 conditional responses.
- **Bounded Concurrency**: Fires 30 concurrent uploads with `POOL_SIZE=5, MAX_OVERFLOW=5` and verifies 0 HTTP 500 errors, eventual completion of all 30 jobs, and fast polling responses.

---

### Fixture Reference and Accuracy Benchmarks

All test fixtures are located in `Docs/test_fixtures/fixtures/`. Expected geodesic reference values are defined in `expected.json`.

| Fixture | Type | Expected Geodesic Value | API Measured Value | Error (%) | Tolerance | Result |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `shapefile/equator_box.zip` | Polygon | 12,308,778,361.47 m² | 12,322,539,175.58 m² | 0.1118% | < 0.20% | Passed |
| `shapefile/box_60n.zip` | Polygon | 6,122,943,163.07 m² | 6,118,946,987.84 m² | 0.0653% | < 0.20% | Passed |
| `shapefile/polygon_with_hole.zip` | Polygon | 118,167,064.40 m² | 118,388,225.14 m² | 0.1872% | < 0.20% | Passed |
| `shapefile/multipolygon.zip` | MultiPolygon | 246,180,288.11 m² | 246,597,890.33 m² | 0.1696% | < 0.20% | Passed |
| `shapefile/lines.zip` (feat 0) | LineString | 111,319.49 m | 111,383.12 m | 0.0572% | < 0.20% | Passed |
| `shapefile/lines.zip` (feat 1) | LineString | 55,799.47 m | 55,782.11 m | 0.0311% | < 0.20% | Passed |
| `shapefile/points.zip` (0, 1, 2) | Point | None | None | 0.0000% | Exact Null | Passed |
| `shapefile/utm_projected_rect.zip` | Polygon | 2,000,000.00 m² | 2,000,000.00 m² | 0.0000% | < 0.20% | Passed |
| `shapefile/webmercator_box_60n.zip` | Polygon | 6,122,943,163.07 m² | 6,118,946,987.84 m² | 0.0653% | < 0.20% | Passed |
| `shapefile/nested_folder.zip` | Polygon | 12,308,778,361.47 m² | 12,322,539,175.58 m² | 0.1118% | < 0.20% | Passed |
| `kml/equator_box.kml` | Polygon | 12,308,778,361.47 m² | 12,322,539,175.58 m² | 0.1118% | < 0.20% | Passed |
| `kml/box_60n.kml` | Polygon | 6,122,943,163.07 m² | 6,118,946,987.84 m² | 0.0653% | < 0.20% | Passed |
| `kml/polygon_with_hole.kml` | Polygon | 118,167,064.40 m² | 118,388,225.14 m² | 0.1872% | < 0.20% | Passed |
| `kml/mixed_geometries.kml` (Point) | Point | None | None | 0.0000% | Exact Null | Passed |
| `kml/mixed_geometries.kml` (Line) | LineString | 111,319.49 m | 111,383.12 m | 0.0572% | < 0.20% | Passed |
| `kml/mixed_geometries.kml` (Poly) | Polygon | 12,308,778,361.47 m² | 12,322,539,175.58 m² | 0.1118% | < 0.20% | Passed |
| `kml/utm_zone_crossing.kml` | Polygon | 159,328,793.71 m² | 159,381,879.86 m² | 0.0333% | < 0.20% | Passed |
| `kml/line_60n.kml` | LineString | 55,799.47 m | 55,782.11 m | 0.0311% | < 0.20% | Passed |
| `load/small_10.zip` (10 feats) | Total Area | 2,607,852.05 m² | 2,608,557.79 m² | 0.0271% | < 0.20% | Passed |
| `load/medium_1000.zip` (1k feats) | Total Area | 314,756,859.82 m² | 314,726,224.48 m² | 0.0097% | < 0.20% | Passed |
| `load/large_20000.zip` (20k feats) | Total Area | 6,149,712,001.51 m² | 6,149,000,682.32 m² | 0.0116% | < 0.20% | Passed |

---

## 7. Performance and Load Testing

### Running Locust Load Tests
To evaluate throughput and latency under load, run Locust in headless mode with rate limiting disabled:
```bash
# Ensure rate limiter is disabled for load test benchmarking
locust -f tests/locustfile.py --host http://localhost:8000 --headless -u 25 -r 5 --run-time 150s --csv locust_report
```

### Benchmark Environment & Constraints
- **Test Topology**: Single Windows machine running both the FastAPI ASGI server and Locust load generator.
- **Database**: Remote PostgreSQL instance on Supabase (AWS ap-south-1 Mumbai region) connected via SSL/TLS over the public internet.
- **Round-Trip Baseline**: Minimum base round-trip latency to the remote database is $\approx 100\text{--}140\text{ ms}$. Throughput is governed by network round-trips to the remote database rather than local CPU limits.

### 25-User Load Test Results

Summary of performance test with **25 concurrent users**, **spawn rate 5 users/sec**, over **2.5 minutes (150 seconds)**:

| Endpoint | Method | Requests | Failures | Req/s | Median Latency | P90 Latency | P95 Latency | P99 Latency |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `/api/files/` (Upload) | POST | 412 | 0 (0.00%) | 2.7 | 510 ms | 620 ms | 710 ms | 2,500 ms |
| `/api/files/[id]/` (Polling) | GET | 4,725 | 0 (0.00%) | 31.5 | 230 ms | 280 ms | 360 ms | 460 ms |
| `/api/files/[id]/measurements/` | GET | 335 | 0 (0.00%) | 2.2 | 330 ms | 410 ms | 440 ms | 520 ms |
| **Aggregated Total** | **All** | **5,472** | **0 (0.00%)** | **36.5** | **240 ms** | **310 ms** | **480 ms** | **2,400 ms** |

*All fixture accuracy checks executed during the Locust test verified against `expected.json` passed with 0 validation errors.*

### Time-to-COMPLETED Benchmarks by File Size

Measured end-to-end processing times from initial upload (`HTTP 202`) until status reached `COMPLETED`:

| File Name | Format | Feature Count | File Size on Disk | Time to Complete |
| :--- | :---: | :---: | :---: | :---: |
| `small_10.zip` | Shapefile | 10 | 1.3 KB | **3.71 seconds** |
| `medium_1000.zip` | Shapefile | 1,000 | 52.4 KB | **5.96 seconds** |
| `large_20000.zip` | Shapefile | 20,000 | 1,030.5 KB | **13.30 seconds** |

### Cache Hit vs. Miss Read Latency

| Request Scenario | Cache State | Response Header | Measured Latency | Speedup |
| :--- | :---: | :---: | :---: | :---: |
| First GET query for completed file | Cold Cache | `X-Cache: MISS` | 820 – 1,150 ms | 1.0x (DB baseline) |
| Subsequent GET query for completed file | Hot TTLCache | `X-Cache: HIT` | **2.5 – 3.2 ms** | **~300x faster** |
| Conditional GET with `If-None-Match` | ETag Match | `HTTP 304 Not Modified` | **2.0 – 2.8 ms** | Zero body transfer |
| Read-heavy cached Locust run (X-Cache HIT) | TODO | TODO: Measure pure cached Locust run | TODO | TODO |

---

### Root Cause Analysis: The Connection Pool Exhaustion Incident

During early iterations of load testing with 25 concurrent users, the application experienced a severe failure cascade:
- **Failure Symptom**: Over 70% of requests failed with `HTTP 500` or `sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 5 reached, connection timed out, timeout 10.00`. Latencies degraded to 10–40 seconds.
- **Root Cause**:
  1. The background task (`process_geospatial_file_task`) checked out a database session at the start of execution and held it open continuously across archive extraction, file parsing, coordinate reprojection, and geometry measurement.
  2. With unlimited concurrent background tasks dispatched by Starlette's `BackgroundTasks`, 25 concurrent uploads immediately created 25 concurrent jobs that seized every pooled connection (5 steady-state + 5 overflow = 10 total).
  3. Incoming HTTP requests attempting to execute `GET /api/files/{id}/` were queued behind long-running background tasks. When the 10-second pool checkout timeout expired, SQLAlchemy raised `TimeoutError`, resulting in HTTP 500 errors.
- **Remediation**:
  1. **Bounded Job Concurrency**: Introduced a module-level `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` (default: 2) acquired before opening any database session and released in a `finally` block.
  2. **Three Discrete Sessions**: Restructured the task into three separate, short-lived database sessions. Phase (b) runs completely outside any database session.
  3. **Event Loop Offloading**: Offloaded CPU-bound parsing and GeoJSON feature formatting to a worker thread via `await asyncio.to_thread(...)`, and added cooperative `await asyncio.sleep(0)` yields between 2,000-row bulk insert batches.
  4. **Timeout Handler**: Added an explicit exception handler mapping `sqlalchemy.exc.TimeoutError` to `HTTP 503 Service Unavailable` with a `Retry-After: 5` header.
- **Outcome**: Failures dropped from **~70% to 0.00%**, and median response time dropped from **30+ seconds to 240 ms**.

---

## 8. Engineering Learnings

1. **Web Mercator (EPSG:3857) Area Distortion**:
   While Web Mercator is widely used for web tile visualization, calculating geometric areas directly in EPSG:3857 results in massive distortion ($4\times$ inflation at 60°N). Known-answer tests against geodesic reference values (`expected.json`) immediately caught this failure mode and motivated the localized UTM architecture.
2. **Database Connection Lifecycle in Background Tasks**:
   FastAPI `BackgroundTasks` run in the same event loop after the HTTP response has been sent. Holding database sessions open during non-database operations (CPU parsing, file I/O) quickly starves the connection pool under load. Background tasks should only open sessions for the brief moments they read or write data.
3. **App-Side Pooling with Transaction Poolers**:
   Connecting to an external transaction pooler (such as PgBouncer or Supavisor) does not eliminate the need for an application-side pool. The application pool regulates local TCP connections and SSL handshakes, while the external pooler multiplexes transactions against backend PostgreSQL connections.
4. **Archive Handling Edge Cases**:
   - Shapefiles inside zip archives frequently reside inside nested subdirectories; parsing must search recursively (`rglob("*.shp")`).
   - Missing or corrupt `.shx` index files cause GDAL/Fiona to read 0 features rather than raising an immediate error; this must be explicitly validated.
   - Missing `.prj` files must result in a clear `FAILED` status rather than guessing a coordinate reference system.

---

## 9. Future Improvements

- **Distributed Task Queue**: Migrate in-process background tasks to Celery or Arq with Redis/RabbitMQ to decouple worker processes from the API web tier and enable independent autoscaling.
- **Distributed Cache & Rate Limiter**: Replace process-local `cachetools` and SlowAPI storage with a shared Redis cluster for consistent rate limiting and caching across multi-replica deployments.
- **Geodesic Calculation Engine**: Add an optional geodesic calculation mode using `pyproj.Geod` on the WGS84 ellipsoid for applications requiring zero projection scale distortion.
- **Pagination & Streaming**: Implement cursor-based pagination and streaming JSON responses for `GET /api/files/{id}/measurements/` on datasets exceeding 50,000 features.
- **Authentication & Multi-Tenancy**: Introduce OAuth2 / JWT authentication with tenant-isolated row-level access control.
- **Containerization & CI/CD**: Provide production Dockerfiles, Docker Compose service definitions, and automated GitHub Actions testing workflows.
- **Observability**: Integrate Prometheus metrics for pool saturation, job duration histograms, and OpenTelemetry distributed tracing.
- **Expanded GIS Format Support**: Support additional geospatial formats including GeoJSON, FlatGeoBuf, and GeoPackage (`.gpkg`).
