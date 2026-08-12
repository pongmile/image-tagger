"""Create the development virtual environment on Windows, macOS, or Linux."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / "apps" / "indexer" / ".venv"
REQUIREMENTS = ROOT / "apps" / "indexer" / "requirements.txt"


def venv_python() -> Path:
    name = "python.exe" if os.name == "nt" else "python"
    folder = "Scripts" if os.name == "nt" else "bin"
    return VENV / folder / name


def main() -> int:
    if not ((3, 10) <= sys.version_info[:2] < (3, 13)):
        print("Python 3.10-3.12 is required (3.12 recommended).", file=sys.stderr)
        return 2
    if not venv_python().exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run(
        [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
        check=True,
        cwd=ROOT,
    )
    print(f"Development environment ready: {venv_python()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
