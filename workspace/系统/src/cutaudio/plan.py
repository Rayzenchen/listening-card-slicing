"""Plan the cut points between consecutive segments, then verify them.

Hard rule: the pieces tile the decoded source exactly - piece 1 starts at sample 0
and piece N ends at the last sample, and every cut sits strictly between the
previous segment's last word and the next segment's first word.  Audio is never
dropped; only the *position* of each cut inside its gap moves.

For every gap between segment i and i+1 we look at the audio, not only at the
aligner's guesses:
  * the speech edge of the outgoing segment is confirmed on the waveform (the
    aligner can drag it early, losing the last phoneme)
  * the speech edge of the incoming segment is confirmed too (dragging late would
    clip the first phoneme)
  * inside the remaining gap we prefer the stable centre of real silence; with no
    silence we cut at the quietest point, but only when both word/char edges are
    trustworthy.

Every decision carries an `evidence` score and a list of `flags`; anything weak
becomes "needs review" instead of silently producing a bad cut.
"""
from __future__ import annotations

import json
import difflib
from dataclasses import dataclass, field

import numpy as np

from . import align as align_mod
from . import energy

# --- tolerances ------------------------------------------------------------
EDGE_LATE = 0.180         # s: how far speech may run past the aligner's edge and still be sane
EDGE_EARLY = 0.060        # s: how far inside the aligner's edge speech may start/end
GAP_KEEP_MIN = 0.008      # s: keep at least 8 ms away from a detected speech edge
SILENCE_PREF = 0.080      # s: gap length at which silent-centre cutting is preferred
OVERLAP_TOL = 0.150       # s: words from both segments closer than this = overlap
ASR_MARGIN = 0.150        # s: include machine words this close to the cut
ASR_LOOKBACK = 0.700      # s: how far each side of a cut we read machine words
ASR_MIN_SIM = 0.45        # overall similarity below which a piece is suspect
ASR_SUFFIX_MIN = 0.40     # both prefix and suffix must be at least this good
ASR_CONFIRM = 0.90        # both neighbours heard this well -> the pause is real
WAVE_SEARCH = 1.2         # s: search radius around the anchored boundary for a pause
WAVE_MIN_SILENCE = 0.12   # s: shortest pause that can host a cut


@dataclass
class SegmentPlan:
    index: int
    text: str
    start_sample: int = 0
    end_sample: int = 0
    cut_in: dict | None = None
    cut_out: dict | None = None
    flags: list[str] = field(default_factory=list)
    evidence: float = 1.0
    asr_text: str = ""
    asr_similarity: float | None = None
    voiced_sec: float = 0.0
    peak_db: float = -120.0

    def start_sec(self, sr: int) -> float:
        return self.start_sample / sr

    def end_sec(self, sr: int) -> float:
        return self.end_sample / sr

    @property
    def num_samples(self) -> int:
        return self.end_sample - self.start_sample

    def duration(self, sr: int) -> float:
        return (self.end_sample - self.start_sample) / sr

    def to_dict(self, sr: int) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "num_samples": self.num_samples,
            "start_sec": round(self.start_sample / sr, 6),
            "end_sec": round(self.end_sample / sr, 6),
            "duration_sec": round(self.num_samples / sr, 6),
            "cut_in": self.cut_in,
            "cut_out": self.cut_out,
            "flags": list(self.flags),
            "evidence": self.evidence,
            "asr_text": self.asr_text,
            "asr_similarity": self.asr_similarity,
            "voiced_sec": self.voiced_sec,
            "peak_db": self.peak_db,
            "word_start_sec": self.cut_in.get("word_start_sec") if self.cut_in else None,
            "word_end_sec": self.cut_out.get("word_end_sec") if self.cut_out else None,
        }


