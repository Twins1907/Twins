"""Render PDF pages to grayscale images so scanned PDFs can be fed to the
pipeline. Requires the optional 'pdf' extra (pymupdf)."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def is_pdf(path: str | Path) -> bool:
    return str(path).lower().endswith(".pdf")


def render_pdf(path: str | Path, dpi: int = 300) -> list[np.ndarray]:
    """Render every page of a PDF to a grayscale numpy array."""
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "Reading PDFs needs PyMuPDF. Install with: pip install 'scan2dwg[pdf]' "
            "(or: pip install pymupdf)."
        ) from exc

    import cv2

    pages: list[np.ndarray] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            if pix.n >= 3:
                gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2GRAY)
            else:
                gray = img[:, :, 0]
            pages.append(np.ascontiguousarray(gray))
    return pages
