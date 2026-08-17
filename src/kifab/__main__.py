"""`python -m kifab` — the same entry point as the `kifab` console script.

Exists so the Claude Code plugin's shim has a way to run the tool from a source
checkout, where no console script has been installed.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
