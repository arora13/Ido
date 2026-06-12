# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).parent.parent

a = Analysis(
    [str(root / "companion" / "cli.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "web" / "dist"), "web/dist"),
    ],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ido",
    console=True,
)
