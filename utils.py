import sys
from pathlib import Path

import numpy
import numpy as np
import pandas
import requests
import shapely

import config
import constants

def build_bbox(lat: np.ndarray, lon: np.ndarray, size_km: float = 10.0) -> np.ndarray:
    """
    Build an approximately `size_km` x `size_km` bounding box centred on
    (lat, lon). The box is `size_km` across in both dimensions, i.e. it extends
    half of `size_km` in each direction from the centre.

    Uses a local equirectangular approximation, which is accurate for small
    boxes (a few km) at any latitude, including the high latitudes of the
    Aleutians. Longitude degrees are scaled by cos(latitude).

    Args:
        lat: Latitude(s) as a scalar float or numpy array.
        lon: Longitude(s) as a scalar float or numpy array.
        size_km: Size of the bounding box in kilometers (default 10.0).

    Returns:
        A numpy array of shapely box geometries.
    """
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)

    if np.any(lat < -90.0) or np.any(lat > 90.0):
        raise ValueError(f"latitude out of range: {lat}")
    if np.any(lon < -180.0) or np.any(lon > 180.0):
        raise ValueError(f"longitude out of range: {lon}")

    half = size_km / 2.0
    # 1 degree of latitude ~= 111.32 km everywhere.
    dlat = half / 111.32
    # 1 degree of longitude shrinks toward the poles.
    cos_lat = np.cos(np.radians(lat))
    # Guard against division by ~0 at the poles.
    dlon = np.where(cos_lat > 1e-6, half / (111.32 * cos_lat), 180.0)

    west = lon - dlon
    east = lon + dlon
    south = np.maximum(lat - dlat, -90.0)
    north = np.minimum(lat + dlat, 90.0)

    return shapely.box(west, south, east, north)

_NAME_SUFFIXES = (" seamount", " volcano", " islands", " island")

def normalize_volcano_names(names: pandas.Series) -> pandas.Series:
    n = names.fillna("").astype(str).str.strip().str.lower()
    n = n.str.partition("(")[0].str.strip()  # drop parenthetical
    for suffix in _NAME_SUFFIXES:
        n = n.str.removesuffix(suffix)
    return n.str.partition("-")[0].str.strip()  # first hyphen component

COLOR_RANK = {"RED": 0, "ORANGE": 1, "YELLOW": 2, "GREEN": 3}
DEFAULT_COLOR = "GREEN"
USGS_ELEVATED_URL = (
    "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes"
)
def fetch_elevated_colors() -> tuple[pandas.Series, pandas.Series]:
    """
    Fetch the USGS elevated-volcanoes feed and return two Series:
    - by_vnum: Series indexed by vnum with color code values.
    - by_name: Series indexed by normalized name with color code values,
      containing ONLY the volcanoes that have no vnum (no overlap with by_vnum).

    On any network/parse error this returns two empty Series so
    callers can proceed treating every volcano as GREEN.
    """
    try:
        resp = requests.get(USGS_ELEVATED_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        print(
            f"WARNING: could not fetch USGS alert levels ({exc}); "
            "treating all volcanoes as GREEN for ordering.",
            file=sys.stderr,
        )
        empty = pandas.Series(dtype=str)
        return (empty, empty)


    df = pandas.DataFrame(data or []).reindex(
        columns=["vnum", "volcano_name", "color_code"]
    )

    df["color_code"] = df["color_code"].fillna("").str.upper()

    # Filter to only the color codes we recognize and sort by severity
    df = df[df["color_code"].isin(COLOR_RANK)].sort_values(
        "color_code", key=lambda s: s.map(COLOR_RANK)
    )

    # Normalize the volcano names for matching against the targets CSV
    df["volcano_name"] = normalize_volcano_names(df["volcano_name"])

    # Make sure the vnum column is clean
    vnum = df["vnum"] = pandas.to_numeric(df["vnum"], errors="coerce")
    df["vnum"] = vnum.mask(vnum == "")

    has_vnum= df["vnum"].notna()
    by_vnum=(
        df[has_vnum]
        .drop_duplicates("vnum")
        .astype({"vnum": "int64"})  # safe: NaNs were filtered out above
        .set_index("vnum")["color_code"]
    )

    by_name = (
        df[~has_vnum & (df["volcano_name"] != "")]
        .drop_duplicates("volcano_name")
        .set_index("volcano_name")["color_code"]
    )
    return by_vnum, by_name

# A target with latitude below this is treated as Northern Mariana Islands;
# at/above it as Alaska (incl. Aleutians). The two regions are ~16-21degN vs
# ~45-62degN, so 30deg is an unambiguous split for this target set.
MARIANA_LAT_MAX = 30

def load_targets() -> pandas.DataFrame:
    """Load the targets CSV file into a pandas DataFrame.

    Fetches USGS elevated color codes and adds a *color* column to the
    DataFrame, matching by *vnum* (preferred) or normalized volcano name
    (fallback).
    """
    targets_path = Path(__file__).parent / constants.TARGETS_CSV

    # Columns: volcano, latitude, longitude, vnum (from first row of CSV)
    # Use vnum as the index for direct series assignment
    df = pandas.read_csv(targets_path, sep=',', index_col='vnum')

    # Fetch color codes: by_vnum (preferred) and by_name (only volcanoes without vnum)
    colors_by_vnum, colors_by_name = fetch_elevated_colors()

    # Direct assignment by vnum index
    df["color"] = colors_by_vnum

    # For volcanoes without vnum, switch index to volcano name and assign
    names = normalize_volcano_names(df["volcano"])
    df["color"] = df["color"].fillna(
        colors_by_name.reindex(names).set_axis(df.index)
    )

    # Default any remaining unmatched volcanoes to GREEN
    df["color"] = df["color"].fillna(DEFAULT_COLOR)

    # Sort dataframe by color severity (RED, ORANGE, YELLOW, GREEN), region, and longitude
    is_alaska:numpy.ndarray = df["latitude"] > MARIANA_LAT_MAX
    df = (
        df.assign(
            _color=df["color"].map(COLOR_RANK),
            _region=(~is_alaska).astype(int),  # Alaska 0, Mariana 1
            _pos=np.where(is_alaska, df["longitude"] % 360, -df["latitude"]),
        )
        .sort_values(["_color", "_region", "_pos"], kind="stable")
        .drop(columns=["_color", "_region", "_pos"])
    )

    boxes = build_bbox(df["latitude"].values, df["longitude"].values)
    df["bbox"] = boxes
    points=shapely.points(df["longitude"].values, df["latitude"].values)
    df['point'] = points
    tree = shapely.STRtree(boxes)

    return df