@dataclass
class Analysis:
    sr: int
    frames: int
    language: str
    threshold_db: float
    plans: list[SegmentPlan]
    issues: list[dict]
    status: str
    info: dict


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _gap_cut(prev_words, next_words, mono, frame_db, hop, sr, thr_db, floor_db):
    """Choose the cut sample between two segments. Returns (sample, meta).

    Edges are checked on the energy envelope, not on individual samples: a decay
    tail (e.g. the "k" of "clock") can ring 120 ms past the aligner's word end, so
    a strict wait-for-samples-to-drop test would always "fail".
    Tolerance is asymmetric: the aligner typically ends a word slightly early and
    starts the next one slightly late, so a late true end / early true start is
    expected and trustworthy.
    """
    edge_a = int(round(prev_words[-1].end * sr))     # aligner: last phoneme ends
    edge_b = int(round(next_words[0].start * sr))    # aligner: first phoneme starts
    flags: list[str] = []

    # Confirm both speech edges on the energy envelope.
    onset_b = energy.refine_onset(frame_db, hop, thr_db, floor_db, edge_b, sr, mono=mono)
    offset_a = energy.refine_offset(frame_db, hop, thr_db, floor_db, edge_a, sr, mono=mono)

    d_a = (offset_a - edge_a) / sr          # >0: speech really ends after the aligner said
    d_b = (edge_b - onset_b) / sr           # >0: speech really starts before the aligner said
    edge_a_ok = -EDGE_EARLY <= d_a <= EDGE_LATE
    edge_b_ok = -EDGE_EARLY <= d_b <= EDGE_LATE
    if not edge_a_ok:
        flags.append("edge_out_refined")
    if not edge_b_ok:
        flags.append("edge_in_refined")

    # Allowed band: never cut inside the outgoing speech nor into the incoming one.
    # A loose 300 ms pad around the aligner's guess is only allowed when the measured
    # edges line up with it; otherwise the waveform is the authority (a decay tail can
    # run 130 ms past the word end and must stay inside the piece).
    pad = int(0.30 * sr) if (edge_a_ok and edge_b_ok) else int(0.02 * sr)
    # The heart of the pause is found on the 10 ms frame grid (it is quantised there),
    # while the waveform edges above are deliberately conservative about where speech
    # stops.  Starting the search range at the frame-level edge keeps a 50 ms envelope
    # smear from pulling the centre of a short pause to one side.
    # Probe INSIDE the band between the aligner's two edges: that zone is normally the
    # pause, and it is where the digital-silence run can be entered from.  (Probing
    # from the aligner's edge directly often lands on a decay tail that is still
    # above the zero level, and then no run is found at all.)
    mid_a = (edge_a + edge_b) // 2 - int(0.05 * sr)
    mid_b = (edge_a + edge_b) // 2 + int(0.05 * sr)
    out_frame = energy.digital_silence_start(mono, max(0, mid_a), sr)
    in_frame = energy.digital_silence_end(mono, mid_b, sr)
    lo = max(offset_a + int(GAP_KEEP_MIN * sr), edge_a - pad)
    hi = min(onset_b - int(GAP_KEEP_MIN * sr), edge_b + pad)
    if in_frame - out_frame > int(0.50 * sr):
        lo = max(lo, out_frame + hop)
        hi = min(hi, in_frame - hop)
    order = "ok"
    if lo >= hi:
        # The waveform says the two sounds overlap (or touch). Cut in the middle of
        # the claimed boundary - there is nothing better to go on.
        centre = int((max(offset_a, edge_a) + min(onset_b, edge_b)) / 2)
        order = "overlap"
        flags.append("waveform_overlap")
        meta = dict(mode="touching", sample=centre, gap_ms=round((min(onset_b, edge_b) - max(offset_a, edge_a)) / sr * 1000.0, 1),
                    silence_quality=0.0, flags=flags, evidence=0.35, order=order,
                    edge_out_aligner=edge_a, edge_out_wave=offset_a,
                    edge_in_aligner=edge_b, edge_in_wave=onset_b,
                    word_end_sec=round(prev_words[-1].end, 4),
                    word_start_sec=round(next_words[0].start, 4))
        return centre, meta

    sample, quality = energy.silence_centre(mono, lo, hi, sr, frame_db, hop, thr_db, floor_db)
    gap_ms = (hi - lo) / sr * 1000.0
    if gap_ms >= SILENCE_PREF * 1000.0 and quality >= 0.55:
        mode = "silence_centre"
        evidence = min(1.0, 0.65 + 0.35 * quality)
    elif gap_ms >= SILENCE_PREF * 1000.0 and quality >= 0.30:
        # Long quiet gap but the "silence" is broken by a few louder frames.  Still a
        # silent-centre cut; the ASR cross-check decides whether it is trustworthy
        # (if both neighbouring pieces are heard correctly, the pause was real).
        mode = "silence_centre"
        evidence = 0.55
    else:
        mode = "quiet_edge"
        evidence = 0.78 if (edge_a_ok and edge_b_ok) else 0.45
        if not (edge_a_ok and edge_b_ok):
            flags.append("boundary_uncertain")
    sample = int(_clamp(sample, lo, hi - 1))
    meta = dict(mode=mode, sample=sample, gap_ms=round(gap_ms, 1),
                silence_quality=round(quality, 3), flags=flags, evidence=round(evidence, 3), order=order,
                edge_out_aligner=edge_a, edge_out_wave=offset_a,
                edge_in_aligner=edge_b, edge_in_wave=onset_b,
                word_end_sec=round(prev_words[-1].end, 4),
                word_start_sec=round(next_words[0].start, 4))
    return sample, meta

