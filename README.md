# Geospatial File Measurement API

A production-ready FastAPI service designed to ingest geospatial files (`.zip` Shapefiles and `.kml` files), reproject geographic coordinate systems (e.g., WGS84 `EPSG:4326`) into a metric projected coordinate system (`EPSG:3857`), and compute spatial measurements (area for polygons, length for linestrings) asynchronously in the background.

---

## 🏗️ Architecture & Features

- **Framework**: FastAPI with asynchronous endpoints and background worker tasks (`BackgroundTasks`).
- **Database**: PostgreSQL with SQLAlchemy ORM using UUID primary keys and native JSONB columns for geometries and properties.
- **GIS Stack**:
  - `geopandas` for geospatial data manipulation.
  - `fiona` with KML & LIBKML driver support.
  - `shapely` for vector geometry analysis and metric calculations.
  - `pyproj` for coordinate reference system (CRS) transformations.
- **Robust Processing**:
  - Automatically identifies whether geometries are geographic (degrees) and reprojects to `EPSG:3857` (meters) prior to computing measurements.
  - Calculates area in square meters for `Polygon` and `MultiPolygon`.
  - Calculates length in meters for `LineString` and `MultiLineString`.
  - Leaves measurements null for `Point` and `MultiPoint`.
  - Gracefully handles and skips invalid/unsupported geometries without crashing the upload job.
  - Cleans up uploaded temporary files upon task completion or failure.

---

## 📁 Project Structure

```plaintext
GeospatialFileMeasurement/
├── src/
│   ├── logs/
│   │   ├── controllers.py
│   │   ├── data_transfer_objects.py
│   │   ├── models.py
│   │   └── router.py
│   ├── db.py
│   ├── helpers.py
│   ├── settings.py
│   └── utils/
├── .gitignore
├── main.py
├── README.md
└── requirements.txt
```

---

## 🗄️ Database Schema

### 1. `GeospatialFile` (Table: `geospatial_files`)
| Column | Type | Description |
|---|---|---|
| `id` | `UUID` (PK) | Unique job identifier |
| `filename` | `VARCHAR(255)` | Original uploaded filename |
| `status` | `ENUM` | `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED` |
| `crs` | `VARCHAR(100)` | Detected coordinate reference system (e.g., `EPSG:4326`) |
| `feature_count` | `INTEGER` | Total valid features extracted and stored |
| `error_message` | `TEXT` | Failure details if processing fails |
| `created_at` | `TIMESTAMPTZ` | Timestamp when file was submitted |
| `updated_at` | `TIMESTAMPTZ` | Timestamp of last status update |

### 2. `Feature` (Table: `features`)
| Column | Type | Description |
|---|---|---|
| `id` | `UUID` (PK) | Feature primary key |
| `file_id` | `UUID` (FK) | Reference to parent `geospatial_files.id` (Cascading delete) |
| `feature_index` | `INTEGER` | Original row index from GeoDataFrame |
| `geometry_type` | `VARCHAR(50)` | Geometry type (`Polygon`, `LineString`, `Point`, etc.) |
| `geometry` | `JSONB` | GeoJSON representation in WGS84 coordinates |
| `properties` | `JSONB` | Non-geometry attribute attributes sanitized to JSON |
| `measurement` | `FLOAT` | Computed area ($m^2$) or length ($m$); `null` for points |

---

## 🚀 Quickstart & Setup

### 1. Prerequisites
- Python 3.10+
- PostgreSQL instance running with database created:
  ```sql
  CREATE DATABASE geospatial_db;
  ```

### 2. Install Dependencies
Create a virtual environment and install the required dependencies:
```bash
python -m venv .venv
# Activate on Windows:
.venv\Scripts\activate
# Activate on Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure Environment
Create a `.env` file in the project root:
```env
DATABASE_URL=postgresql://postgres:yourpassword@localhost:5432/geospatial_db
TARGET_PROJECTED_CRS=EPSG:3857
LOG_LEVEL=INFO
```

### 4. Run the Application
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
Interactive API docs are available at:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 📡 API Endpoints

### 1. Upload Geospatial File
- **Method**: `POST`
- **Path**: `/api/files/`
- **Request**: Multipart form-data with field `file` containing `.zip` or `.kml`.
- **Response**: `202 Accepted`
```json
{
  "id": "c1f7b823-1d02-4f36-8c44-31518fdf9921",
  "filename": "parcels.zip",
  "status": "PENDING",
  "message": "File uploaded successfully. Processing started in background."
}
```

#### Example cURL:
```bash
curl -X POST "http://localhost:8000/api/files/" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "file=@sample_shapes.zip"
```

---

### 2. Inspect Processing Status
- **Method**: `GET`
- **Path**: `/api/files/{id}/`
- **Response**: `200 OK`
```json
{
  "id": "c1f7b823-1d02-4f36-8c44-31518fdf9921",
  "filename": "parcels.zip",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "feature_count": 42,
  "error_message": null,
  "created_at": "2026-10-07T09:00:00Z",
  "updated_at": "2026-10-07T09:00:03Z"
}
```

#### Example cURL:
```bash
curl -X GET "http://localhost:8000/api/files/c1f7b823-1d02-4f36-8c44-31518fdf9921/"
```

---

### 3. Retrieve Extracted Features & Measurements
- **Method**: `GET`
- **Path**: `/api/files/{id}/measurements/`
- **Response**: `200 OK`
```json
{
  "file_id": "c1f7b823-1d02-4f36-8c44-31518fdf9921",
  "filename": "parcels.zip",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "feature_count": 42,
  "features": [
    {
      "id": "49f50682-1279-4d69-b3a1-e377f0ff82a1",
      "feature_index": 0,
      "geometry_type": "Polygon",
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [-122.4194, 37.7749],
            [-122.4194, 37.7759],
            [-122.4184, 37.7759],
            [-122.4184, 37.7749],
            [-122.4194, 37.7749]
          ]
        ]
      },
      "properties": {
        "parcel_id": "10023",
        "owner": "City Parks"
      },
      "measurement": 9621.432,
      "measurement_unit": "sq meters"
    }
  ]
}
```

#### Example cURL:
```bash
curl -X GET "http://localhost:8000/api/files/c1f7b823-1d02-4f36-8c44-31518fdf9921/measurements/"
```
