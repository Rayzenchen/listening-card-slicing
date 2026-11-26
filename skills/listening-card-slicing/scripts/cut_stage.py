#!/usr/bin/env python3
"""cut_stage.py -- E6 (cut) and E7 (pack) as one blocking, idempotent wrapper.

``--stage cut`` (E6)
    Refuses to start unless E4 (assembled 5-column manifest) and E5 (translated
    6-column manifest, user approved) both pass, then runs the workspace cutter
    (``<work>/系统/tools/cut``) with the assembled ``切分文本.txt``.  The
    cutter's exit code is passed through (0 ok / 3 needs_review / 1 error /
    2 usage).  The authoritative ``report.json`` is copied to
    ``$RUN/docs/<id>/cut-report.json`` and a receipt is written.  For
    ``needs_review`` the ``issues.md`` inside ``<结果目录>_review.zip`` is
    extracted next to the report and printed, so the agent can show the user
    the exact segment numbers.

``--stage pack`` (E7)
    Requires E4/E5/E6, takes the ZIP path **only** from
    ``cut-report.json.zip.path`` (A5) and runs the workspace
    ``split_cards.py`` with the **6-column** ``doc.json.manifest`` to build the
    cards-only pack (the 6-column manifest is copied into the pack).  Writes
    ``$RUN/docs/<id>/pack.json`` + ``receipts/pack.json``.  A non-empty target
    directory is exit 2 (never overwrite a delivered pack).

``--reset`` (A7, cut/all only)
    Deletes exactly the three paths derived from ``doc.result_dir``
    (``<result>``, ``<result>.zip``, ``<result>_review.zip``) and only after
    resolving them inside the output area.  Any other path is refused.

``--stage all`` runs cut and then pack in the same process (still blocking;
run it as a background job from the caller).

Content binding (0.0.8): the cut report and the cut receipt carry
``text_sha256`` (the ``切分文本.txt`` actually handed to the cutter) and
``manifest_sha256`` -- the **E4-frozen 5-column manifest** (``manifest-5col.md``,
falling back to the 产出区 copy only before E5).  E5 overwrites the live
manifests with the 6-column version, so the cut must never bind that one; this
is what lets ``gate e6`` tell whether a ZIP really belongs to the current cut
text.  On ``--stage all``, an existing result directory/ZIP whose report binds
the **same** text sha is resumed (cut skipped, pack runs); a mismatch or a
missing binding is exit 2 and asks for ``--reset`` instead of silently packing
a stale ZIP.

Review-ignore (0.0.6, DESIGN C1): before cutting, the 1-based numbers of the
``keep=false`` rows in ``segments.json`` are written to
``$RUN/docs/<id>/review-ignore.json``; when the installed ``cut`` advertises
``--review-ignore`` (probed once through ``cut --help``, cached per process),
the file is handed to the cutter so a review issue that only touches non-card
rows no longer blocks E6.  A cutter without the flag keeps the 0.0.5 behaviour
and the receipt records the warning ``cutaudio_old_no_review_ignore``.  The
cutter's ``review_ignore`` / ``ignored_issues`` fields survive verbatim in
``cut-report.json`` for the E6 audit; the receipt records the file path and the
row count.  ``--stage all`` resume and ``_resume_reason`` are unchanged: a
rewritten ``segments.json`` is caught by ``gate e6`` as before.

``doc.json.pack_manifest`` is written by ``--stage pack`` (E7), not by E5:
before the pack exists there is nothing to point at.

Serial (0.0.10, D3): every ``--stage cut|pack|all`` holds
``$RUN/.locks/e6-e7.lock`` (``fcntl.flock``, ``LOCK_EX|LOCK_NB``) for the whole
process; a second concurrent cut/pack exits 3 with the holder's pid/doc.
``--stage all`` takes the lock once and reuses it for cut + pack.  The file
holds ``{"pid","doc","stage","started_at","started_ts"}`` so ``status.py`` can
print ``CUT_RUNNING``; a crash leaves a stale file that status only WARNs
about (it is never deleted automatically).

Timeout / live log (0.0.10): ``--timeout`` now defaults to 3600 s (a
25-minute audio took ~15 minutes; 2x headroom).  The cutter's stdout+stderr
stream straight into ``$RUN/docs/<id>/cut-cutter.log`` (line buffered,
``PYTHONUNBUFFERED=1``) instead of ``capture_output=True``; the last 200 lines
are appended to the StageLog and the path lands in ``cut-report.json`` and the
receipt as ``cutter_log``.  The cutter is invoked **without** ``--json``
(cutaudio treats ``--json`` as quiet and would suppress all progress lines);
the report is read from ``report.json`` and the exit code is passed through, so
exit-code semantics are unchanged.

Pre/post guards (0.0.10, D3): before the cut, a CJK line in
``切分文本.txt`` requires cutaudio >= 1.1.1 (1.1.0 gives up ASR anchoring for
ANY CJK text and squeezes a long audio); >= 1.1.1 proceeds record
``mixed_script_text=true``.  After the cut, ``alignment.cut_mode == "edge"``
with ``coverage < 0.95`` is exit 3 ("不要用这份切分"), a missing coverage field
only WARNs (old cutaudio), and ``wave`` + ``matched < 0.9`` only WARNs.

Exit codes: cut passes the cutter through (0/1/2/3); pack is 0 / 1 (split
failed) / 2 (missing report, bad ZIP, target not empty); pre-checks and safety
refusals are 1 / 2, the serial lock and the two 0.0.10 guards are 3.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402
import gates  # noqa: E402

# --- 0.0.10 (D3) constants --------------------------------------------------

#: Serial lock shared by E6 (cut) and E7 (pack), relative to ``$RUN``.
LOCK_RELATIVE = os.path.join(".locks", "e6-e7.lock")
#: Default cut timeout: a 25-minute audio measured ~15 minutes; 2x headroom.
DEFAULT_TIMEOUT = 3600
#: Live cutter log (stdout+stderr) next to ``cut-report.json``.
CUTTER_LOG_NAME = "cut-cutter.log"
#: How many trailing cutter-log lines are copied into the StageLog.
CUTTER_LOG_TAIL_LINES = 200
#: ``alignment.cut_mode == "edge"`` must cover at least this much audio.
COVERAGE_MIN = 0.95
#: ``wave`` mode below this ``matched`` only warns (ASR timeline still exists).
WAVE_MATCHED_MIN = 0.90
MIXED_SCRIPT_WARNING = "mixed_script_text=true"
COVERAGE_MISSING_WARNING = "alignment_coverage_missing"
LOW_MATCHED_WARNING = "alignment_low_matched"
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
#: ``cut --version`` answer per cutter (realpath), cached per process.
_CUT_VERSIONS = {}


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "cut_stage.py",
        "E6/E7：调工作区 cut 切音频、读 report.json、调 split_cards.py 分卡；不覆盖、不拼路径。",
        epilog="cut 的退出码透传（0 ok / 3 needs_review / 1 error / 2 usage）；pack 为 0/1/2。\n"
               "E6/E7 必须串行：同一 $RUN 同时只允许一个 cut/pack，锁冲突 exit 3。\n"
               "25 分钟音频实测约 15 分钟；--timeout 默认 3600 给 2 倍余量。\n"
               "原文含中文行需 cutaudio >= 1.1.1，否则切前 exit 3；"
               "edge 模式覆盖率 < 95% 切后 exit 3。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--stage", required=True, choices=("cut", "pack", "all"),
                        help="cut=E6 切分；pack=E7 分卡；all=先切再分卡")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="单次 cut 的超时秒数（默认 3600；25 分钟音频实测约 "
                             "15 分钟，给 2 倍余量）")
    parser.add_argument("--reset", action="store_true",
                        help="切分前删除 doc.result_dir 对应的旧结果目录/ZIP（A7）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- paths / guards ---------------------------------------------------------


def _paths(run: dict, doc: dict) -> dict:
    work = os.path.abspath(run.get("work") or "")
    doc_dir = common.doc_dir(run["run"], doc["id"])
    result_dir = common.resolve_work_path(work, doc["result_dir"])
    pack_dir = common.resolve_work_path(work, doc["pack_dir"])
    manifest = common.resolve_work_path(work, doc["manifest"])
    area = os.path.dirname(os.path.abspath(pack_dir))
    return {
        "work": work,
        "doc_dir": doc_dir,
        "result_dir": os.path.abspath(result_dir),
        "zip": os.path.abspath(result_dir) + ".zip",
        "review_zip": os.path.abspath(result_dir) + "_review.zip",
        "pack_dir": os.path.abspath(pack_dir),
        "manifest": os.path.abspath(manifest),
        "area": area,
        "manifest5": os.path.join(doc_dir, "manifest-5col.md"),
        "cut_text": os.path.join(doc_dir, "切分文本.txt"),
        "review_ignore": os.path.join(doc_dir, "review-ignore.json"),
    }


def _guard_area(paths: dict, path: str, stem: str) -> None:
    """A7: only the paths derived from doc.result_dir, inside the output area."""
    area_real = os.path.realpath(paths["area"])
    real = os.path.realpath(path)
    if os.path.dirname(real) != area_real:
        raise common.Failure(common.EXIT_USAGE,
                             "拒绝清理产出区之外的路径：%s" % path)
    if not os.path.basename(real).startswith(stem + "_切分结果"):
        raise common.Failure(common.EXIT_USAGE,
                             "拒绝清理不是本 doc 结果目录的路径：%s" % path)


def _reset_residuals(paths: dict, stem: str, log) -> list:
    removed = []
    for target in (paths["result_dir"], paths["zip"], paths["review_zip"]):
        if not os.path.exists(target):
            continue
        _guard_area(paths, target, stem)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        removed.append(target)
        log("reset removed %s" % target)
    return removed


def _residuals(paths: dict) -> list:
    return [path for path in (paths["result_dir"], paths["zip"], paths["review_zip"])
            if os.path.exists(path)]


def _sha_or_none(path: str):
    try:
        return common.sha256_file(path) if os.path.isfile(path) else None
    except OSError:
        return None


def _cut_bindings(paths: dict) -> dict:
    """The content identity recorded in cut-report.json / receipts/cut.json.

    0.0.8: E5 rewrites the live manifests with the 6-column version, so the
    cut must bind the **E4-frozen** ``manifest-5col.md``.  Only before E5
    (no snapshot yet) may the 产出区 copy be used as the fallback.
    """
    manifest_sha = _sha_or_none(paths["manifest5"])
    if manifest_sha is None:
        manifest_sha = _sha_or_none(paths["manifest"])
    return {
        "text_sha256": _sha_or_none(paths["cut_text"]),
        "manifest_sha256": manifest_sha,
    }


def _resume_reason(paths: dict):
    """``None`` when the existing result/ZIP may be reused for the current text.

    Returns a human reason otherwise (missing/stale report, changed text,
    unverified or missing ZIP).  Used by ``--stage all`` only; a plain
    ``--stage cut`` still refuses to overwrite anything.
    """
    report_path = os.path.join(paths["doc_dir"], "cut-report.json")
    if not os.path.isfile(report_path):
        return "缺 cut-report.json"
    try:
        report = common.read_json(report_path)
    except common.Failure as exc:
        return "cut-report.json 不可读：%s" % exc.message
    if report.get("status") != "ok":
        return "cut-report.status=%r" % report.get("status")
    current = _sha_or_none(paths["cut_text"])
    if not current:
        return "缺切分文本.txt（先跑 assemble.py）"
    if report.get("text_sha256") != current:
        return "cut-report 绑定的切分文本 sha 与当前不一致（切分文本变过）"
    zip_info = report.get("zip") or {}
    zip_path = zip_info.get("path") or ""
    if zip_info.get("verified") is not True or not os.path.isfile(zip_path):
        return "cut-report 记录的 ZIP 不存在或未验证"
    return None


def _require_gates(run_dir: str, doc_id: str, stages, log) -> None:
    for stage in stages:
        checker = gates.run_stage(stage, run_dir, doc_id)
        for line in checker.lines():
            log("gate " + line)
        if not checker.passed:
            for line in checker.lines():
                print(line)
            raise common.Failure(
                common.EXIT_FAIL,
                "前置关卡 %s 未通过：%s（不跳步、不硬闯；按 E4 汇编 → E5 定稿 → "
                "E6 切分 → E7 分包 的顺序补做）" % (stage, checker.verdict()),
                failed_gate=stage,
            )


# --- 0.0.10 (D3): serial lock ------------------------------------------------


class E6E7Lock:
    """``$RUN/.locks/e6-e7.lock`` held for the whole cut/pack invocation.

    ``fcntl.flock(LOCK_EX | LOCK_NB)``: there is no waiting queue, a second
    E6/E7 process exits 3 immediately with the holder's pid/doc/stage.  The
    file content is for humans (``status.py`` reads it to print
    ``CUT_RUNNING``); the OS lock, not the file, is what serialises.
    """

    def __init__(self, run_dir: str, doc_id: str, stage: str):
        self.run_dir = os.path.abspath(run_dir)
        self.doc = doc_id
        self.stage = stage
        self.path = os.path.join(self.run_dir, LOCK_RELATIVE)
        self.handle = None

    @staticmethod
    def _read(handle) -> dict:
        try:
            handle.seek(0)
            raw = handle.read()
        except OSError:
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        payload = {
            "pid": os.getpid(),
            "doc": self.doc,
            "stage": self.stage,
            "started_at": common.now_iso(),
            "started_ts": round(time.time(), 3),
        }
        self.handle.seek(0)
        self.handle.truncate()
        json.dump(payload, self.handle, ensure_ascii=False)
        self.handle.flush()
        try:
            os.fsync(self.handle.fileno())
        except OSError:  # pragma: no cover - filesystems without fsync
            pass

    def acquire(self) -> "E6E7Lock":
        common.ensure_dir(os.path.dirname(self.path))
        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:
            raise common.Failure(
                common.EXIT_FAIL,
                "建不了 E6/E7 串行锁（%s）：%s" % (self.path, exc),
            ) from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read(handle)
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass
            raise common.Failure(
                common.EXIT_ASK,
                "另一个 cut/pack 正在跑（pid=%s, doc=%s, stage=%s）；"
                "E6/E7 必须串行，等它结束再跑。"
                % (holder.get("pid", "未知"), holder.get("doc", "未知"),
                   holder.get("stage", "未知")),
                reason="cut_lock_busy",
                lock=self.path,
            ) from exc
        self.handle = handle
        self._write()
        return self

    def update_stage(self, stage: str) -> None:
        """Record the sub-stage (cut -> pack) without releasing the lock."""
        if self.handle is not None and stage != self.stage:
            self.stage = stage
            self._write()

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        # Unlink *while still holding* the flock: a contender that already
        # opened this inode fails its LOCK_NB now; later contenders get a
        # fresh file.  A killed process never reaches here, which is exactly
        # how the stale lock that status.py warns about is left behind.
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        try:
            handle.close()
        except OSError:  # pragma: no cover
            pass


# --- 0.0.10 (D3): pre-cut mixed-script guard --------------------------------


def cut_version(cut: str):
    """``(major, minor, patch)`` from ``cut --version``; ``None`` when unknown.

    Cached per cutter realpath so the probe runs at most once per process (it
    never loads the ASR model).  A cutter without ``--version`` answers
    ``None``, which the mixed-script guard treats as < 1.1.1 (the safe side).
    """
    key = os.path.realpath(cut)
    if key not in _CUT_VERSIONS:
        try:
            proc = subprocess.run([cut, "--version"], capture_output=True,
                                  text=True, timeout=120)
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            match = _VERSION_RE.search(text)
            _CUT_VERSIONS[key] = (
                tuple(int(part) for part in match.groups())
                if proc.returncode == 0 and match else None)
        except (OSError, subprocess.SubprocessError):
            _CUT_VERSIONS[key] = None
    return _CUT_VERSIONS[key]


def _version_text(version) -> str:
    return ".".join(str(part) for part in version) if version else "未知"


def mixed_script_guard(paths: dict, cut: str, log, warnings: list) -> None:
    """Refuse mixed-script text on cutaudio < 1.1.1 **before** the long cut.

    1.1.0's ``can_anchor()`` bails out on the first CJK segment, so the whole
    document loses the ASR timeline and a 25-minute audio is squeezed into the
    first ~18 minutes (measured: 1089.96 s for a 1518 s audio).  The probe is
    cheap; the cut it saves is ~15 minutes.
    """
    text = common.read_text(paths["cut_text"])
    cjk_lines = [line for line in text.splitlines() if CJK_RE.search(line)]
    if not cjk_lines:
        return
    version = cut_version(cut)
    if version is not None and version >= (1, 1, 1):
        warnings.append(MIXED_SCRIPT_WARNING)
        log("WARN 原文含 %d 行中文，cutaudio %s >= 1.1.1：继续切分（%s）"
            % (len(cjk_lines), _version_text(version), MIXED_SCRIPT_WARNING))
        return
    raise common.Failure(
        common.EXIT_ASK,
        "原文含中文行（%d 行），cutaudio %s 会放弃 ASR 锚定并把长音频压时；"
        "请先安装 1.1.1（见 系统/README），或把非英文行改为纯英文。"
        % (len(cjk_lines), _version_text(version)),
        reason="mixed_script_text",
        cut_version=_version_text(version),
    )


# --- 0.0.10 (D3): post-cut coverage guard -----------------------------------


def alignment_guard(report: dict, doc: dict, log, warnings: list) -> None:
    """Refuse an ``edge`` cut whose alignment covers < 95% of the audio.

    ``coverage`` is added by cutaudio 1.1.1; a report without it (old cutter)
    only WARNs, and ``wave`` mode never blocks because it still has the ASR
    timeline (low ``matched`` is a quality hint, not a wrong timeline).
    """
    alignment = report.get("alignment")
    if not isinstance(alignment, dict):
        return
    mode = alignment.get("cut_mode")
    if mode == "edge":
        coverage = alignment.get("coverage")
        if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
            warnings.append(COVERAGE_MISSING_WARNING)
            log("WARN alignment.coverage 缺失（旧 cutaudio）；按旧口径放行（%s）"
                % COVERAGE_MISSING_WARNING)
            return
        coverage = float(coverage)
        if coverage >= COVERAGE_MIN:
            log("coverage=%.3f >= %.2f（edge），放行" % (coverage, COVERAGE_MIN))
            return
        duration = alignment.get("duration")
        seconds = "秒数未知"
        if (isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration > 0 and coverage > 0):
            seconds = "%.1f/%.1f 秒" % (float(duration), float(duration) / coverage)
        raise common.Failure(
            common.EXIT_ASK,
            "对齐只覆盖 %.1f%% 音频（%s，coverage=%.3f）；大概率是原文含中文/"
            "原文与音频不匹配。不要用这份切分；先处理 %s 的原文或升级 cutaudio。"
            % (coverage * 100, seconds, coverage, doc["id"]),
            reason="alignment_incomplete",
            coverage=round(coverage, 3),
            cut_mode="edge",
        )
    if mode == "wave":
        matched = alignment.get("matched")
        if (isinstance(matched, (int, float)) and not isinstance(matched, bool)
                and float(matched) < WAVE_MATCHED_MIN):
            warnings.append(LOW_MATCHED_WARNING)
            log("WARN wave 模式 matched=%.3f < %.2f；有 ASR 时间轴，通常仍可用，"
                "不拦（%s）" % (float(matched), WAVE_MATCHED_MIN,
                                LOW_MATCHED_WARNING))


# --- E6 review-ignore (0.0.6, DESIGN C1) ------------------------------------

#: ``cut --help`` answer per cutter (realpath), cached so the probe runs once
#: per process and never loads the ASR model.
_CUT_REVIEW_IGNORE_SUPPORT = {}

#: Warning token recorded when the installed cutaudio predates the flag.
REVIEW_IGNORE_WARNING = "cutaudio_old_no_review_ignore"


def review_ignore_numbers(doc_dir: str) -> list:
    """1-based numbers of the ``segments.json`` rows that do not enter cards."""
    segments_path = os.path.join(doc_dir, "segments.json")
    data = common.read_json(segments_path)
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise common.Failure(common.EXIT_FAIL,
                             "segments.json 没有 rows 数组：%s" % segments_path)
    numbers = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            number = int(row["n"])
        except (KeyError, TypeError, ValueError):
            raise common.Failure(common.EXIT_FAIL,
                                 "segments.json 的行缺少整数 n：%r" % (row,))
        if not row.get("keep"):
            numbers.append(number)
    return sorted(numbers)


def write_review_ignore(paths: dict, doc: dict, log) -> list:
    """Write ``review-ignore.json``; return the non-card row numbers.

    The file is written on every cut (even with an empty list) so the audit
    always has one artefact; it is only handed to the cutter when the
    installed cutaudio advertises ``--review-ignore``.
    """
    numbers = review_ignore_numbers(paths["doc_dir"])
    common.atomic_write_json(paths["review_ignore"], {
        "version": 1,
        "doc": doc["id"],
        "segments": numbers,
    })
    log("review-ignore: %d non-card rows" % len(numbers))
    log("wrote %s" % paths["review_ignore"])
    return numbers


def cut_supports_review_ignore(cut: str) -> bool:
    """Whether ``cut --help`` advertises ``--review-ignore`` (probed once).

    A failed probe (old cutter, OSError, timeout) answers ``False``: the
    0.0.5 behaviour is the safe fallback and E6 then stops on the whole-text
    review issues exactly as it did before 0.0.6.
    """
    key = os.path.realpath(cut)
    if key not in _CUT_REVIEW_IGNORE_SUPPORT:
        try:
            proc = subprocess.run([cut, "--help"], capture_output=True,
                                  text=True, timeout=120)
            _CUT_REVIEW_IGNORE_SUPPORT[key] = (
                proc.returncode == 0
                and "--review-ignore" in (proc.stdout or ""))
        except (OSError, subprocess.SubprocessError):
            _CUT_REVIEW_IGNORE_SUPPORT[key] = False
    return _CUT_REVIEW_IGNORE_SUPPORT[key]


def _find_report(paths: dict):
    report_path = os.path.join(paths["result_dir"], "report.json")
    if os.path.isfile(report_path):
        return common.read_json(report_path)
    for archive in (paths["zip"], paths["review_zip"]):
        if os.path.isfile(archive) and zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as handle:
                names = [name for name in handle.namelist() if name.endswith("report.json")]
                if names:
                    return json.loads(handle.read(names[0]).decode("utf-8"))
    return None


def _extract_issues(paths: dict, doc_dir: str, log) -> str | None:
    if not (os.path.isfile(paths["review_zip"]) and zipfile.is_zipfile(paths["review_zip"])):
        return None
    with zipfile.ZipFile(paths["review_zip"]) as handle:
        names = [name for name in handle.namelist() if name.endswith("issues.md")]
        if not names:
            return None
        target = os.path.join(doc_dir, "issues.md")
        common.atomic_write_bytes(target, handle.read(names[0]))
    log("issues extracted to %s" % target)
    return target


def _cutter_log_tail(path: str, limit: int = CUTTER_LOG_TAIL_LINES) -> list:
    """Last ``limit`` non-transformed lines of ``cut-cutter.log`` (never raises)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return lines[-limit:]


