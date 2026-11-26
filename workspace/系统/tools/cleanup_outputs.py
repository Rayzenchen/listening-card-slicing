#!/usr/bin/env python
"""Delete process artifacts after the cards pack has been built.

Run this LAST.  It never touches a pack folder (a directory containing
index.json with cards_only=true).  It removes, inside 产出区:

  * <name>_切分结果/          (report-only folder left by the cutter)
  * <name>_切分结果.zip       (full 120-piece archive)
  * <name>_切分结果_review.zip / <name>_review.zip
  * <name>_切分文本.txt       (cut input)
  * <name>_纯卡组.zip         (legacy zip of the pack)
  * <name>_不进卡组/          (legacy skipped pack)
  * <name>_切分清单.md        only if a pack already contains the same file
  * .tmp_* dirs, *.partial, .DS_Store

It also clears regenerable caches (__pycache__, .pytest_cache).

Usage:
  .venv/bin/python tools/cleanup_outputs.py [--dry-run] [--no-caches]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys

WS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AREA = os.path.join(WS, "产出区")

FILE_PATTERNS = ("*_切分结果.zip", "*_切分结果_review.zip", "*_review.zip",
                 "*_切分文本.txt", "*_纯卡组.zip", "*.partial")
DIR_PATTERNS = ("*_切分结果", "*_不进卡组", ".tmp_*")
MANIFEST_PATTERN = "*_切分清单.md"


def pack_dirs():
    """Card packs are recognised by name + at least one wav (no index file)."""
    found = []
    if not os.path.isdir(AREA):
        return found
    for name in os.listdir(AREA):
        path = os.path.join(AREA, name)
        if not os.path.isdir(path):
            continue
        if not (name.endswith("_音频切分（进牌组）") or name.endswith("_纯卡组")):
            continue
        try:
            names = os.listdir(path)
        except OSError:
            continue
        if any(n.lower().endswith(".wav") for n in names):
            found.append(path)
    return found


def collect():
    items = []
    packs = pack_dirs()
    packed_manifests = set()
    for pack in packs:
        for name in os.listdir(pack):
            if fnmatch.fnmatch(name, MANIFEST_PATTERN):
                packed_manifests.add(name)
    if not os.path.isdir(AREA):
        return items, packs
    for name in sorted(os.listdir(AREA)):
        path = os.path.join(AREA, name)
        if any(fnmatch.fnmatch(name, pat) for pat in FILE_PATTERNS) and os.path.isfile(path):
            items.append(path)
        elif any(fnmatch.fnmatch(name, pat) for pat in DIR_PATTERNS) and os.path.isdir(path):
            items.append(path)
        elif fnmatch.fnmatch(name, MANIFEST_PATTERN) and os.path.isfile(path):
            if name in packed_manifests:
                items.append(path)
            else:
                print("keep (no packed copy yet):", path)
        elif name == ".DS_Store" and os.path.isfile(path):
            items.append(path)
    return items, packs


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def cache_paths():
    paths = []
    for sub in ("src", "tests", "tools"):
        root = os.path.join(WS, "系统", sub)
        if not os.path.isdir(root):
            continue
        for cur, dirs, _files in os.walk(root):
            for name in list(dirs):
                if name == "__pycache__":
                    paths.append(os.path.join(cur, name))
                    dirs.remove(name)
    pytest_cache = os.path.join(WS, "系统", ".pytest_cache")
    if os.path.isdir(pytest_cache):
        paths.append(pytest_cache)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-caches", action="store_true")
    args = ap.parse_args()

    items, packs = collect()
    caches = [] if args.no_caches else cache_paths()
    ds_store = os.path.join(WS, ".DS_Store")
    if os.path.isfile(ds_store):
        caches.append(ds_store)

    print("packs kept:", *[os.path.relpath(p, WS) for p in packs], sep="\n  ")
    if not items and not caches:
        print("nothing to clean")
        return 0

    total = 0
    for path in items + caches:
        total += size_of(path)
        print(("would remove" if args.dry_run else "remove"), os.path.relpath(path, WS))
        if not args.dry_run:
            remove(path)
    print("%s %d items, %.1f MB" % ("would free" if args.dry_run else "freed",
                                   len(items) + len(caches), total / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
