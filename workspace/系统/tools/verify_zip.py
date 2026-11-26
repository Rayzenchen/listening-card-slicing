#!/usr/bin/env python3
"""Independent acceptance check for a cutaudio ZIP.

Usage:  .venv/bin/python tools/verify_zip.py <zip> [--source <original audio>]

Checks ZIP integrity, piece order/completeness, decodability, exact sample
geometry (start_sec == start_sample / sample_rate), that pieces tile the whole
source with no gaps or overlaps, that the sample total equals the source, and
(with --source) that every piece is a bit-identical slice of the decoded source.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from cutaudio import audio as A  # noqa: E402


def check(zpath, source=None):
    zf = zipfile.ZipFile(zpath)
    idx = json.loads(zf.read("index.json").decode("utf-8"))
    rep = json.loads(zf.read("report.json").decode("utf-8"))
    names = zf.namelist()
    problems = []
    if zf.testzip() is not None:
        problems.append("corrupt member in ZIP")
    segs = idx["segments"]
    wavs = [n for n in names if n.endswith(".wav")]
    expected = ["%03d.wav" % s["segment"] for s in segs]
    if wavs != expected:
        problems.append("piece names/order wrong: %r" % (wavs,))
    for required in ("index.json", "index.csv", "report.json", "segments.txt", "original_text.txt"):
        if required not in names:
            problems.append("missing " + required)
    sr = idx["sample_rate"]
    total, prev = 0, 0
    src_pcm = None
    if source:
        src = A.decode(source)
        src_pcm = src.pcm
        if src.sample_rate != sr:
            problems.append("sample rate %d != source %d" % (sr, src.sample_rate))
        if src.frames != idx["source_num_samples"]:
            problems.append("index source samples %d, decode %d" % (idx["source_num_samples"], src.frames))
    tmpdir = tempfile.mkdtemp()
    for s in segs:
        if s["start_sample"] != prev:
            problems.append("segment %d starts at %d, expected %d" % (s["segment"], s["start_sample"], prev))
        prev = s["end_sample"]
        total += s["num_samples"]
        if abs(s["start_sec"] - s["start_sample"] / sr) > 1e-6:
            problems.append("segment %d start_sec inconsistent" % s["segment"])
        piece_path = os.path.join(tmpdir, s["file"])
        with open(piece_path, "wb") as fh:
            fh.write(zf.read(s["file"]))
        pcm, rate = A.read_wav(piece_path)
        if rate != sr:
            problems.append("%s: %d Hz" % (s["file"], rate))
        if pcm.shape[0] != s["num_samples"]:
            problems.append("%s: %d samples != %d" % (s["file"], pcm.shape[0], s["num_samples"]))
        if src_pcm is not None and not np.array_equal(pcm, src_pcm[s["start_sample"]:s["end_sample"]]):
            problems.append("%s: not a bit-identical slice of the source" % s["file"])
    if total != idx["source_num_samples"]:
        problems.append("pieces hold %d samples, source has %d" % (total, idx["source_num_samples"]))
    print("ZIP: %s" % zpath)
    print("  segments: %d  entries: %d  status: %s" % (len(segs), len(names), rep["status"]))
    for s in segs:
        print("   %s %.3f-%.3fs [%d:%d] %s" % (s["file"], s["start_sec"], s["end_sec"],
                                               s["start_sample"], s["end_sample"], s["text"]))
    print("  coverage: %d/%d samples exact" % (total, idx["source_num_samples"]))
    for p in problems:
        print("  PROBLEM: " + p)
    print("RESULT: " + ("FAIL" if problems else "PASS"))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--source", default=None, help="original audio, to check bit-identical slices")
    args = ap.parse_args()
    return check(args.zip, args.source)


if __name__ == "__main__":
    sys.exit(main())
