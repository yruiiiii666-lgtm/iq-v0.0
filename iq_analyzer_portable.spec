# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


ROOT = Path(SPECPATH)
DEPS = Path(os.environ.get("IQ_ANALYZER_PORTABLE_DEPS", ROOT / ".portable_build" / "deps"))
if DEPS.is_dir():
    sys.path.insert(0, str(DEPS))

hiddenimports = [
    "pyvisa",
    "pyvisa_py",
    "pyvisa_py.attributes",
    "pyvisa_py.common",
    "pyvisa_py.highlevel",
    "pyvisa_py.sessions",
    "pyvisa_py.tcpip",
    "pyvisa_py.protocols.hislip",
    "pyvisa_py.protocols.rpc",
    "pyvisa_py.protocols.vxi11",
    "pyvisa_py.protocols.xdrlib",
]
datas = copy_metadata("pyvisa")
datas += copy_metadata("pyvisa-py")
datas.append((str(ROOT / "场景地点_IQ关联表_更新版.csv"), "."))

a = Analysis(
    [str(ROOT / "iq_analyzer_gui.py")],
    pathex=[str(ROOT), str(DEPS)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["TkAgg"]}},
    runtime_hooks=[],
    excludes=[
        "cv2",
        "fsspec",
        "grpc",
        "h5py",
        "IPython",
        "jedi",
        "jupyter",
        "keras",
        "llvmlite",
        "lxml",
        "numba",
        "openpyxl",
        "pandas",
        "PyQt5",
        "pytest",
        "rich",
        "sklearn",
        "soundfile",
        "sympy",
        "tensorflow",
        "torch",
        "torchaudio",
        "torchvision",
        "tornado",
        "zmq",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IQAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="IQAnalyzer_Portable",
)
