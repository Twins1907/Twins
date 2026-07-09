"""Turn a clean binary stroke image into vector primitives.

Two complementary detectors run and their outputs are merged:

* **Line detection** (probabilistic Hough) captures straight segments — the
  bulk of most technical drawings.
* **Contour tracing** captures closed/curved shapes that Hough misses, emitted
  as simplified polylines.

Coordinates coming out of here are still in *image* pixel space with y pointing
down. The DXF writer flips y so the drawing is right-way-up in CAD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class VectorizeConfig:
    #: Detect straight line segments via Hough transform.
    detect_lines: bool = True
    #: Detect closed contours as polylines.
    detect_contours: bool = True
    #: Hough accumulator threshold (higher => fewer, stronger lines).
    hough_threshold: int = 50
    #: Shortest line segment (pixels) we keep.
    min_line_length: int = 25
    #: Largest gap (pixels) Hough will bridge within one segment.
    max_line_gap: int = 8
    #: Contours with area (px^2) below this are dropped as noise.
    min_contour_area: float = 60.0
    #: Douglas-Peucker simplification tolerance as a fraction of contour arc
    #: length. Larger => fewer polyline vertices.
    contour_epsilon_frac: float = 0.01


@dataclass
class Vectors:
    """Primitives in image pixel coordinates (y-down)."""

    lines: list[tuple[float, float, float, float]] = field(default_factory=list)
    polylines: list[list[tuple[float, float]]] = field(default_factory=list)

    def __len__(self) -> int:  # convenience for "did we find anything"
        return len(self.lines) + len(self.polylines)


def _detect_lines(binary: np.ndarray, cfg: VectorizeConfig):
    segments = cv2.HoughLinesP(
        binary,
        1,
        np.pi / 180.0,
        threshold=cfg.hough_threshold,
        minLineLength=cfg.min_line_length,
        maxLineGap=cfg.max_line_gap,
    )
    if segments is None:
        return []
    return [tuple(float(v) for v in seg) for seg in segments.reshape(-1, 4)]


def _detect_contours(binary: np.ndarray, cfg: VectorizeConfig):
    contours, _ = cv2.findContours(
        binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    polylines: list[list[tuple[float, float]]] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < cfg.min_contour_area:
            continue
        peri = cv2.arcLength(contour, closed=True)
        epsilon = max(1.0, cfg.contour_epsilon_frac * peri)
        approx = cv2.approxPolyDP(contour, epsilon, closed=True)
        pts = [(float(p[0][0]), float(p[0][1])) for p in approx]
        if len(pts) >= 2:
            polylines.append(pts)
    return polylines


def vectorize(binary: np.ndarray, cfg: VectorizeConfig | None = None) -> Vectors:
    """Extract line and polyline primitives from a binary stroke image."""
    cfg = cfg or VectorizeConfig()
    vectors = Vectors()
    if cfg.detect_lines:
        vectors.lines = _detect_lines(binary, cfg)
    if cfg.detect_contours:
        vectors.polylines = _detect_contours(binary, cfg)
    return vectors
