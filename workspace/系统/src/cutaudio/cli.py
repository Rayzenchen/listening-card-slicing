"""cutaudio command line entry point.

Exit codes (contract used by DSH and by AGENTS.md):
  0  ok            - ZIP written, every check passed
  3  needs_review  - results are not trustworthy enough to ship; only a small
                     preview pack + problem report was written
  1  error         - the run failed (nothing usable was produced)
  2  usage error   - bad arguments / missing input files
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback

from . import align as align_mod
from . import audio as audio_mod
from . import output as out_mod
from .text import TextError
from . import plan as plan_mod
from . import runtime

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REVIEW = 3

VERSION = "1.1.1"


class UsageError(ValueError):
    """Bad command-line input that must exit 2 instead of being a runtime error."""


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def default_out_dir(audio_path: str) -> str:
    base = os.path.splitext(os.path.basename(audio_path))[0]
    safe = "".join(ch for ch in base if ch.isalnum() or ch in "-_.（）()").strip() or "audio"
    root = os.path.join(runtime.project_root(), "产出区")
    return os.path.join(root, f"{_ts()}_{safe}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cutaudio",
        description="Offline forced-alignment audio splitter: cut one audio file into the "
                    "user's exact text segments, verify the cuts, and ship a ZIP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 ok | 3 needs_review | 1 error | 2 usage error",
    )
    p.add_argument("--audio", required=True, help="input audio (mp3/m4a/wav/flac) - read only")
    p.add_argument("--text", required=True, help="UTF-8 transcript, one segment per line by default")
    p.add_argument("--out", default=None,
                   help="result directory (default: 产出区/<timestamp>_<audio name> in the workspace)")
    p.add_argument("--lang", default="auto", choices=["auto", "en", "zh", "ja", "ko", "fr", "de", "es", "ru"],
                   help="spoken language; auto sniffs from the text (en/zh) - default auto")
    p.add_argument("--segments", default="line", choices=["line", "blank_line"],
                   help="segment rule: one non-empty line (default) or blank-line separated blocks")
    p.add_argument("--model-dir", default=None, help="MLX Whisper model directory (default models/whisper-large-v3-turbo)")
    p.add_argument("--force", action="store_true",
                   help="accepted for compatibility; existing result directories are never overwritten")
    p.add_argument("--no-asr-check", action="store_true", help="skip the second (ASR) verification pass")
    p.add_argument("--review-ignore", default=None, metavar="FILE",
                   help="JSON list/object of 1-based segment numbers whose review issues are "
                        "non-blocking (rows that do not enter cards)")
    p.add_argument("--json", action="store_true", help="print the machine-readable report to stdout")
    p.add_argument("--quiet", action="store_true", help="no progress output")
    p.add_argument("--version", action="version", version=f"cutaudio {VERSION}")
    return p


def _load_review_ignore(path: str, segment_count: int) -> list[int]:
    """Read the rows whose review issues must not block the cut.

    Accepted shapes: ``{"version": 1, "doc": "d01", "segments": [1, 2, 3]}``
    (the file the skill writes) or a bare ``[1, 2, 3]``.  Only integers in
    ``1..segment_count`` are accepted; anything else is a usage error - never
    silently dropped, because an out-of-range row would exempt nothing.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise UsageError(f"cannot read review-ignore file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"review-ignore file is not valid JSON ({path}): {exc}") from exc
    if isinstance(payload, dict):
        if "segments" not in payload:
            raise UsageError(f"review-ignore object has no 'segments' list: {path}")
        payload = payload["segments"]
    if not isinstance(payload, list):
        raise UsageError(f"review-ignore must be a JSON array or an object with 'segments': {path}")
    rows: set[int] = set()
    for item in payload:
        if isinstance(item, bool) or not isinstance(item, int):
            raise UsageError(f"review-ignore row is not an integer: {item!r} (in {path})")
        if not 1 <= item <= segment_count:
            raise UsageError(
                f"review-ignore row {item} is outside 1..{segment_count} (in {path})")
        rows.add(item)
    return sorted(rows)


def _review_report_fields(analysis, review_ignore) -> dict:
    """Report fields that let the caller audit the per-row review exemption."""
    return {
        "review_ignore": sorted(int(n) for n in (review_ignore or [])),
        "ignored_issues": [i for i in analysis.issues if i.get("ignored")],
    }


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _alignment_guard(info: dict, asr_words) -> str | None:
    """Annotate ``info`` with coverage and decide whether cutting may continue.

    Edge ("forced aligner") mode is refused when the aligned span covers less
    than ``COVERAGE_MIN`` of the ASR word timeline: that means the aligner ran
    out of audio and squeezed the whole text into the first minutes, so every
    cut point after it is wrong.  Wave mode (ASR-anchored) reports the same
    number but is never blocked here; a low ``matched`` there is only a warning.

    The mutation is deliberate: whatever happens next, the report carries the
    coverage number (wave mode falls back to 1.0), so a failed run stays
    diagnosable.  The caller keeps ``alignment`` / ``asr`` / ``source`` in the
    report before calling this.
    """
    coverage = None
    if asr_words:
        coverage = align_mod.alignment_coverage(info.get("duration"), asr_words)
    if coverage is not None:
        info["coverage"] = coverage
    elif info.get("cut_mode") == "wave":
        info["coverage"] = 1.0
    if info.get("cut_mode") != "edge" or coverage is None:
        return None
    if coverage >= align_mod.COVERAGE_MIN:
        return None
    last = align_mod.asr_last_end(asr_words)
    duration = float(info.get("duration") or 0.0)
    return (f"forced alignment covered only {coverage:.0%} of the ASR timeline "
            f"({duration:.1f}s / {last:.1f}s); the text and audio likely do not match "
            f"(mixed-script text?); refusing to cut")


def _wave_match_warning(info: dict) -> str | None:
    """Wave mode keeps real word timings, so a low text match is a warning only."""
    if info.get("cut_mode") != "wave":
        return None
    matched = float(info.get("matched") or 0.0)
    if matched >= 0.9:
        return None
    return (f"ASR anchoring matched only {matched:.0%} of the text words; "
            f"wave mode still has real word timings, so the cut continues")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.quiet or args.json
    started = time.time()

    out_dir = args.out or default_out_dir(args.audio)
    out_dir = os.path.abspath(out_dir)
    work_dir = os.path.join(os.path.dirname(out_dir), ".tmp_" + os.path.basename(out_dir))

    report = {
        "tool": f"cutaudio {VERSION}",
        "status": "error",
        "exit_code": EXIT_ERROR,
        "audio": args.audio,
        "text": args.text,
        "language": args.lang,
        "out_dir": out_dir,
        "errors": [],
        "warnings": [],
    }

    def fail(code: int, kind: str, message: str, detail: str | None = None) -> int:
        report["status"] = "needs_review" if code == EXIT_REVIEW else "error"
        report["exit_code"] = code
        report["errors"].append({"kind": kind, "message": message, **({"detail": detail} if detail else {})})
        print(f"ERROR: [{kind}] {message}", file=sys.stderr)
        _finish_report(out_dir, report, started, quiet, code)
        return code

    # --- preflight -------------------------------------------------------
    # Refuse to touch an existing non-empty directory: not even a report is written,
    # so a previous run's results can never be clobbered.  The same goes for a ZIP
    # of either flavour that already sits next to it.
    for produced in (out_dir + ".zip", out_dir + "_review.zip"):
        if os.path.exists(produced):
            print(f"ERROR: a finished ZIP already exists there: {produced}", file=sys.stderr)
            print("       pick a fresh --out or leave --out off; nothing was written.", file=sys.stderr)
            return EXIT_USAGE
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        print(f"ERROR: result directory exists and is not empty: {out_dir}", file=sys.stderr)
        print("       pick a fresh --out or leave --out off; nothing was written.", file=sys.stderr)
        return EXIT_USAGE
    try:
        runtime.apply()
        if not os.path.isfile(args.audio):
            raise audio_mod.AudioError(f"audio file not found: {args.audio}")
        if not os.path.isfile(args.text):
            raise TextError(f"text file not found: {args.text}")
    except (audio_mod.AudioError, TextError) as exc:
        return fail(EXIT_USAGE, "bad_input", str(exc))
    except FileExistsError as exc:
        return fail(EXIT_USAGE, "output_exists", str(exc))


    try:
        from . import text as text_mod
        segments, hint = text_mod.load_text(args.text, args.segments)
        report["segments"] = len(segments)
        report["segment_rule"] = args.segments
        review_ignore: list[int] = []
        if args.review_ignore:
            try:
                review_ignore = _load_review_ignore(args.review_ignore, len(segments))
            except UsageError as exc:
                return fail(EXIT_USAGE, "bad_review_ignore", str(exc))
        lang = hint if args.lang == "auto" else args.lang
        report["language"] = lang
        _log(f"[1/6] {len(segments)} text segments, language={lang} ({'sniffed' if args.lang == 'auto' else 'explicit'})", quiet)
        if args.review_ignore:
            _log(f"[1/6] review-ignore: {len(review_ignore)} non-card row(s) exempt from review", quiet)

        src = audio_mod.decode(args.audio)
        report["source"] = {
            "sample_rate": src.sample_rate,
            "channels": src.channels,
            "num_samples": src.frames,
            "duration_sec": round(src.duration, 4),
            "codec": src.codec,
        }
        audio_mod.check_disk_space(out_dir, int(src.frames * src.channels * 4 * 1.2) + 5_000_000)
        _log(f"[2/6] decoded {src.frames} samples @ {src.sample_rate} Hz x {src.channels} ch "
             f"({src.duration:.2f}s)", quiet)

        model = align_mod.load_model(args.model_dir)
        mono = src.mono                                  # source rate: all cuts happen here
        mono16 = audio_mod.resample_to(mono, src.sample_rate, 16000)   # MLX requires 16 kHz

        asr_words = None
        asr_stats = None
        if not args.no_asr_check:
            _log("[3/6] chunked ASR (120 s windows, word timestamps) ...", quiet)
            try:
                asr_words, asr_stats = align_mod.transcribe_windows(mono16, lang, model=model)
            except Exception as exc:  # noqa: BLE001 - fall back to one long pass
                _log(f"[3/6] chunked ASR failed ({type(exc).__name__}); using one pass instead", quiet)
                asr_words = align_mod.transcribe(mono16, lang, model=model)
                asr_stats = {"chunks": 1, "words": len(asr_words)}
            report["asr_words"] = len(asr_words)
            report["asr"] = asr_stats

        aligned, info = None, None
        wave_cuts = False
        if asr_words and align_mod.can_anchor(segments):
            _log("[4/6] anchoring user text to the ASR word timeline ...", quiet)
            try:
                aligned, info = align_mod.anchor(asr_words, segments, src.duration)
            except align_mod.ModelError:
                aligned, info = None, None
            if info is not None and info.get("matched", 0.0) < 0.5:
                aligned, info = None, None
            if aligned is not None:
                wave_cuts = True
                for key in ("window_sec", "overlap_sec", "chunks"):
                    if asr_stats and key in asr_stats:
                        info[key] = asr_stats[key]
                info["cut_mode"] = "wave"
        if aligned is None:
            _log("[4/6] force-aligning user text (fallback; word level) ...", quiet)
            aligned, info = align_mod.align(mono16, segments, lang, model=model)
            info["cut_mode"] = "edge"
        report["alignment"] = info
        guard = _alignment_guard(info, asr_words)
        if guard:
            return fail(EXIT_REVIEW, "alignment_incomplete", guard)
        wave_warn = _wave_match_warning(info)
        if wave_warn:
            report["warnings"].append({"kind": "wave_low_match", "message": wave_warn})
            _log(f"[4/6] WARNING: {wave_warn}", quiet)
        if info.get("blocks") != len(segments):
            return fail(EXIT_REVIEW, "align_block_mismatch",
                        f"aligner produced {info.get('blocks')} blocks for {len(segments)} text segments; "
                        "the text may not match the audio")

        _log("[5/6] planning and verifying cut points ...", quiet)
        analysis = plan_mod.analyze(mono, src.sample_rate, segments, lang, aligned, asr_words,
                                    run_asr_check=not args.no_asr_check, wave_cuts=wave_cuts,
                                    review_ignore=review_ignore)
        report["status"] = analysis.status
        report["checks"] = _checks_summary(analysis)
        report["issues"] = analysis.issues
        report.update(_review_report_fields(analysis, review_ignore))
        report["segments_detail"] = [p.to_dict(src.sample_rate) for p in analysis.plans]

        # --- render -------------------------------------------------------
        os.makedirs(out_dir, exist_ok=True)
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir)
        os.makedirs(work_dir)

        if analysis.status == "ok":
            _log("[6/6] writing pieces, verifying, zipping ...", quiet)
            files = out_mod.write_pieces(analysis, src.pcm, src.sample_rate, work_dir)
            ver = out_mod.verify_pieces(analysis, src.pcm, src.sample_rate, files)
            report["verification"] = ver
            if not ver["ok"]:
                return fail(EXIT_ERROR, "verify_failed", "piece verification failed", "; ".join(ver["problems"]))
            rows = out_mod.index_rows(analysis, src.sample_rate, src.channels, files)
            out_mod.write_json(os.path.join(work_dir, "index.json"), {
                "audio": os.path.basename(args.audio),
                "sample_rate": src.sample_rate, "channels": src.channels,
                "source_num_samples": src.frames, "language": lang,
                "segment_rule": args.segments, "segments": rows})
            _write_csv(os.path.join(work_dir, "index.csv"), rows)
            side = out_mod.write_text_sidecars(analysis, args.text, work_dir)
            report["text_files"] = {k: os.path.basename(v) for k, v in side.items()}
            out_mod.write_json(os.path.join(work_dir, "report.json"), report)
            entries = [os.path.basename(p) for p in files.values()] + [
                "index.json", "index.csv", "report.json", "segments.txt", "original_text.txt"]
            zip_path = out_mod.build_zip(os.path.dirname(out_dir),
                                         os.path.basename(out_dir) + ".zip", work_dir, entries)
            zver = out_mod.verify_zip(zip_path, analysis)
            report["zip"] = {"path": zip_path, "entries": zver["entries"], "verified": zver["ok"]}
            if not zver["ok"]:
                os.remove(zip_path)
                return fail(EXIT_ERROR, "zip_verify_failed", "ZIP verification failed", "; ".join(zver["problems"]))
            shutil.rmtree(work_dir, ignore_errors=True)
            return _finish_report(out_dir, report, started, quiet, EXIT_OK)

        # needs_review: small preview pack only
        _log("[6/6] review needed - writing previews + problem report only", quiet)
        picks = plan_mod.preview(analysis, 3)
        made = out_mod.render_preview(analysis, src.pcm, src.sample_rate, work_dir, picks)
        out_mod.write_json(os.path.join(work_dir, "report.json"), report)
        issues_path = os.path.join(work_dir, "issues.md")
        with open(issues_path, "w", encoding="utf-8") as fh:
            fh.write(_issues_markdown(analysis, src.sample_rate))
        entries = made + ["report.json", "issues.md"]
        zip_path = out_mod.build_zip(os.path.dirname(out_dir),
                                     os.path.basename(out_dir) + "_review.zip", work_dir, entries)
        report["zip"] = {"path": zip_path, "entries": len(entries), "verified": True}
        report["preview_files"] = made
        shutil.rmtree(work_dir, ignore_errors=True)
        return _finish_report(out_dir, report, started, quiet, EXIT_REVIEW, final=True)

    except (audio_mod.AudioError, TextError) as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return fail(EXIT_ERROR, "input_error", str(exc))
    except MemoryError:
        shutil.rmtree(work_dir, ignore_errors=True)
        return fail(EXIT_ERROR, "out_of_memory", "ran out of memory decoding/handling the audio")
    except FileExistsError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return fail(EXIT_ERROR, "output_exists", str(exc))
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(work_dir, ignore_errors=True)
        kind = "model_error" if "model" in type(exc).__name__.lower() or "Model" in str(exc)[:80] else "internal"
        return fail(EXIT_ERROR, kind, f"{type(exc).__name__}: {exc}", traceback.format_exc()[-3000:])


