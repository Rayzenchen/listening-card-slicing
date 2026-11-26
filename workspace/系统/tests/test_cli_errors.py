"""Error paths and contract of the cutaudio CLI (no model needed).

Run: .venv/bin/python -m pytest tests/test_cli_errors.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUT = os.path.join(ROOT, "tools", "cut")


def run(*args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([CUT, *args], capture_output=True, text=True, env=env)


def _wav(path, seconds=0.2, sr=16000):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import numpy as np
    from cutaudio import audio as A
    t = np.arange(int(sr * seconds)) / sr
    A.write_wav_16(str(path), (0.2 * np.sin(2 * np.pi * 440 * t) * (t < 0.1)).astype("float32"), sr)
    return str(path)


def test_missing_audio_exits_2(tmp_path):
    text = tmp_path / "t.txt"
    text.write_text("hello\n", encoding="utf-8")
    proc = run("--audio", str(tmp_path / "nope.wav"), "--text", str(text), "--out", str(tmp_path / "o"))
    assert proc.returncode == 2
    assert "audio file not found" in proc.stderr


def test_missing_text_exits_2(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    proc = run("--audio", wav, "--text", str(tmp_path / "nope.txt"), "--out", str(tmp_path / "o"))
    assert proc.returncode == 2
    assert "text file not found" in proc.stderr


def test_empty_text_exits_1_and_writes_report(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    text = tmp_path / "empty.txt"
    text.write_text("  \n\n", encoding="utf-8")
    out = tmp_path / "o"
    proc = run("--audio", wav, "--text", str(text), "--out", str(out))
    assert proc.returncode == 1
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "error" and report["errors"]
    assert report["errors"][0]["kind"] == "input_error"


def test_existing_dir_is_never_touched(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    text = tmp_path / "t.txt"
    text.write_text("hello world\n", encoding="utf-8")
    out = tmp_path / "o"
    out.mkdir()
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    proc = run("--audio", wav, "--text", str(text), "--out", str(out))
    assert proc.returncode == 2
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]
    assert (out / "keep.txt").read_text() == "keep"


def test_existing_zip_is_refused_up_front(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    text = tmp_path / "t.txt"
    text.write_text("hello world\n", encoding="utf-8")
    out = tmp_path / "o"
    (tmp_path / "o.zip").write_bytes(b"not really a zip")
    proc = run("--audio", wav, "--text", str(text), "--out", str(out))
    assert proc.returncode == 2
    assert (tmp_path / "o.zip").read_bytes() == b"not really a zip"
    assert not out.exists()


def test_corrupt_audio_is_an_error(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFFxxxxWAVEjunkjunk")
    text = tmp_path / "t.txt"
    text.write_text("hello\n", encoding="utf-8")
    out = tmp_path / "o"
    proc = run("--audio", str(bad), "--text", str(text), "--out", str(out))
    assert proc.returncode in (1, 2)
    assert "audio" in proc.stderr.lower() or "no audio stream" in proc.stderr.lower()


def test_unsupported_extension_message(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    text = tmp_path / "t.txt"
    text.write_text("hello\n", encoding="utf-8")
    proc = run("--audio", str(f), "--text", str(text), "--out", str(tmp_path / "o"))
    assert proc.returncode in (1, 2)
    assert "extension" in proc.stderr.lower()
