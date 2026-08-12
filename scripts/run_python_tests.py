"""Run all standalone indexer tests with a deterministic interpreter."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "apps" / "indexer" / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
    "python.exe" if os.name == "nt" else "python"
)


def main() -> int:
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    tests = sorted((ROOT / "apps" / "indexer" / "tests").glob("test_*.py"))
    if not tests:
        print("No Python tests found.", file=sys.stderr)
        return 2
    for test in tests:
        print(f"\n=== {test.name} ===", flush=True)
        completed = subprocess.run([str(python), str(test)], cwd=ROOT)
        if completed.returncode:
            return completed.returncode
    print(f"\nPASS: {len(tests)} Python test suites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
