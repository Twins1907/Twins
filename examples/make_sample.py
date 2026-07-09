"""Generate a synthetic 'scanned drawing' so the pipeline can be demoed and
tested without needing a real scan. Produces a slightly rotated, noisy image of
a floor-plan-like set of rectangles and lines.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def make_sample(path: str | Path, skew_deg: float = 2.0, noise: float = 6.0) -> Path:
    path = Path(path)
    img = np.full((700, 1000), 255, dtype=np.uint8)  # white paper

    # Outer wall.
    cv2.rectangle(img, (80, 80), (920, 620), 0, 3)
    # An interior room.
    cv2.rectangle(img, (80, 80), (450, 360), 0, 2)
    # A doorway gap + swing line.
    cv2.line(img, (450, 360), (450, 620), 0, 2)
    cv2.line(img, (450, 360), (560, 360), 0, 2)
    # Diagonals / a truss-like detail.
    cv2.line(img, (600, 420), (880, 580), 0, 2)
    cv2.line(img, (600, 580), (880, 420), 0, 2)
    # A circle (fixture).
    cv2.circle(img, (700, 200), 60, 0, 2)
    # Some label text.
    cv2.putText(img, "ROOM A", (150, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
    cv2.putText(img, "PLAN", (760, 600), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)

    # Rotate slightly to simulate a crooked scan.
    if skew_deg:
        h, w = img.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), skew_deg, 1.0)
        img = cv2.warpAffine(img, m, (w, h), borderValue=255)

    # Add gaussian speckle noise.
    if noise:
        rng = np.random.default_rng(0)
        img = np.clip(img.astype(np.float32) + rng.normal(0, noise, img.shape), 0, 255)
        img = img.astype(np.uint8)

    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate a synthetic scan sample.")
    ap.add_argument("-o", "--output", default="samples/sample_plan.png")
    ap.add_argument("--skew", type=float, default=2.0)
    args = ap.parse_args()
    out = make_sample(args.output, skew_deg=args.skew)
    print(f"wrote {out}")
