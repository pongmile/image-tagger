"""Run the indexer with the project virtual environment when available."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "apps" / "indexer" / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
    "python.exe" if os.name == "nt" else "python"
)


if __name__ == "__main__":
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    raise SystemExit(subprocess.call([str(python), "-m", "indexer.main"], cwd=ROOT / "apps" / "indexer"))