def _append_cutter_tail(path: str, log) -> None:
    """Copy the tail of the live cutter log into the StageLog (0.0.10)."""
    lines = _cutter_log_tail(path)
    if not lines:
        log("cut-cutter.log 还没有内容（%s）" % path)
        return
    log("cut-cutter.log tail (%d 行):" % len(lines))
    for line in lines:
        log("cutter| " + line)


def run_cut(run: dict, doc: dict, args, log, lock=None) -> int:
    if lock is None:
        owned = E6E7Lock(run["run"], doc["id"], "cut").acquire()
        try:
            return run_cut(run, doc, args, log, owned)
        finally:
            owned.release()
    lock.update_stage("cut")
    paths = _paths(run, doc)
    stem = doc["stem"]
    _require_gates(run["run"], doc["id"], ("e4", "e5"), log)
    work = paths["work"]
    cut = os.path.join(work, "系统", "tools", "cut")
    if not os.path.isfile(cut):
        raise common.Failure(common.EXIT_USAGE, "切分入口不存在：%s" % cut)
    if not os.path.isfile(paths["cut_text"]):
        raise common.Failure(common.EXIT_USAGE,
                             "缺 切分文本.txt（先跑 assemble.py）：%s" % paths["cut_text"])
    warnings = []
    # 0.0.10 (D3): fail before --reset too (never delete old results for a cut
    # we are about to refuse) and before the ~15 minute subprocess.
    mixed_script_guard(paths, cut, log, warnings)
    if args.reset:
        removed = _reset_residuals(paths, stem, log)
        if not removed:
            log("--reset: no residual result directory/ZIP found")
    else:
        residuals = _residuals(paths)
        if residuals:
            raise common.Failure(
                common.EXIT_USAGE,
                "结果目录/ZIP 已存在，E6 cut 会拒绝覆盖：\n  %s\n"
                "先跑 cleanup.py --run <$RUN> --doc %s 或加 --reset"
                "（只删 doc.result_dir 派生路径）" % ("\n  ".join(residuals), doc["id"]),
            )
    bindings = _cut_bindings(paths)
    common.ensure_dir(paths["area"])
    review_ignore = write_review_ignore(paths, doc, log)
    supports_review_ignore = cut_supports_review_ignore(cut)
    # 0.0.10 stage 2: do NOT pass --json.  cutaudio sets ``quiet = args.quiet
    # or args.json``, so --json would suppress every ``[1/6] .. [6/6]`` progress
    # line and leave the live log empty until the very end (measured on the d02
    # sandbox: 0 bytes at +3 min, everything only after the run).  The machine
    # report is read from ``report.json`` and the exit code is passed through,
    # so the flag was never needed.
    command = [cut, "--audio", os.path.abspath(doc["audio"]),
               "--text", paths["cut_text"], "--out", paths["result_dir"]]
    if review_ignore and supports_review_ignore:
        command.extend(["--review-ignore", paths["review_ignore"]])
        log("passing --review-ignore (%d rows)" % len(review_ignore))
    if not supports_review_ignore:
        warnings.append(REVIEW_IGNORE_WARNING)
        log("WARN %s: cut --help 不含 --review-ignore，按旧行为切分"
            "（gate e6 仍按整份文本复核）" % REVIEW_IGNORE_WARNING)
    cutter_log = os.path.join(paths["doc_dir"], CUTTER_LOG_NAME)
    log("RUN %s" % " ".join(command))
    log("cutter log: %s" % cutter_log)
    started = time.time()
    # 0.0.10: stream stdout+stderr into the log file instead of capturing it
    # until the end, so "tail -f cut-cutter.log" / status.py show progress.
    child_env = dict(os.environ)
    child_env["PYTHONUNBUFFERED"] = "1"
    try:
        with open(cutter_log, "w", encoding="utf-8", buffering=1) as cutter_fh:
            try:
                proc = subprocess.run(command, stdout=cutter_fh,
                                      stderr=subprocess.STDOUT, text=True,
                                      timeout=args.timeout, env=child_env)
            except subprocess.TimeoutExpired:
                cutter_fh.flush()
                _append_cutter_tail(cutter_log, log)
                log("TIMEOUT after %ss" % args.timeout)
                raise common.Failure(
                    common.EXIT_FAIL,
                    "cut 超时（%ss）：%s" % (args.timeout, paths["result_dir"]),
                    reason="cut_timeout", cutter_log=cutter_log)
    except OSError as exc:
        raise common.Failure(common.EXIT_FAIL,
                             "cut 启动失败（%s）：%s" % (cut, exc),
                             cutter_log=cutter_log)
    elapsed = round(time.time() - started, 2)
    _append_cutter_tail(cutter_log, log)
    log("exit=%d elapsed=%ss" % (proc.returncode, elapsed))
    report = _find_report(paths)
    if report is None:
        report = {
            "tool": None,
            "status": "error",
            "exit_code": proc.returncode,
            "segments": 0,
            "issues": [],
            "zip": {},
            "error": "cut 没有留下 report.json（看日志：%s）" % log.path,
        }
    # 0.0.4: bind the report to the exact inputs; gate e6 verifies these.
    report.update(bindings)
    report["cutter_log"] = cutter_log
    common.atomic_write_json(os.path.join(paths["doc_dir"], "cut-report.json"), report)
    # 0.0.10 (D3): keep the report on disk even when the coverage guard refuses
    # it, so the agent can show the user the numbers.
    alignment_guard(report, doc, log, warnings)
    issues_path = None
    if report.get("status") == "needs_review":
        issues_path = _extract_issues(paths, paths["doc_dir"], log)
    receipt = {
        "version": 1,
        "doc": doc["id"],
        "stage": "cut",
        "at": common.now_iso(),
        "exit_code": proc.returncode,
        "status": report.get("status"),
        "segments": report.get("segments"),
        "issues": len(report.get("issues") or []),
        "zip": report.get("zip") or {},
        "out_dir": paths["result_dir"],
        "audio": os.path.abspath(doc["audio"]),
        "text": paths["cut_text"],
        "text_sha256": bindings["text_sha256"],
        "manifest_sha256": bindings["manifest_sha256"],
        "elapsed_sec": elapsed,
        "log": log.path,
        "cutter_log": cutter_log,
        "issues_md": issues_path,
        "review_ignore": {
            "path": paths["review_ignore"],
            "count": len(review_ignore),
            "supported": supports_review_ignore,
            "passed": bool(review_ignore and supports_review_ignore),
        },
        "warnings": warnings,
    }
    receipts_dir = os.path.join(paths["doc_dir"], "receipts")
    common.atomic_write_json(os.path.join(receipts_dir, "cut.json"), receipt)
    log("cut-report.json written (%s)" % (report.get("status")))
    code = proc.returncode
    if code == 0 and report.get("status") != "ok":
        code = common.EXIT_FAIL
    _print_cut_summary(doc, report, paths, issues_path, args.json,
                       receipt["review_ignore"], warnings)
    return code


