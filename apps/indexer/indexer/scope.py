"""Scan-scope resolution — spec §7.0.

A file is indexed iff its path is under some enabled `include` root AND not
under a more-specific enabled `exclude` root AND matches no enabled exclude
pattern. Excludes beat includes at equal-or-deeper depth ("most-specific wins"),
so you can include D:\\Pictures but exclude D:\\Pictures\\WIP.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path, PurePath


def _norm(p: str) -> str:
    # Case-insensitive on Windows; always forward-slashed, no trailing slash.
    n = os.path.normpath(p).replace("\\", "/")
    if os.name == "nt":
        n = n.lower()
    return n.rstrip("/") or n


def _is_under(path: str, root: str) -> bool:
    """True if `path` == root or lives inside the `root` subtree."""
    p, r = _norm(path), _norm(root)
    if p == r:
        return True
    return p.startswith(r + "/")


@dataclass(frozen=True)
class Root:
    path: str
    mode: str          # include | exclude
    recursive: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class ExcludeRule:
    pattern: str
    enabled: bool = True


class ScopeResolver:
    """Decides membership for a path given the roots + exclude patterns."""

    def __init__(self, roots: list[Root], excludes: list[ExcludeRule]):
        self.roots = [r for r in roots if r.enabled]
        self.excludes = [e for e in excludes if e.enabled]

    def _matching_roots(self, path: str, mode: str) -> list[tuple[int, Root]]:
        """Return (depth_of_root, root) for every enabled root of `mode`
        whose subtree contains `path`. Depth = specificity."""
        out = []
        for r in self.roots:
            if r.mode != mode:
                continue
            if not _is_under(path, r.path):
                continue
            if not r.recursive and _norm(os.path.dirname(path)) != _norm(r.path) \
                    and _norm(path) != _norm(r.path):
                # non-recursive root reaches only its direct children
                continue
            depth = len(_norm(r.path).split("/"))
            out.append((depth, r))
        return out

    def matches_exclude_pattern(self, path: str) -> str | None:
        """Return the first enabled glob/substring pattern that matches, else None."""
        n = _norm(path)
        for e in self.excludes:
            pat = _norm(e.pattern) if os.name == "nt" else e.pattern
            # glob-style (**, *, ?) via fnmatch, plus plain substring fallback.
            if fnmatch.fnmatch(n, pat) or _glob_anywhere(n, pat):
                return e.pattern
        return None

    def is_included(self, path: str) -> bool:
        """Full resolution rule (§7.0)."""
        inc = self._matching_roots(path, "include")
        if not inc:
            return False
        if self.matches_exclude_pattern(path):
            return False
        exc = self._matching_roots(path, "exclude")
        if not exc:
            return True
        # Excludes win at equal-or-deeper depth than the deepest include.
        deepest_inc = max(d for d, _ in inc)
        deepest_exc = max(d for d, _ in exc)
        return deepest_exc < deepest_inc

    def enabled_include_roots(self) -> list[Root]:
        return [r for r in self.roots if r.mode == "include"]

    def dir_pruned(self, dirpath: str) -> bool:
        """True if a directory subtree can be skipped entirely during a walk:
        it matches an exclude pattern, or an exclude root covers it at least as
        specifically as the deepest include root that reaches it."""
        if self.matches_exclude_pattern(dirpath):
            return True
        exc = self._matching_roots(dirpath, "exclude")
        if not exc:
            return False
        inc = self._matching_roots(dirpath, "include")
        deepest_inc = max((d for d, _ in inc), default=0)
        deepest_exc = max(d for d, _ in exc)
        return deepest_exc >= deepest_inc


def _glob_anywhere(norm_path: str, pattern: str) -> bool:
    """Handle `**/x/**` and bare-substring patterns that fnmatch misses
    when the pattern is meant to match *anywhere* in the path."""
    if "**" in pattern:
        # Turn **/name/** into a segment containment test.
        core = pattern.strip("*/").strip("/")
        if core and "/" not in core and "*" not in core:
            return f"/{core}/" in f"/{norm_path}/"
    if "*" not in pattern and "?" not in pattern:
        return pattern in norm_path
    # e.g. *.tmp anywhere
    return fnmatch.fnmatch(os.path.basename(norm_path), pattern)


def load_scope(con) -> ScopeResolver:
    """Build a ScopeResolver from the DB `roots` + `exclude_rules` tables."""
    roots = [
        Root(path=r[0], mode=r[1], recursive=bool(r[2]), enabled=bool(r[3]))
        for r in con.execute(
            "SELECT path, mode, recursive, enabled FROM roots"
        ).fetchall()
    ]
    excludes = [
        ExcludeRule(pattern=e[0], enabled=bool(e[1]))
        for e in con.execute(
            "SELECT pattern, enabled FROM exclude_rules"
        ).fetchall()
    ]
    return ScopeResolver(roots, excludes)


def iter_scoped_files(scope: ScopeResolver, supported_ext: set[str],
                      roots: list[Root] | None = None):
    """Walk every enabled include root and yield in-scope image file paths.

    Prunes excluded subtrees during the walk so we never descend into, e.g.,
    a huge node_modules that an exclude pattern would reject anyway.
    """
    seen: set[str] = set()
    for root in roots if roots is not None else scope.enabled_include_roots():
        base = Path(root.path)
        if not base.exists():
            continue
        if base.is_file():
            if _norm(str(base)) not in seen and scope.is_included(str(base)):
                seen.add(_norm(str(base)))
                yield str(base)
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # prune fully-excluded subtrees in-place so we never descend them
            dirnames[:] = [
                d for d in dirnames
                if not scope.dir_pruned(os.path.join(dirpath, d))
            ]
            if not root.recursive:
                dirnames[:] = []
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in supported_ext:
                    continue
                full = os.path.join(dirpath, fn)
                key = _norm(full)
                if key in seen:
                    continue
                if scope.is_included(full):
                    seen.add(key)
                    yield full
