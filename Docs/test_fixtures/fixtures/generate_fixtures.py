"""Generates test fixtures (zipped shapefiles + KML) and expected.json.

Expected values are GEODESIC references (WGS84 ellipsoid via pyproj.Geod),
independent of any projection-based implementation. Use ~0.5% tolerance.
"""
import json, os, random, shutil, zipfile, tempfile
import geopandas as gpd
from pyproj import Geod
from shapely.geometry import Polygon, LineString, Point, MultiPolygon, box

OUT = os.path.dirname(os.path.abspath(__file__))
SHP = os.path.join(OUT, "shapefile")
KML = os.path.join(OUT, "kml")
os.makedirs(SHP, exist_ok=True)
os.makedirs(KML, exist_ok=True)
geod = Geod(ellps="WGS84")
expected = {}


def area(g):
    return abs(geod.geometry_area_perimeter(g)[0])


def length(g):
    return geod.geometry_length(g)


def write_zip(name, gdf, crs, drop_prj=False, drop=()):
    """Write gdf as shapefile, zip the sidecar files (flat, no folder)."""
    tmp = tempfile.mkdtemp()
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=crs)
    gdf.to_file(os.path.join(tmp, name + ".shp"), driver="ESRI Shapefile")
    zpath = os.path.join(SHP, name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(tmp)):
            ext = f.rsplit(".", 1)[-1]
            if drop_prj and ext == "prj":
                continue
            if ext in drop:
                continue
            z.write(os.path.join(tmp, f), f)
    shutil.rmtree(tmp)


# ---------------------------------------------------------------- shapefiles
eq_box = box(0, 0, 1, 1)
n60_box = box(10, 60, 11, 61)
outer = box(0, 0, 0.1, 0.1)
hole = box(0.04, 0.04, 0.06, 0.06)
donut = Polygon(outer.exterior.coords, [hole.exterior.coords])

# 1. basic polygon with attributes
write_zip("equator_box", {"name": ["equator_box"], "owner": ["survey_team"], "code": [1],
                          "geometry": [eq_box]}, 4326)
expected["shapefile/equator_box.zip"] = {
    "crs": "EPSG:4326", "feature_count": 1,
    "features": [{"type": "Polygon", "area_m2": area(eq_box)}],
    "properties_feature0": {"name": "equator_box", "owner": "survey_team", "code": 1}}

# 2. 60N polygon (catches degree / Web Mercator bugs)
write_zip("box_60n", {"name": ["box_60n"], "geometry": [n60_box]}, 4326)
expected["shapefile/box_60n.zip"] = {
    "crs": "EPSG:4326", "feature_count": 1,
    "features": [{"type": "Polygon", "area_m2": area(n60_box)}],
    "note": "Web Mercator would give ~25.17e9, degrees would give 1.0"}

# 3. polygon with hole
write_zip("polygon_with_hole", {"name": ["donut"], "geometry": [donut]}, 4326)
expected["shapefile/polygon_with_hole.zip"] = {
    "crs": "EPSG:4326", "feature_count": 1,
    "features": [{"type": "Polygon", "area_m2": area(outer) - area(hole)}],
    "note": "hole must be subtracted"}

# 4. multipolygon (area = sum of parts)
mp = MultiPolygon([box(0, 0, 0.1, 0.1), box(0.2, 0.2, 0.3, 0.3)])
write_zip("multipolygon", {"name": ["two_squares"], "geometry": [mp]}, 4326)
expected["shapefile/multipolygon.zip"] = {
    "crs": "EPSG:4326", "feature_count": 1,
    "features": [{"type": "MultiPolygon",
                  "area_m2": sum(area(p) for p in mp.geoms)}]}

# 5. lines (equator + 60N: 60N line catches Web Mercator on lines)
l_eq = LineString([(0, 0), (1, 0)])
l_60 = LineString([(10, 60), (11, 60)])
write_zip("lines", {"name": ["equator_line", "line_60n"], "geometry": [l_eq, l_60]}, 4326)
expected["shapefile/lines.zip"] = {
    "crs": "EPSG:4326", "feature_count": 2,
    "features": [{"type": "LineString", "length_m": length(l_eq)},
                 {"type": "LineString", "length_m": length(l_60)}]}

# 6. points only -> no measurement, no crash
write_zip("points", {"name": ["a", "b", "c"],
                     "geometry": [Point(0.5, 0.5), Point(10, 60), Point(-70, -33)]}, 4326)
