"""Unit tests for ASR-anchored alignment (no model needed)."""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cutaudio import align as AL  # noqa: E402


class Seg:
    def __init__(self, index, text):
        self.index = index
        self.text = text


def test_anchor_maps_text_to_asr_words():
    asr = [AL.AsrWord("Hello", 0.0, 0.4), AL.AsrWord("world", 0.5, 0.9),
           AL.AsrWord("again", 1.4, 1.8)]
    segs = [Seg(1, "Hello world"), Seg(2, "again")]
    aligned, info = AL.anchor(asr, segs, 2.0)
    assert [w.text for w in aligned[0].words] == ["Hello", "world"]
    assert aligned[0].words[0].start == 0.0
    assert aligned[0].words[1].end == 0.9
    assert aligned[1].words[0].start == 1.4
    assert info["matched"] == 1.0


def test_anchor_interpolates_unmatched_words():
    asr = [AL.AsrWord("Hello", 0.0, 0.4), AL.AsrWord("world", 0.8, 1.2)]
    aligned, info = AL.anchor(asr, [Seg(1, "Hello brave world")], 1.5)
    words = aligned[0].words
    assert [w.text for w in words] == ["Hello", "brave", "world"]
    assert abs(words[1].start - 0.4) < 1e-6
    assert abs(words[1].end - 0.8) < 1e-6
    assert words[1].start < words[1].end
    assert info["matched"] == 0.667


def test_can_anchor_requires_latin_majority():
    # Pure Chinese keeps the forced-aligner fallback (old contract).
    assert AL.can_anchor([Seg(1, "图书馆")]) is False
    assert AL.can_anchor([Seg(1, "图书馆里很安静")]) is False
    assert AL.can_anchor([Seg(1, "hello there")]) is True
    # d02 shape: English-dominant transcript with a few Chinese lines -> anchor.
    assert AL.can_anchor([
        Seg(1, "In this part, there are two passages. 听力考试现在开始。"),
        Seg(2, "Questions 1 to 3 are based on the passage you have just heard."),
    ]) is True
    # Chinese-dominant text with one stray English word still falls back.
    assert AL.can_anchor([Seg(1, "今天我们学习一篇关于 climate 的课文")]) is False


def test_anchor_pure_cjk_segment_gets_slot_between_neighbours():
    asr = [AL.AsrWord("Hello", 0.0, 0.4), AL.AsrWord("world", 0.5, 0.9),
           AL.AsrWord("again", 1.4, 1.8), AL.AsrWord("now", 1.9, 2.3)]
    segs = [Seg(1, "Hello world"), Seg(2, "听力考试结束"), Seg(3, "again now")]
    aligned, info = AL.anchor(asr, segs, 2.5)
    assert info["empty_segments"] == 1
    assert len(aligned[1].words) == 1                 # synthetic word so plan can cut
    slot = aligned[1].words[0]
    assert slot.start < slot.end
    assert 0.9 <= slot.start and slot.end <= 1.4      # inside the neighbours' gap
    assert aligned[0].words[-1].end <= slot.start     # no overlap on the left
    assert slot.end <= aligned[2].words[0].start      # no overlap on the right


def test_anchor_splits_consecutive_empty_segments_by_chars():
    asr = [AL.AsrWord("Alpha", 0.0, 0.5), AL.AsrWord("Beta", 2.0, 2.5)]
    segs = [Seg(1, "Alpha"), Seg(2, "图书馆"), Seg(3, "听力"), Seg(4, "Beta")]
    aligned, info = AL.anchor(asr, segs, 2.5)
    assert info["empty_segments"] == 2
    a, b = aligned[1].words[0], aligned[2].words[0]
    # Both slots exactly tile the 0.5s -> 2.0s gap, 3-char line gets the bigger one.
    assert a.start == pytest.approx(0.5)
    assert a.end == pytest.approx(b.start)
    assert b.end == pytest.approx(2.0)
    assert (a.end - a.start) == pytest.approx(0.05 + 1.40 * 3 / 5)
    assert (b.end - b.start) == pytest.approx(0.05 + 1.40 * 2 / 5)
    assert (a.end - a.start) > (b.end - b.start)


def test_anchor_empty_segments_at_both_edges():
    asr = [AL.AsrWord("Hello", 0.6, 1.0), AL.AsrWord("world", 1.1, 1.5)]
    segs = [Seg(1, "听力考试开始"), Seg(2, "Hello world"), Seg(3, "听力考试结束")]
    aligned, info = AL.anchor(asr, segs, 2.0)
    assert info["empty_segments"] == 2
    head = aligned[0].words[-1]
    tail = aligned[2].words[0]
    assert 0.0 <= head.start < head.end <= 0.6
    assert 1.5 <= tail.start < tail.end <= 2.0
