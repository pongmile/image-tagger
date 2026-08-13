"""Runtime-packages ABI guard — spec §5.2 deps, §7 "survive crashes and recover".

Optional AI dependencies all live in one user-data directory that `pip --target`
fills using whichever interpreter happened to install them — normally the
packaged 3.12 runtime. When the daemon later starts on a *different* Python (a
3.10 dev venv against a tree installed by the packaged app), those cp3XX
extension modules sit first on sys.path and `import numpy` dies with a missing
`_multiarray_umath` before the daemon can report anything at all; the desktop app
shows only "indexer daemon exited during startup (1)".

Verified here:
  1. foreign_abi_tag() reports the mismatching tag for a tree built elsewhere.
  2. It stays quiet for a matching tree, an abi3 tree, a pure-Python tree, a tree
     that merely *contains* a foreign extension alongside usable ones, and a
     directory that isn't there at all.
  3. The module-level guard keeps a mismatched directory off sys.path while
     still adding a compatible one — the behaviour that keeps startup alive.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_runtime_abi_guard
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OURS = f"cp{sys.version_info.major}{sys.version_info.minor}"
ALIEN = "cp999"


def _tree(root: Path, *relative: str) -> Path:
    """Create empty files at *relative* under *root*; only their names matter."""
    for item in relative:
        path = root / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return root


def run() -> int:
    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    from indexer import config

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_abi_"))

    alien = _tree(tmp / "alien", f"numpy/_core/_multiarray_umath.{ALIEN}-win_amd64.pyd")
    check(config.foreign_abi_tag(alien) == ALIEN,
          f"extensions built for {ALIEN} are reported as foreign")

    ours = _tree(tmp / "ours", f"numpy/_core/_multiarray_umath.{OURS}-win_amd64.pyd")
    check(config.foreign_abi_tag(ours) is None,
          "extensions built for this interpreter are accepted")

    abi3 = _tree(tmp / "abi3", f"safetensors/_rust.{ALIEN}-abi3-win_amd64.pyd")
    check(config.foreign_abi_tag(abi3) is None,
          "abi3 extensions stay importable on later 3.x — never a clash")

    pure = _tree(tmp / "pure", "click/__init__.py", "click/core.py")
    check(config.foreign_abi_tag(pure) is None,
          "a pure-Python tree has nothing to clash")

    mixed = _tree(tmp / "mixed",
                  f"alienpkg/_ext.{ALIEN}-win_amd64.pyd",
                  f"numpy/_core/_multiarray_umath.{OURS}-win_amd64.pyd")
    check(config.foreign_abi_tag(mixed) is None,
          "a tree carrying this interpreter's own extensions is usable")

    check(config.foreign_abi_tag(tmp / "missing") is None,
          "a directory that does not exist is not a mismatch")

    # The guard itself. Reloading config against a mismatched tree must leave it
    # off sys.path instead of letting the next `import numpy` kill the daemon.
    original_home = os.environ.get("IMAGE_TAGGER_HOME")
    original_path = list(sys.path)
    try:
        bad = _tree(tmp / "home/runtime-packages",
                    f"numpy/_core/_multiarray_umath.{ALIEN}-win_amd64.pyd")
        os.environ["IMAGE_TAGGER_HOME"] = str(tmp / "home")
        importlib.reload(config)
        check(config.RUNTIME_PACKAGES_ABI_MISMATCH == ALIEN,
              "config records the mismatching tag")
        check(str(bad) not in sys.path,
              "a mismatched runtime-packages stays off sys.path")

        good = _tree(tmp / "good/runtime-packages",
                     f"numpy/_core/_multiarray_umath.{OURS}-win_amd64.pyd")
        os.environ["IMAGE_TAGGER_HOME"] = str(tmp / "good")
        importlib.reload(config)
        check(config.RUNTIME_PACKAGES_ABI_MISMATCH is None,
              "a compatible runtime-packages reports no mismatch")
        check(str(good) in sys.path,
              "a compatible runtime-packages is still added to sys.path")
    finally:
        if original_home is None:
            os.environ.pop("IMAGE_TAGGER_HOME", None)
        else:
            os.environ["IMAGE_TAGGER_HOME"] = original_home
        sys.path[:] = original_path
        importlib.reload(config)

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — runtime-packages ABI guard verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
