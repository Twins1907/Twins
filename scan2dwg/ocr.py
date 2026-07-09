"""Optional text recognition.

Uses pytesseract if it (and the tesseract binary) are installed. When absent,
:func:`detect_text` returns an empty list so the rest of the pipeline still
runs — OCR is a nice-to-have, not a hard dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TextItem:
    text: str
    x: float  #: left, image pixel coords (y-down)
    y: float  #: top, image pixel coords (y-down)
    height: float  #: glyph box height in pixels (used as CAD text height)


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def detect_text(gray: np.ndarray, min_confidence: float = 40.0) -> list[TextItem]:
    """Return recognised text boxes, or [] if OCR is unavailable."""
    if not ocr_available():
        return []
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(gray, output_type=Output.DICT)
    items: list[TextItem] = []
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if conf < min_confidence:
            continue
        items.append(
            TextItem(
                text=text,
                x=float(data["left"][i]),
                y=float(data["top"][i]),
                height=float(data["height"][i]) or 10.0,
            )
        )
    return items
