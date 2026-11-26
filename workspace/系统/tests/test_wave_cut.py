"""Unit tests for the waveform-first cut placement (no model needed)."""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cutaudio import plan as P  # noqa: E402


class W:
    def __init__(self, start, end):
        self.start = start
        self.end = end


def test_wave_cut_prefers_real_pause():
    sr, hop = 16000, 160
    fd = np.full(300, -10.0)
    fd[100:200] = -80.0          # 1.0s - 2.0s is silence
    prev = [W(0.0, 0.2)]
    nxt = [W(2.8, 3.0)]
    sample, meta = P._wave_cut(prev, nxt, np.zeros(sr * 4, dtype=np.float32),
                               fd, hop, sr, -45.0, -80.0)
    assert meta["mode"] == "silence_centre"
    assert meta["evidence"] == 1.0
    assert 1.2 * sr <= sample <= 1.8 * sr
    assert meta["gap_ms"] >= 900


def test_clamp_words_keeps_everything_inside_piece():
    class Seg:
        def __init__(self, words):
            self.words = words

    class Plan:
        def __init__(self, start, end):
            self.start_sample = start
            self.end_sample = end

    sr = 1000
    words = [W(0.05, 0.20), W(0.30, 0.90)]
    aligned = [Seg(words)]
    plans = [Plan(100, 500)]
    P._clamp_words(aligned, plans, sr)
    assert words[0].start >= 100 / sr
    assert words[-1].end <= 500 / sr
