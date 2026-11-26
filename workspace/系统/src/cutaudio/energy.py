"""Waveform/energy analysis: thresholds, speech regions, edge refinement, cut points."""
from __future__ import annotations

import numpy as np

FRAME_MS = 10.0
EDGE_DB = 15.0            # a speech edge must clear the noise floor by this much
SILENCE_ABOVE_FLOOR = 6.0 # frames this close to the noise floor count as silence
EDGE_BELOW_SPEECH = 18.0  # an edge is where the 50 ms envelope is this far below speech level


def frame_energy(mono: np.ndarray, sr: int, frame_ms: float = FRAME_MS) -> tuple[np.ndarray, int]:
    """RMS energy in dBFS per frame (10 ms default)."""
    hop = max(1, int(round(sr * frame_ms / 1000.0)))
    n_frames = int(np.ceil(len(mono) / hop))
    if n_frames == 0:
        return np.zeros(0), hop
    pad = n_frames * hop - len(mono)
    x = np.pad(mono, (0, pad)) if pad else mono
    frames = x.reshape(n_frames, hop).astype(np.float64)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    return 20.0 * np.log10(rms), hop


def speech_threshold_db(energy: np.ndarray) -> float:
    """Adaptive silence/speech threshold, robust to quiet recordings."""
    if energy.size == 0:
        return -60.0
    floor = float(np.percentile(energy, 10))
    peak = float(np.percentile(energy, 95))
    if peak - floor < 12.0:
        return floor + max(4.0, (peak - floor) * 0.4)
    return float(max(floor + 10.0, min(peak - 12.0, -45.0)))


def voiced_mask(energy: np.ndarray, thr_db: float, min_gap_ms: float = 60.0) -> np.ndarray:
    """Boolean speech mask with small holes closed (breaths/pauses inside a word)."""
    mask = energy > thr_db
    gap_frames = max(1, int(round(min_gap_ms / FRAME_MS)))
    i, n = 0, mask.size
    while i < n:
        if not mask[i]:
            j = i
            while j < n and not mask[j]:
                j += 1
            if 0 < i and j < n and (j - i) < gap_frames:
                mask[i:j] = True
            i = j
        else:
            i += 1
    return mask


def speech_regions(mask: np.ndarray, hop: int, min_len_frames: int = 3) -> list:
    """Contiguous voiced regions as (start_sample, end_sample) pairs."""
    regions = []
    n = mask.size
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_len_frames:
                regions.append((i * hop, j * hop))
            i = j
        else:
            i += 1
    return regions


def edge_frames(frame_db: np.ndarray, thr_db: float, floor_db: float) -> np.ndarray:
    """Frames that really hold speech energy (clearly above the noise floor)."""
    limit = max(thr_db, floor_db + EDGE_DB)
    return frame_db > limit


def _hysteresis_sample(mono: np.ndarray, frame_db: np.ndarray, hop: int, thr_db: float,
                       floor_db: float, coord: int, direction: int, sr: int,
                       reach_ms: float = 300.0) -> int:
    """Sample-accurate edge by hysteresis search around a rough sample position.

    The 10 ms envelope locates the edge to within a frame or two; from there we
    scan the raw waveform outwards and stop at the outermost sample whose 3 ms RMS
    still crosses the edge level.  direction +1 = end of a sound, -1 = beginning.
    The scan is bounded symmetrically around coord, so it can never run away.
    """
    # Always measure over a fixed 250 ms window on both sides of the rough edge, so
    # the reference "speech level" is the same whether the aligner was early or late.
    reach = int(sr * 0.25)
    if direction > 0:
        lo, hi = max(0, coord - int(0.05 * sr)), min(len(mono), coord + reach)
    else:
        lo, hi = max(0, coord - reach), min(len(mono), coord + int(0.05 * sr))
    if hi - lo < 16:
        return coord
    seg = np.abs(mono[lo:hi]).astype(np.float64)
    # Use a ~50 ms RMS envelope of the waveform, not single samples: one stray click
    # must not count as speech.  The level must be near speech level (wideband RMS
    # reads ~15 dB below the frame peak), with the noise floor as a hard minimum, and
    # always well above digital zero - otherwise the dither left after a decay tail
    # keeps "speech" alive for another 100 ms and the pause is measured too short.
    win = max(4, int(sr * 0.050))
    if seg.size < win:
        return coord
    env = np.sqrt(np.convolve(seg ** 2, np.ones(win) / win, mode="same"))
    # Speech level is taken from the loudest ~40 ms window around the rough edge
    # (TTS material often carries a quiet -70 dB "tail" after the last phoneme that
    # must not be counted as speech).
    loud = float(np.percentile(env[env > np.percentile(env, 50)], 90)) if np.any(env > np.percentile(env, 50)) else float(np.percentile(env, 95))
    speech_db = 20.0 * np.log10(max(loud, 1e-9))
    level_db = max(speech_db - EDGE_BELOW_SPEECH, floor_db + 30.0, -85.0)
    idx = np.nonzero(env > 10 ** (level_db / 20.0))[0]
    if idx.size == 0:
        return coord
    return lo + int(idx[-1]) + 1 if direction > 0 else lo + int(idx[0])


