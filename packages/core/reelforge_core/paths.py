"""Location of the shared /data volume inside containers."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
INBOX_DIR = DATA_DIR / "inbox"
WORKING_DIR = DATA_DIR / "working"
OUTPUTS_DIR = DATA_DIR / "outputs"
