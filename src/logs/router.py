from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

try:
    from src.db import get_db
    from src.logs.controllers import (
        get_file_measurements_controller,
        get_file_status_controller,
        handle_file_upload,
    )
    from src.logs.data_transfer_objects import (
        FileMeasurementsResponse,
        FileStatusResponse,
        FileUploadResponse,
    )
except ImportError:
    from db import get_db
    from logs.controllers import (
        get_file_measurements_controller,
        get_file_status_controller,
        handle_file_upload,
    )
    from logs.data_transfer_objects import (
        FileMeasurementsResponse,
        FileStatusResponse,
        FileUploadResponse,
    )

router = APIRouter(prefix="/api/files", tags=["Geospatial Files"])


@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload geospatial file (.zip Shapefile or .kml)",
)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Zipped Shapefile (.zip) or KML file (.kml)"),
    db: Session = Depends(get_db),
):
    return handle_file_upload(file=file, background_tasks=background_tasks, db=db)


@router.get(
    "/{file_id}/",
    response_model=FileStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get file processing status and metadata",
)
def get_file_status(
    file_id: UUID,
    db: Session = Depends(get_db),
):
    return get_file_status_controller(file_id=file_id, db=db)


@router.get(
    "/{file_id}/measurements/",
    response_model=FileMeasurementsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get extracted features and measurements",
)
def get_file_measurements(
    file_id: UUID,
    db: Session = Depends(get_db),
):
    return get_file_measurements_controller(file_id=file_id, db=db)
