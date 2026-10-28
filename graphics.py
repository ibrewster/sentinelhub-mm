#!/usr/bin/env python3
"""
graphics.py

Render annotated preview images for matched volcano scenes using the Sentinel
Hub Process API (CDSE). For each scene this fetches the exact 10x10 km bounding
box as a highlight-optimized natural color PNG, or a thermal Band 10 rendering
for nighttime Landsat acquisitions, then annotates it with:

  - a header: target name, sensor, and acquisition datetime
  - a scale bar (km)
  - latitude / longitude tick labels around the image

This is the piece that genuinely needs the **Process API** (it renders pixels),
as opposed to the Catalog API used for scene discovery.
"""

from __future__ import annotations

import io
import math
import os
import re
from dataclasses import dataclass

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib import patheffects as pe
from matplotlib.ticker import FuncFormatter, MaxNLocator
from PIL import Image, ImageDraw
from pycoast import ContourWriterAGG
from pyresample.geometry import AreaDefinition

from constants import PROCESS_URL, SENSORS
from utils import bbox_edges
import config

# Pixel size of the fetched imagery (square). 1000 px over 10 km = 10 m/px,
# matching Sentinel-2's native resolution; Landsat (30 m) is upsampled.
# Using a smaller image size (e.g. 512 px) reduces the download size.
IMAGE_PX = 512


# -----------------------------------------------------------------------------
# Geometry helpers
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class BBox:
    """A geographic bounding box in WGS84 decimal degrees."""

    west: float
    south: float
    east: float
    north: float

    def as_list(self) -> list[float]:
        """Return the bbox as [west, south, east, north] (STAC/GeoJSON order)."""
        return [self.west, self.south, self.east, self.north]

    @classmethod
    def centered(cls, lat: float, lon: float, size_km: float = 10.0) -> BBox:
        """
        Build an approximately `size_km` x `size_km` box centred on (lat, lon).
        Scalar counterpart of `utils.build_bbox`; see `utils.bbox_edges`.
        """
        return cls(**bbox_edges(lat, lon, size_km)._asdict())

# Highlight-optimized natural color evalscripts (Sentinel Hub evalscript V3).
# Sentinel-2 L1C uses the contrast-boosted form; Landsat 8-9 OLI shares the same
# true-color band names (B04/B03/B02) and uses the plain cube-root form.
# Source: https://custom-scripts.sentinel-hub.com/sentinel-2/highlight_optimized_natural_color/
_S2_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "dataMask"],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  return [
    Math.cbrt(0.6 * s.B04 - 0.035),
    Math.cbrt(0.6 * s.B03 - 0.035),
    Math.cbrt(0.6 * s.B02 - 0.035),
    s.dataMask
  ];
}
"""

_LANDSAT_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "dataMask"],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  return [
    Math.cbrt(0.6 * s.B04 - 0.035),
    Math.cbrt(0.6 * s.B03 - 0.035),
    Math.cbrt(0.6 * s.B02 - 0.035),
    s.dataMask
  ];
}
"""

_LANDSAT_THERMAL_EVALSCRIPT = """//VERSION=3
const thermalRamp = new ColorRampVisualizer([
  [223, 0x003d99],
  [253, 0x2e82ff],
  [263, 0x80b3ff],
  [272, 0xe0edff],
  [273, 0xffffff],
  [274, 0xfefce7],
  [283, 0xfde191],
  [293, 0xf69855],
  [303, 0xec6927],
  [323, 0xaa2d1d],
  [363, 0x650401],
  [373, 0x3d0200]
]);

function setup() {
  return {
    input: [{ bands: ["B10"] }],
    output: { bands: 3 }
  };
}

function evaluatePixel(sample) {
  return thermalRamp.process(sample.B10);
}
"""

EVALSCRIPTS = {
    "Sentinel-2 L1C": _S2_EVALSCRIPT,
    "Landsat 8-9 L1": _LANDSAT_EVALSCRIPT,
}


