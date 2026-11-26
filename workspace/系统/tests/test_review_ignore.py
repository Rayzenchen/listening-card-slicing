"""cutaudio 1.1.1: per-row review exemption + empty-piece WAV round-trip.

Covers DESIGN Workstream B (B1-B4) without loading a model:
  * _apply_review_ignore classification (review only, non-empty segments, fully covered)
  * status derived only from review issues that are not ignored
  * plan.analyze integration (light monkeypatch of the cut planner)
  * CLI contract: --review-ignore parsing / validation (exit 2), version, help
  * report fields, checks count, two-section issues.md
  * write_wav_f32 / read_wav 0-sample round-trip (empty pieces must be writable)

Run: .venv/bin/python -m pytest tests/test_review_ignore.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
CUT = os.path.join(ROOT, "tools", "cut")
sys.path.insert(0, SRC)

from cutaudio import __version__, cli, plan  # noqa: E402
from cutaudio import audio as A  # noqa: E402
from cutaudio.plan import Analysis, SegmentPlan  # noqa: E402


def _issue(kind="asr_mismatch", severity="review", segments=(1,), detail="problem"):
    issue = {"kind": kind, "severity": severity, "detail": detail}
    if segments is not None:
        issue["segments"] = list(segments)
    return issue


# ---------------------------------------------------------------- classification


def test_ignored_when_every_segment_is_a_non_card_row():
    issues = [_issue(segments=[1]), _issue(kind="overlap", segments=[2, 3])]
    used = plan._apply_review_ignore(issues, [1, 2, 3, 3])
    assert used == {1, 2, 3}
    assert [i["ignored"] for i in issues] == [True, True]


def test_not_ignored_when_issue_spans_a_kept_row():
    issues = [_issue(kind="overlap", segments=[1, 4])]
    plan._apply_review_ignore(issues, [1, 2, 3])
    assert "ignored" not in issues[0]


def test_error_issues_are_never_ignored():
    issues = [_issue(kind="internal", severity="error", segments=[1])]
    plan._apply_review_ignore(issues, [1])
    assert "ignored" not in issues[0]


def test_issue_without_segments_is_never_ignored():
    issues = [_issue(segments=[]), _issue(segments=None)]
    plan._apply_review_ignore(issues, [1, 2])
    assert all("ignored" not in i for i in issues)


def test_reapply_resets_stale_ignored_flags():
    issues = [_issue(segments=[1])]
    plan._apply_review_ignore(issues, [1])
    assert issues[0]["ignored"] is True
    plan._apply_review_ignore(issues, [])
    assert "ignored" not in issues[0]


def test_empty_ignore_set_marks_nothing():
    issues = [_issue(segments=[1])]
    assert plan._apply_review_ignore(issues, None) == set()
    assert "ignored" not in issues[0]


# ---------------------------------------------------------------- status


def test_status_ok_when_blocking_issues_are_all_ignored():
    issues = [_issue(segments=[1])]
    plan._apply_review_ignore(issues, [1])
    assert plan._derive_status(issues) == "ok"


def test_status_blocks_on_mixed_issue():
    issues = [_issue(segments=[1]), _issue(segments=[2, 3])]
    plan._apply_review_ignore(issues, [1, 2])
    assert plan._derive_status(issues) == "needs_review"


def test_status_ignores_error_issues_like_0_0_5():
    assert plan._derive_status([_issue(severity="error", segments=[])]) == "ok"
    assert plan._derive_status([]) == "ok"


# ---------------------------------------------------------------- analyze integration


def _fake_take():
    sr = 16000
    t = np.arange(sr) / sr
    mono = (0.2 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    segs = [SimpleNamespace(index=i + 1, text="same line") for i in range(2)]
    aligned = [
        SimpleNamespace(words=[SimpleNamespace(start=0.0, end=0.45, text="same")]),
        SimpleNamespace(words=[SimpleNamespace(start=0.55, end=1.0, text="same")]),
    ]
    return sr, mono, segs, aligned


def _analyze(monkeypatch, review_ignore=None):
    sr, mono, segs, aligned = _fake_take()

    def fake_cut(*_args, **_kwargs):
        return 8000, dict(mode="silence_centre", sample=8000, gap_ms=100.0,
                          silence_quality=1.0, flags=[], evidence=1.0, order="ok",
                          word_end_sec=0.45, word_start_sec=0.55)

    monkeypatch.setattr(plan, "_gap_cut", fake_cut)
    kwargs = {} if review_ignore is None else {"review_ignore": review_ignore}
    return plan.analyze(mono, sr, segs, "en", aligned, asr_words=None,
                        run_asr_check=False, **kwargs)


def test_analyze_ignores_fully_non_card_issue_and_keeps_geometry(monkeypatch):
    plain = _analyze(monkeypatch)
    assert plain.status == "needs_review"
    dup = [i for i in plain.issues if i["kind"] == "duplicate_text"]
    assert len(dup) == 1 and dup[0]["segments"] == [1, 2]

    exempt = _analyze(monkeypatch, review_ignore=[2, 1])
    assert exempt.status == "ok"
    dup2 = [i for i in exempt.issues if i["kind"] == "duplicate_text"]
    assert len(dup2) == 1
    assert dup2[0]["ignored"] is True          # kept in report for audit, not deleted
    assert len(exempt.issues) == len(plain.issues)
    # ignoring a review issue must not move a single cut point
    assert [p.to_dict(16000) for p in exempt.plans] == [p.to_dict(16000) for p in plain.plans]


def test_analyze_keeps_blocking_when_only_part_of_issue_is_exempt(monkeypatch):
    a = _analyze(monkeypatch, review_ignore=[1])
    dup = [i for i in a.issues if i["kind"] == "duplicate_text"]
    assert dup and "ignored" not in dup[0]
    assert a.status == "needs_review"


# ---------------------------------------------------------------- report / issues.md


def _fake_analysis(issues):
    plans = [SegmentPlan(
        index=1, text="a", start_sample=0, end_sample=16000,
        cut_in=dict(mode="source_start", sample=0, flags=[], evidence=1.0),
        cut_out=dict(mode="source_end", sample=16000, flags=[], evidence=1.0),
        flags=[], evidence=1.0)]
    return Analysis(sr=16000, frames=16000, language="en", threshold_db=-55.0,
                    plans=plans, issues=issues, status=plan._derive_status(issues), info={})


def test_review_report_fields_sorted_and_audited():
    issues = [_issue(segments=[2]), _issue(segments=[1])]
    plan._apply_review_ignore(issues, [2, 1])
    fields = cli._review_report_fields(_fake_analysis(issues), [2, 1])
    assert fields["review_ignore"] == [1, 2]
    assert len(fields["ignored_issues"]) == 2
    assert fields["ignored_issues"][0] is issues[0]


def test_review_report_fields_without_flag_are_empty():
    fields = cli._review_report_fields(_fake_analysis([_issue()]), None)
    assert fields == {"review_ignore": [], "ignored_issues": []}


def test_checks_summary_counts_ignored_issues():
    issues = [_issue(segments=[1]), _issue(segments=[1])]
    issues[0]["ignored"] = True
    checks = cli._checks_summary(_fake_analysis(issues))
    assert checks["issues"] == 2
    assert checks["ignored_issues"] == 1


def test_issues_markdown_without_ignored_has_old_shape():
    md = cli._issues_markdown(_fake_analysis([_issue(kind="weak_evidence", segments=[2], detail="blocks")]), 16000)
    assert "已忽略" not in md
    assert md.startswith("# 需要人工复核\n\n- [weak_evidence] 段 2: blocks\n")


def test_issues_markdown_splits_ignored_section():
    blocking = _issue(kind="weak_evidence", segments=[2], detail="segment 2 blocks")
    ignored = _issue(kind="asr_mismatch", segments=[1], detail="segment 1 is a non-card row")
    ignored["ignored"] = True
    md = cli._issues_markdown(_fake_analysis([blocking, ignored]), 16000)
    head, sep, tail = md.partition("## 已忽略（不进卡组）")
    assert sep
    assert "segment 2 blocks" in head
    assert "segment 1 is a non-card row" not in head
    assert "segment 1 is a non-card row" in tail


# ---------------------------------------------------------------- CLI contract


def test_parser_accepts_review_ignore():
    args = cli.build_parser().parse_args(
        ["--audio", "a.wav", "--text", "t.txt", "--review-ignore", "rows.json"])
    assert args.review_ignore == "rows.json"
    assert cli.build_parser().parse_args(
        ["--audio", "a.wav", "--text", "t.txt"]).review_ignore is None


def test_load_review_ignore_object_and_bare_array(tmp_path):
    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({"version": 1, "doc": "d01", "segments": [112, 1, 2, 3, 3]}),
                   encoding="utf-8")
    assert cli._load_review_ignore(str(obj), 112) == [1, 2, 3, 112]

    arr = tmp_path / "arr.json"
    arr.write_text("[3, 1]", encoding="utf-8")
    assert cli._load_review_ignore(str(arr), 3) == [1, 3]


@pytest.mark.parametrize("payload", [
    '{"segments": [0]}',
    '{"segments": [4]}',
    '{"segments": [1.5]}',
    '{"segments": ["1"]}',
    '{"segments": [true]}',
    '{"segments": 3}',
    '{"version": 1}',
    "[1, 2",
])
def test_load_review_ignore_rejects_bad_rows(tmp_path, payload):
    p = tmp_path / "bad.json"
    p.write_text(payload, encoding="utf-8")
    with pytest.raises(cli.UsageError):
        cli._load_review_ignore(str(p), 3)


def test_load_review_ignore_missing_file_is_usage_error(tmp_path):
    with pytest.raises(cli.UsageError):
        cli._load_review_ignore(str(tmp_path / "nope.json"), 3)


@pytest.mark.parametrize("payload", [
    '{"version": 1, "doc": "d01", "segments": [3]}',
    '{"segments": [1.5]}',
])
def test_cli_rejects_bad_review_ignore_with_exit_2(tmp_path, payload):
    wav = tmp_path / "a.wav"
    t = np.arange(int(16000 * 0.2)) / 16000
    A.write_wav_16(str(wav), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 16000)
    text = tmp_path / "t.txt"
    text.write_text("one\ntwo\n", encoding="utf-8")
    rows = tmp_path / "rows.json"
    rows.write_text(payload, encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.run([CUT, "--audio", str(wav), "--text", str(text),
                           "--review-ignore", str(rows), "--out", str(out)],
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert "review-ignore" in proc.stderr.lower()
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["errors"][0]["kind"] == "bad_review_ignore"


# ---------------------------------------------------------------- version / help


def test_version_is_1_1_1():
    assert __version__ == "1.1.1"
    assert cli.VERSION == "1.1.1"
    proc = subprocess.run([CUT, "--version"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "cutaudio 1.1.1"


def test_cli_help_lists_review_ignore():
    proc = subprocess.run([CUT, "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "--review-ignore" in proc.stdout


# ---------------------------------------------------------------- 0-sample WAV


@pytest.mark.parametrize("shape,channels", [((0,), 1), ((0, 1), 1), ((0, 2), 2)])
def test_zero_sample_wav_roundtrip(tmp_path, shape, channels):
    pcm = np.zeros(shape, dtype=np.float32)
    path = tmp_path / "empty.wav"
    A.write_wav_f32(str(path), pcm, 16000)
    back, rate = A.read_wav(str(path))
    assert rate == 16000
    assert back.shape == (0, channels)
    assert back.dtype == np.float32
    assert path.stat().st_size > 44          # real header, empty data chunk


def test_empty_slice_of_stereo_source_roundtrips(tmp_path):
    src = np.arange(10, dtype=np.float32).reshape(5, 2)
    path = tmp_path / "empty_slice.wav"
    A.write_wav_f32(str(path), src[3:3], 44100)
    back, rate = A.read_wav(str(path))
    assert rate == 44100
    assert back.shape == (0, 2)
