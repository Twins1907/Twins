"""End-to-end orchestration: a scan file in, a DXF (and optionally DWG) out."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import dwg as dwg_mod
from . import io_pdf, ocr
from .dxf_writer import DxfConfig, write_dxf
from .preprocess import PreprocessConfig, preprocess, load_image
from .vectorize import VectorizeConfig, vectorize


@dataclass
class PipelineConfig:
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    vectorize: VectorizeConfig = field(default_factory=VectorizeConfig)
    dxf: DxfConfig = field(default_factory=DxfConfig)
    #: Run OCR to capture drawing text/labels (needs tesseract installed).
    ocr: bool = True
    #: Also emit a .dwg alongside the .dxf (needs an external DWG backend).
    emit_dwg: bool = False
    #: DPI used when rasterising PDF pages.
    pdf_dpi: int = 300


@dataclass
class PageResult:
    dxf_path: Path
    dwg_path: Path | None
    counts: dict[str, int]
    skew_deg: float


@dataclass
class PipelineResult:
    pages: list[PageResult] = field(default_factory=list)

    @property
    def dxf_paths(self) -> list[Path]:
        return [p.dxf_path for p in self.pages]

    @property
    def total_entities(self) -> int:
        return sum(sum(p.counts.values()) for p in self.pages)


def _load_pages(input_path: Path, cfg: PipelineConfig):
    """Yield grayscale page arrays for an image or PDF input."""
    if io_pdf.is_pdf(input_path):
        return io_pdf.render_pdf(input_path, dpi=cfg.pdf_dpi)
    return [load_image(input_path)]


def convert(
    input_path: str | Path,
    output_path: str | Path | None = None,
    config: PipelineConfig | None = None,
) -> PipelineResult:
    """Convert ``input_path`` (image or PDF) into DXF (+ optional DWG).

    For a single-page input the output is ``output_path`` (default: input with a
    ``.dxf`` suffix). For a multi-page PDF, ``_p1``, ``_p2`` … are appended.
    """
    cfg = config or PipelineConfig()
    input_path = Path(input_path)
    base = Path(output_path) if output_path else input_path.with_suffix(".dxf")

    pages = _load_pages(input_path, cfg)
    result = PipelineResult()

    for index, gray in enumerate(pages):
        pre = preprocess(gray, cfg.preprocess)
        vectors = vectorize(pre.binary, cfg.vectorize)

        texts = []
        if cfg.ocr:
            # OCR reads the cleaned/deskewed binary for consistent coordinates.
            texts = ocr.detect_text(pre.binary)

        if len(pages) == 1:
            dxf_path = base
        else:
            dxf_path = base.with_name(f"{base.stem}_p{index + 1}{base.suffix}")
        dxf_path.parent.mkdir(parents=True, exist_ok=True)

        counts = write_dxf(
            dxf_path,
            vectors,
            image_height=pre.size[1],
            texts=texts,
            cfg=cfg.dxf,
        )

        dwg_path = None
        if cfg.emit_dwg:
            dwg_path = dwg_mod.dxf_to_dwg(dxf_path, dxf_path.with_suffix(".dwg"))

        result.pages.append(
            PageResult(
                dxf_path=dxf_path,
                dwg_path=dwg_path,
                counts=counts,
                skew_deg=pre.skew_deg,
            )
        )

    return result
