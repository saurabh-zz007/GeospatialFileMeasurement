import asyncio
import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, UploadFile, status
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.utils.db import AsyncSessionLocal, get_async_db_context
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

# Bounded concurrency semaphore for background processing jobs
job_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_JOBS)

# Bounded worker thread pool for CPU-bound background processing
_WORKER_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def get_worker_executor() -> ThreadPoolExecutor:
    """Returns singleton bounded ThreadPoolExecutor for background CPU-bound processing."""
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


async def handle_file_upload(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    db: Optional[AsyncSession] = None,
) -> FileUploadResponse:
    """
    Validates uploaded file, stores temp copy asynchronously,
    persists initial PENDING record, and queues processing via BackgroundTasks.
    """
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

    # Prepare upload directory and save uploaded file asynchronously
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    temp_path = upload_dir / f"{uuid.uuid4()}_{file.filename}"

    def _save_file_to_disk(src_stream, dest: Path) -> None:
        with open(dest, "wb") as buffer:
            shutil.copyfileobj(src_stream, buffer)

    try:
        await asyncio.to_thread(_save_file_to_disk, file.file, temp_path)
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

    # Persist PENDING file tracking record using dedicated short session so no connection is held
    async with AsyncSessionLocal() as session:
        db_file = GeospatialFile(
            filename=file.filename,
            status=FileStatus.PENDING,
            feature_count=0,
        )
        session.add(db_file)
        await session.commit()
        await session.refresh(db_file)
        file_id = db_file.id
        file_name = db_file.filename
        file_status = db_file.status


    # Dispatch main background processing task
    background_tasks.add_task(
        process_geospatial_file_task,
        file_id=file_id,
        temp_file_path=str(temp_path),
        original_filename=file_name,
    )

    return FileUploadResponse(
        id=file_id,
        filename=file_name,
        status=file_status,
    )


def _cpu_bound_parse_and_measure(
    path: str,
    filename: str,
    file_id: UUID,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str], Optional[str], int]:
    """Synchronous CPU-bound geospatial parsing, measurement, and record formatting."""
    try:
        gdf, crs_str = read_geospatial_file(path, filename)
        is_kml = Path(filename).suffix.lower() == ".kml"
        records = extract_features_and_measurements(
            gdf=gdf,
            is_kml=is_kml,
        )
        if not records:
            return None, crs_str, ERROR_NO_FEATURES, 0

        # Construct database feature dictionaries in thread to keep event loop free
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
        return feature_dicts, crs_str, None, len(records)
    except GeospatialProcessingError as proc_err:
        logger.warning(f"Processing error for file ({filename}): {proc_err.message}")
        return None, None, proc_err.message, 0
    except Exception as proc_exc:
        logger.exception(f"Unexpected processing error for file ({filename}): {proc_exc}")
        return None, None, ERROR_UNEXPECTED, 0
    finally:
        _cleanup_temp_file(path)


async def process_geospatial_file_task(
    file_id: UUID,
    temp_file_path: str,
    original_filename: str,
) -> None:
    """
    Three-phase background processing pipeline:
      Phase (a): Short async DB session via AsyncSessionLocal() - mark PROCESSING and verify job existence.
      Phase (b): NO session open - offloaded CPU/IO-bound file reading, projecting, measuring, and
                 formatting via asyncio.to_thread.
      Phase (c): Short async DB session via AsyncSessionLocal() - chunked bulk insert features (2000 per chunk)
                 and mark COMPLETED or FAILED.
    """
    await job_semaphore.acquire()
    try:
        logger.info(f"Processing started for file {file_id} ({original_filename})")

        # --------------------------------------------------------------------------
        # Phase (a) - Top Bread: Short async session - mark status as PROCESSING
        # --------------------------------------------------------------------------
        file_exists = False
        async with AsyncSessionLocal() as db:
            stmt = select(GeospatialFile).where(GeospatialFile.id == file_id)
            result = await db.execute(stmt)
            db_file = result.scalars().first()
            if db_file:
                db_file.status = FileStatus.PROCESSING
                await db.commit()
                file_exists = True
            else:
                logger.error(f"File ID {file_id} not found in database.")

        if not file_exists:
            _cleanup_temp_file(temp_file_path)
            return

        # --------------------------------------------------------------------------
        # Phase (b) - Filling: Offloaded CPU-bound parsing & calculations (NO DB session held)
        # --------------------------------------------------------------------------
        feature_dicts, crs_str, error_msg, total_count = await asyncio.to_thread(
            _cpu_bound_parse_and_measure,
            temp_file_path,
            original_filename,
            file_id,
        )

        # --------------------------------------------------------------------------
        # Phase (c) - Bottom Bread: Short async session - chunked bulk insert & mark terminal status
        # --------------------------------------------------------------------------
        async with AsyncSessionLocal() as db:
            stmt = select(GeospatialFile).where(GeospatialFile.id == file_id)
            result = await db.execute(stmt)
            db_file = result.scalars().first()
            if not db_file:
                logger.error(f"File ID {file_id} disappeared from database.")
                return

            if error_msg is not None:
                db_file.status = FileStatus.FAILED
                db_file.error_message = error_msg
                db_file.error = error_msg
                await db.commit()
                return

            # Bulk insert in chunks of 2,000 rows per statement in one single transaction
            CHUNK_SIZE = 2000
            for i in range(0, len(feature_dicts), CHUNK_SIZE):
                chunk = feature_dicts[i : i + CHUNK_SIZE]
                await db.execute(insert(Feature), chunk)
                await asyncio.sleep(0)

            db_file.crs = crs_str
            db_file.feature_count = total_count
            db_file.status = FileStatus.COMPLETED
            db_file.error_message = None
            db_file.error = None
            await db.commit()
            logger.info(f"Successfully saved {total_count} features for file {file_id}.")
    except Exception as unhandled_err:
        logger.exception(f"Unhandled error in background task for file {file_id}: {unhandled_err}")
        try:
            async with AsyncSessionLocal() as db:
                stmt = select(GeospatialFile).where(GeospatialFile.id == file_id)
                res = await db.execute(stmt)
                db_file = res.scalars().first()
                if db_file:
                    db_file.status = FileStatus.FAILED
                    db_file.error_message = str(unhandled_err)
                    db_file.error = str(unhandled_err)
                    await db.commit()
        except Exception as mark_err:
            logger.error(f"Failed to record FAILED status for file {file_id}: {mark_err}")
    finally:
        job_semaphore.release()


async def get_file_status_controller(file_id: UUID, db: AsyncSession) -> FileStatusResponse:
    """Queries and returns file status metadata asynchronously."""
    stmt = select(GeospatialFile).where(GeospatialFile.id == file_id)
    result = await db.execute(stmt)
    db_file = result.scalars().first()
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


async def get_file_measurements_controller(file_id: UUID, db: AsyncSession) -> FileMeasurementsResponse:
    """Queries file features and assembles the measurement response DTO asynchronously."""
    stmt = select(GeospatialFile).where(GeospatialFile.id == file_id)
    result = await db.execute(stmt)
    db_file = result.scalars().first()
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

    stmt_features = (
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index.asc())
    )
    res_features = await db.execute(stmt_features)
    features = res_features.scalars().all()

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
