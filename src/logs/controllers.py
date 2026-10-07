import logging
import os
import shutil
import uuid
from pathlib import Path
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

try:
    from src.db import get_db_context
    from src.helpers import extract_features_and_measurements, read_geospatial_file
    from src.logs.data_transfer_objects import (
        FeatureItemResponse,
        FileMeasurementsResponse,
        FileStatusResponse,
        FileUploadResponse,
    )
    from src.logs.models import Feature, FileStatus, GeospatialFile
    from src.settings import settings
except ImportError:
    from db import get_db_context
    from helpers import extract_features_and_measurements, read_geospatial_file
    from logs.data_transfer_objects import (
        FeatureItemResponse,
        FileMeasurementsResponse,
        FileStatusResponse,
        FileUploadResponse,
    )
    from logs.models import Feature, FileStatus, GeospatialFile
    from settings import settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".zip", ".kml"}


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

    # Prepare upload directory and save uploaded file
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    temp_path = upload_dir / f"{uuid.uuid4()}_{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
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

    # Queue background processing task
    background_tasks.add_task(
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
    """Background pipeline executing file parsing, metric reprojection, and feature persistence."""
    logger.info(f"Processing started for file {file_id} ({original_filename})")

    try:
        with get_db_context() as db:
            db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
            if not db_file:
                logger.error(f"File ID {file_id} not found in database.")
                return

            db_file.status = FileStatus.PROCESSING
            db.commit()

            try:
                gdf, crs_str = read_geospatial_file(temp_file_path, original_filename)
                db_file.crs = crs_str

                records = extract_features_and_measurements(
                    gdf=gdf,
                    target_projected_crs=settings.TARGET_PROJECTED_CRS,
                )

                feature_entities = [
                    Feature(
                        file_id=file_id,
                        feature_index=rec["feature_index"],
                        geometry_type=rec["geometry_type"],
                        geometry=rec["geometry"],
                        properties=rec["properties"],
                        measurement=rec["measurement"],
                    )
                    for rec in records
                ]

                db.bulk_save_objects(feature_entities)

                db_file.feature_count = len(records)
                db_file.status = FileStatus.COMPLETED
                db_file.error_message = None
                db.commit()
                logger.info(f"Successfully processed {len(records)} features for file {file_id}.")

            except Exception as proc_exc:
                logger.exception(f"Processing failed for file {file_id}: {proc_exc}")
                db.rollback()
                db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
                if db_file:
                    db_file.status = FileStatus.FAILED
                    db_file.error_message = str(proc_exc)
                    db.commit()

    finally:
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError as err:
                logger.warning(f"Failed to delete temp file {temp_file_path}: {err}")


def get_file_status_controller(file_id: UUID, db: Session) -> FileStatusResponse:
    """Queries and returns file status metadata."""
    db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
    if not db_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geospatial file '{file_id}' not found.",
        )
    return FileStatusResponse.model_validate(db_file)


def get_file_measurements_controller(file_id: UUID, db: Session) -> FileMeasurementsResponse:
    """Queries file features and assembles the measurement response DTO."""
    db_file = db.query(GeospatialFile).filter(GeospatialFile.id == file_id).first()
    if not db_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geospatial file '{file_id}' not found.",
        )

    features = (
        db.query(Feature)
        .filter(Feature.file_id == file_id)
        .order_by(Feature.feature_index.asc())
        .all()
    )

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
            measurement_unit=unit_mapping.get(f.geometry_type),
        )
        for f in features
    ]

    return FileMeasurementsResponse(
        file_id=db_file.id,
        filename=db_file.filename,
        status=db_file.status,
        crs=db_file.crs,
        feature_count=db_file.feature_count,
        features=feature_items,
    )
