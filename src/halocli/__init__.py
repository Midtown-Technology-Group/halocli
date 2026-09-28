"""Standalone HaloPSA CLI package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    __version__ = _package_version("halocli")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"
