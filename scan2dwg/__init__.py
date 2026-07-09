"""scan2dwg — convert raster scans of 2D drawings into CAD geometry (DXF/DWG).

The pipeline is:

    load -> preprocess -> vectorize -> (optional OCR) -> DXF -> (optional DWG)

Each stage lives in its own module so it can be tested and swapped in isolation.
"""

__version__ = "0.1.0"

from .pipeline import convert, PipelineConfig, PipelineResult  # noqa: E402,F401

__all__ = ["convert", "PipelineConfig", "PipelineResult", "__version__"]
