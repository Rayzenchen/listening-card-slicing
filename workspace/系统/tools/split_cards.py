#!/usr/bin/env python
"""Build the cards-only pack from a cutaudio result (directory or ZIP).

The cut manifest (切分清单.md) is the source of truth: rows carry 行号 / 类型 /
进卡组.  Only rows with 进卡组=是 are copied out; the manifest itself is copied
into the pack so the folder is self-describing.

Usage:
  .venv/bin/python tools/split_cards.py --result RESULT.zip --manifest FILE --out DIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import zipfile

ROW_RE = re.compile(r"^\|\s*(\d{3})\s*\|\s*([^|]+?)\s*\|\s*(是|否)\s*\|")


def read_manifest(path):
    rows = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = ROW_RE.match(line)
            if m:
                rows[int(m.group(1))] = {"type": m.group(2).strip(),
                                         "keep": m.group(3) == "是"}
    return rows


def link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True, help="cutaudio result directory or ZIP")
    ap.add_argument("--manifest", required=True, help="切分清单.md")
    ap.add_argument("--out", required=True, help="cards-only output directory")
    args = ap.parse_args()

    rows = read_manifest(args.manifest)
    zf = None
    if os.path.isdir(args.result):
        with open(os.path.join(args.result, "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)

        def sink(name, dst):
            link_or_copy(os.path.join(args.result, name), dst)
    elif zipfile.is_zipfile(args.result):
        zf = zipfile.ZipFile(args.result)
        names = zf.namelist()
        prefix = ""
        if "index.json" not in names:
            found = [n for n in names if n.endswith("/index.json")]
            if not found:
                sys.exit("index.json missing from ZIP: %s" % args.result)
            prefix = found[0][:-len("index.json")]
        index = json.loads(zf.read(prefix + "index.json").decode("utf-8"))

        def sink(name, dst):
            with open(dst, "wb") as fh:
                fh.write(zf.read(prefix + name))
    else:
        sys.exit("result is neither a directory nor a ZIP: %s" % args.result)

    segments = index["segments"]
    if len(rows) != len(segments):
        sys.exit("manifest rows %d != result segments %d" % (len(rows), len(segments)))

    if os.path.isdir(args.out) and os.listdir(args.out):
        sys.exit("output directory exists and is not empty: %s" % args.out)
    os.makedirs(args.out, exist_ok=True)

    kept = []
    for seg in segments:
        n = seg["segment"]
        info = rows.get(n)
        if info is None:
            sys.exit("segment %d missing from manifest" % n)
        if info["keep"]:
            sink(seg["file"], os.path.join(args.out, seg["file"]))
            kept.append({**seg, "type": info["type"]})

    if zf is not None:
        zf.close()

    # The pack is intentionally minimal: audio pieces + the manifest.  No index
    # files are produced (they are not needed for importing the cards).
    shutil.copy2(args.manifest, os.path.join(args.out, os.path.basename(args.manifest)))

    print("kept:", len(kept), "of", len(segments))
    print("cards:", args.out)


if __name__ == "__main__":
    sys.exit(main())
