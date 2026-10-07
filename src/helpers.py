import logging
import math
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

# Dispatch table mapping geometry types to metric measurement calculations
MEASUREMENT_DISPATCH: dict[str, Callable[[BaseGeometry], tuple[Optional[float], Optional[str]]]] = {
    "Polygon": lambda g: (round(float(g.area), 4), "sq meters"),
    "MultiPolygon": lambda g: (round(float(g.area), 4), "sq meters"),
    "LineString": lambda g: (round(float(g.length), 4), "meters"),
    "MultiLineString": lambda g: (round(float(g.length), 4), "meters"),
    "Point": lambda _: (None, None),
    "MultiPoint": lambda _: (None, None),
}


def enable_kml_driver() -> None:
    """Configures Fiona to enable KML driver support."""
    try:
        import fiona.drvsupport

        fiona.drvsupport.supported_drivers["KML"] = "rw"
        fiona.drvsupport.supported_drivers["LIBKML"] = "rw"
    except Exception as exc:
        logger.warning(f"Could not configure Fiona KML drivers: {exc}")


def sanitize_for_json(val: Any) -> Any:
    """Converts numpy, pandas, and datetime objects into JSON-compliant values."""
    match val:
        case None | int() | bool() | str():
            return val
        case float() | np.floating():
            return None if (math.isnan(val) or math.isinf(val)) else float(val)
        case np.integer():
            return int(val)
        case datetime() | date() | pd.Timestamp():
            return val.isoformat()
        case list() | tuple() | np.ndarray():
            return [sanitize_for_json(item) for item in val]
        case dict():
            return {str(k): sanitize_for_json(v) for k, v in val.items()}
        case _ if pd.isna(val):
            return None
        case _:
            return str(val)


def read_kml(file_path: Path) -> gpd.GeoDataFrame:
    """Reads a KML file into a GeoDataFrame."""
    enable_kml_driver()
    try:
        return gpd.read_file(str(file_path), driver="KML")
    except Exception:
        return gpd.read_file(str(file_path))


def read_shapefile_zip(zip_path: Path) -> gpd.GeoDataFrame:
    """Extracts a zipped shapefile and reads the .shp layer."""
    temp_dir = tempfile.mkdtemp(prefix="shp_")
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(temp_dir)

        shp_files = list(Path(temp_dir).rglob("*.shp"))
        if shp_files:
            return gpd.read_file(str(shp_files[0]))
        return gpd.read_file(str(zip_path))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


FILE_READERS = {
    ".kml": read_kml,
    ".zip": read_shapefile_zip,
}


def read_geospatial_file(file_path: Path | str, original_filename: str) -> tuple[gpd.GeoDataFrame, Optional[str]]:
    """Ingests a supported geospatial file and returns GeoDataFrame with identified CRS."""
    file_path = Path(file_path)
    extension = Path(original_filename).suffix.lower()

    reader = FILE_READERS.get(extension)
    if not reader:
        raise ValueError(f"Unsupported file format '{extension}'. Allowed: {list(FILE_READERS.keys())}")

    gdf = reader(file_path)
    if gdf is None or gdf.empty:
        raise ValueError("The uploaded geospatial file contains no valid data or layers.")

    # Detect or establish initial CRS
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
        crs_str = "EPSG:4326"
    else:
        crs_str = gdf.crs.to_string()

    return gdf, crs_str


def calculate_feature_measurement(geom: Optional[BaseGeometry]) -> tuple[Optional[float], Optional[str]]:
    """Calculates metric area/length using the dispatch table."""
    if not geom or geom.is_empty:
        return None, None
    handler = MEASUREMENT_DISPATCH.get(geom.geom_type)
    return handler(geom) if handler else (None, None)


def extract_features_and_measurements(
    gdf: gpd.GeoDataFrame,
    target_projected_crs: str = "EPSG:3857"
) -> list[dict[str, Any]]:
    """Projects geographic data to metric CRS and extracts features and measurements."""
    # Ensure WGS84 for GeoJSON geometry export
    try:
        gdf_wgs84 = gdf.to_crs(epsg=4326) if (gdf.crs and gdf.crs.to_epsg() != 4326) else gdf
    except Exception:
        gdf_wgs84 = gdf

    # Reproject to metric system if CRS is geographic (degrees)
    gdf_projected = gdf.to_crs(target_projected_crs) if (gdf.crs and gdf.crs.is_geographic) else gdf

    records = []
    geom_col_name = gdf.geometry.name

    for idx in range(len(gdf)):
        orig_geom = gdf_wgs84.geometry.iloc[idx]
        proj_geom = gdf_projected.geometry.iloc[idx]

        if not orig_geom or orig_geom.is_empty:
            continue

        measurement, unit = calculate_feature_measurement(proj_geom)
        row_attributes = gdf.iloc[idx].drop(labels=[geom_col_name], errors="ignore").to_dict()

        records.append({
            "feature_index": idx,
            "geometry_type": orig_geom.geom_type,
            "geometry": sanitize_for_json(mapping(orig_geom)),
            "properties": sanitize_for_json(row_attributes),
            "measurement": measurement,
            "measurement_unit": unit,
        })

    return records
