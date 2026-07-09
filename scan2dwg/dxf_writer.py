"""Write detected primitives to a DXF file with sensible layers.

Handles the two coordinate concerns CAD users care about:

* **Y flip** — images have y pointing down; CAD has y pointing up. We flip
  around the image height so the drawing is oriented correctly.
* **Scale** — pixels are converted to drawing units via ``units_per_pixel`` so
  the output can be to real-world scale (e.g. derived from a known dimension).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import ezdxf

from .ocr import TextItem
from .vectorize import Vectors

LAYER_LINES = "SCAN_LINES"
LAYER_POLY = "SCAN_POLYLINES"
LAYER_TEXT = "SCAN_TEXT"


@dataclass
class DxfConfig:
    #: Drawing units per source pixel. 1.0 keeps pixel coordinates 1:1.
    units_per_pixel: float = 1.0
    #: DXF version. R2010 is broadly compatible with modern CAD tools.
    dxf_version: str = "R2010"
    #: Emit recognised text as CAD TEXT entities.
    include_text: bool = True


def _mk_transform(image_height: int, units_per_pixel: float):
    """Return a fn mapping (px, py) image coords -> CAD coords (y flipped)."""

    def transform(px: float, py: float) -> tuple[float, float]:
        return (px * units_per_pixel, (image_height - py) * units_per_pixel)

    return transform


def write_dxf(
    path: str | Path,
    vectors: Vectors,
    image_height: int,
    texts: list[TextItem] | None = None,
    cfg: DxfConfig | None = None,
) -> dict[str, int]:
    """Write vectors (+ optional text) to ``path``. Returns entity counts."""
    cfg = cfg or DxfConfig()
    texts = texts or []
    transform = _mk_transform(image_height, cfg.units_per_pixel)

    doc = ezdxf.new(cfg.dxf_version)
    msp = doc.modelspace()
    for name, color in ((LAYER_LINES, 7), (LAYER_POLY, 3), (LAYER_TEXT, 5)):
        if name not in doc.layers:
            doc.layers.add(name, color=color)

    counts = {"lines": 0, "polylines": 0, "text": 0}

    for x1, y1, x2, y2 in vectors.lines:
        msp.add_line(transform(x1, y1), transform(x2, y2), dxfattribs={"layer": LAYER_LINES})
        counts["lines"] += 1

    for pts in vectors.polylines:
        cad_pts = [transform(px, py) for px, py in pts]
        msp.add_lwpolyline(
            cad_pts, close=True, dxfattribs={"layer": LAYER_POLY}
        )
        counts["polylines"] += 1

    if cfg.include_text:
        for item in texts:
            # Anchor at the text box baseline-ish (bottom-left in CAD space).
            insert = transform(item.x, item.y + item.height)
            height = max(1e-6, item.height * cfg.units_per_pixel)
            entity = msp.add_text(
                item.text,
                dxfattribs={"layer": LAYER_TEXT, "height": height},
            )
            entity.set_placement(insert)
            counts["text"] += 1

    doc.saveas(path)
    return counts
