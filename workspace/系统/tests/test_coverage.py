"""Alignment coverage: pure calculation, CLI guard, and report retention.

No model is needed: the coverage math is a pure function and the CLI guard runs
before any planning, so the report contract can be tested with a fake aligner.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cutaudio import align as AL, cli  # noqa: E402


class Seg:
    def __init__(self, index, text):
        self.index = index
        self.text = text


def test_alignment_coverage_math():
    # The measured d02 failure: forced alignment stopped at 1089.96 s of a
    # 1514 s ASR timeline -> 72%.
    asr = [AL.AsrWord("a", 0.0, 1.0), AL.AsrWord("b", 1400.0, 1514.0)]
    assert AL.alignment_coverage(1089.96, asr) == 0.72
    # A well-matched run covers ~everything.
    assert AL.alignment_coverage(1514.0, asr) == 1.0
    # The denominator never falls below 1.0 s (short clips must not divide by ~0).
    assert AL.alignment_coverage(0.6, [AL.AsrWord("x", 0.0, 0.4)]) == 0.6
    # No usable ASR timeline -> no number (caller skips the guard).
    assert AL.alignment_coverage(1.0, []) is None
    assert AL.alignment_coverage(1.0, [AL.AsrWord("x", 0.0, 0.0)]) is None


def test_anchor_reports_coverage_and_empty_segments():
    asr = [AL.AsrWord("Hello", 0.0, 0.5),
           AL.AsrWord("world", 2.0, 2.5)]
    segs = [Seg(1, "Hello"), Seg(2, "听力考试结束"), Seg(3, "world")]
    _, info = AL.anchor(asr, segs, 2.5)
    assert info["empty_segments"] == 1
    assert info["coverage"] == 1.0          # 2.5 s aligned / 2.5 s ASR timeline


def test_guard_blocks_edge_mode_when_coverage_is_low():
    asr = [AL.AsrWord("a", 0.0, 1.0), AL.AsrWord("b", 1400.0, 1514.0)]
    info = {"mode": "stable_whisper", "cut_mode": "edge", "duration": 1089.96}
    msg = cli._alignment_guard(info, asr)
    assert msg, "edge mode at 72% coverage must refuse to cut"
    assert "72%" in msg
    assert "1090.0s" in msg and "1514.0s" in msg
    assert "do not match" in msg
    assert info["coverage"] == 0.72


def test_guard_allows_edge_mode_when_coverage_is_high():
    asr = [AL.AsrWord("a", 0.0, 1.0), AL.AsrWord("b", 1400.0, 1514.0)]
    info = {"mode": "stable_whisper", "cut_mode": "edge", "duration": 1500.0}
    assert cli._alignment_guard(info, asr) is None
    assert info["coverage"] == pytest.approx(0.991)


def test_guard_never_blocks_wave_mode():
    asr = [AL.AsrWord("a", 0.0, 1.0), AL.AsrWord("b", 1400.0, 1514.0)]
    info = {"mode": "asr_words", "cut_mode": "wave", "duration": 1089.96}
    assert cli._alignment_guard(info, asr) is None       # only a WARN upstream
    assert info["coverage"] == 0.72
    # Wave mode with no ASR timeline still reports a readable number.
    bare = {"mode": "asr_words", "cut_mode": "wave", "duration": 10.0}
    assert cli._alignment_guard(bare, []) is None
    assert bare["coverage"] == 1.0
    # Edge mode without ASR cannot measure coverage -> nothing added, not blocked.
    edge = {"cut_mode": "edge", "duration": 10.0}
    assert cli._alignment_guard(edge, None) is None
    assert "coverage" not in edge


def test_wave_low_match_only_warns():
    msg = cli._wave_match_warning({"cut_mode": "wave", "matched": 0.82})
    assert msg is not None
    assert "82%" in msg and "continues" in msg
    assert cli._wave_match_warning({"cut_mode": "wave", "matched": 0.9}) is None
    assert cli._wave_match_warning({"cut_mode": "wave", "matched": 0.99}) is None
    # Edge mode is governed by the coverage guard, not this warning.
    assert cli._wave_match_warning({"cut_mode": "edge", "matched": 0.4}) is None


def _fake_source(sr=16000, seconds=0.2):
    frames = int(sr * seconds)
    t = np.arange(frames) / sr
    mono = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return SimpleNamespace(sample_rate=sr, channels=1, frames=frames,
                           duration=seconds, codec="pcm_s16le",
                           mono=mono, pcm=mono.reshape(-1, 1))


def test_cli_incomplete_report_keeps_alignment_asr_source(tmp_path, monkeypatch):
    """EXIT_REVIEW must still leave the diagnostic fields in report.json."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")                       # decode is faked below
    text = tmp_path / "t.txt"
    text.write_text("one line\ntwo line\n", encoding="utf-8")
    out = tmp_path / "out"

    asr = [AL.AsrWord("one", 0.0, 0.9), AL.AsrWord("two", 1.0, 1.5)]
    monkeypatch.setattr(cli.audio_mod, "decode", lambda _path: _fake_source())
    monkeypatch.setattr(cli.audio_mod, "check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(cli.align_mod, "load_model", lambda *a, **k: object())
    monkeypatch.setattr(cli.align_mod, "transcribe_windows",
                        lambda *a, **k: (asr, {"chunks": 1, "words": 2}))

    def cannot_anchor(*_args, **_kwargs):
        raise AL.ModelError("cannot anchor this text")

    monkeypatch.setattr(cli.align_mod, "anchor", cannot_anchor)

    fake_aligned = [SimpleNamespace(words=[AL.Word("one", 0.0, 0.9)]),
                    SimpleNamespace(words=[AL.Word("two", 1.0, 1.5)])]
    monkeypatch.setattr(
        cli.align_mod, "align",
        lambda *a, **k: (fake_aligned,
                         {"blocks": 2, "segments": 2, "duration": 0.3,
                          "cut_mode": "edge"}))

    code = cli.main(["--audio", str(audio), "--text", str(text), "--out", str(out)])
    assert code == cli.EXIT_REVIEW
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "needs_review"
    assert report["exit_code"] == cli.EXIT_REVIEW
    assert report["errors"][0]["kind"] == "alignment_incomplete"
    # The report keeps the facts needed to diagnose, not just the error.
    assert report["alignment"]["cut_mode"] == "edge"
    assert report["alignment"]["coverage"] == 0.2     # 0.3 s / 1.5 s
    assert report["asr"]["words"] == 2
    assert report["source"]["duration_sec"] == 0.2