def refine_offset(frame_db: np.ndarray, hop: int, thr_db: float, floor_db: float,
                  pos: int, sr: int, window_ms: float = 300.0, mono=None) -> int:
    """Sample just after the last speech-energy frame near pos (tail of a sound)."""
    if mono is not None:
        return _hysteresis_sample(mono, frame_db, hop, thr_db, floor_db, pos, +1, sr, window_ms)
    ef = edge_frames(frame_db, thr_db, floor_db)
    lo = max(0, (pos - int(sr * window_ms / 1000.0)) // hop)
    hi = min(len(ef), pos // hop + 1)
    if hi <= lo:
        return pos
    idx = np.nonzero(ef[lo:hi])[0]
    return (lo + int(idx[-1]) + 1) * hop if idx.size else pos


def refine_onset(frame_db: np.ndarray, hop: int, thr_db: float, floor_db: float,
                 pos: int, sr: int, window_ms: float = 300.0, mono=None) -> int:
    """Sample where speech energy first appears near pos (start of a sound)."""
    if mono is not None:
        return _hysteresis_sample(mono, frame_db, hop, thr_db, floor_db, pos, -1, sr, window_ms)
    ef = edge_frames(frame_db, thr_db, floor_db)
    lo = max(0, pos // hop)
    hi = min(len(ef), (pos + int(sr * window_ms / 1000.0)) // hop)
    if hi <= lo:
        return pos
    idx = np.nonzero(ef[lo:hi])[0]
    return (lo + int(idx[0])) * hop if idx.size else pos

def silence_centre(mono: np.ndarray, a: int, b: int, sr: int, frame_db: np.ndarray, hop: int,
                   thr_db: float, floor_db: float = -120.0) -> tuple:
    """Pick the cut sample inside gap [a, b).

    Returns (sample, quality 0..1).  Prefers the middle of the longest run of
    quiet AND flat frames, so the cut sits in the stable centre of a real pause.
    If the gap holds no quiet frame at all, falls back to the quietest point.
    """
    a = max(0, a)
    b = min(len(mono), b)
    if b - a < 4:
        return (a + b) // 2, 0.0
    f0 = a // hop
    f1 = max(f0 + 1, b // hop)
    chunk = frame_db[f0:f1]
    # "Quiet" means quiet relative to speech AND to the recording's own noise floor.
    # Real files keep a residual hiss that sits far below speech but above -inf; a
    # strict < speech-threshold test would fragment a perfectly good pause into
    # ragged pieces and pull the cut off-centre.
    quiet = chunk < max(thr_db, floor_db + SILENCE_ABOVE_FLOOR)
    best = None
    i, n = 0, len(chunk)
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            length = j - i
            lo, hi = max(i, 1), min(j, n - 1)
            spread = float(chunk[lo:hi].std()) if hi > lo else 99.0
            centre = int((f0 + (i + j) / 2.0) * hop)
            # Land on the quietest 5 ms block within +-5 ms of the frame centre: the
            # 10 ms frame grid alone would bias the cut by up to half a frame, and a
            # pause can be a few dB quieter on one side of its middle.
            centre = _nudge_to_quietest(mono, centre, min(hop // 2, int(sr * 0.005) * 2),
                                       max(2, int(sr * 0.005)))
            centre = min(max(centre, a), b - 1)
            # Prefer: long quiet run > flat run > run whose centre is close to the
            # centre of the whole gap.  Near-silence is never perfectly flat, so
            # without the third term a slightly quieter patch at the start of a
            # pause wins and the cut drifts out of the true middle.
            mid_gap = (a + b) / 2.0
            # When the run already spans the whole search range, its middle IS the
            # middle of the pause - no tie-breaking needed (and the fallback "closest
            # to gap middle" term would otherwise pull the cut off by half a frame).
            if i == 0 and j >= n:
                centre = int(round(mid_gap))
                return min(max(centre, a), b - 1), 1.0
            closeness = 1.0 - min(1.0, abs(centre - mid_gap) / max(1.0, (b - a) / 2.0))
            score = length - spread * 0.03 + closeness * max(4.0, length * 0.5)
            if best is None or score > best[0]:
                best = (score, centre, length, spread)
            i = j
        else:
            i += 1
    if best is None:
        idx = int(np.argmin(chunk)) if n else 0
        centre = min(max((f0 + idx) * hop, a), b - 1)
        return centre, 0.0
    _score, centre, length, spread = best
    gap_ms = (b - a) / sr * 1000.0
    quality = min(1.0, (length * FRAME_MS) / max(gap_ms, 1.0)) * (1.0 if spread < 6.0 else 0.6)
    return centre, float(quality)


def voiced_frames(frame_db: np.ndarray, a: int, b: int, hop: int, thr_db: float) -> float:
    """Seconds of voiced audio inside [a, b)."""
    f0 = max(0, a // hop)
    f1 = max(f0 + 1, min(len(frame_db), max(f0 + 1, b // hop)))
    return float(np.sum(frame_db[f0:f1] > thr_db)) * FRAME_MS / 1000.0


def noise_floor_db(frame_db: np.ndarray) -> float:
    """Robust noise floor.

    Quiet files decode to exact zeros even though the source had dither, so the
    10th percentile is -inf.  The median of frames that are not digital silence is
    the better proxy for the recorder's own hiss and keeps "is this silence?"
    meaningful.
    """
    if not frame_db.size:
        return -120.0
    # Digital silence (-inf) is not a meaningful floor, but it does tell us the file
    # itself is very quiet, in which case a fixed "anything below X dB is silence"
    # rule is already safe.
    real = frame_db[np.isfinite(frame_db)]
    if real.size < frame_db.size * 0.05:
        return -120.0
    low = float(np.percentile(real, 10))
    peak = float(np.percentile(frame_db, 95))
    # Never let the floor come within 25 dB of the loud parts: a recording without
    # pause must not turn half of its quiet speech into "silence".
    return min(low, peak - 25.0)

def _digital_silence_run(mono: np.ndarray, coord: int, sr: int, direction: int,
                         reach: float = 0.5, max_level: float = 1e-3) -> int:
    """Start (direction +1) or end (-1) of the digital-silence run around coord.

    Inside a real pause a digital recording holds exact zeros (or the dither floor,
    well under -60 dBFS), so this is the unambiguous heart of the pause.  Returns
    coord unchanged when no such run covers it.
    """
    lo = max(0, coord - int(reach * sr))
    hi = min(len(mono), coord + int(reach * sr))
    if hi - lo < 8:
        return coord
    quiet = np.abs(mono[lo:hi]) <= max_level
    c = min(max(coord - lo, 0), hi - lo - 1)
    if not quiet[c]:
        return coord
    i = c
    while i > 0 and quiet[i - 1]:
        i -= 1
    j = c
    while j < hi - lo - 1 and quiet[j + 1]:
        j += 1
    # +1 = end of the run (speech resumes), -1 = start of the run (speech stops)
    return lo + j + 1 if direction > 0 else lo + i


def digital_silence_start(mono: np.ndarray, coord: int, sr: int) -> int:
    """Where digital silence begins (speech stops) around coord."""
    return _digital_silence_run(mono, coord, sr, -1)


def digital_silence_end(mono: np.ndarray, coord: int, sr: int) -> int:
    """Where digital silence ends (speech resumes) around coord."""
    return _digital_silence_run(mono, coord, sr, +1)


def _nudge_to_quietest(mono: np.ndarray, centre: int, radius: int, block: int) -> int:
    """Shift centre within +-radius to the start of the quietest `block` samples."""
    s0 = max(0, centre - radius)
    s1 = min(len(mono), centre + radius)
    if s1 - s0 <= block:
        return centre
    seg = mono[s0:s1].astype(np.float64)
    energy_blocks = np.convolve(seg ** 2, np.ones(block) / block, mode="valid")
    return int(s0 + np.argmin(energy_blocks))
