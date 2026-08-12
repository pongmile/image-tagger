"""Indexer entrypoint — spec §7.

`python -m indexer.main` = auto mode (watch include roots + drain jobs), the
default the Electron app spawns. For one-off control (add roots, rescan, manual
tags) use the CLI: `python -m indexer.cli ...`.
"""
from . import __version__
from . import db
from .cli import _cmd_watch


def main() -> None:
    con = db.connect()
    n = con.execute("SELECT count(*) FROM files").fetchone()[0]
    roots = con.execute("SELECT count(*) FROM roots WHERE enabled=1").fetchone()[0]
    print(f"indexer v{__version__} — db ready, {n} files, {roots} enabled root(s).")
    if roots == 0:
        print("no roots configured. Add one:  python -m indexer.cli add-root <path>")
        return
    con.close()
    _cmd_watch(None, type("A", (), {})())  # auto mode


if __name__ == "__main__":
    main()
