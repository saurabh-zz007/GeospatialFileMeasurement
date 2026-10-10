from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

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
from src.logs.models import FileStatus
from src.utils.cache import (
    generate_etag,
    get_cached_entry,
    is_etag_match,
    set_cached_entry,
)
from src.utils.db import get_db
from src.utils.limiter import (
    get_get_rate_limit,
    get_post_rate_limit,
    is_rate_limit_exempt,
    limiter,
)
from src.utils.settings import settings


router = APIRouter(prefix="/api/files", tags=["Geospatial Files"])


@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload geospatial file (.zip Shapefile or .kml)",
)
@limiter.limit(get_post_rate_limit, exempt_when=is_rate_limit_exempt)
async def upload_file(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Zipped Shapefile (.zip) or KML file (.kml)"),
):
    return await handle_file_upload(file=file, background_tasks=background_tasks)


@router.get(
    "/{file_id}/",
    response_model=FileStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get file processing status and metadata",
)
@limiter.limit(get_get_rate_limit, exempt_when=is_rate_limit_exempt)
async def get_file_status(
    file_id: UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"status:{file_id}"
    cached = get_cached_entry(cache_key)
    if cached is not None:
        etag = cached["etag"]
        cache_control = f"public, max-age={settings.CACHE_TTL_SECONDS}"
        if is_etag_match(request.headers.get("if-none-match"), etag):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={
                    "ETag": etag,
                    "Cache-Control": cache_control,
                    "X-Cache": "HIT",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=cached["data"],
            headers={
                "ETag": etag,
                "Cache-Control": cache_control,
                "X-Cache": "HIT",
            },
        )

    dto = await get_file_status_controller(file_id=file_id, db=db)
    data = jsonable_encoder(dto)

    if dto.status in (FileStatus.COMPLETED, FileStatus.FAILED):
        etag = generate_etag(data)
        cache_control = f"public, max-age={settings.CACHE_TTL_SECONDS}"
        set_cached_entry(cache_key, data, etag)
        if is_etag_match(request.headers.get("if-none-match"), etag):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={
                    "ETag": etag,
                    "Cache-Control": cache_control,
                    "X-Cache": "MISS",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=data,
            headers={
                "ETag": etag,
                "Cache-Control": cache_control,
                "X-Cache": "MISS",
            },
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=data,
        headers={"X-Cache": "MISS"},
    )


@router.get(
    "/{file_id}/measurements/",
    response_model=FileMeasurementsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get extracted features and measurements",
)
@limiter.limit(get_get_rate_limit, exempt_when=is_rate_limit_exempt)
async def get_file_measurements(
    file_id: UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"measurements:{file_id}"
    cached = get_cached_entry(cache_key)
    if cached is not None:
        etag = cached["etag"]
        cache_control = f"public, max-age={settings.CACHE_TTL_SECONDS}"
        if is_etag_match(request.headers.get("if-none-match"), etag):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={
                    "ETag": etag,
                    "Cache-Control": cache_control,
                    "X-Cache": "HIT",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=cached["data"],
            headers={
                "ETag": etag,
                "Cache-Control": cache_control,
                "X-Cache": "HIT",
            },
        )

    dto = await get_file_measurements_controller(file_id=file_id, db=db)
    data = jsonable_encoder(dto)

    if dto.status in (FileStatus.COMPLETED, FileStatus.FAILED):
        etag = generate_etag(data)
        cache_control = f"public, max-age={settings.CACHE_TTL_SECONDS}"
        set_cached_entry(cache_key, data, etag)
        if is_etag_match(request.headers.get("if-none-match"), etag):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={
                    "ETag": etag,
                    "Cache-Control": cache_control,
                    "X-Cache": "MISS",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=data,
            headers={
                "ETag": etag,
                "Cache-Control": cache_control,
                "X-Cache": "MISS",
            },
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=data,
        headers={"X-Cache": "MISS"},
    )
