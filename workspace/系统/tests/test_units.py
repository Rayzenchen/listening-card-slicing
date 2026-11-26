"""Unit tests for text parsing, resampling and WAV round-trips."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cutaudio import audio as A, energy  # noqa: E402
from cutaudio.text import TextError, load_text  # noqa: E402


def test_line_segments_skip_blanks(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("\ufefffirst line\n\n  second line  \n\n", encoding="utf-8")
    segs, hint = load_text(str(p))
    assert [s.text for s in segs] == ["first line", "second line"]
    assert hint == "en"


def test_blank_line_segments_join_both_scripts(tmp_path):
    en = tmp_path / "en.txt"
    en.write_text("line one\nline two\n\nsecond para\n", encoding="utf-8")
    segs, _ = load_text(str(en), "blank_line")
    assert [s.text for s in segs] == ["line one line two", "second para"]

    zh = tmp_path / "zh.txt"
    zh.write_text("第一行\n第二行\n\n第二段\n", encoding="utf-8")
    segs, hint = load_text(str(zh), "blank_line")
    assert [s.text for s in segs] == ["第一行第二行", "第二段"]
    assert hint == "zh"


def test_empty_file_raises(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   ", encoding="utf-8")
    with pytest.raises(TextError):
        load_text(str(p))


@pytest.mark.parametrize("sr", [16000, 22050, 44100, 48000])
def test_resample_keeps_pitch_and_duration(sr):
    t = np.arange(sr * 2) / sr
    mono = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    out = A.resample_to(mono, sr)
    assert abs(len(out) / 16000 - 2.0) < 0.01
    spectrum = np.abs(np.fft.rfft(out[8000:24000]))
    peak_hz = np.argmax(spectrum) * 16000 / 16000
    assert abs(peak_hz - 440) <= 15


def test_wav_float32_roundtrip_bit_exact(tmp_path):
    rng = np.random.default_rng(0)
    pcm = (rng.standard_normal((1000, 2)) * 0.1).astype(np.float32)
    p = tmp_path / "x.wav"
    A.write_wav_f32(str(p), pcm, 44100)
    back, rate = A.read_wav(str(p))
    assert rate == 44100 and back.shape == pcm.shape
    assert np.array_equal(back, pcm)


def test_envelope_edges_on_synthetic_pause():
    sr = 16000
    speech = (0.3 * np.sin(2 * np.pi * 200 * np.arange(int(sr * 0.5)) / sr)).astype(np.float32)
    gap = np.zeros(int(sr * 0.5), dtype=np.float32)
    mono = np.concatenate([speech, gap, speech])
    fd, hop = energy.frame_energy(mono, sr)
    thr = energy.speech_threshold_db(fd)
    floor = energy.noise_floor_db(fd)
    off = energy.refine_offset(fd, hop, thr, floor, int(sr * 0.45), sr, mono=mono)
    on = energy.refine_onset(fd, hop, thr, floor, int(sr * 1.05), sr, mono=mono)
    assert abs(off - int(sr * 0.5)) < int(0.03 * sr)
    assert abs(on - int(sr * 1.0)) < int(0.03 * sr)
    centre, q = energy.silence_centre(mono, off, on, sr, fd, hop, thr, floor)
    assert abs(centre - int(sr * 0.75)) < int(0.01 * sr)
    assert q > 0.9
