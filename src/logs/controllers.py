import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, UploadFile, status
from sqlalchemy import insert
from sqlalchemy.orm import Session

from src.utils.db import get_db_context
from src.utils.helpers import (
    ERROR_NO_FEATURES,
    ERROR_UNEXPECTED,
    GeospatialProcessingError,
    extract_features_and_measurements,
    read_geospatial_file,
)
from src.logs.data_transfer_objects import (
    FeatureItemResponse,
    FileMeasurementsResponse,
    FileStatusResponse,
    FileUploadResponse,
)
from src.logs.models import Feature, FileStatus, GeospatialFile
from src.utils.settings import settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".zip", ".kml"}

# Bounded worker thread pool for CPU-bound background processing
_WORKER_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def get_worker_executor() -> ThreadPoolExecutor:
    """Returns singleton bounded ThreadPoolExecutor for background file processing."""
    global _WORKER_EXECUTOR
    if _WORKER_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _WORKER_EXECUTOR is None:
                _WORKER_EXECUTOR = ThreadPoolExecutor(
                    max_workers=settings.MAX_CONCURRENT_WORKERS,
                    thread_name_prefix="geo_worker",
                )
    return _WORKER_EXECUTOR


def _cleanup_temp_file(path_str: str) -> None:
    """Safely removes temporary upload file if it exists."""
    if os.path.exists(path_str):
        try:
            os.remove(path_str)
        except OSError as err:
            logger.warning(f"Failed to delete temp file {path_str}: {err}")


def handle_file_upload(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    db: Session,
) -> FileUploadResponse:
    """Validates uploaded file, stores temp copy, persists initial PENDING record, and queues processing."""
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid filename is required.",
        )

    extension = Path(file.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format '{extension}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    # Reject zero-byte uploads at upload time
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    if (file.size is not None and file.size == 0) or file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty (0 bytes).",
        )

    # Prepare upload directory and save uploaded file
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    temp_path = upload_dir / f"{uuid.uuid4()}_{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        if temp_path.stat().st_size == 0:
            temp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty (0 bytes).",
            )
    except HTTPException:
        raise
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {exc}",
        ) from exc

    # Persist PENDING file tracking record
    db_file = GeospatialFile(
        filename=file.filename,
        status=FileStatus.PENDING,
        feature_count=0,
    )
    db.add(db_file)
    db.commit()
    db.refresh(db_file)

    # Queue processing in bounded worker pool (extra jobs remain PENDING)
    executor = get_worker_executor()
    executor.submit(
        process_geospatial_file_task,
        file_id=db_file.id,
        temp_file_path=str(temp_path),
        original_filename=file.filename,
    )

    return FileUploadResponse(
        id=db_file.id,
        filename=db_file.filename,
        status=db_file.status,
    )


