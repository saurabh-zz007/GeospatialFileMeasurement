from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from src.logs.models import FileStatus



class FileUploadResponse(BaseModel):
    id: UUID
    filename: str
    status: FileStatus
    message: str = "File uploaded successfully. Processing started in background."

    model_config = ConfigDict(from_attributes=True)


class FileStatusResponse(BaseModel):
    id: UUID
    filename: str
    status: FileStatus
    crs: Optional[str] = None
    feature_count: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FeatureItemResponse(BaseModel):
    id: UUID
    feature_index: int
    geometry_type: str
    geometry: dict[str, Any]
    properties: dict[str, Any]
    measurement: Optional[float] = Field(default=None)
    measurement_unit: Optional[str] = Field(default=None)

    model_config = ConfigDict(from_attributes=True)


class FileMeasurementsResponse(BaseModel):
    file_id: UUID
    filename: str
    status: FileStatus
    crs: Optional[str] = None
    feature_count: Optional[int] = None
    features: list[FeatureItemResponse]

    model_config = ConfigDict(from_attributes=True)
