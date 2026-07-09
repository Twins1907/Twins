"""End-to-end and unit tests for the scan2dwg pipeline.

These run without any real scan by generating a synthetic drawing, and without
optional deps (OCR / DWG backend) — those paths are exercised only when the
tools are installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.make_sample import make_sample  # noqa: E402
from scan2dwg import convert  # noqa: E402
from scan2dwg.dxf_writer import DxfConfig, write_dxf  # noqa: E402
from scan2dwg.preprocess import preprocess  # noqa: E402
from scan2dwg.vectorize import Vectors, vectorize  # noqa: E402


@pytest.fixture()
def sample(tmp_path: Path) -> Path:
    return make_sample(tmp_path / "plan.png", skew_deg=3.0)


def test_preprocess_binarizes_and_deskews(sample: Path):
    import cv2

    gray = cv2.imread(str(sample), cv2.IMREAD_GRAYSCALE)
    res = preprocess(gray)
    # Binary image is strictly two-valued.
    assert set(np.unique(res.binary)).issubset({0, 255})
    # Strokes should be the minority (drawing on paper).
    assert (res.binary == 255).mean() < 0.5
    # Deskew should detect and (roughly) correct the 3° rotation.
    assert abs(res.skew_deg) > 0.5


def test_vectorize_finds_primitives(sample: Path):
    import cv2

    gray = cv2.imread(str(sample), cv2.IMREAD_GRAYSCALE)
    res = preprocess(gray)
    vectors = vectorize(res.binary)
    assert len(vectors.lines) > 0
    assert len(vectors.polylines) > 0


def test_write_dxf_flips_y_and_scales(tmp_path: Path):
    vectors = Vectors(lines=[(0.0, 0.0, 10.0, 0.0)], polylines=[])
    out = tmp_path / "t.dxf"
    counts = write_dxf(
        out, vectors, image_height=100, cfg=DxfConfig(units_per_pixel=2.0)
    )
    assert counts["lines"] == 1
    doc = ezdxf.readfile(str(out))
    line = list(doc.modelspace().query("LINE"))[0]
    # x scaled by 2; y flipped about height*scale (100*2=200) then scaled.
    assert line.dxf.start.x == pytest.approx(0.0)
    assert line.dxf.start.y == pytest.approx(200.0)
    assert line.dxf.end.x == pytest.approx(20.0)
    assert line.dxf.end.y == pytest.approx(200.0)


def test_convert_end_to_end_produces_readable_dxf(sample: Path, tmp_path: Path):
    out = tmp_path / "plan.dxf"
    result = convert(sample, out)
    assert out.exists()
    assert result.total_entities > 0

    # The DXF must be valid and contain geometry on our layers.
    doc = ezdxf.readfile(str(out))
    msp = doc.modelspace()
    entities = list(msp)
    assert len(entities) > 0
    layers = {e.dxf.layer for e in entities}
    assert layers & {"SCAN_LINES", "SCAN_POLYLINES"}


def test_convert_default_output_path(sample: Path):
    result = convert(sample)
    expected = sample.with_suffix(".dxf")
    assert expected.exists()
    assert result.dxf_paths == [expected]
