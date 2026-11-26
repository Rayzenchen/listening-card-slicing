"""Forced alignment + ASR cross-check using mlx-whisper large-v3-turbo.

Two independent passes over the same audio:
  1. align():  force-align the user's exact text to the audio -> per-word/char
     timings for every segment (this is what we cut on).
  2. transcribe(): plain ASR of the audio -> the machine's own reading, used to
     check that what we are about to cut really matches the user's text.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

import numpy as np

from . import runtime

DEFAULT_MODEL = os.path.join(runtime.workspace_root(), "models", "whisper-large-v3-turbo")
MODEL_REPO = "mlx-community/whisper-large-v3-turbo"

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}


class ModelError(RuntimeError):
    pass


def model_dir() -> str:
    env = os.environ.get("CUTAUDIO_MODEL_DIR")
    return env if env else DEFAULT_MODEL


def load_model(directory: str | None = None):
    """Load the MLX Whisper model once per process."""
    runtime.apply()
    path = directory or model_dir()
    if not os.path.isdir(path):
        raise ModelError(
            f"model directory missing: {path}. Run tools/setup.sh or restore "
            f"{MODEL_REPO} into models/whisper-large-v3-turbo")
    if not any(f.endswith(".safetensors") for f in os.listdir(path)):
        raise ModelError(f"model directory has no weights (*.safetensors): {path}")
    with _LOCK:
        if path in _CACHE:
            return _CACHE[path]
        _limit_memory()
        import stable_whisper
        try:
            model = stable_whisper.load_mlx_whisper(path)
        except Exception as exc:  # noqa: BLE001
            raise ModelError(f"failed to load model from {path}: {type(exc).__name__}: {exc}") from exc
        _CACHE[path] = model
        return model


def _limit_memory() -> None:
    """Cap MLX's buffer cache so the model coexists with other apps on small Macs.

    Without a limit MLX caches every temporary buffer; on an 8 GB machine that means
    a 1.6 GB model quickly grows to several GB and the OS starts swapping, which
    turns a 3 s alignment into minutes.
    """
    try:
        import mlx.core as mx
        limit = int(os.environ.get("CUTAUDIO_MLX_CACHE_MB", "1024")) * 1024 * 1024
        mx.set_cache_limit(limit)
        mx.set_wired_limit(0) if False else None
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Word:
    text: str
    start: float
    end: float
    prob: float = 0.0


@dataclass
class AlignedSegment:
    index: int
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0


def _audio_array(mono: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(mono, dtype=np.float32)


def align(mono: np.ndarray, segments, language: str, model=None, fast: bool = True) -> tuple[list[AlignedSegment], dict]:
    """Force-align plain text (one entry per segment) to the audio waveform."""
    model = model or load_model()
    text = "\n".join(seg.text for seg in segments)
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = model.align(
            _audio_array(mono),
            text,
            language=language,
            original_split=True,
            fast_mode=fast,
            max_word_dur=None if fast else 3.0,
            verbose=None,
        )
    if result is None:
        raise ModelError("alignment returned nothing (the audio may not contain the given text)")
    out: list[AlignedSegment] = []
    for i, seg in enumerate(segments):
        if i >= len(result.segments):
            raise ModelError(
                f"alignment produced {len(result.segments)} blocks for {len(segments)} text segments; "
                "the number of lines in the text may not match the audio")
        block = result.segments[i]
        words = [Word(w.word.strip(), float(w.start), float(w.end), float(getattr(w, "probability", 0.0) or 0.0))
                 for w in block.words if w.word.strip()]
        if not words:
            raise ModelError(f"segment {seg.index} aligned to zero words (text/audio mismatch?)")
        out.append(AlignedSegment(seg.index, seg.text, words))
    info = {
        "blocks": len(result.segments),
        "segments": len(segments),
        "duration": float(result.duration) if getattr(result, "duration", None) else None,
    }
    return out, info


@dataclass
class AsrWord:
    text: str
    start: float
    end: float


def asr_last_end(asr_words) -> float:
    """End (s) of the latest ASR word; 0.0 when there is no usable timeline."""
    last = 0.0
    for word in asr_words or ():
        try:
            last = max(last, float(word.end))
        except (TypeError, ValueError):
            continue
    return last


def alignment_coverage(duration, asr_words) -> float | None:
    """How much of the ASR word timeline the alignment actually covers.

    ``duration`` is the aligned speech span (edge mode) or the source duration
    (wave mode); the denominator is the end of the last ASR word.  A forced
    aligner that runs out of audio squeezes the whole text into the first
    minutes, so the ratio collapses (measured d02: 1090 s / 1514 s = 0.72) and
    the caller can fail fast instead of writing 140 wrong pieces.
    """
    last = asr_last_end(asr_words)
    if last <= 0.0:
        return None
    return round(float(duration or 0.0) / max(last, 1.0), 3)


def transcribe(mono: np.ndarray, language: str, model=None) -> list[AsrWord]:
    """Plain local ASR pass (no user text involved) for the cross-check."""
    model = model or load_model()
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = model.transcribe(
            _audio_array(mono),
            language=language,
            word_timestamps=True,
            verbose=None,
            condition_on_previous_text=False,
        )
    words: list[AsrWord] = []
    for seg in result.segments:
        for w in getattr(seg, "words", []) or []:
            token = w.word.strip()
            if token:
                words.append(AsrWord(token, float(w.start), float(w.end)))
    return words


def transcribe_windows(mono16: np.ndarray, language: str, model=None,
                       window: float = 120.0, overlap: float = 5.0) -> tuple[list[AsrWord], dict]:
    """ASR with word timestamps in overlapping windows.

    Long-form Whisper word timestamps drift locally (a pause can end up inside the
    previous word), which ruins boundary anchoring.  Transcribing 120 s windows and
    stitching their word lists keeps every timestamp close to the audio it came
    from.  Only words whose midpoint falls in the window core (outside the overlap)
    are kept, so the two sides never duplicate a word.
    """
    model = model or load_model()
    import contextlib, io, time

    total = len(mono16) / 16000.0
    step = max(1.0, window - overlap)
    words: list[AsrWord] = []
    chunks = 0
    t0 = time.time()
    start = 0.0
    while start < total - 1e-6:
        end = min(total, start + window)
        a = int(round(start * 16000))
        b = int(round(end * 16000))
        chunk = _audio_array(mono16[a:b])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            result = model.transcribe(
                chunk,
                language=language,
                word_timestamps=True,
                verbose=None,
                condition_on_previous_text=False,
            )
        core_lo = 0.0 if start <= 0 else overlap / 2.0
        core_hi = (end - start) if end >= total else (end - start - overlap / 2.0)
        for seg in result.segments:
            for w in getattr(seg, "words", []) or []:
                token = w.word.strip()
                if not token:
                    continue
                mid = (float(w.start) + float(w.end)) / 2.0
                if core_lo <= mid < core_hi:
                    words.append(AsrWord(token, start + float(w.start), start + float(w.end)))
        chunks += 1
        start += step
    words.sort(key=lambda w: w.start)
    stats = {"chunks": chunks, "words": len(words), "window_sec": window,
             "overlap_sec": overlap, "elapsed": round(time.time() - t0, 1)}
    return words, stats


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text.lower())


def similar(a: str, b: str) -> float:
    import difflib
    a, b = normalize(a), normalize(b)
    if not a and not b:
        return 1.0
    return float(difflib.SequenceMatcher(None, a, b).ratio())


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")

#: Below this share of the ASR timeline, a forced alignment is too compressed to
#: cut on (the classic symptom of text that does not match the audio, or of a
#: mixed-script transcript sent to the stable-whisper fallback).
COVERAGE_MIN = 0.95


def can_anchor(segments) -> bool:
    """Can the user text ride the independent ASR word timeline?

    Anchoring needs enough space-delimited words to line up; a handful of
    Chinese lines inside an English transcript are harmless because ``anchor()``
    gives those segments an interpolated slot.  Pure Chinese and Chinese-
    dominant text still fall back to the forced aligner, which emits CJK
    character timings itself.
    """
    latin = cjk = 0
    for seg in segments:
        latin += len(_LATIN_WORD_RE.findall(seg.text))
        cjk += len(_CJK_RE.findall(seg.text))
    if latin == 0:
        return False
    return latin * 2 >= cjk


def _anchor_key(token: str) -> str:
    return re.sub(r"[^0-9a-z\u00c0-\u024f]+", "", token.lower())


def anchor(asr_words, segments, duration: float) -> tuple[list[AlignedSegment], dict]:
    """Build word timings for the user text from an independent ASR word timeline.

    stable-ts aligns text in fixed token batches; on long recordings with many
    pauses (CET listening) it consumes text faster than speech and drifts early.
    The ASR pass already required for the cross-check produces a timeline that
    follows the audio, so anchoring to it removes that drift.
    """
    text_words: list[tuple[int, str, str]] = []
    for si, seg in enumerate(segments):
        for token in seg.text.split():
            key = _anchor_key(token)
            if key:
                text_words.append((si, token, key))
    if not text_words or not asr_words:
        raise ModelError("not enough words to anchor (empty text or empty ASR)")

    import difflib

    asr_keys = [_anchor_key(w.text) for w in asr_words]
    total = len(text_words)
    starts: list[float | None] = [None] * total
    ends: list[float | None] = [None] * total

    matcher = difflib.SequenceMatcher(None, [t[2] for t in text_words], asr_keys, autojunk=False)
    matched = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            starts[i1 + k] = float(asr_words[j1 + k].start)
            ends[i1 + k] = float(asr_words[j1 + k].end)
            matched += 1

    durs = sorted(
        ends[k] - starts[k]
        for k in range(total)
        if starts[k] is not None and ends[k] is not None and ends[k] > starts[k]
    )
    med = max(0.05, durs[len(durs) // 2]) if durs else 0.3

    i = 0
    while i < total:
        if starts[i] is not None:
            i += 1
            continue
        j = i
        while j < total and starts[j] is None:
            j += 1
        run = list(range(i, j))
        weights = [max(1, len(text_words[k][2])) for k in run]
        wsum = float(sum(weights))
        prev_end = ends[i - 1] if i > 0 and ends[i - 1] is not None else None
        next_start = starts[j] if j < total and starts[j] is not None else None

        if prev_end is not None and next_start is not None:
            span = next_start - prev_end
            if span <= 0:
                span = med * len(run)
            t = prev_end
            for k, w in zip(run, weights):
                d = span * w / wsum
                starts[k] = t
                ends[k] = t + d
                t += d
        elif prev_end is not None:
            t = prev_end
            for k in run:
                starts[k] = t
                ends[k] = t + med
                t += med
        elif next_start is not None:
            t = next_start - med * len(run)
            for k in run:
                starts[k] = t
                ends[k] = t + med
                t += med
        else:
            t = 0.0
            step = float(duration) / max(1, total)
            for k in run:
                starts[k] = t
                ends[k] = t + step
                t += step
        i = j

    prev_start = -1.0
    prev_end = -1.0
    for k in range(total):
        st = float(starts[k])
        en = float(ends[k])
        if st < prev_end - 0.05:
            st = max(prev_start, prev_end - 0.05)
        if st < prev_start:
            st = prev_start
        if en <= st:
            en = st + 0.02
        starts[k], ends[k] = st, en
        prev_start, prev_end = st, en

    words_by_seg: list[list[Word]] = [[] for _ in segments]
    for k, (si, token, _key) in enumerate(text_words):
        words_by_seg[si].append(Word(token, float(starts[k]), float(ends[k])))

    # ---- zero-token segments (pure CJK / punctuation) get an interpolated slot
    # ``_anchor_key`` strips CJK, so a Chinese line inside an English transcript
    # has no words to anchor.  Such a segment is still a real piece of audio:
    # put one synthetic word in the gap between its word-bearing neighbours, so
    # plan.py can cut around it instead of dropping the line (or aborting).
    empty_segments = 0
    i = 0
    while i < len(segments):
        if words_by_seg[i]:
            i += 1
            continue
        j = i
        while j < len(segments) and not words_by_seg[j]:
            j += 1
        run = list(range(i, j))
        empty_segments += len(run)
        prev_end = float(words_by_seg[i - 1][-1].end) if i > 0 else 0.0
        next_start = float(words_by_seg[j][0].start) if j < len(segments) else float(duration)
        weights = [max(1, len((segments[k].text or "").strip())) for k in run]
        span = next_start - prev_end
        total_min = 0.05 * len(run)
        t = prev_end
        if span >= total_min:
            # Fill the whole gap, each segment at least 0.05 s, the rest split
            # by character count so a long Chinese line gets the bigger slot.
            wsum = float(sum(weights))
            extra = span - total_min
            for k, weight in zip(run, weights):
                d = 0.05 + extra * weight / wsum
                label = (segments[k].text or "").strip()[:1] or "…"
                words_by_seg[k].append(Word(label, t, t + d))
                t += d
        else:
            # The neighbours overlap (or leave no room): keep the piece usable
            # and non-negative with one median word each, per SPEC 5.2.
            for k in run:
                label = (segments[k].text or "").strip()[:1] or "…"
                words_by_seg[k].append(Word(label, t, t + med))
                t += med
        i = j

    aligned: list[AlignedSegment] = []
    for si, seg in enumerate(segments):
        words = words_by_seg[si]
        if not words:                       # unreachable: text_words is non-empty
            raise ModelError(f"segment {seg.index} anchored to zero words (text/audio mismatch?)")
        aligned.append(AlignedSegment(seg.index, seg.text, words))

    info = {
        "mode": "asr_words",
        "blocks": len(aligned),
        "segments": len(segments),
        "duration": round(float(duration), 3),
        "matched": round(matched / total, 3),
        "asr_words": len(asr_words),
        "empty_segments": empty_segments,
    }
    coverage = alignment_coverage(duration, asr_words)
    if coverage is not None:
        info["coverage"] = coverage
    return aligned, info