def _print_cut_summary(doc, report, paths, issues_path, quiet: bool,
                       review_ignore=None, warnings=None) -> None:
    if quiet:
        return
    print("DOC=%s  E6 cut" % doc["id"])
    print("状态：%s  segments=%r" % (report.get("status"), report.get("segments")))
    zip_info = report.get("zip") or {}
    print("ZIP：%s  verified=%r" % (zip_info.get("path"), zip_info.get("verified")))
    issues = report.get("issues") or []
    print("issues：%d" % len(issues))
    for issue in issues[:8]:
        if isinstance(issue, dict):
            segments = "、".join(str(value) for value in (issue.get("segments") or []))
            print("  [%s] 段 %s：%s" % (issue.get("kind"), segments,
                                       str(issue.get("detail") or "")[:160]))
    if issues_path:
        print("ISSUES %s" % issues_path)
    if review_ignore is not None:
        if review_ignore["passed"]:
            note = "（已传给 cut）"
        elif review_ignore["supported"]:
            note = "（没有非卡片行，未传）"
        else:
            note = "（cut 不支持 --review-ignore，按旧行为）"
        print("review-ignore: %d non-card rows%s"
              % (review_ignore["count"], note))
    for warning in warnings or []:
        print("WARN %s" % warning)
    errors = report.get("errors") or []
    for error in errors[:8]:
        if isinstance(error, dict):
            print("  ERROR [%s] %s" % (error.get("kind"), error.get("message")))


