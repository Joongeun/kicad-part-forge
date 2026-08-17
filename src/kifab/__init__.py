"""kifab — generate, validate and manage KiCad 9 symbols and footprints."""

from __future__ import annotations

__all__ = ["__version__"]

# Single source of truth. `pyproject.toml` reads it (hatch `version.source =
# "code"`), so `kifab --version` and the wheel metadata can never disagree.
__version__ = "0.1.0"
