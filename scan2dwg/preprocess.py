"""Image loading and cleanup before vectorization.

Turns an arbitrary scanned page (photo, flatbed scan, PDF render) into a clean
binary image where drawing strokes are white (255) on a black (0) background,
which is what the vectorizer expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class PreprocessConfig:
    """Tunables for the cleanup stage."""

    #: Longest side the image is scaled down to before processing. Keeps big
    #: scans fast without meaningfully hurting line detection. 0 disables.
    max_dimension: int = 2000
    #: Median blur kernel used to knock out speckle noise. Must be odd; 0 skips.
    denoise_kernel: int = 3
    #: Block size for adaptive thresholding (must be odd, >= 3).
    threshold_block_size: int = 35
    #: Constant subtracted from the local mean in adaptive thresholding.
    threshold_c: int = 15
    #: Whether to auto-deskew based on the dominant text/line angle.
    deskew: bool = True
    #: Max absolute angle (degrees) we are willing to rotate during deskew.
    max_skew_deg: float = 15.0


@dataclass
class PreprocessResult:
    binary: np.ndarray  #: uint8, strokes=255 on background=0
    scale: float  #: factor applied to the original (processed = original * scale)
    skew_deg: float  #: rotation applied during deskew (0 if none)
    size: tuple[int, int]  #: (width, height) of the processed image


def load_image(path: str | Path) -> np.ndarray:
    """Load an image file as grayscale. PDFs must be rendered to images first
    (see :mod:`scan2dwg.io_pdf`)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    # imread with a unicode path can fail on some builds; decode from bytes.
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(
            f"Could not decode image: {path}. Supported: png/jpg/tif/bmp. "
            "For PDFs, install the 'pdf' extra."
        )
    return img


def _resize(gray: np.ndarray, max_dimension: int) -> tuple[np.ndarray, float]:
    if max_dimension <= 0:
        return gray, 1.0
    h, w = gray.shape[:2]
    longest = max(h, w)
    if longest <= max_dimension:
        return gray, 1.0
    scale = max_dimension / float(longest)
    resized = cv2.resize(
        gray, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
    )
    return resized, scale


def _binarize(gray: np.ndarray, cfg: PreprocessConfig) -> np.ndarray:
    if cfg.denoise_kernel and cfg.denoise_kernel >= 3:
        k = cfg.denoise_kernel | 1  # force odd
        gray = cv2.medianBlur(gray, k)
    block = max(3, cfg.threshold_block_size | 1)  # force odd, >= 3
    # THRESH_BINARY_INV so dark ink -> white strokes on black background.
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block,
        cfg.threshold_c,
    )
    return binary


def _estimate_skew(binary: np.ndarray, max_skew_deg: float) -> float:
    """Estimate page skew from the dominant near-horizontal line angle."""
    lines = cv2.HoughLinesP(
        binary,
        1,
        np.pi / 180.0,
        threshold=200,
        minLineLength=max(binary.shape) // 4,
        maxLineGap=20,
    )
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        # Fold to nearest horizontal reference in [-45, 45].
        angle = (angle + 90) % 180 - 90
        if -45 <= angle <= 45 and abs(angle) <= max_skew_deg:
            angles.append(angle)
    if not angles:
        return 0.0
    return float(np.median(angles))


def _rotate(image: np.ndarray, angle_deg: float) -> np.ndarray:
    h, w = image.shape[:2]
    center = (w / 2.0, h / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    return cv2.warpAffine(
        image, matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=0
    )


def preprocess(gray: np.ndarray, cfg: PreprocessConfig | None = None) -> PreprocessResult:
    """Clean a grayscale scan into a binary stroke image."""
    cfg = cfg or PreprocessConfig()
    resized, scale = _resize(gray, cfg.max_dimension)
    binary = _binarize(resized, cfg)

    skew = 0.0
    if cfg.deskew:
        skew = _estimate_skew(binary, cfg.max_skew_deg)
        if abs(skew) > 0.1:
            binary = _rotate(binary, skew)

    h, w = binary.shape[:2]
    return PreprocessResult(binary=binary, scale=scale, skew_deg=skew, size=(w, h))
