"""Optional DXF -> DWG conversion.

DWG is Autodesk's proprietary format; there is no pure-Python writer. We shell
out to whichever converter is installed:

* **ODA File Converter** (free, from the Open Design Alliance) — preferred.
* **LibreDWG's `dwg2dxf`/`dxf2dwg`** if present.

If neither is found, we tell the user how to get one and leave the DXF in place.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class DwgConversionError(RuntimeError):
    pass


def _find_oda() -> str | None:
    for name in ("ODAFileConverter", "ODAFileConverter.exe", "TeighaFileConverter"):
        found = shutil.which(name)
        if found:
            return found
    return None


def dwg_backend() -> str | None:
    """Return the name of an available backend, or None."""
    if _find_oda():
        return "oda"
    if shutil.which("dwg2dxf") or shutil.which("dxf2dwg"):
        return "libredwg"
    return None


def _convert_with_oda(exe: str, dxf_path: Path, dwg_path: Path, version: str) -> None:
    # ODA converts by directory: it processes every matching file in a folder.
    with tempfile.TemporaryDirectory() as in_dir, tempfile.TemporaryDirectory() as out_dir:
        staged = Path(in_dir) / dxf_path.name
        shutil.copy2(dxf_path, staged)
        cmd = [exe, in_dir, out_dir, version, "DWG", "0", "1", staged.name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        produced = Path(out_dir) / (dxf_path.stem + ".dwg")
        if not produced.exists():
            raise DwgConversionError(
                f"ODA File Converter did not produce a DWG.\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        shutil.copy2(produced, dwg_path)


def _convert_with_libredwg(dxf_path: Path, dwg_path: Path) -> None:
    exe = shutil.which("dxf2dwg")
    if not exe:
        raise DwgConversionError(
            "LibreDWG's dxf2dwg is not available (only dwg2dxf was found)."
        )
    proc = subprocess.run(
        [exe, "-o", str(dwg_path), str(dxf_path)], capture_output=True, text=True
    )
    if proc.returncode != 0 or not dwg_path.exists():
        raise DwgConversionError(f"dxf2dwg failed: {proc.stderr or proc.stdout}")


def dxf_to_dwg(dxf_path: str | Path, dwg_path: str | Path, version: str = "ACAD2018") -> Path:
    """Convert a DXF to DWG using an available backend.

    Raises :class:`DwgConversionError` (with install guidance) if no backend
    is installed.
    """
    dxf_path = Path(dxf_path)
    dwg_path = Path(dwg_path)
    backend = dwg_backend()
    if backend == "oda":
        _convert_with_oda(_find_oda(), dxf_path, dwg_path, version)
    elif backend == "libredwg":
        _convert_with_libredwg(dxf_path, dwg_path)
    else:
        raise DwgConversionError(
            "No DWG backend found. The DXF was written and is readable by "
            "AutoCAD and every major CAD tool. To also emit .dwg, install one "
            "of:\n"
            "  * ODA File Converter (free): https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "  * LibreDWG (dxf2dwg): https://www.gnu.org/software/libredwg/"
        )
    return dwg_path