def _checks_summary(analysis) -> dict:
    total = len(analysis.plans)
    by_flag: dict[str, int] = {}
    for p in analysis.plans:
        for f in p.flags:
            by_flag[f] = by_flag.get(f, 0) + 1
    return {
        "segments": total,
        "cuts": max(0, total - 1),
        "silence_centre_cuts": sum(1 for p in analysis.plans if (p.cut_out or {}).get("mode") == "silence_centre"),
        "quiet_edge_cuts": sum(1 for p in analysis.plans if (p.cut_out or {}).get("mode") == "quiet_edge"),
        "touching_cuts": sum(1 for p in analysis.plans if (p.cut_out or {}).get("mode") == "touching"),
        "flags": by_flag,
        "issues": len(analysis.issues),
        "ignored_issues": sum(1 for i in analysis.issues if i.get("ignored")),
        "min_cut_evidence": min([p.evidence for p in analysis.plans], default=1.0),
    }


def _write_csv(path: str, rows: list[dict]) -> None:
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["segment", "text", "start_sec", "end_sec", "duration_sec",
                    "start_sample", "end_sample", "num_samples", "file"])
        for r in rows:
            w.writerow([r["segment"], r["text"], r["start_sec"], r["end_sec"], r["duration_sec"],
                        r["start_sample"], r["end_sample"], r["num_samples"], r["file"]])


