"""Entry point for the MTR-P lexiang transfer proxy (single-port HTTP service)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mtrp_proxy import main  # noqa: E402

if __name__ == "__main__":
    os.environ.setdefault("MTRP_PROXY_HOST", "0.0.0.0")
    main()