def _silence_runs(frame_db, hop: int, sr: int, lo_sec: float, hi_sec: float,
                  threshold: float):
    """Frame runs (a, b) below the speech threshold inside [lo_sec, hi_sec]."""
    a = max(0, int(lo_sec * sr) // hop)
    b = min(len(frame_db), int(hi_sec * sr) // hop + 1)
    runs, cur = [], None
    for k in range(a, b):
        if frame_db[k] < threshold:
            if cur is None:
                cur = k
        else:
            if cur is not None:
                runs.append((cur, k))
                cur = None
    if cur is not None:
        runs.append((cur, b))
    return runs


def _wave_cut(prev_words, next_words, mono, frame_db, hop, sr, thr_db, floor_db):
    """Cut in the real pause nearest the anchored line boundary.

    The ASR anchor is good to ~0.3 s but its word edges can absorb a pause; the
    waveform is the authority for where the pause actually is.
    """
    ea = float(prev_words[-1].end)
    eb = float(next_words[0].start)
    mid = (ea + eb) / 2.0
    runs = _silence_runs(frame_db, hop, sr, mid - WAVE_SEARCH, mid + WAVE_SEARCH, thr_db)
    best = None
    for a, b in runs:
        dur = (b - a) * hop / sr
        if dur < WAVE_MIN_SILENCE:
            continue
        centre = ((a + b) // 2) * hop
        score = abs(centre / sr - mid) - 0.10 * dur
        if best is None or score < best[0]:
            best = (score, centre, dur)
    if best is None:
        return _gap_cut(prev_words, next_words, mono, frame_db, hop, sr, thr_db, floor_db)
    _score, centre, dur = best
    meta = dict(mode="silence_centre", sample=int(centre),
                gap_ms=round(dur * 1000.0, 1),
                silence_quality=round(min(1.0, dur / 0.5), 3),
                flags=[], evidence=1.0, order="ok",
                edge_out_aligner=int(round(ea * sr)),
                edge_in_aligner=int(round(eb * sr)),
                word_end_sec=round(prev_words[-1].end, 4),
                word_start_sec=round(next_words[0].start, 4))
    return int(centre), meta


def _clamp_words(aligned, plans, sr: int) -> None:
    """Keep every word inside its piece; cuts are in verified silence."""
    for i, seg in enumerate(aligned):
        lo = plans[i].start_sample / sr
        hi = plans[i].end_sample / sr
        words = seg.words
        if not words:
            continue
        words[0].start = max(words[0].start, lo)
        words[-1].end = min(words[-1].end, hi)
        prev = lo
        for w in words:
            w.start = min(max(w.start, prev), hi)
            w.end = min(max(w.end, w.start), hi)
            prev = w.end


def _words_in(asr_words, a_sec: float, b_sec: float) -> str:
    """Machine words whose midpoint lies inside [a_sec, b_sec] - in time order."""
    picked = [w.text for w in asr_words if a_sec <= (w.start + w.end) / 2.0 <= b_sec]
    return " ".join(picked)


def _check_cut_asr(plan_in, plan_out, asr_words, sr):
    """Compare the machine's own reading with the user text on both sides of a cut.

    The window is the PIECE ITSELF, not the cut: machine words are collected by
    their midpoint falling inside the piece's sample range, in reading order.  A
    fixed window around the cut would pull in the neighbouring sentence on long
    pauses and report nonsense; the piece range cannot.
    """
    out = {}
    hi = plan_out.end_sample / sr
    lo = plan_in.start_sample / sr
    out["out_heard"] = _words_in(asr_words, plan_out.start_sample / sr, hi + ASR_MARGIN)
    out["in_heard"] = _words_in(asr_words, lo - ASR_MARGIN, plan_in.end_sample / sr)
    return out


def _score(user_text: str, heard: str, si: int, ei: int) -> dict:
    """Score the whole piece against the machine reading (prefix / suffix / overall)."""
    user = align_mod.normalize(user_text)
    heard_n = align_mod.normalize(heard)
    if not heard_n:
        return {"similarity": 1.0, "prefix": 1.0, "suffix": 1.0, "heard": heard, "checked": False}
    overall = difflib.SequenceMatcher(None, user, heard_n).ratio()
    # words of the machine reading that belong to this piece
    words = heard_n.split() if " " in heard else [heard_n]
    head_words = " ".join(words[:max(1, len(words) // 3)])
    tail_words = " ".join(words[-max(1, len(words) // 3):])
    h = align_mod.normalize(head_words)
    t = align_mod.normalize(tail_words)
    prefix = difflib.SequenceMatcher(None, user[:max(4, len(h))], h).ratio()
    suffix = difflib.SequenceMatcher(None, user[-max(4, len(t)):], t).ratio()
    # is the machine text actually contiguously present in the user text?
    contiguous = heard_n in user or user in heard_n
    return {"similarity": round(float(overall), 3), "prefix": round(float(prefix), 3),
            "suffix": round(float(suffix), 3), "heard": heard, "checked": True,
            "contiguous": bool(contiguous)}


def _similar_tail(user: str, heard: str) -> float:
    heard_n = align_mod.normalize(heard)
    if not heard_n:
        return 1.0
    ref = user[-max(6, len(heard_n)):]
    return float(difflib.SequenceMatcher(None, ref, heard_n).ratio())


def _similar_head(user: str, heard: str) -> float:
    heard_n = align_mod.normalize(heard)
    if not heard_n:
        return 1.0
    ref = user[:max(6, len(heard_n))]
    return float(difflib.SequenceMatcher(None, ref, heard_n).ratio())

def _apply_review_ignore(issues: list[dict], ignore) -> set[int]:
    """Mark review issues that only touch non-card rows as non-blocking.

    An issue is ignored only when it is ``severity=review``, has at least one
    segment, and *every* segment is in the ignore set.  Error issues, issues
    without segments, and issues spanning both ignored and kept rows stay
    blocking.  Mutates ``issues`` in place (adds/removes ``ignored``) and returns
    the normalised ignore set so the caller can report exactly what was used.
    """
    ignore_set = {int(x) for x in (ignore or [])}
    for issue in issues:
        issue.pop("ignored", None)
        segs = {int(s) for s in issue.get("segments") or []}
        if issue.get("severity") == "review" and segs and segs <= ignore_set:
            issue["ignored"] = True
    return ignore_set


def _derive_status(issues: list[dict]) -> str:
    """Review status counts only review issues that still block."""
    if any(i.get("severity") == "review" and not i.get("ignored") for i in issues):
        return "needs_review"
    return "ok"


def analyze(mono: np.ndarray, sr: int, segments, language: str, aligned, asr_words=None,
            run_asr_check: bool = True, wave_cuts: bool = False,
            review_ignore=None) -> Analysis:
    n = len(mono)
    frame_db, hop = energy.frame_energy(mono, sr)
    thr_db = energy.speech_threshold_db(frame_db)
    floor_db = energy.noise_floor_db(frame_db)

    plans = [SegmentPlan(index=s.index, text=s.text, start_sample=0, end_sample=n) for s in segments]
    issues: list[dict] = []

    # ---- cut points (full coverage is structural: 0 ... n) ----------------
    cutter = _wave_cut if wave_cuts else _gap_cut
    for i in range(len(plans) - 1):
        sample, meta = cutter(aligned[i].words, aligned[i + 1].words, mono, frame_db, hop, sr,
                              thr_db, floor_db)
        plans[i].end_sample = sample
        plans[i].cut_out = meta
        plans[i + 1].start_sample = sample
        plans[i + 1].cut_in = dict(meta)

    plans[0].cut_in = dict(mode="source_start", sample=0, flags=[], evidence=1.0,
                           word_start_sec=round(aligned[0].words[0].start, 4))
    plans[-1].cut_out = dict(mode="source_end", sample=n, flags=[], evidence=1.0,
                             word_end_sec=round(aligned[-1].words[-1].end, 4))

    if wave_cuts:
        _clamp_words(aligned, plans, sr)

    # ---- per-segment measurements ---------------------------------------
    for i, plan in enumerate(plans):
        a, b = plan.start_sample, plan.end_sample
        seg = mono[a:b]
        plan.voiced_sec = round(energy.voiced_frames(frame_db, a, b, hop, thr_db), 3)
        plan.peak_db = round(float(20 * np.log10(max(1e-9, float(np.max(np.abs(seg)))))) if seg.size else -120.0, 2)
        words = aligned[i].words
        if words[0].start * sr < a - 0.02 * sr or words[-1].end * sr > b + 0.02 * sr:
            plan.flags.append("speech_outside_piece")
        if plan.voiced_sec < 0.08:
            plan.flags.append("near_silent_piece")
        if plan.duration(sr) < 0.12:
            plan.flags.append("very_short_piece")

    # ---- text ordering / overlap ----------------------------------------
    for i in range(len(aligned) - 1):
        a_end = aligned[i].words[-1].end
        b_start = aligned[i + 1].words[0].start
        if b_start < a_end - OVERLAP_TOL:
            issues.append(dict(kind="overlap", severity="review", segments=[i + 1, i + 2],
                               detail=f"segment {i+1} ends at {a_end:.3f}s but segment {i+2} starts at "
                                      f"{b_start:.3f}s (overlap {a_end - b_start:.3f}s)"))
            plans[i].flags.append("overlap_with_next")
            plans[i + 1].flags.append("overlap_with_prev")

    # ---- duplicate text: review only when the TIMING is ambiguous --------
    # A repeated line is normal in drill material; only complain when two identical
    # texts end up overlapping or within a breath of each other in time.
    norm = [align_mod.normalize(p.text) for p in plans]
    for i in range(len(norm)):
        if not norm[i]:
            continue
        for j in range(i + 1, len(norm)):
            if norm[i] != norm[j]:
                continue
            gap = plans[j].start_sample - plans[i].end_sample
            overlapping = aligned[j].words[0].start < aligned[i].words[-1].end - OVERLAP_TOL
            if gap < int(0.10 * sr) or overlapping:
                issues.append(dict(kind="duplicate_text", severity="review", segments=[i + 1, j + 1],
                                   detail=f"segments {i+1} and {j+1} have identical text and are only "
                                          f"{max(gap, 0) / sr:.2f}s apart - the cut position is ambiguous"))

    # ---- ASR cross-check: whole piece vs machine reading ----------------
    # The machine never writes the user text: it transcribes independently.  We ask
    # "does what was actually spoken inside this piece match this text?" via
    # overall / prefix / suffix similarity, so one odd word does not fail a piece,
    # while a missing or repeated sentence does.
    if run_asr_check and asr_words:
        for i, plan in enumerate(plans):
            heard = _words_in(asr_words, plan.start_sample / sr - ASR_MARGIN,
                              plan.end_sample / sr + ASR_MARGIN)
            plan.asr_text = heard
            sc = _score(plan.text, heard, plan.start_sample, plan.end_sample)
            plan.asr_similarity = sc["similarity"]
            if not sc["checked"]:
                plan.flags.append("asr_heard_nothing")
                issues.append(dict(kind="asr_mismatch", severity="review", segments=[plan.index],
                                   detail=f"segment {plan.index}: the ASR pass heard no words inside this piece"))
                continue
            if sc["similarity"] < ASR_MIN_SIM or (sc["suffix"] < ASR_SUFFIX_MIN and sc["prefix"] < ASR_SUFFIX_MIN):
                plan.flags.append("asr_mismatch")
                issues.append(dict(kind="asr_mismatch", severity="review", segments=[plan.index],
                                   detail=(f"segment {plan.index}: machine hears {heard!r} inside the piece; "
                                           f"overall {sc['similarity']:.2f}, prefix {sc['prefix']:.2f}, "
                                           f"suffix {sc['suffix']:.2f} vs the user text")))

        # neighbouring pieces must not both contain the same machine words
        for i in range(len(plans) - 1):
            a = align_mod.normalize(plans[i].asr_text)
            b = align_mod.normalize(plans[i + 1].asr_text)
            if len(a) >= 12 and len(b) >= 12 and (a in b or b in a):
                issues.append(dict(kind="asr_duplicate", severity="review", segments=[i + 1, i + 2],
                                   detail=f"segments {i+1} and {i+2} were heard as the same words - the "
                                          f"sentence meant for one piece seems to be in both"))


    # A cut whose two neighbours were BOTH heard correctly by the ASR pass is a real
    # pause even when its silence was interrupted by a stray louder frame.
    if run_asr_check and asr_words:
        for plan in plans:
            for cut in (plan.cut_in, plan.cut_out):
                if not cut or cut.get("mode") != "silence_centre":
                    continue
                idx = plan.index
                left = plans[idx - 1] if cut is plan.cut_in else plan
                right = plan if cut is plan.cut_in else (plans[idx + 1] if idx < len(plans) - 1 else None)
                sims = [p.asr_similarity for p in (left, right) if p is not None and p.asr_similarity is not None]
                if sims and min(sims) >= ASR_CONFIRM:
                    cut["evidence"] = max(cut["evidence"], 0.9)
                    cut["flags"] = [f for f in cut.get("flags", []) if f != "boundary_uncertain"]
                    plan.evidence = max(plan.evidence, 0.9)

    # ---- coverage safety net --------------------------------------------
    if plans[0].start_sample != 0 or plans[-1].end_sample != n:
        issues.append(dict(kind="internal", severity="error", segments=[], detail="coverage does not span the source"))
    for i in range(len(plans) - 1):
        if plans[i].end_sample != plans[i + 1].start_sample:
            issues.append(dict(kind="internal", severity="error", segments=[i + 1, i + 2],
                               detail="discontinuous cut"))

    # ---- weakness detection ---------------------------------------------
    review_kinds = ("overlap", "asr_mismatch", "duplicate_text")
    flagged = {s for x in issues if x["kind"] in review_kinds for s in x.get("segments", [])}
    for plan in plans:
        cut_ev = min([(plan.cut_in or {}).get("evidence", 1.0), (plan.cut_out or {}).get("evidence", 1.0)])
        plan.evidence = round(float(cut_ev), 3)
        weak = [f for f in plan.flags if f in ("boundary_uncertain", "speech_outside_piece")]
        if cut_ev < 0.5 and not weak:
            weak.append("weak_evidence")
        if weak and plan.index not in flagged:
            issues.append(dict(kind="weak_evidence", severity="review", segments=[plan.index],
                               detail=f"segment {plan.index}: cut evidence {cut_ev:.2f} ({', '.join(weak)})"))

    # ---- per-row review exemption ---------------------------------------
    # Every issue exists by now; rows that do not enter cards (intro / station
    # id / directions / outro) may be exempted, but only for review issues that
    # touch nothing else.  Ignored issues stay in ``issues`` for audit.
    _apply_review_ignore(issues, review_ignore)

    status = _derive_status(issues)
    info = dict(threshold_db=round(thr_db, 2), hop=hop, frames=n, sample_rate=sr)
    return Analysis(sr=sr, frames=n, language=language, threshold_db=round(thr_db, 2),
                    plans=plans, issues=issues, status=status, info=info)


def _similar_tail(user: str, heard: str) -> float:
    heard_n = align_mod.normalize(heard)
    if not heard_n:
        return 1.0
    ref = user[-max(6, len(heard_n)):]
    return float(difflib.SequenceMatcher(None, ref, heard_n).ratio())


def _similar_head(user: str, heard: str) -> float:
    heard_n = align_mod.normalize(heard)
    if not heard_n:
        return 1.0
    ref = user[: max(6, len(heard_n))]
    return float(difflib.SequenceMatcher(None, ref, heard_n).ratio())


def preview(analysis: Analysis, n: int = 3) -> list[int]:
    """Which segments to render as short review listens (1-based)."""
    pick: list[int] = []
    for issue in analysis.issues:
        for s in issue.get("segments", []):
            if s not in pick:
                pick.append(s)
    for plan in analysis.plans:
        if plan.flags and plan.index not in pick:
            pick.append(plan.index)
    return sorted(pick)[:n]