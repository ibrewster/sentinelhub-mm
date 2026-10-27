#!/usr/bin/env python3
"""
graphics.py

Render annotated preview images for matched volcano scenes using the Sentinel
Hub Process API (CDSE). For each scene this fetches the exact 10x10 km bounding
box as a highlight-optimized natural color PNG, then annotates it with:

  - a header: target name, sensor, and acquisition datetime
  - a scale bar (km)
  - latitude / longitude tick labels around the image

Images are saved to a graphics/ subfolder as:  target_datetime_sensor.png

This is the piece that genuinely needs the **Process API** (it renders pixels),
as opposed to the Catalog API used for scene discovery.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from datetime import datetime

from constants import PROCESS_URL, SENSORS
from utils import bbox_edges

# Pixel size of the fetched imagery (square). 1000 px over 10 km = 10 m/px,
# matching Sentinel-2's native resolution; Landsat (30 m) is upsampled.
IMAGE_PX = 1000


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
    sensor: str, bbox: BBox, start_iso: str, end_iso: str, px: int = IMAGE_PX
) -> dict:
    """Build the Process API request body for a single scene's bbox and day."""
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
        "evalscript": EVALSCRIPTS[sensor],
    }


def fetch_scene_png(
    session, sensor: str, bbox: BBox, acquired: str
) -> bytes:
    """
    Fetch a single scene's imagery as PNG bytes via the Process API. The time
    range is pinned to the acquisition day so the specific scene is rendered.
    """
    day = acquired[:10]
    start_iso = f"{day}T00:00:00Z"
    end_iso = f"{day}T23:59:59Z"
    payload = build_process_payload(sensor, bbox, start_iso, end_iso)
    resp = session.post(PROCESS_URL, json=payload)
    resp.raise_for_status()
    return resp.content


# -----------------------------------------------------------------------------
# Annotation
# -----------------------------------------------------------------------------
def _load_font(size: int):
    from PIL import ImageFont

    # Try a few common TrueType fonts; fall back to PIL's bitmap default.
    for name in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _decimals_for_step(step: float) -> int:
    """Number of decimal places needed to represent `step` exactly (min 1)."""
    s = f"{step:.6f}".rstrip("0").rstrip(".")
    if "." in s:
        return len(s.split(".")[1])
    return 0


def _fmt_deg(value: float, decimals: int, pos: str, neg: str) -> str:
    """Format a degree value with `decimals` places (trailing zeros trimmed)."""
    hemi = pos if value >= 0 else neg
    num = f"{abs(value):.{decimals}f}"
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return f"{num}\u00b0{hemi}"


def _fmt_lat(lat: float, decimals: int = 3) -> str:
    return _fmt_deg(lat, decimals, "N", "S")


def _fmt_lon(lon: float, decimals: int = 3) -> str:
    return _fmt_deg(lon, decimals, "E", "W")


# Candidate "round" tick spacings in degrees, largest first.
_NICE_STEPS = [1.0, 0.5, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001]


def nice_ticks(lo: float, hi: float, min_ticks: int = 2) -> list[float]:
    """
    Return round-number tick values within [lo, hi], choosing the largest nice
    step (1, 0.5, 0.25, 0.2, 0.1, 0.05, ...) that still yields at least
    `min_ticks` ticks inside the range. Ticks are multiples of the step.
    """
    lo, hi = (lo, hi) if lo <= hi else (hi, lo)
    span = hi - lo
    if span <= 0:
        return [lo]
    chosen = _NICE_STEPS[-1]
    for step in _NICE_STEPS:
        # Count multiples of `step` strictly within [lo, hi].
        first = math.ceil(lo / step) * step
        n = 0
        x = first
        while x <= hi + 1e-9:
            n += 1
            x += step
        if n >= min_ticks:
            chosen = step
            break
    ticks = []
    first = math.ceil(lo / chosen - 1e-9) * chosen
    x = first
    while x <= hi + 1e-9:
        ticks.append(round(x, 6))
        x += chosen
    return ticks