# -----------------------------------------------------------------------------
# Filenames
# -----------------------------------------------------------------------------
def safe_filename(target: str, acquired: str, sensor: str) -> str:
    """
    Build the output filename 'target_datetime_sensor.png', sanitized so it is
    safe on all platforms (spaces, parentheses, colons, slashes removed).
    """
    def clean(s: str) -> str:
        s = s.strip().replace(" ", "-")
        return re.sub(r"[^A-Za-z0-9._-]", "", s)

    # Compact the datetime to YYYYMMDDTHHMMSS (drop fractional secs / 'Z').
    dt = acquired.replace("-", "").replace(":", "")
    dt = dt.split(".")[0].rstrip("Z")
    return f"{clean(target)}_{clean(dt)}_{clean(sensor)}.png"


# -----------------------------------------------------------------------------
# Process API fetch
# -----------------------------------------------------------------------------
def build_process_payload(
    sensor: str,
    bbox: BBox,
    start_iso: str,
    end_iso: str,
    px: int = IMAGE_PX,
    thermal: bool = False,
) -> dict:
    """Build the Process API request body for a single scene's bbox and day."""
    if thermal and sensor != "Landsat 8-9 L1":
        raise ValueError("Thermal rendering is only supported for Landsat 8-9 L1")

    process_type = SENSORS[sensor]["process_type"]
    return {
        "input": {
            "bounds": {
                "bbox": bbox.as_list(),
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [
                {
                    "type": process_type,
                    "dataFilter": {"timeRange": {"from": start_iso, "to": end_iso}},
                }
            ],
        },
        "output": {
            "width": px,
            "height": px,
            "responses": [
                {"identifier": "default", "format": {"type": "image/png"}}
            ],
        },
        "evalscript": (
            _LANDSAT_THERMAL_EVALSCRIPT if thermal else EVALSCRIPTS[sensor]
        ),
    }


def fetch_scene_png(
    session, sensor: str, bbox: BBox, acquired: str, thermal: bool = False
) -> bytes:
    """
    Fetch a single scene's imagery as PNG bytes via the Process API. The time
    range is pinned to the acquisition day so the specific scene is rendered.
    """
    day = acquired[:10]
    start_iso = f"{day}T00:00:00Z"
    end_iso = f"{day}T23:59:59Z"
    payload = build_process_payload(
        sensor, bbox, start_iso, end_iso, thermal=thermal
    )
    resp = session.post(PROCESS_URL, json=payload)
    resp.raise_for_status()
    return resp.content


def nighttime_placeholder_png() -> bytes:
    """Create a local notice image for nighttime sensors without thermal bands."""
    scene = Image.new("RGB", (IMAGE_PX, IMAGE_PX), (32, 35, 40))
    draw = ImageDraw.Draw(scene)
    draw.multiline_text(
        (IMAGE_PX // 2, IMAGE_PX // 2),
        "Nighttime acquisition\nNo thermal bands available",
        anchor="mm",
        align="center",
        fill="white",
        spacing=8,
    )
    buffer = io.BytesIO()
    scene.save(buffer, format="PNG")
    return buffer.getvalue()


# -----------------------------------------------------------------------------
# Annotation
# -----------------------------------------------------------------------------
DPI = 100
M_X, M_TOP, M_BOTTOM = 70, 90, 70  # figure margins, px


def _deg_formatter(pos: str, neg: str) -> FuncFormatter:
    def fmt(v: float, _pos) -> str:
        num = f"{abs(v):.4f}".rstrip("0").rstrip(".")
        return f"{num}\u00b0{pos if v >= 0 else neg}"

    return FuncFormatter(fmt)

def add_coastlines(scene, bbox, gshhg_dir, level=2):
    area = AreaDefinition("scene", "", "", "EPSG:4326", scene.width, scene.height,
                          (bbox.west, bbox.south, bbox.east, bbox.north))
    ContourWriterAGG(gshhg_dir).add_coastlines(
        scene, area, resolution="f", level=level, width=1, outline=(255, 255, 0))

def annotate_image(
        png_bytes: bytes, target: str, sensor: str, acquired: str, bbox, bbox_km: float
) -> Image.Image:
    scene = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    #add_coastlines(scene, bbox, config.GSHHG_DIR, level=1)

    w, h = scene.size
    W, H = w + 2 * M_X, h + M_TOP + M_BOTTOM

    # Build the Figure directly (no pyplot): no global state, safe in threads/GUI.
    fig = Figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([M_X / W, M_BOTTOM / H, w / W, h / H])

    ax.imshow(scene, extent=(bbox.west, bbox.east, bbox.south, bbox.north),
              interpolation="none")
    # Image is square on the ground, so 1 deg lon must be drawn cos(lat) as wide
    # as 1 deg lat. (imshow's default aspect='equal' would stretch it E-W.)
    ax.set_aspect(1 / math.cos(math.radians((bbox.south + bbox.north) / 2)))

    ax.xaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
    ax.xaxis.set_major_formatter(_deg_formatter("E", "W"))
    ax.yaxis.set_major_formatter(_deg_formatter("N", "S"))
    ax.tick_params(top=True, right=True, labeltop=True, labelright=True,
                   length=6, width=1.5, labelsize=15)
    ax.tick_params(axis="y", labelrotation=90)
    for spine in ax.spines.values():
        spine.set_linewidth(1.5)

    ax.text(
        0.05, 0.95, f"{target} | {sensor}\n{acquired}",
        transform=ax.transAxes,
        ha="left", va="top",
        color="white", fontsize=16,
    #    bbox=dict(facecolor="black", alpha=0.6, edgecolor="none", boxstyle="round,pad=0.3")
        path_effects=[
            pe.SimpleLineShadow(offset=(2, -2), shadow_color="black", alpha=0.8),
            pe.Normal()
        ]
    )

    # Scale bar, lower-left, in axes coordinates (image spans bbox_km edge to edge).
    bar_km = 2.0 if bbox_km >= 6 else 1.0
    x0, y0 = 0.06, 0.07
    ax.add_patch(Rectangle((x0, y0), bar_km / bbox_km, 0.012,
                           transform=ax.transAxes, fc="white", ec="black", lw=1.5))
    ax.text(x0 + bar_km / bbox_km / 2, y0 + 0.02, f"{bar_km:g} km",
            transform=ax.transAxes, ha="center", va="bottom", fontsize=15, color="white")

    fig.canvas.draw()
    return Image.frombuffer("RGBA", fig.canvas.get_width_height(),
                            fig.canvas.buffer_rgba()).convert("RGB")


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------
def generate_scene_image(
    session,
    target: str,
    lat: float,
    lon: float,
    sensor: str,
    acquired: str,
    out_dir: str,
    bbox_km: float = 10.0,
    *,
    nighttime: bool,
) -> str:
    """
    Fetch, annotate, and save one scene image. Returns the saved file path.
    Raises on fetch/render errors (caller decides whether to continue).
    """
    img = render_scene_image(
        session,
        target,
        lat,
        lon,
        sensor,
        acquired,
        bbox_km,
        nighttime=nighttime,
    )
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, safe_filename(target, acquired, sensor))
    img.save(path, format="PNG")
    return path


def render_scene_image(
    session,
    target: str,
    lat: float,
    lon: float,
    sensor: str,
    acquired: str,
    bbox_km: float = 10.0,
    *,
    nighttime: bool,
):
    """Fetch and annotate one scene without writing it to disk."""
    bbox = BBox.centered(lat, lon, size_km=bbox_km)
    thermal = sensor == "Landsat 8-9 L1" and nighttime

    if sensor == "Sentinel-2 L1C" and nighttime:
        png = nighttime_placeholder_png()
        display_sensor = f"{sensor} (night; no thermal bands)"
    else:
        png = fetch_scene_png(session, sensor, bbox, acquired, thermal=thermal)
        display_sensor = f"{sensor} (B10 TIR)" if thermal else sensor

    return annotate_image(png, target, display_sensor, acquired, bbox, bbox_km)
