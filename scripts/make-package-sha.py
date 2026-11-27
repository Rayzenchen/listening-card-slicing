#!/usr/bin/env python3
"""Regenerate skills/listening-card-slicing/PACKAGE.sha256 deterministically.

Usage::

    python3 scripts/make-package-sha.py              # write PACKAGE.sha256
    python3 scripts/make-package-sha.py --check      # recompute + compare, never write
    python3 scripts/make-package-sha.py --root DIR   # operate on a repo copy

The manifest is the 7-line 0.1.0 header followed by one line per file under
``skills/listening-card-slicing``::

    <sha256>  <path relative to the skill directory>

Included: every regular file except ``PACKAGE.sha256`` itself, anything under
``__pycache__/``, ``*.pyc`` files and ``.DS_Store``.  Entries are sorted by the
UTF-8 bytes of the relative path (C locale order), not by host locale.

``source_scripts_tree_sha`` is the SHA-256 of the newline-terminated
``<sha256>  <path>`` lines whose path starts with ``scripts/`` (an empty set
hashes the empty byte string).  That keeps the header stable when only docs or
roles change, while still tracking every shipped script byte.

Exit codes: 0 ok / 1 --check found a difference / 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_REL = Path("skills") / "listening-card-slicing"
MANIFEST_NAME = "PACKAGE.sha256"
SKILL_NAME = "listening-card-slicing"
CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_version(skill_dir: Path) -> str:
    version_path = skill_dir / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (version_path, exc)) from exc
    if not version:
        raise ValueError("%s is empty" % version_path)
    return version


def _collect(skill_dir: Path) -> list[tuple[str, str]]:
    """Return ``[(relpath, sha256), ...]`` in C-locale (UTF-8 byte) order."""
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        # Prune these at the directory level so nested caches never show up.
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(skill_dir).as_posix()
            if rel == MANIFEST_NAME or name == ".DS_Store" or name.endswith(".pyc"):
                continue
            if not path.is_file():
                continue
            entries.append((rel, _sha256_file(path)))
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    return entries


def _render(skill_dir: Path) -> tuple[str, list[tuple[str, str]], str]:
    version = _read_version(skill_dir)
    entries = _collect(skill_dir)
    entry_lines = ["%s  %s" % (digest, rel) for rel, digest in entries]

    scripts_text = "".join(
        line + "\n"
        for line, (rel, _digest) in zip(entry_lines, entries)
        if rel.startswith("scripts/")
    )
    source_scripts_tree_sha = hashlib.sha256(scripts_text.encode("utf-8")).hexdigest()

    header = [
        "# PACKAGE.sha256 — " + SKILL_NAME,
        "# VERSION: " + version,
        "# source: GitHub distribution (" + SKILL_NAME + " workbench)",
        "# source_scripts_tree_sha: " + source_scripts_tree_sha,
        "# install_note: %s GitHub package: runtime skill + scripts only; "
        "AI Studio transcription SOP/prompts/scripts, 材料区 transcripts, and "
        "transcript-derived tests/fixtures are intentionally removed. "
        "2 skills; no model/venv/assets." % version,
        "# excluded: _build/ _report/ tests/ tests/reports/ __pycache__/ *.pyc "
        ".pytest_cache/ .DS_Store .git/",
        "# self-check: shasum -a 256 -c PACKAGE.sha256   "
        "(PACKAGE.sha256 itself is not listed)",
    ]
    text = "".join(line + "\n" for line in header + entry_lines)
    return text, entries, source_scripts_tree_sha


def _parse_entries(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "  " not in line:
            continue
        digest, rel = line.split("  ", 1)
        rows[rel] = digest
    return rows


def _show_paths(paths: list[str], limit: int = 8) -> str:
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += ", ... (+%d more)" % (len(paths) - limit)
    return shown


def _check(manifest_path: Path, expected: str, entries: list[tuple[str, str]]) -> int:
    if not manifest_path.exists():
        print("CHECK FAILED: %s is missing" % manifest_path, file=sys.stderr)
        return 1

    expected_bytes = expected.encode("utf-8")
    try:
        actual_bytes = manifest_path.read_bytes()
    except OSError as exc:
        print("ERROR: cannot read %s: %s" % (manifest_path, exc), file=sys.stderr)
        return 2

    if actual_bytes == expected_bytes:
        print("OK: %s is up to date (%d entries)" % (manifest_path, len(entries)))
        return 0

    print("CHECK FAILED: %s is stale (run without --check to regenerate)" % manifest_path,
          file=sys.stderr)
    actual_text = actual_bytes.decode("utf-8", "replace")
    expected_lines = expected.splitlines()
    actual_lines = actual_text.splitlines()

    for index in range(7):
        want = expected_lines[index] if index < len(expected_lines) else "<missing>"
        got = actual_lines[index] if index < len(actual_lines) else "<missing>"
        if want != got:
            print("  header line %d differs:" % (index + 1), file=sys.stderr)
            print("    expected: %s" % want, file=sys.stderr)
            print("    found:    %s" % got, file=sys.stderr)

    actual_rows = _parse_entries(actual_text)
    expected_rows = _parse_entries(expected)
    changed = sorted(rel for rel in expected_rows.keys() & actual_rows.keys()
                     if expected_rows[rel] != actual_rows[rel])
    missing = sorted(expected_rows.keys() - actual_rows.keys())
    extra = sorted(actual_rows.keys() - expected_rows.keys())
    print("  entries: %d expected / %d listed" % (len(expected_rows), len(actual_rows)),
          file=sys.stderr)
    if changed:
        print("  changed: %s" % _show_paths(changed), file=sys.stderr)
    if missing:
        print("  missing: %s" % _show_paths(missing), file=sys.stderr)
    if extra:
        print("  extra:   %s" % _show_paths(extra), file=sys.stderr)
    if not (changed or missing or extra):
        print("  the entry set matches; only header/format bytes differ", file=sys.stderr)
    return 1


def _write(manifest_path: Path, expected: str, entries: list[tuple[str, str]],
           source_scripts_tree_sha: str) -> int:
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    try:
        tmp_path.write_bytes(expected.encode("utf-8"))
        os.replace(tmp_path, manifest_path)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        print("ERROR: cannot write %s: %s" % (manifest_path, exc), file=sys.stderr)
        return 2
    print("wrote %s" % manifest_path)
    print("  entries: %d" % len(entries))
    print("  source_scripts_tree_sha: %s" % source_scripts_tree_sha)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate skills/listening-card-slicing/PACKAGE.sha256.")
    parser.add_argument("--check", action="store_true",
                        help="recompute and compare with the existing file; never write")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo root (default: parent of this script's directory)")
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve() if args.root else REPO_ROOT
    skill_dir = root / SKILL_REL
    if not skill_dir.is_dir():
        print("ERROR: skill directory not found: %s" % skill_dir, file=sys.stderr)
        return 2

    try:
        expected, entries, source_sha = _render(skill_dir)
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    manifest_path = skill_dir / MANIFEST_NAME
    if args.check:
        return _check(manifest_path, expected, entries)
    return _write(manifest_path, expected, entries, source_sha)


if __name__ == "__main__":
    sys.exit(main())