def _issues_markdown(analysis, sample_rate: int) -> str:
    review = [i for i in analysis.issues if not i.get("ignored")]
    ignored = [i for i in analysis.issues if i.get("ignored")]
    lines = ["# 需要人工复核", ""]
    for issue in review:
        segs = ", ".join(str(s) for s in issue.get("segments", []))
        lines.append(f"- [{issue['kind']}] 段 {segs}: {issue['detail']}")
    if ignored:
        lines.append("")
        lines.append("## 已忽略（不进卡组）")
        lines.append("")
        lines.append("以下问题只出现在非卡片行（开场 / 报幕 / 指令 / 结尾等），"
                     "不影响任何进卡组的片段；保留在此供审计。")
        lines.append("")
        for issue in ignored:
            segs = ", ".join(str(s) for s in issue.get("segments", []))
            lines.append(f"- [{issue['kind']}] 段 {segs}: {issue['detail']}")
    lines.append("")
    lines.append("| 段 | 起 | 止 | 时长 | 证据 | flags |")
    lines.append("|---|---|---|---|---|---|")
    for p in analysis.plans:
        lines.append(f"| {p.index} | {p.start_sample/sample_rate:.3f} | {p.end_sample/sample_rate:.3f} | "
                     f"{p.duration(sample_rate):.3f} | {p.evidence:.2f} | {', '.join(p.flags) or '-'} |")
    return "\n".join(lines) + "\n"


