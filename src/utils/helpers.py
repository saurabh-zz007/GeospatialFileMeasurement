import logging
import math
import shutil
import tempfile
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
import shapely
import shapely.ops
import shapely.validation
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

# Standard human-readable processing error messages
ERROR_INVALID_ZIP = "not a valid zip"
ERROR_EMPTY_FILE = "empty file"
ERROR_NO_SHP = "no .shp found in archive"
ERROR_MISSING_PRJ = "missing .prj (coordinate system unknown)"
ERROR_NO_FEATURES = "no features could be read (possibly missing/corrupt .shx)"
ERROR_UNEXPECTED = "unexpected processing error"


class GeospatialProcessingError(Exception):
    """User-facing processing error with specific message."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# Fields always injected by Fiona/GDAL KML driver with default/null values
KML_DROP_FIELDS = {
    "id",
    "icon",
    "drawOrder",
    "altitudeMode",
    "tessellate",
    "visibility",
    "extrude",
}

# Thread-safe cache of pyproj Transformer instances
_TRANSFORMER_CACHE: dict[tuple[str, str], Transformer] = {}


def get_transformer(to_crs: str, from_crs: str = "EPSG:4326") -> Transformer:
    """Retrieves or creates a cached pyproj Transformer with always_xy=True."""
    key = (from_crs, to_crs)
    if key not in _TRANSFORMER_CACHE:
        _TRANSFORMER_CACHE[key] = Transformer.from_crs(from_crs, to_crs, always_xy=True)
    return _TRANSFORMER_CACHE[key]


def utm_epsg(lon: float, lat: float) -> int:
    """Calculates UTM EPSG code based on longitude and latitude."""
    zone = int((lon + 180) // 6) + 1
    zone = min(60, max(1, zone))
    return (32600 if lat >= 0 else 32700) + zone


def choose_measurement_crs(lon: float, lat: float) -> str:
    """
    Selects metric projected CRS based on centroid (lon, lat) in WGS84:
    - UTM zone for latitudes between 80°S and 84°N
    - EPSG:3031 (Antarctic Polar Stereographic) for lat < -80°
    - EPSG:3413 (NSIDC Sea Ice Polar Stereographic North) for lat > 84°
    """
    if lat < -80.0:
        return "EPSG:3031"
    elif lat > 84.0:
        return "EPSG:3413"
    else:
        return f"EPSG:{utm_epsg(lon, lat)}"


def round_coordinates(coords: Any, decimals: int = 9) -> Any:
    """Recursively rounds coordinates in nested lists/tuples to specified decimals."""
    if isinstance(coords, (float, np.floating)):
        return round(float(coords), decimals)
    if isinstance(coords, (list, tuple)):
        return [round_coordinates(c, decimals) for c in coords]
    return coords


def round_geometry_coordinates(geo_dict: Optional[dict[str, Any]], decimals: int = 9) -> Optional[dict[str, Any]]:
    """Rounds coordinates of GeoJSON geometry mapping to specified decimals."""
    if not geo_dict:
        return geo_dict
    result = dict(geo_dict)
    if "coordinates" in result:
        result["coordinates"] = round_coordinates(result["coordinates"], decimals)
    if "geometries" in result:
        result["geometries"] = [round_geometry_coordinates(g, decimals) for g in result["geometries"]]
    return result


def sanitize_for_json(val: Any) -> Any:
    """Converts numpy, pandas, and datetime objects into JSON-compliant values, converting NaN/inf/NaT to None."""
    if val is None:
        return None

    if val is pd.NA or val is pd.NaT:
        return None

    if isinstance(val, (int, np.integer)) and not isinstance(val, bool):
        return int(val)

    if isinstance(val, bool):
        return bool(val)

    if isinstance(val, (float, np.floating)):
        if math.isnan(val) or math.isinf(val):
            return None
        return float(val)

    if isinstance(val, str):
        s_val = val.strip()
        if s_val in ("NaT", "nan", "NaN", "NAN", "<NA>", "inf", "-inf", "Infinity", "-Infinity"):
            return None
        return val

    if isinstance(val, (datetime, date, pd.Timestamp)):
        if pd.isna(val):
            return None
        return val.isoformat()

    if isinstance(val, (list, tuple, np.ndarray)):
        return [sanitize_for_json(item) for item in val]

    if isinstance(val, dict):
        return {str(k): sanitize_for_json(v) for k, v in val.items()}

    try:
        if pd.isna(val):
            return None
    except Exception:
        pass

    return str(val)


def clean_feature_properties(
    row_attributes: dict[str, Any],
    is_kml: bool = False,
) -> dict[str, Any]:
    """
    Sanitizes properties dictionary:
    - Drops default/always-null KML metadata attributes if is_kml is True
    - Converts NaT, NaN, Infinity, and empty/unrepresentable values to None
    """
    cleaned = {}
    for k, v in row_attributes.items():
        if is_kml and k in KML_DROP_FIELDS:
            continue
        cleaned[str(k)] = sanitize_for_json(v)
    return cleaned


def calculate_metric_measurement(
    geom_wgs84: Optional[BaseGeometry],
) -> tuple[Optional[float], Optional[str],Optional[float], Optional[str],Optional[float], Optional[str], Optional[str], Optional[str]]:
    """
    Calculates metric area or length by projecting 2D WGS84 geometry to appropriate UTM/polar CRS.
    Returns: (area, area_unit, perimeter, perimeter_unit, measurement_crs, note)
    """
    if geom_wgs84 is None:
        return None, None, None, None, None, None, None, "Geometry is null"

    if geom_wgs84.is_empty:
        return None, None, None, None, None, None, None, "Geometry is empty"

    if not geom_wgs84.is_valid:
        reason = shapely.validation.explain_validity(geom_wgs84)
        return None, None, None, None, None, None, None, f"Invalid geometry: {reason}"

    geom_type = geom_wgs84.geom_type

    # Point / MultiPoint: measurement null, no crash, no note
    if geom_type in ("Point", "MultiPoint"):
         return None, None, None, None, None, None, None, None

    # Unsupported types (GeometryCollection, etc.)
    if geom_type not in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"):
        return None, None, None, None, None, None, None, f"Unsupported geometry type: {geom_type}"

    try:
        # Ignore Z coordinates (2D only)
        geom_2d = shapely.force_2d(geom_wgs84)
        centroid = geom_2d.centroid

        if centroid.is_empty or math.isnan(centroid.x) or math.isnan(centroid.y):
            return None, None, None, None, None, None, None, "Could not compute geometry centroid"

        target_crs = choose_measurement_crs(centroid.x, centroid.y)
        transformer = get_transformer(to_crs=target_crs, from_crs="EPSG:4326")
        proj_geom = shapely.ops.transform(transformer.transform, geom_2d)

        if geom_type in ("Polygon", "MultiPolygon"):
            # Area in square meters (holes are automatically subtracted)
            area = round(float(proj_geom.area), 4)
            area_unit = "sq meters"
            perimeter = round(float(proj_geom.length), 4)
            perimeter_unit = "meters"
            return area, area_unit, perimeter, perimeter_unit, None, None, target_crs, None
        elif geom_type in ("LineString", "MultiLineString"):
            # Length in meters
            length = round(float(proj_geom.length), 4)
            length_unit = "meters"
            return None, None, None, None, length, length_unit, target_crs, None
        else:
            measurement = None
            unit = None

        return None, None, None, None, None, None, None, None

    except Exception as exc:
        logger.warning(f"Error computing measurement for geometry {geom_type}: {exc}")
        return None, None, None, None, None, None, None, f"Measurement calculation failed: {exc}"


def enable_kml_driver() -> None:
    """Configures Fiona to enable KML driver support."""
    try:
        import fiona.drvsupport

        fiona.drvsupport.supported_drivers["KML"] = "rw"
        fiona.drvsupport.supported_drivers["LIBKML"] = "rw"
    except Exception as exc:
        logger.warning(f"Could not configure Fiona KML drivers: {exc}")


def read_kml(file_path: Path) -> gpd.GeoDataFrame:
    """Reads a KML file into a GeoDataFrame."""
    file_path = Path(file_path)
    if not file_path.exists() or file_path.stat().st_size == 0:
        raise GeospatialProcessingError(ERROR_EMPTY_FILE)

    enable_kml_driver()
    try:
        gdf = gpd.read_file(str(file_path), driver="KML")
    except Exception:
        try:
            gdf = gpd.read_file(str(file_path))
        except Exception as exc:
            logger.exception(f"Error reading KML {file_path}: {exc}")
            raise GeospatialProcessingError(ERROR_NO_FEATURES) from exc

    if gdf is None or gdf.empty or len(gdf) == 0:
        raise GeospatialProcessingError(ERROR_NO_FEATURES)

    return gdf


def read_shapefile_zip(zip_path: Path) -> gpd.GeoDataFrame:
    """Extracts a zipped shapefile and reads the .shp layer."""
    zip_path = Path(zip_path)
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        raise GeospatialProcessingError(ERROR_EMPTY_FILE)

    if not zipfile.is_zipfile(zip_path):
        raise GeospatialProcessingError(ERROR_INVALID_ZIP)

    temp_dir = tempfile.mkdtemp(prefix="shp_")
    try:
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                namelist = archive.namelist()
                if not namelist:
                    raise GeospatialProcessingError(ERROR_EMPTY_FILE)
                archive.extractall(temp_dir)
        except zipfile.BadZipFile:
            raise GeospatialProcessingError(ERROR_INVALID_ZIP)

        shp_files = list(Path(temp_dir).rglob("*.shp"))
        if not shp_files:
            raise GeospatialProcessingError(ERROR_NO_SHP)

        shp_file = shp_files[0]
        if shp_file.stat().st_size == 0:
            raise GeospatialProcessingError(ERROR_EMPTY_FILE)

        # Check for .prj file
        prj_file = shp_file.with_suffix(".prj")
        if not prj_file.exists() or prj_file.stat().st_size == 0:
            raise GeospatialProcessingError(ERROR_MISSING_PRJ)

        try:
            gdf = gpd.read_file(str(shp_file))
        except Exception as exc:
            logger.exception(f"Error reading shapefile {shp_file}: {exc}")
            raise GeospatialProcessingError(ERROR_NO_FEATURES) from exc

        if gdf is None or gdf.empty or len(gdf) == 0:
            raise GeospatialProcessingError(ERROR_NO_FEATURES)

        if gdf.crs is None:
            raise GeospatialProcessingError(ERROR_MISSING_PRJ)

        return gdf
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

    if not file_path.exists() or file_path.stat().st_size == 0:
        raise GeospatialProcessingError(ERROR_EMPTY_FILE)

    reader = FILE_READERS.get(extension)
    if not reader:
        raise GeospatialProcessingError(f"Unsupported file format '{extension}'. Allowed: {list(FILE_READERS.keys())}")

    gdf = reader(file_path)
    if gdf is None or gdf.empty or len(gdf) == 0:
        raise GeospatialProcessingError(ERROR_NO_FEATURES)

    # Detect or establish initial CRS
    if gdf.crs is None:
        if extension == ".zip":
            raise GeospatialProcessingError(ERROR_MISSING_PRJ)
        else:
            gdf.set_crs(epsg=4326, inplace=True)
            crs_str = "EPSG:4326"
    else:
        crs_str = gdf.crs.to_string()

    return gdf, crs_str


def extract_features_and_measurements(
    gdf: gpd.GeoDataFrame,
    is_kml: bool = False,
    target_projected_crs: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Reprojects source geographic data to WGS84 if not already 4326,
    rounds geometry coordinates to 9 decimals, and calculates measurements.
    """
    # Ensure WGS84 (EPSG:4326) for GeoJSON geometry export and centroid calculations
    if gdf.crs is not None:
        try:
            is_4326 = (gdf.crs.to_epsg() == 4326)
        except Exception:
            is_4326 = ("4326" in str(gdf.crs))

        if not is_4326:
            gdf_wgs84 = gdf.to_crs(epsg=4326)
        else:
            gdf_wgs84 = gdf
    else:
        gdf_wgs84 = gdf

    geom_col_name = gdf.geometry.name
    non_geom_cols = [c for c in gdf.columns if c != geom_col_name]
    props_records = gdf[non_geom_cols].to_dict(orient="records") if non_geom_cols else [{} for _ in range(len(gdf))]
    geoms = list(gdf_wgs84.geometry) if hasattr(gdf_wgs84, "geometry") else [None] * len(gdf)

    records = []
    for idx in range(len(gdf)):
        # if idx % 50 == 0:
        #     time.sleep(0.001)

        orig_geom = geoms[idx]

        geom_type = orig_geom.geom_type if orig_geom is not None else None
        geom_2d_wgs84 = shapely.force_2d(orig_geom) if orig_geom is not None else None

        area, area_unit, perimeter, perimeter_unit, length, length_unit, measurement_crs, note = calculate_metric_measurement(orig_geom)
        clean_properties = clean_feature_properties(props_records[idx], is_kml=is_kml)

        # Round returned geometry coordinates to 9 decimals
        geom_mapping = mapping(geom_2d_wgs84) if geom_2d_wgs84 is not None else None
        rounded_geom = round_geometry_coordinates(geom_mapping, decimals=9) if geom_mapping is not None else None

        records.append({
            "feature_index": idx,
            "geometry_type": geom_type,
            "geometry": sanitize_for_json(rounded_geom) if rounded_geom is not None else None,
            "properties": clean_properties,
            "area": area,
            "area_unit": area_unit,
            "perimeter": perimeter,
            "perimeter_unit": perimeter_unit,
            "length": length,
            "length_unit": length_unit,
            "measurement_crs": measurement_crs,
            "note": note,
        })

    return records