expected["shapefile/points.zip"] = {
    "crs": "EPSG:4326", "feature_count": 3,
    "features": [{"type": "Point", "measurement": None}] * 3}

# 7. already-projected file (EPSG:32632) - exact planar answer: 1000 m x 2000 m
rect = box(500000, 6650000, 501000, 6652000)
write_zip("utm_projected_rect", {"name": ["rect"], "geometry": [rect]}, 32632)
expected["shapefile/utm_projected_rect.zip"] = {
    "crs": "EPSG:32632", "feature_count": 1,
    "features": [{"type": "Polygon", "area_m2": 2_000_000.0}],
    "note": "file CRS is projected; reported crs must be EPSG:32632, area must NOT be treated as degrees"}

# 8. Web Mercator file (EPSG:3857): same 60N box, stored in 3857.
#    Naive planar area in file CRS would give ~25.17e9; correct is ~6.12e9
gdf_3857 = gpd.GeoDataFrame({"name": ["box_60n_3857"], "geometry": [n60_box]}, crs=4326).to_crs(3857)
write_zip("webmercator_box_60n", gdf_3857, 3857)
expected["shapefile/webmercator_box_60n.zip"] = {
    "crs": "EPSG:3857", "feature_count": 1,
    "features": [{"type": "Polygon", "area_m2": area(n60_box)}],
    "note": "must transform to 4326/UTM, not measure in 3857"}

# 9. missing .prj -> define behaviour: FAILED with clear error (or assume 4326 + warning)
write_zip("no_prj", {"name": ["equator_box"], "geometry": [eq_box]}, 4326, drop_prj=True)
expected["shapefile/no_prj.zip"] = {
    "behaviour": "status FAILED with clear message OR assume EPSG:4326 with a warning. Must not crash.",
    "if_assumed_4326_area_m2": area(eq_box)}

# 10. broken inputs
with open(os.path.join(SHP, "corrupt.zip"), "wb") as f:
    f.write(os.urandom(2048))
expected["shapefile/corrupt.zip"] = {"behaviour": "400 at upload or status FAILED; no 500, no crash"}

with zipfile.ZipFile(os.path.join(SHP, "no_shp_inside.zip"), "w") as z:
    z.writestr("readme.txt", "no shapefile here")
expected["shapefile/no_shp_inside.zip"] = {"behaviour": "400 / FAILED: no .shp found in archive"}

open(os.path.join(SHP, "empty.zip"), "wb").close()
expected["shapefile/empty.zip"] = {"behaviour": "400: empty file"}

with open(os.path.join(SHP, "not_a_zip.txt"), "w") as f:
    f.write("hello")
expected["shapefile/not_a_zip.txt"] = {"behaviour": "400 / 415: unsupported extension"}

# 11. missing .shx (GDAL may restore it, may not): both outcomes acceptable, but no crash
write_zip("missing_shx", {"name": ["equator_box"], "geometry": [eq_box]}, 4326, drop=("shx",))
expected["shapefile/missing_shx.zip"] = {
    "behaviour": "either COMPLETED (shx restored) or FAILED with clear message; must not crash",
    "if_completed_area_m2": area(eq_box)}

# 12. nested folder inside zip (common when users zip a folder)
tmp = tempfile.mkdtemp()
gpd.GeoDataFrame({"name": ["nested"], "geometry": [eq_box]}, crs=4326).to_file(
    os.path.join(tmp, "nested.shp"))
with zipfile.ZipFile(os.path.join(SHP, "nested_folder.zip"), "w") as z:
    for f in sorted(os.listdir(tmp)):
        z.write(os.path.join(tmp, f), "my_data/" + f)
shutil.rmtree(tmp)
expected["shapefile/nested_folder.zip"] = {
    "crs": "EPSG:4326", "feature_count": 1,
    "features": [{"type": "Polygon", "area_m2": area(eq_box)}],
    "note": ".shp is inside a subfolder of the zip; should still be found"}

# ---------------------------------------------------------------- KML files
def kml_ring(coords):
    return " ".join(f"{x},{y},0" for x, y in coords)


def kml_doc(placemarks):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
            + "\n".join(placemarks) + "\n</Document></kml>\n")


