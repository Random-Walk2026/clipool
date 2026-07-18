"""Runtime version sourced from installed package metadata."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("clipool")
except PackageNotFoundError:  # source tree before installation
    __version__ = "0+unknown"
