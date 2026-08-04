from __future__ import annotations

import sys
from pathlib import Path


def application_dir() -> Path:
    """Return the user-visible application directory in source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return application_dir() / "data"
    return application_dir().parent / "data"
