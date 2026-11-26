"""Produce deliverables: exact WAV pieces, index files, ZIP; or review previews.

Nothing is left behind that looks like a finished product unless every check
passed:
  * pieces are written into a temp folder first, verified, then moved into the
    result directory
  * on any error the result directory is removed
  * on needs_review only a tiny preview pack (short listens + report) is produced
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import asdict

import numpy as np

from .audio import AudioError, read_wav, write_wav_f32
from .plan import Analysis


def piece_name(index: int, language: str = "en") -> str:
    return f"{index:03d}.wav"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def index_rows(analysis: Analysis, sample_rate: int, channels: int, files: dict[int, str]) -> list[dict]:
    rows = []
    for plan in analysis.plans:
        rows.append({
            "segment": plan.index,
            "text": plan.text,
            "start_sec": round(plan.start_sample / sample_rate, 6),
            "end_sec": round(plan.end_sample / sample_rate, 6),
            "duration_sec": round((plan.end_sample - plan.start_sample) / sample_rate, 6),
            "start_sample": plan.start_sample,
            "end_sample": plan.end_sample,
            "num_samples": plan.end_sample - plan.start_sample,
            "file": os.path.basename(files[plan.index]),
            "sample_rate": sample_rate,
            "channels": channels,
            "flags": plan.flags,
            "cut_in": plan.cut_in,
            "cut_out": plan.cut_out,
            "voiced_sec": plan.voiced_sec,
        })
    return rows


def write_pieces(analysis: Analysis, pcm: np.ndarray, sample_rate: int, folder: str) -> dict[int, str]:
    files: dict[int, str] = {}
    for plan in analysis.plans:
        path = os.path.join(folder, piece_name(plan.index))
        write_wav_f32(path, pcm[plan.start_sample:plan.end_sample], sample_rate)
        files[plan.index] = path
    return files


def verify_pieces(analysis: Analysis, pcm: np.ndarray, sample_rate: int, files: dict[int, str]) -> dict:
    """Re-read every piece: decodable, exact geometry, bit-identical to the source slice."""
    total = 0
    problems = []
    for plan in analysis.plans:
        got, rate = read_wav(files[plan.index])
        total += got.shape[0]
        if rate != sample_rate:
            problems.append(f"{plan.index}: sample rate {rate} != {sample_rate}")
        if got.shape[1] != pcm.shape[1]:
            problems.append(f"{plan.index}: channels {got.shape[1]} != {pcm.shape[1]}")
        expected = pcm[plan.start_sample:plan.end_sample]
        if got.shape[0] != expected.shape[0]:
            problems.append(f"{plan.index}: {got.shape[0]} samples, expected {expected.shape[0]}")
        elif not np.array_equal(got, expected):
            problems.append(f"{plan.index}: samples differ from source slice")
    cov_ok = total == pcm.shape[0]
    if not cov_ok:
        problems.append(f"piece samples {total} != source samples {pcm.shape[0]}")
    return {"ok": not problems, "problems": problems, "total_samples": int(total),
            "source_samples": int(pcm.shape[0]), "coverage_exact": bool(cov_ok)}


def write_text_sidecars(analysis: Analysis, text_path: str, folder: str) -> dict[str, str]:
    paths = {}
    numbered = os.path.join(folder, "segments.txt")
    with open(numbered, "w", encoding="utf-8") as fh:
        for plan in analysis.plans:
            fh.write(plan.text + "\n")
    paths["segments"] = numbered
    orig = os.path.join(folder, "original_text.txt")
    shutil.copyfile(text_path, orig)
    paths["original"] = orig
    return paths


def write_json(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def build_zip(root: str, base_name: str, src_dir: str, entries: list[str]) -> str:
    """Deterministic ZIP (sorted entries) at root/base_name - never overwrites a file."""
    zip_path = os.path.join(root, base_name)
    if os.path.exists(zip_path):
        raise FileExistsError(f"refusing to overwrite an existing ZIP: {zip_path}")
    tmp = zip_path + ".partial"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in sorted(entries):
            zf.write(os.path.join(src_dir, name), arcname=name)
    os.replace(tmp, zip_path)
    return zip_path


def verify_zip(zip_path: str, analysis: Analysis) -> dict:
    problems = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad:
            problems.append(f"corrupt member: {bad}")
        expected = [piece_name(p.index) for p in analysis.plans]
        got = [n for n in names if n.endswith(".wav")]
        if got != expected:
            problems.append(f"piece order/coverage mismatch: {got[:5]}... vs {expected[:5]}...")
        for required in ("index.json", "index.csv", "report.json", "segments.txt", "original_text.txt"):
            if required not in names:
                problems.append(f"missing {required}")
        if zf.getinfo("index.json").file_size <= 2:
            problems.append("index.json is empty")
    return {"ok": not problems, "problems": problems, "entries": len(names)}


def render_preview(analysis: Analysis, pcm: np.ndarray, sample_rate: int, folder: str,
                   indexes: list[int]) -> list[str]:
    """Small review listens: the cut region with a short context around it."""
    made = []
    ctx = int(0.45 * sample_rate)
    for idx in indexes:
        plan = next((p for p in analysis.plans if p.index == idx), None)
        if plan is None:
            continue
        a = max(0, plan.start_sample - ctx)
        b = min(pcm.shape[0], plan.end_sample + ctx)
        path = os.path.join(folder, f"preview_{idx:03d}.wav")
        write_wav_f32(path, pcm[a:b], sample_rate)
        made.append(os.path.basename(path))
    return made
