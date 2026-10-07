import enum
import uuid
from sqlalchemy import (
    Column,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import relationship

from src.utils.db import Base



class FileStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


JSONType = JSON().with_variant(JSONB(), "postgresql")


class GeospatialFile(Base):
    __tablename__ = "geospatial_files"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    filename = Column(String(255), nullable=False)
    status = Column(
        SqlEnum(FileStatus, name="file_status_enum", native_enum=False),
        nullable=False,
        default=FileStatus.PENDING,
        index=True
    )
    crs = Column(String(100), nullable=True)
    feature_count = Column(Integer, nullable=True, default=0)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    features = relationship(
        "Feature",
        back_populates="geospatial_file",
        cascade="all, delete-orphan",
        order_by="Feature.feature_index"
    )


class Feature(Base):
    __tablename__ = "features"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    file_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("geospatial_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    feature_index = Column(Integer, nullable=False)
    geometry_type = Column(String(50), nullable=False)
    geometry = Column(JSONType, nullable=False)
    properties = Column(JSONType, nullable=False, default=dict)
    measurement = Column(Float, nullable=True)

    geospatial_file = relationship("GeospatialFile", back_populates="features")