def kml_poly(name, g):
    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{kml_ring(list(r.coords))}</coordinates></LinearRing></innerBoundaryIs>"
        for r in g.interiors)
    return (f"<Placemark><name>{name}</name><Polygon><outerBoundaryIs><LinearRing><coordinates>"
            f"{kml_ring(list(g.exterior.coords))}</coordinates></LinearRing></outerBoundaryIs>{inner}</Polygon></Placemark>")


def kml_line(name, g):
    return (f"<Placemark><name>{name}</name><LineString><coordinates>{kml_ring(list(g.coords))}"
            f"</coordinates></LineString></Placemark>")


def kml_point(name, g, extra=""):
    return f"<Placemark><name>{name}</name>{extra}<Point><coordinates>{g.x},{g.y},0</coordinates></Point></Placemark>"


def write_kml(name, placemarks):
    with open(os.path.join(KML, name + ".kml"), "w") as f:
        f.write(kml_doc(placemarks))


write_kml("equator_box", [kml_poly("equator_box", eq_box)])
expected["kml/equator_box.kml"] = {"crs": "EPSG:4326", "feature_count": 1,
                                   "features": [{"type": "Polygon", "area_m2": area(eq_box)}]}
write_kml("box_60n", [kml_poly("box_60n", n60_box)])
expected["kml/box_60n.kml"] = {"crs": "EPSG:4326", "feature_count": 1,
                               "features": [{"type": "Polygon", "area_m2": area(n60_box)}]}
write_kml("polygon_with_hole", [kml_poly("donut", donut)])
expected["kml/polygon_with_hole.kml"] = {"crs": "EPSG:4326", "feature_count": 1,
                                         "features": [{"type": "Polygon", "area_m2": area(outer) - area(hole)}]}
write_kml("mixed_geometries", [
    kml_point("my_point", Point(0.5, 0.5),
              '<ExtendedData><Data name="owner"><value>survey_team</value></Data></ExtendedData>'),
    kml_line("equator_line", l_eq), kml_poly("equator_box", eq_box)])
expected["kml/mixed_geometries.kml"] = {
    "crs": "EPSG:4326", "feature_count": 3,
    "features": [{"type": "Point", "measurement": None},
                 {"type": "LineString", "length_m": length(l_eq)},
                 {"type": "Polygon", "area_m2": area(eq_box)}]}
zc = box(5.9, 50, 6.1, 50.1)
write_kml("utm_zone_crossing", [kml_poly("zone_border", zc)])
expected["kml/utm_zone_crossing.kml"] = {"crs": "EPSG:4326", "feature_count": 1,
                                         "features": [{"type": "Polygon", "area_m2": area(zc)}]}
write_kml("line_60n", [kml_line("line_60n", l_60)])
expected["kml/line_60n.kml"] = {"crs": "EPSG:4326", "feature_count": 1,
                                "features": [{"type": "LineString", "length_m": length(l_60)}],
                                "note": "Web Mercator would give 111319 m"}

# ------------------------------------------------- load-test files (both formats)
LOAD = os.path.join(OUT, "load")
os.makedirs(LOAD, exist_ok=True)
random.seed(42)


def make_features(n):
    polys = []
    for _ in range(n):
        x, y = random.uniform(-120, 120), random.uniform(-60, 60)
        w, h = random.uniform(0.001, 0.01), random.uniform(0.001, 0.01)
        polys.append(box(x, y, x + w, y + h))
    return polys


for label, n in (("small_10", 10), ("medium_1000", 1000), ("large_20000", 20000)):
    polys = make_features(n)
    gdf = gpd.GeoDataFrame({"name": [f"f{i}" for i in range(n)], "geometry": polys}, crs=4326)
    tmp = tempfile.mkdtemp()
    gdf.to_file(os.path.join(tmp, label + ".shp"))
    with zipfile.ZipFile(os.path.join(LOAD, label + ".zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(tmp)):
            z.write(os.path.join(tmp, f), f)
    shutil.rmtree(tmp)
    pms = [kml_poly(f"f{i}", p) for i, p in enumerate(polys)]
    with open(os.path.join(LOAD, label + ".kml"), "w") as f:
        f.write(kml_doc(pms))
    expected[f"load/{label}"] = {"feature_count": n,
                                 "note": "same content in .zip and .kml; total geodesic area below",
                                 "total_area_m2": sum(area(p) for p in polys)}

with open(os.path.join(OUT, "expected.json"), "w") as f:
    json.dump(expected, f, indent=2)
print("done")
