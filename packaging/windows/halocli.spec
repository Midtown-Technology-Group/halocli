import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH).resolve().parents[1]
datas = collect_data_files("halocli")
hiddenimports = collect_submodules("halocli")
datas += collect_data_files("mtg_microsoft_auth")
hiddenimports += collect_submodules("mtg_microsoft_auth")
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