def process_geospatial_file_task(
    file_id: UUID,
    temp_file_path: str,
    original_filename: str,
) -> None:
    """
    Three-phase background processing pipeline:
      Phase (a): Short DB session - mark PROCESSING and verify job existence.
      Phase (b): NO DB session - perform CPU-bound file reading, projecting, and measuring.
      Phase (c): Short DB session - chunked bulk insert all features in one transaction and mark COMPLETED/FAILED.
    """
    logger.info(f"Processing started for file {file_id} ({original_filename})")

    # --------------------------------------------------------------------------
    # Phase (a): Short session - mark status as PROCESSING
    # --------------------------------------------------------------------------
    file_exists = False
    with get_db_context() as db:
        db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
        if db_file:
            db_file.status = FileStatus.PROCESSING
            db.commit()
            file_exists = True
        else:
            logger.error(f"File ID {file_id} not found in database.")

    if not file_exists:
        _cleanup_temp_file(temp_file_path)
        return

    # --------------------------------------------------------------------------
    # Phase (b): NO session - parse, reproject, and measure (CPU-bound)
    # --------------------------------------------------------------------------
    records = None
    crs_str = None
    error_msg = None

    try:
        gdf, crs_str = read_geospatial_file(temp_file_path, original_filename)
        is_kml = Path(original_filename).suffix.lower() == ".kml"
        records = extract_features_and_measurements(
            gdf=gdf,
            is_kml=is_kml,
        )
        if not records:
            error_msg = ERROR_NO_FEATURES

    except GeospatialProcessingError as proc_err:
        logger.warning(f"Processing error for file {file_id} ({original_filename}): {proc_err.message}")
        error_msg = proc_err.message

    except Exception as proc_exc:
        logger.exception(f"Unexpected processing error for file {file_id}: {proc_exc}")
        error_msg = ERROR_UNEXPECTED

    finally:
        _cleanup_temp_file(temp_file_path)

    # --------------------------------------------------------------------------
    # Phase (c): Short session - chunked bulk insert & mark terminal status
    # --------------------------------------------------------------------------
    with get_db_context() as db:
        db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
        if not db_file:
            logger.error(f"File ID {file_id} disappeared from database.")
            return

        if error_msg is not None:
            db_file.status = FileStatus.FAILED
            db_file.error_message = error_msg
            db_file.error = error_msg
            db.commit()
            return

        # Prepare records for chunked bulk insertion
        feature_dicts = [
            {
                "id": uuid.uuid4(),
                "file_id": file_id,
                "feature_index": rec["feature_index"],
                "geometry_type": rec["geometry_type"],
                "geometry": rec["geometry"],
                "properties": rec["properties"],
                "measurement": rec["measurement"],
                "measurement_crs": rec.get("measurement_crs"),
                "note": rec.get("note"),
            }
            for rec in records
        ]

        # Bulk insert in chunks of 2,000 rows per statement in one single transaction
        CHUNK_SIZE = 2000
        for i in range(0, len(feature_dicts), CHUNK_SIZE):
            chunk = feature_dicts[i : i + CHUNK_SIZE]
            db.execute(insert(Feature), chunk)

        db_file.crs = crs_str
        db_file.feature_count = len(records)
        db_file.status = FileStatus.COMPLETED
        db_file.error_message = None
        db_file.error = None
        db.commit()
        logger.info(f"Successfully saved {len(records)} features for file {file_id}.")


def get_file_status_controller(file_id: UUID, db: Session) -> FileStatusResponse:
    """Queries and returns file status metadata using an immediately-released session."""
    try:
        db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
        if not db_file:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Geospatial file '{file_id}' not found.",
            )
        return FileStatusResponse(
            id=db_file.id,
            filename=db_file.filename,
            status=db_file.status,
            source_crs=db_file.crs,
            geometry_crs="EPSG:4326",
            feature_count=db_file.feature_count,
            error=db_file.error or db_file.error_message,
            created_at=db_file.created_at,
            updated_at=db_file.updated_at,
        )
    finally:
        # Immediately release DB connection back to the pool
        db.close()


def get_file_measurements_controller(file_id: UUID, db: Session) -> FileMeasurementsResponse:
    """Queries file features and assembles the measurement response DTO, releasing session immediately."""
    try:
        db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
        if not db_file:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Geospatial file '{file_id}' not found.",
            )

        file_id_val = db_file.id
        filename_val = db_file.filename
        status_val = db_file.status
        source_crs_val = db_file.crs
        feature_count_val = db_file.feature_count
        error_val = db_file.error or db_file.error_message

        features = (
            db.query(Feature)
            .filter(Feature.file_id == file_id)
            .order_by(Feature.feature_index.asc())
            .all()
        )
    finally:
        # Release DB connection back to the pool before building Pydantic response items
        db.close()

    unit_mapping = {
        "Polygon": "sq meters",
        "MultiPolygon": "sq meters",
        "LineString": "meters",
        "MultiLineString": "meters",
    }

    feature_items = [
        FeatureItemResponse(
            id=f.id,
            feature_index=f.feature_index,
            geometry_type=f.geometry_type,
            geometry=f.geometry,
            properties=f.properties or {},
            measurement=f.measurement,
            measurement_unit=unit_mapping.get(f.geometry_type) if f.measurement is not None else None,
            measurement_crs=f.measurement_crs,
            note=f.note,
        )
        for f in features
    ]

    return FileMeasurementsResponse(
        file_id=file_id_val,
        filename=filename_val,
        status=status_val,
        source_crs=source_crs_val,
        geometry_crs="EPSG:4326",
        feature_count=feature_count_val,
        error=error_val,
        features=feature_items,
    )