# --- E7 ---------------------------------------------------------------------


def _read_pack_files(paths: dict) -> list:
    files = []
    manifest_name = os.path.basename(paths["manifest"])
    for name in sorted(os.listdir(paths["pack_dir"])):
        path = os.path.join(paths["pack_dir"], name)
        if not os.path.isfile(path):
            continue
        if name == manifest_name:
            continue
        match = re.match(r"^(\d{3,})\.wav$", name)
        if not match:
            raise common.Failure(common.EXIT_FAIL,
                                 "包内出现非 wav/清单文件：%s" % name)
        try:
            duration = gates.wav_duration_sec(path)
        except (OSError, ValueError) as exc:
            raise common.Failure(common.EXIT_FAIL, "读不了 %s：%s" % (name, exc))
        files.append({
            "n": int(match.group(1)),
            "file": name,
            "bytes": os.path.getsize(path),
            "duration_sec": round(duration, 6),
            "sha256": common.sha256_file(path),
        })
    files.sort(key=lambda item: item["n"])
    return files


def run_pack(run: dict, doc: dict, args, log, lock=None) -> int:
    """E7: pack the cards-only wavs + the 6-column manifest into 产出区."""
    if lock is None:
        owned = E6E7Lock(run["run"], doc["id"], "pack").acquire()
        try:
            return run_pack(run, doc, args, log, owned)
        finally:
            owned.release()
    lock.update_stage("pack")
    paths = _paths(run, doc)
    doc_dir = paths["doc_dir"]
    _require_gates(run["run"], doc["id"], ("e4", "e5", "e6"), log)
    report_path = os.path.join(doc_dir, "cut-report.json")
    if not os.path.isfile(report_path):
        raise common.Failure(common.EXIT_USAGE,
                             "缺 cut-report.json（先跑 E6 --stage cut）：%s" % report_path)
    cut_report = common.read_json(report_path)
    zip_info = cut_report.get("zip") or {}
    zip_path = zip_info.get("path") or ""
    if cut_report.get("status") != "ok":
        raise common.Failure(common.EXIT_USAGE,
                             "cut-report.status=%r，不能分卡（先过 E6）"
                             % cut_report.get("status"))
    current_text_sha = _sha_or_none(paths["cut_text"])
    if not current_text_sha:
        raise common.Failure(common.EXIT_USAGE,
                             "缺 切分文本.txt，不能证明 ZIP 属于当前文本：%s"
                             % paths["cut_text"])
    if cut_report.get("text_sha256") != current_text_sha:
        raise common.Failure(
            common.EXIT_USAGE,
            "cut-report 未绑定当前切分文本（文本在切分后被改过？），"
            "不能拿旧 ZIP 分卡：重跑 cut_stage --stage cut --reset")
    if zip_info.get("verified") is not True:
        raise common.Failure(common.EXIT_USAGE,
                             "cut-report.zip.verified 不是 true（先过 E6）")
    if not zip_path or not os.path.isfile(zip_path):
        raise common.Failure(common.EXIT_USAGE, "cut-report 记录的 ZIP 不存在：%r" % zip_path)
    if not common.is_within(os.path.abspath(paths["area"]), os.path.abspath(zip_path)):
        raise common.Failure(common.EXIT_USAGE,
                             "ZIP 不在产出区内，拒绝分卡：%s" % zip_path)
    if not os.path.isfile(paths["manifest"]):
        raise common.Failure(common.EXIT_USAGE,
                             "6 列清单不存在：%s（先跑 E4 assemble、E5 translate_apply）"
                             % paths["manifest"])
    manifest_text = common.read_text(paths["manifest"])
    if common.manifest_columns(manifest_text) != 6:
        raise common.Failure(
            common.EXIT_USAGE,
            "E7 分包必须用 6 列清单（含译文），当前还是 5 列：%s\n"
            "先跑 E5（translate_prepare → 翻译员 → translate_apply → approve.py），"
            "不要拿 5 列清单分包" % paths["manifest"])
    if os.path.isdir(paths["pack_dir"]) and os.listdir(paths["pack_dir"]):
        raise common.Failure(common.EXIT_USAGE,
                             "目标包目录非空，拒绝覆盖：%s\n"
                             "旧包先归档（bootstrap --archive-existing）或请用户决定保留" % paths["pack_dir"])
    split_cards = os.path.join(paths["work"], "系统", "tools", "split_cards.py")
    if not os.path.isfile(split_cards):
        raise common.Failure(common.EXIT_USAGE, "分卡脚本不存在：%s" % split_cards)
    command = [sys.executable, split_cards, "--result", zip_path,
               "--manifest", paths["manifest"], "--out", paths["pack_dir"]]
    log("RUN %s" % " ".join(command))
    existed_before = os.path.isdir(paths["pack_dir"])
    proc = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
    log("exit=%d" % proc.returncode)
    if proc.stdout:
        log("stdout:\n" + proc.stdout[-10000:])
    if proc.stderr:
        log("stderr:\n" + proc.stderr[-10000:])
    if proc.returncode != 0:
        if not existed_before and os.path.isdir(paths["pack_dir"]):
            shutil.rmtree(paths["pack_dir"], ignore_errors=True)
        detail = (proc.stderr or proc.stdout).strip().split("\n")[-1][:300]
        raise common.Failure(common.EXIT_FAIL,
                             "split_cards 失败（exit=%d）：%s" % (proc.returncode, detail))
    if not os.path.isdir(paths["pack_dir"]):
        raise common.Failure(common.EXIT_FAIL,
                             "split_cards 返回 0 但没有包目录：%s" % paths["pack_dir"])
    files = _read_pack_files(paths)
    keep_rows = [row for row in common.parse_manifest_6col(paths["manifest"])["rows"]
                 if row["keep"]]
    if [item["n"] for item in files] != [row["n"] for row in keep_rows]:
        raise common.Failure(
            common.EXIT_FAIL,
            "包内 wav 编号与清单进卡组行号不一致（%d 个文件 vs %d 行）"
            % (len(files), len(keep_rows)),
        )
    manifest_sha = common.sha256_file(paths["manifest"])
    pack_report = {
        "version": 1,
        "doc": doc["id"],
        "pack_dir": paths["pack_dir"],
        "manifest": os.path.join(paths["pack_dir"], os.path.basename(paths["manifest"])),
        "manifest_sha256": manifest_sha,
        "manifest_columns": 6,
        "zip_path": zip_path,
        "zip_verified": True,
        "count": len(files),
        "keep_rows": [row["n"] for row in keep_rows],
        "files": files,
        "built_at": common.now_iso(),
    }
    common.atomic_write_json(os.path.join(doc_dir, "pack.json"), pack_report)
    receipt = {
        "version": 1,
        "doc": doc["id"],
        "stage": "pack",
        "at": common.now_iso(),
        "exit_code": 0,
        "pack_dir": paths["pack_dir"],
        "manifest": pack_report["manifest"],
        "manifest_sha256": manifest_sha,
        "manifest_columns": 6,
        "count": len(files),
        "zip_path": zip_path,
        "log": log.path,
    }
    common.atomic_write_json(os.path.join(doc_dir, "receipts", "pack.json"), receipt)
    # 0.0.8: the pack copy is now the canonical 6-column manifest for approve.py /
    # gate e9 fallbacks (E5 wrote only $RUN + 产出区, before the pack existed).
    doc["pack_manifest"] = pack_report["manifest"]
    doc["manifest_sha256_v2"] = manifest_sha
    common.atomic_write_json(os.path.join(doc_dir, "doc.json"), doc)
    log("pack.json written (%d wav)" % len(files))
    _print_pack_summary(doc, pack_report, args.json)
    return common.EXIT_OK