def _finish_report(out_dir: str, report: dict, started: float, quiet: bool, code: int, final: bool = False) -> int:
    report["elapsed_sec"] = round(time.time() - started, 2)
    report["status"] = report.get("status") if code == EXIT_OK else report["status"]
    if code == EXIT_OK:
        report["status"] = "ok"
    elif code == EXIT_REVIEW:
        report["status"] = "needs_review"
    else:
        report["status"] = "error"
    report["exit_code"] = code
    try:
        # Always leave a machine-readable report for failures and review runs; for a
        # successful run the report was already placed inside the ZIP.
        if final or code != EXIT_OK:
            os.makedirs(out_dir, exist_ok=True)
            out_mod.write_json(os.path.join(out_dir, "report.json"), report)
        elif os.path.isdir(out_dir) and not os.path.exists(os.path.join(out_dir, "report.json")):
            out_mod.write_json(os.path.join(out_dir, "report.json"), report)
    except OSError:
        pass
    if quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_summary(report, stream=sys.stderr)
    return code


def _print_summary(report: dict, stream=None) -> None:
    """Human summary. Goes to stderr so stdout stays clean for machine use."""
    out = stream or sys.stderr
    print(file=out)
    print(f"STATUS: {report['status']}  (exit {report['exit_code']}, {report.get('elapsed_sec', 0)}s)", file=out)
    if report.get("zip"):
        print(f"ZIP:    {report['zip']['path']}", file=out)
    for issue in report.get("issues", [])[:8]:
        tag = " [ignored: non-card row]" if issue.get("ignored") else ""
        print(f"ISSUE:  [{issue['kind']}]{tag} {issue['detail']}", file=out)
    for err in report.get("errors", []):
        print(f"ERROR:  [{err['kind']}] {err['message']}", file=out)
    if report.get("checks"):
        c = report["checks"]
        ignored = c.get("ignored_issues", 0)
        extra = f", {ignored} ignored (non-card rows)" if ignored else ""
        print(f"CHECKS: {c['cuts']} cuts ({c['silence_centre_cuts']} in silence, {c['quiet_edge_cuts']} on edges, "
              f"{c['touching_cuts']} touching), {c['issues']} issues{extra}", file=out)
    print(file=out)