def annotate_image(
    png_bytes: bytes,
    target: str,
    sensor: str,
    acquired: str,
    bbox: BBox,
    bbox_km: float,
):
    """
    Return a Pillow Image rendering the scene on a white background with:
      - a black-text header (target | sensor | datetime)
      - a black frame around the imagery
      - latitude/longitude tick marks and labels on all four sides, placed at
        round-number values (>=2 per side) via `nice_ticks`
      - a scale bar (km)
    """
    from PIL import Image, ImageDraw

    scene = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = scene.size

    header_h = max(70, h // 11)
    # Margins hold tick labels. Latitude labels are rotated vertical, so left/
    # right need only ~label-height of space (not full label width).
    tick_px = max(13, header_h // 5)
    m_left = max(40, tick_px + h // 90 + 12)
    m_right = max(40, tick_px + h // 90 + 12)
    m_bottom = max(70, h // 14)
    m_top_pad = max(10, h // 100)

    canvas_w = w + m_left + m_right
    canvas_h = h + header_h + m_bottom
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))  # white bg

    img_x, img_y = m_left, header_h
    canvas.paste(scene, (img_x, img_y))

    draw = ImageDraw.Draw(canvas)
    black = (0, 0, 0)
    title_font = _load_font(max(20, header_h // 3))
    tick_font = _load_font(max(13, header_h // 5))

    # --- header (black text) ---
    header = f"{target}   |   {sensor}   |   {acquired}"
    draw.text((img_x, m_top_pad), header, fill=black, font=title_font)

    # --- black frame around the imagery ---
    frame_w = max(2, h // 400)
    draw.rectangle(
        [img_x - 1, img_y - 1, img_x + w, img_y + h], outline=black, width=frame_w
    )

    # --- lat/lon ticks on all four sides ---
    tick_len = max(8, h // 90)

    def lon_to_x(lon: float) -> float:
        return img_x + (lon - bbox.west) / (bbox.east - bbox.west) * w

    def lat_to_y(lat: float) -> float:
        # North at top: higher latitude -> smaller y.
        return img_y + (bbox.north - lat) / (bbox.north - bbox.south) * h

    def text_wh(s, font):
        box = draw.textbbox((0, 0), s, font=font)
        return box[2] - box[0], box[3] - box[1]

    lon_ticks = nice_ticks(bbox.west, bbox.east, min_ticks=2)
    lat_ticks = nice_ticks(bbox.south, bbox.north, min_ticks=2)
    lon_dec = _decimals_for_step(lon_ticks[1] - lon_ticks[0]) if len(lon_ticks) > 1 else 3
    lat_dec = _decimals_for_step(lat_ticks[1] - lat_ticks[0]) if len(lat_ticks) > 1 else 3

    # Longitude ticks: top and bottom edges.
    for lon in lon_ticks:
        x = lon_to_x(lon)
        label = _fmt_lon(lon, lon_dec)
        tw, th = text_wh(label, tick_font)
        # top edge
        draw.line([(x, img_y), (x, img_y - tick_len)], fill=black, width=2)
        draw.text((x - tw / 2, img_y - tick_len - th - 3), label, fill=black, font=tick_font)
        # bottom edge
        draw.line([(x, img_y + h), (x, img_y + h + tick_len)], fill=black, width=2)
        draw.text((x - tw / 2, img_y + h + tick_len + 3), label, fill=black, font=tick_font)

    # Latitude ticks: left and right edges, with labels rotated vertical so they
    # do not extend far into the margins.
    def _vlabel(text: str):
        """Render `text` to a tight RGBA image rotated 90deg (reads bottom->top)."""
        tw, th = text_wh(text, tick_font)
        chip = Image.new("RGBA", (tw + 2, th + 2), (255, 255, 255, 0))
        ImageDraw.Draw(chip).text((1, 1), text, fill=black, font=tick_font)
        return chip.rotate(90, expand=True)

    for lat in lat_ticks:
        y = lat_to_y(lat)
        label = _fmt_lat(lat, lat_dec)
        vlab = _vlabel(label)
        lw, lh = vlab.size
        # Clamp vertical position so labels near the top/bottom edges are not
        # clipped by the canvas; keep them as close to their tick as possible.
        paste_y = int(y - lh / 2)
        paste_y = max(0, min(paste_y, canvas_h - lh))
        # left edge
        draw.line([(img_x, y), (img_x - tick_len, y)], fill=black, width=2)
        canvas.paste(vlab, (int(img_x - tick_len - lw - 3), paste_y), vlab)
        # right edge
        draw.line([(img_x + w, y), (img_x + w + tick_len, y)], fill=black, width=2)
        canvas.paste(vlab, (int(img_x + w + tick_len + 3), paste_y), vlab)

    # --- scale bar (inside image, lower-left, white bar w/ black outline) ---
    px_per_km = w / bbox_km
    bar_km = 2.0 if bbox_km >= 6 else 1.0
    bar_px = int(px_per_km * bar_km)
    bar_x0 = img_x + int(0.06 * w)
    bar_y = img_y + h - int(0.07 * h)
    bar_h = max(5, h // 150)
    draw.rectangle(
        [bar_x0, bar_y, bar_x0 + bar_px, bar_y + bar_h],
        fill=(255, 255, 255),
        outline=black,
        width=2,
    )
    # Label centered horizontally on the bar, above it, no background chip.
    _tw, _th = text_wh(f"{bar_km:g} km", tick_font)
    lx = bar_x0 + (bar_px - _tw) / 2
    ly = bar_y - _th - 4
    draw.text((lx, ly), f"{bar_km:g} km", fill=black, font=tick_font)
    return canvas


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
) -> str:
    """
    Fetch, annotate, and save one scene image. Returns the saved file path.
    Raises on fetch/render errors (caller decides whether to continue).
    """
    bbox = BBox.centered(lat, lon, size_km=bbox_km)
    png = fetch_scene_png(session, sensor, bbox, acquired)
    img = annotate_image(png, target, sensor, acquired, bbox, bbox_km)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, safe_filename(target, acquired, sensor))
    img.save(path, format="PNG")
    return path