def _print_pack_summary(doc, pack_report, quiet: bool) -> None:
    if quiet:
        return
    print("DOC=%s  E7 pack" % doc["id"])
    print("包：%s" % pack_report["pack_dir"])
    print("wav：%d 个（清单 %d 行）" % (pack_report["count"],
                                    len(pack_report["keep_rows"])))
    print("清单 sha256：%s" % pack_report["manifest_sha256"])


# --- main -------------------------------------------------------------------


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    lock = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": args.stage,
              "doc": args.doc}
    try:
        run = common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        if args.reset and args.stage == "pack":
            raise common.Failure(common.EXIT_USAGE,
                                 "--reset 只能用于 --stage cut/all（A7）")
        log = common.StageLog(run_dir, "e6" if args.stage in ("cut", "all") else "e7",
                              args.doc)
        # 0.0.10 (D3): one lock for the whole process; --stage all takes it once
        # and reuses it for cut + pack.
        lock = E6E7Lock(run_dir, args.doc,
                        "cut" if args.stage in ("cut", "all") else "pack").acquire()
        log("lock acquired: %s (pid=%d, stage=%s)"
            % (lock.path, os.getpid(), lock.stage))
        if args.stage in ("cut", "all"):
            resumed = False
            if args.stage == "all" and not args.reset:
                paths = _paths(run, doc)
                if _residuals(paths):
                    reason = _resume_reason(paths)
                    if reason is not None:
                        raise common.Failure(
                            common.EXIT_USAGE,
                            "结果目录/ZIP 已存在，不能按当前切分文本续跑：%s\n"
                            "重切加 --reset（只删 doc.result_dir 派生路径），"
                            "或先跑 cleanup.py 清掉旧结果" % reason)
                    _require_gates(run_dir, doc["id"], ("e4",), log)
                    log("resume: cut-report.text_sha256 与当前切分文本一致，跳过 cut")
                    if not args.json:
                        print("RESUME cut：切分文本未变，复用已有结果 %s"
                              % paths["result_dir"])
                    code = common.EXIT_OK
                    resumed = True
            if not resumed:
                code = run_cut(run, doc, args, log, lock)
            result.update({"cut_exit": code, "cut_resumed": resumed,
                           "cut_report": os.path.join(
                               common.doc_dir(run_dir, args.doc), "cut-report.json")})
            if code != common.EXIT_OK or args.stage == "cut":
                result["exit_code"] = code
                result["ok"] = code == common.EXIT_OK
                common.emit_json(result)
                return code
        if args.stage in ("pack", "all"):
            code = run_pack(run, doc, args, log, lock)
            result.update({"pack_exit": code,
                           "pack_json": os.path.join(
                               common.doc_dir(run_dir, args.doc), "pack.json")})
            result["exit_code"] = code
            result["ok"] = code == common.EXIT_OK
            common.emit_json(result)
            return code
        result["ok"] = True
        result["exit_code"] = common.EXIT_OK
        common.emit_json(result)
        return common.EXIT_OK
    except common.Failure as failure:
        if log:
            log("FAIL " + failure.message)
        return common.report_failure(args.stage, failure, {"doc": args.doc})
    except OSError as exc:
        return common.report_failure(
            args.stage,
            common.Failure(common.EXIT_FAIL, "文件读写失败：%s" % exc, reason="os_error"),
            {"doc": args.doc},
        )
    finally:
        if lock is not None:
            lock.release()
        if log:
            log.flush()


if __name__ == "__main__":
    sys.exit(main())
