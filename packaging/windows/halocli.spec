import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

project_root = Path(SPECPATH).resolve().parents[1]
datas = collect_data_files("halocli")
hiddenimports = collect_submodules("halocli")
datas += collect_data_files("mtg_microsoft_auth")
hiddenimports += collect_submodules("mtg_microsoft_auth")
# copy_metadata is load-bearing, not optional: halocli_launcher.py resolves the
# console script via importlib.metadata.entry_points(), which reads the
# halocli-*.dist-info directory. collect_data_files() does NOT include
# dist-info, so without this the frozen exe exits with "Console script entry
# point not found" on every command -- that shipped in every MSI from 0.5.0 to
# 0.8.0. build-msi.ps1 smoke-tests --version so this cannot regress silently.
datas += copy_metadata("halocli")
use_upx = os.environ.get("BUILD_USE_UPX", "").lower() in {"1", "true", "yes"}

a = Analysis(
    [str(project_root / "packaging" / "windows" / "halocli_launcher.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="halocli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
)
