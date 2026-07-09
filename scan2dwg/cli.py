"""Command-line interface: ``scan2dwg input.png -o output.dxf``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .dwg import DwgConversionError, dwg_backend
from .pipeline import PipelineConfig, convert


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scan2dwg",
        description="Convert raster scans of 2D drawings into DXF/DWG CAD files.",
    )
    p.add_argument("input", help="Input scan: png/jpg/tif/bmp or a (scanned) pdf.")
    p.add_argument(
        "-o", "--output", help="Output .dxf path (default: alongside input)."
    )
    p.add_argument(
        "--dwg",
        action="store_true",
        help="Also emit a .dwg (needs ODA File Converter or LibreDWG installed).",
    )
    p.add_argument(
        "--no-ocr", action="store_true", help="Skip text recognition (OCR)."
    )
    p.add_argument(
        "--scale",
        type=float,
        default=1.0,
        metavar="UNITS_PER_PIXEL",
        help="Drawing units per pixel (default 1.0 = pixel coordinates).",
    )
    p.add_argument(
        "--pdf-dpi", type=int, default=300, help="DPI for rasterising PDF pages."
    )
    p.add_argument(
        "--no-deskew", action="store_true", help="Disable automatic deskew."
    )
    p.add_argument(
        "--min-line-length",
        type=int,
        default=None,
        help="Shortest line segment (px) to keep. Lower = more detail/noise.",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="Only print errors.")
    p.add_argument("--version", action="version", version=f"scan2dwg {__version__}")
    return p


def _make_config(args: argparse.Namespace) -> PipelineConfig:
    cfg = PipelineConfig()
    cfg.ocr = not args.no_ocr
    cfg.emit_dwg = args.dwg
    cfg.pdf_dpi = args.pdf_dpi
    cfg.dxf.units_per_pixel = args.scale
    cfg.preprocess.deskew = not args.no_deskew
    if args.min_line_length is not None:
        cfg.vectorize.min_line_length = args.min_line_length
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"error: input not found: {input_path}", file=sys.stderr)
        return 2

    if args.dwg and dwg_backend() is None:
        print(
            "warning: --dwg requested but no DWG backend found; will write DXF "
            "only. Install ODA File Converter or LibreDWG for DWG output.",
            file=sys.stderr,
        )

    cfg = _make_config(args)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg)

    try:
        result = convert(input_path, args.output, cfg)
    except DwgConversionError as exc:
        # DXF still got written; report the DWG-only failure clearly.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - top-level guard
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for i, page in enumerate(result.pages):
        c = page.counts
        skew = f", deskew {page.skew_deg:+.2f}°" if abs(page.skew_deg) > 0.1 else ""
        log(
            f"[page {i + 1}] {c['lines']} lines, {c['polylines']} polylines, "
            f"{c['text']} text{skew} -> {page.dxf_path}"
        )
        if page.dwg_path:
            log(f"          dwg -> {page.dwg_path}")

    log(
        f"done: {len(result.pages)} page(s), {result.total_entities} entities total."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
