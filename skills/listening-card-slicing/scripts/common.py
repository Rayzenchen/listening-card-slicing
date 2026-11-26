#!/usr/bin/env python3
"""common.py -- deterministic helpers shared by every listening-card-slicing script.

Only the Python standard library is used.  Nothing here talks to the network,
spawns a model, or writes outside the paths it is given.  Every writer is
atomic (a sibling ``.tmp-*`` file followed by ``os.replace``), JSON is UTF-8
with ``ensure_ascii=False, indent=2`` and one trailing newline, and the machine
result of a script is always the last stdout line ``JSON {...}``.

Exit codes used by every script in this skill (see ``references/workflow.md`` §8)::

    0  ok
    1  a check or the execution failed
    2  usage or environment problem
    3  the user has to decide something (stop, do not guess)

The module is imported by ``bootstrap.py`` (E1), ``parse_source.py`` (E2) and
``assemble.py`` (E4) directly (they run from this directory), and by
``gates.py`` / ``gate.py`` (B2).  Tests import it with ``sys.path`` pointing
at ``scripts/``.

Manifest notes
--------------
``render_manifest`` / ``parse_manifest`` are the single source of truth for
the byte-exact ``切分清单.md`` format frozen in ``references/runtime/format.md``.  The
harness never invents a second format: E4 writes with the renderer, gates read
with the parser, and the unit tests compare both against the golden manifest.

Phase 2 (E8/E9) additions
--------------------------
* :func:`render_manifest_6col` appends the 译文 column *on top of* the 5-column
  output, so 一列不改五列 holds by construction; :func:`strip_manifest_translation_column`
  is the exact inverse and :func:`parse_manifest_6col` reads the extended form.
* :func:`split_sentences` splits a manifest 原文 cell into English sentences
  with two abbreviation classes (see the section comment further down):
  ``U.S.`` / ``Mr.`` … protect their final dot, while ``B.C.`` / ``A.D.`` keep
  it so ``... 1480 B.C.`` still ends a sentence.  ``Question N. ...`` is one
  sentence on purpose.  Golden check: all 96 keep rows of 卷1 have exactly as
  many sentences as the cloud card has Chinese lines (0 exceptions).
* :func:`split_sentences_with_speakers` / :func:`ends_sentence` are what E9
  uses to put ``M:`` / ``W:`` only on the line where a sentence starts *and*
  the speaker changes (never on a ``- `` clause line); the 96 golden cloud
  cards are reproduced byte for byte with that rule.
* :func:`render_card_content` is the frozen card template (第二期接口 §5);
  audio ids stay ``{{AUDIO_ID}}`` until the push stage replaces them.

Testing hook
------------
``LISTENING_CARD_RUN_ROOT`` overrides the default ``$RUN`` parent directory
(``/tmp`` per the contract).  It exists so the test-suite can keep every run
inside a pytest ``tmp_path`` and never touch ``/tmp`` or the real workspace.
Production callers should not set it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.dont_write_bytecode = True

# --- contract constants -----------------------------------------------------

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_ASK = 3

EXIT_CODES_HELP = "退出码：0 成功 / 1 检查失败 / 2 用法或环境问题 / 3 需要用户决定"

SKILL_VERSION_FALLBACK = "V-1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)

AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac")
TEXT_EXTS = (".md", ".txt")

#: U+2013 EN DASH, used by the frozen manifest range format (``005–011``).
EN_DASH = "\u2013"
#: U+3001 IDEOGRAPHIC COMMA, separator between manifest ranges.
RANGE_SEP = "\u3001"

SEGMENT_TYPES = ("开场", "报幕", "指令", "正文", "题目", "结尾")
KEEP_TYPES = ("正文", "题目")
KIND_TO_TYPE = {
    "title": "开场",
    "marker": "报幕",
    "directions": "指令",
    "questions_meta": "指令",
    "question": "题目",
    "body": "正文",
    "ending": "结尾",
}
BLOCK_KINDS = tuple(KIND_TO_TYPE)

#: Filled by each ``main()`` right before ``emit_json`` so failures and
#: successes share one shape: ``{"ok": bool, "exit_code": int, "stage": ...}``.
_SPEAKER_RE = re.compile(r"\*\*[MW]:\*\*\s*")
#: Same shape but keeps the ``M`` / ``W`` letter: the stage-1 helper only needs
#: to delete the markers, E8/E9 need to know who is talking.
_SPEAKER_MARK_RE = re.compile(r"\*\*([MW]):\*\*\s*")


class Failure(Exception):
    """A stop condition mapped to one of the documented exit codes.

    ``extra`` is merged into the trailing JSON object so the root agent can
    react without parsing human text (e.g. ``reason="run_mismatch"``).
    """

    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.extra = dict(extra)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# --- small utilities --------------------------------------------------------


def now_iso() -> str:
    """Local time with a UTC offset, second precision: ``2026-11-23T15:00:00+08:00``."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_compact() -> str:
    """Filename-safe local timestamp: ``20261123-150000``."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Stream a file through sha256 (works for 20 MB audio without loading it)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_facts(path: str) -> dict:
    """Identity of one input file, as used by the $RUN key in 接口约定 §3.1."""
    stat = os.stat(path)
    return {
        "path": os.path.abspath(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def skill_version() -> str:
    """Read ``VERSION`` next to ``scripts/``; fall back to the frozen default."""
    path = os.path.join(SKILL_ROOT, "VERSION")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            pass
    return SKILL_VERSION_FALLBACK


def scripts_tree_sha(scripts_dir: str | None = None) -> str:
    """Stable digest of every ``*.py`` in ``scripts/`` (used for drift checks).

    Hash covers only file names and their content, in sorted order, so the
    digest does not change when the directory is copied or installed elsewhere.
    """
    base = scripts_dir or HERE
    names = sorted(
        name for name in os.listdir(base)
        if name.endswith(".py") and os.path.isfile(os.path.join(base, name))
    )
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(os.path.join(base, name)).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def is_within(parent: str, child: str) -> bool:
    """True when ``child`` is ``parent`` or lives below it (both absolute)."""
    parent = os.path.abspath(parent)
    child = os.path.abspath(child)
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:  # different drives on Windows
        return False


def rel_to(work: str, path: str) -> str:
    """Workspace-relative path when possible, absolute otherwise.

    ``run.json`` stores inputs as ``材料区/xx.mp3`` (接口约定 §4.1); paths
    outside the workspace are kept absolute so nothing is ever silently
    rebased.
    """
    path = os.path.abspath(path)
    if is_within(work, path):
        return os.path.relpath(path, os.path.abspath(work))
    return path


def resolve_work_path(work: str, value: str) -> str:
    """Turn a ``doc.json`` output path into an absolute path.

    ``doc.json`` keeps ``产出区/...`` relative to the workspace for the default
    output root and absolute paths for a custom ``--out-root`` (接口约定
    §4.2).  Both spellings are accepted here.
    """
    if os.path.isabs(value):
        return value
    return os.path.join(os.path.abspath(work), value)


# --- atomic writers ---------------------------------------------------------


def _atomic_replace_bytes(path: str, data: bytes) -> None:
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    ensure_dir(directory)
    tmp = os.path.join(directory, ".tmp-%s-%d-%d" % (os.path.basename(path), os.getpid(), time.time_ns()))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_bytes(path: str, data: bytes) -> None:
    _atomic_replace_bytes(path, data)


def atomic_write_text(path: str, text: str) -> None:
    """Write UTF-8 without BOM; the caller owns the trailing newline."""
    _atomic_replace_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str, obj) -> None:
    """Write JSON per 接口约定: UTF-8, ``ensure_ascii=False``, indent 2, one ``\\n``."""
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text)


# --- readers ----------------------------------------------------------------


def read_text(path: str) -> str:
    """Read UTF-8 text, tolerating a BOM (the only change allowed on input)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read()
    except FileNotFoundError:
        raise Failure(EXIT_USAGE, "找不到文件：%s" % path)
    except UnicodeDecodeError as exc:
        raise Failure(EXIT_FAIL, "文件不是 UTF-8：%s（%s）" % (path, exc))
    except OSError as exc:
        raise Failure(EXIT_USAGE, "读不了文件：%s（%s）" % (path, exc))


def read_json(path: str):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise Failure(EXIT_USAGE, "找不到 JSON 文件：%s（上一阶段还没跑？）" % path)
    except json.JSONDecodeError as exc:
        raise Failure(
            EXIT_FAIL,
            "JSON 解析失败：%s（第 %d 行第 %d 列：%s）" % (path, exc.lineno, exc.colno, exc.msg),
        )
    except OSError as exc:
        raise Failure(EXIT_USAGE, "读不了 JSON 文件：%s（%s）" % (path, exc))


# --- text helpers -----------------------------------------------------------


def norm_ws(text: str) -> str:
    """Collapse every whitespace run to one space and strip the ends."""
    return " ".join(str(text).split())


def strip_speaker_markers(text: str) -> str:
    """Drop ``**M:** `` / ``**W:** `` prefixes; the result is the cut text."""
    return _SPEAKER_RE.sub("", text)


def speaker_mark(speaker) -> str:
    return "**%s:** " % speaker if speaker in ("M", "W") else ""


def fmt_row(n: int) -> str:
    """3-digit zero padded row number (``001``)."""
    return "%03d" % int(n)


def compress_ranges(numbers) -> str:
    """Compress row numbers into the frozen manifest range format.

    ``[5,6,7,8,9,10,11]`` -> ``005–011``; ``[12,15]`` -> ``012、015``;
    single numbers stay ``3`` digits and consecutive runs of two become
    ``001–002``.  Duplicates are ignored; order of the input does not matter.
    """
    nums = sorted({int(n) for n in numbers})
    parts = []
    index = 0
    while index < len(nums):
        end = index
        while end + 1 < len(nums) and nums[end + 1] == nums[end] + 1:
            end += 1
        if end > index:
            parts.append("%s%s%s" % (fmt_row(nums[index]), EN_DASH, fmt_row(nums[end])))
        else:
            parts.append(fmt_row(nums[index]))
        index = end + 1
    return RANGE_SEP.join(parts)


_RANGE_PART_RE = re.compile(r"^(\d{3})(?:" + re.escape(EN_DASH) + r"(\d{3}))?$")


def parse_ranges(text: str) -> list:
    """Expand a rendered range string back into sorted row numbers.

    Used by gate e4 to compare the two summary sections with ``segments.json``
    without trusting the renderer.  Raises :class:`Failure` on malformed text.
    """
    text = (text or "").strip()
    if not text or text == EMPTY_RANGES:
        return []
    out = []
    for part in text.split(RANGE_SEP):
        part = part.strip()
        match = _RANGE_PART_RE.match(part)
        if not match:
            raise Failure(EXIT_FAIL, "清单区间格式不对：%r（应为 001–003、005 这种）" % part)
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            raise Failure(EXIT_FAIL, "清单区间倒序：%r" % part)
        out.extend(range(start, end + 1))
    return out


# --- manifest: render + parse (frozen format) -------------------------------

_MANIFEST_TITLE = "# {stem} · 切分清单（含进卡组标记）"
_MANIFEST_LINE2 = "> 与 {stem}_切分文本.txt 逐行一一对应；行号 = 切出的音频编号（001、002…）。"
_MANIFEST_LINE3 = (
    "> **进卡组＝是** 的 {keep} 行 = 正文 {body} + 题目 {questions}；"
    "**进卡组＝否** 的 {skip} 行 = {skip_types}。"
)
#: A1 §2.6(c): the skip-type list is always in this order, only types with
#: a non-zero count appear.
SKIP_TYPE_ORDER = ("报幕", "指令", "开场", "结尾")
EMPTY_RANGES = "（无）"
_MANIFEST_LINE4 = (
    "> 报幕（{labels}）单独成行；“Questions X and Y are based on ...” 算指令，"
    "单独成行但不进卡组；每个 Question 单独成卡。"
)
_MANIFEST_TABLE_HEAD = "| 行 | 类型 | 进卡组 | 位置 | 原文（含说话人标识） |"
_MANIFEST_TABLE_SEP = "|---|---|---|---|---|"
_MANIFEST_TAIL = (
    "切完以后按本表处理：只把「进卡组＝是」的片段放进卡组目录；"
    "其余片段（报幕、指令、开场、结尾）单独放到 _不进卡组/ 目录，并从卡片索引里剔除。"
)
_MANIFEST_KEEP_HEAD = "## 进卡组清单（{n} 行）"
_MANIFEST_SKIP_HEAD = "## 不进卡组清单（{n} 行）"

_LABEL_SPLIT_RE = re.compile(r"^(.*?)([A-Za-z0-9]+)$")
_ROW_CELL_RE = re.compile(r"^\d{3}$")
_KEEP_HEAD_RE = re.compile(r"^## 进卡组清单（(\d+) 行）$")
_SKIP_HEAD_RE = re.compile(r"^## 不进卡组清单（(\d+) 行）$")
_STEM_RE = re.compile(r"^> 与 (.+)_切分文本\.txt 逐行一一对应；行号 = 切出的音频编号（001、002…）。$")
_STATS_RE = re.compile(
    r"^> \*\*进卡组＝是\*\* 的 (\d+) 行 = 正文 (\d+) \+ 题目 (\d+)；"
    r"\*\*进卡组＝否\*\* 的 (\d+) 行 = ([^。]*)。$"
)


def _cell(text: str) -> str:
    """Escape a table cell: 接口约定 §5.2 requires ``|`` -> ``\\|``."""
    return str(text).replace("|", "\\|")


def _split_cells(line: str) -> list:
    """Split one markdown table row on unescaped ``|`` and drop outer empties."""
    cells = []
    buf = []
    escaped = False
    for char in line.strip():
        if escaped:
            buf.append(char)
            escaped = False
            continue
        if char == "\\":
            buf.append(char)
            escaped = True
            continue
        if char == "|":
            cells.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    cells.append("".join(buf))
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def split_table_cells(line: str) -> list:
    """Split one markdown table row on unescaped ``|`` (public alias).

    Used by gates that must read the *extended* manifest (A11: downstream may
    append columns, so the first five cells are compared and the rest ignored).
    """
    return _split_cells(line)


def marker_labels(rows) -> str:
    """``报幕（Section A/B/C、News Report 1/2/3、…）`` from the 报幕 rows.

    Each 报幕 row's position ends with its label (``Section A`` or
    ``Section A / News Report 1``); consecutive labels sharing a prefix are
    folded into one group (``Section A/B/C``), groups are joined by ``、``.
    """
    labels = []
    for row in rows:
        if row.get("type") != "报幕":
            continue
        label = str(row.get("position", "")).split(" / ")[-1].strip()
        if label:
            labels.append(label)
    groups = []  # [prefix, tokens, standalone]
    index = {}
    for label in labels:
        match = _LABEL_SPLIT_RE.match(label)
        if not match or not match.group(2):
            # No trailing alphanumeric token: keep the label as its own group
            # at the position it first appears.
            groups.append([label, [""], True])
            continue
        prefix, token = match.group(1), match.group(2)
        if prefix in index:
            groups[index[prefix]][1].append(token)
        else:
            index[prefix] = len(groups)
            groups.append([prefix, [token], False])
    parts = []
    for prefix, tokens, standalone in groups:
        if standalone:
            parts.append(prefix)
        else:
            parts.append(prefix + "/".join(tokens))
    return "、".join(parts)


def render_manifest(stem: str, rows) -> str:
    """Render ``切分清单.md`` byte-for-byte per 接口约定 §5.2.

    ``rows`` is an ordered list of dicts with ``n`` (int), ``type``,
    ``keep`` (bool), ``position`` and ``display``.  The caller is responsible
    for the row order and for keeping ``display`` in sync with the cut text
    (see assemble.py).  The returned string ends with exactly one newline.
    """
    rows = list(rows)
    total = len(rows)
    keep_rows = [row for row in rows if row.get("keep")]
    skip_rows = [row for row in rows if not row.get("keep")]
    body = sum(1 for row in rows if row.get("type") == "正文")
    questions = sum(1 for row in rows if row.get("type") == "题目")
    counts = {}
    for row in rows:
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    skip_types = "/".join(name for name in SKIP_TYPE_ORDER if counts.get(name, 0) > 0)
    lines = [
        _MANIFEST_TITLE.format(stem=stem),
        "",
        _MANIFEST_LINE2.format(stem=stem),
        _MANIFEST_LINE3.format(
            keep=len(keep_rows), body=body, questions=questions,
            skip=len(skip_rows), skip_types=skip_types,
        ),
        _MANIFEST_LINE4.format(labels=marker_labels(rows)),
        "",
        _MANIFEST_TABLE_HEAD,
        _MANIFEST_TABLE_SEP,
    ]
    for row in rows:
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (
                fmt_row(row["n"]),
                row["type"],
                "是" if row.get("keep") else "否",
                _cell(row.get("position", "")),
                _cell(row.get("display", "")),
            )
        )
    lines.extend(
        [
            "",
            _MANIFEST_KEEP_HEAD.format(n=len(keep_rows)),
            "",
            compress_ranges(row["n"] for row in keep_rows) or EMPTY_RANGES,
            "",
            _MANIFEST_SKIP_HEAD.format(n=len(skip_rows)),
            "",
            compress_ranges(row["n"] for row in skip_rows) or EMPTY_RANGES,
            "",
            _MANIFEST_TAIL,
        ]
    )
    return "\n".join(lines) + "\n"


def parse_manifest(path: str) -> dict:
    """Parse a rendered ``切分清单.md`` and re-derive every recorded number.

    The result is deliberately "facts only": the gate recomputes all counts
    from the table rows and compares the recorded header numbers with them,
    so a hand-edited manifest cannot pass by carrying stale headings.

    Raises :class:`Failure` with a human message when the frozen structure is
    missing (wrong title, missing table, missing summary sections, ...).
    """
    return parse_manifest_text(read_text(path), path)


def parse_manifest_text(text: str, path: str = "<text>") -> dict:
    """Same checks as :func:`parse_manifest`, but for text already in memory.

    E8 needs to parse a manifest *snapshot* (``manifest-5col.md``) without
    writing a temporary file, and :func:`parse_manifest_6col` needs to parse
    the 5-column projection of the extended manifest.  Both go through here so
    there is exactly one set of structural checks.
    """
    lines = text.split("\n")
    stem = None
    recorded = None
    rows = []
    keep_ranges_text = None
    skip_ranges_text = None
    mode = None
    for line in lines:
        stripped = line.strip()
        match = _STEM_RE.match(stripped)
        if match and stem is None:
            stem = match.group(1)
            continue
        match = _STATS_RE.match(stripped)
        if match and recorded is None:
            recorded = {
                "keep": int(match.group(1)),
                "body": int(match.group(2)),
                "questions": int(match.group(3)),
                "skip": int(match.group(4)),
                "skip_types": match.group(5),
            }
            continue
        match = _KEEP_HEAD_RE.match(stripped)
        if match:
            mode = "keep"
            recorded = recorded or {}
            recorded.setdefault("keep_declared", int(match.group(1)))
            continue
        match = _SKIP_HEAD_RE.match(stripped)
        if match:
            if mode == "keep" and keep_ranges_text is None:
                keep_ranges_text = ""  # legitimate empty section (keep=0)
            mode = "skip"
            recorded = recorded or {}
            recorded.setdefault("skip_declared", int(match.group(1)))
            continue
        if mode == "keep" and stripped and keep_ranges_text is None:
            keep_ranges_text = "" if stripped == _MANIFEST_TAIL else stripped
            mode = None
            continue
        if mode == "skip" and stripped and skip_ranges_text is None:
            skip_ranges_text = "" if stripped == _MANIFEST_TAIL else stripped
            mode = None
            continue
        if not stripped.startswith("|"):
            continue
        cells = _split_cells(stripped)
        if len(cells) != 5 or not _ROW_CELL_RE.match(cells[0]):
            continue  # table header / separator
        keep = cells[2]
        if keep not in ("是", "否"):
            raise Failure(EXIT_FAIL, "清单行 %s 的「进卡组」列不是 是/否：%r" % (cells[0], keep))
        rows.append(
            {
                "n": int(cells[0]),
                "type": cells[1],
                "keep": keep == "是",
                "position": cells[3].replace("\\|", "|"),
                "display": cells[4].replace("\\|", "|"),
            }
        )
    if stem is None:
        raise Failure(EXIT_FAIL, "清单缺少第 3 行（与 …_切分文本.txt 逐行一一对应）：%s" % path)
    if recorded is None:
        raise Failure(EXIT_FAIL, "清单缺少表头统计行：%s" % path)
    for key in ("keep", "body", "questions", "skip"):
        if key not in recorded:
            raise Failure(EXIT_FAIL, "清单表头统计行缺少 %s：%s" % (key, path))
    if not rows:
        raise Failure(EXIT_FAIL, "清单表格没有任何行：%s" % path)
    if mode == "skip" and skip_ranges_text is None:
        skip_ranges_text = ""  # legitimate empty section (keep=total)
    if keep_ranges_text is None or skip_ranges_text is None:
        raise Failure(EXIT_FAIL, "清单缺少「进卡组清单 / 不进卡组清单」区间段：%s" % path)
    result = {
        "path": path,
        "text": text,
        "stem": stem,
        "rows": rows,
        "total": len(rows),
        "keep_ranges_text": keep_ranges_text,
        "skip_ranges_text": skip_ranges_text,
        "keep_ranges": parse_ranges(keep_ranges_text),
        "skip_ranges": parse_ranges(skip_ranges_text),
        "recorded": recorded,
        "tail_ok": _MANIFEST_TAIL in text,
    }
    return result


_STEM_LINE_RE = re.compile(r"^# (.+) · 切分清单（含进卡组标记）$")


def manifest_stem(line: str):
    """Extract the stem from the manifest title line, or ``None``."""
    match = _STEM_LINE_RE.match(line.strip())
    return match.group(1) if match else None


# --- sentence splitting (E8 译文 / E9 断行) --------------------------------
#
# 译文一格内按「英文一句 = 中文一行」断行（第二期接口 §2、§3.4），所以这里切
# 的是**句子**，不是意群。CET 原文里有三个必须小心的写法：
#
#   * ``U.S. Post Service``（清单 016）—— 点后面跟大写专名，不能断开；
#   * ``around 1480 B.C.``（清单 110）／``around 1060 B.C.``（112）—— 点后面
#     跟大写，而且**确实就是句末**，必须断开。
#
# 所以缩写分两类（lead 冻结、黄金 96/96 实测）：
#
#   * 末点也要保护：U.S. Mr. Mrs. Ms. Dr. St. a.m. p.m. e.g. i.e. etc.
#     —— 全部点换成占位符，句子永远不在这里结束；
#   * 末点不保护：B.C. A.D. —— 只保护内部点、保留末尾句点，句子可以在这里结束。
#
# 十进制数字（``3.5``）同样保护。``Question N. ...`` 整行视为一句（题目行不按
# 句号拆），N 另外由 :func:`question_number` 取出供卡片 ID 使用。

#: Placeholder for a masked dot; restored by :func:`unmask_abbreviations`.
SENTENCE_MASK = "\u00a7"

_ABBREV_HIDE_LAST = (
    "U.S.", "Mr.", "Mrs.", "Ms.", "Dr.", "St.", "a.m.", "p.m.", "e.g.", "i.e.", "etc.",
)
_ABBREV_KEEP_LAST = ("B.C.", "A.D.")
#: E8 counted 1 sentence while E9 counted 2 and cards_build refused to run.
#: (capital | digit | opening quote).  Additions 2026-11-23 (d02): digit
#: start (row 065) and closing quote/bracket (rows 020 / 108), so that
#: split_sentences agrees with ends_sentence.
_SENTENCE_BOUNDARY_RE = re.compile(r'[.!?](?:["\'\u201d\u2019)\]\u3009\u300b])*\s+(?=[A-Z0-9\u201c"])')
_QUESTION_LINE_RE = re.compile(r"^Question\s+(\d+)\.\s*(.*)$", re.S)
#: Closing quotes/brackets that may follow the final punctuation of a sentence.
_TRAILING_CLOSERS = "\"'\u201d\u2019)]\u3009\u300b"


def _abbreviation_pattern(abbreviation: str):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(abbreviation) + r"(?![A-Za-z0-9])")


def mask_abbreviations(text: str) -> str:
    """Mask every dot that must not be read as a sentence boundary.

    ``U.S.`` -> ``U§S§`` (no boundary possible), ``B.C.`` -> ``B§C.`` (the
    final period survives), ``3.5`` -> ``3§5``.  The result is only used to
    locate boundaries; :func:`unmask_abbreviations` restores the text.
    """
    masked = str(text)
    for abbreviation in _ABBREV_HIDE_LAST:
        masked = _abbreviation_pattern(abbreviation).sub(
            abbreviation.replace(".", SENTENCE_MASK), masked)
    for abbreviation in _ABBREV_KEEP_LAST:
        masked = _abbreviation_pattern(abbreviation).sub(
            abbreviation[:-1].replace(".", SENTENCE_MASK) + ".", masked)
    return re.sub(r"(?<=\d)\.(?=\d)", SENTENCE_MASK, masked)


def unmask_abbreviations(text: str) -> str:
    """Undo :func:`mask_abbreviations` (placeholder back to ``.``)."""
    return str(text).replace(SENTENCE_MASK, ".")


def _flatten_origin(text: str) -> tuple:
    """Return ``(norm_ws(text), origins)``: the flattened text plus the source
    index of every character that survived.

    :func:`split_sentences_with_speakers` cuts sentences over the whole text
    with the speaker markers removed, so it needs the boundaries in flattened
    coordinates and, for every sentence start, the index of the character it
    came from (that index decides the speaker).  Whitespace runs collapse to
    one space that remembers where the run began; the ends are stripped
    exactly like :func:`norm_ws`, so ``flat`` == ``norm_ws(text)``.
    """
    flat = []
    origins = []
    index = 0
    length = len(text)
    while index < length:
        if text[index].isspace():
            end = index
            while end < length and text[end].isspace():
                end += 1
            flat.append(" ")
            origins.append(index)
            index = end
        else:
            flat.append(text[index])
            origins.append(index)
            index += 1
    start = 0
    end = len(flat)
    while start < end and flat[start] == " ":
        start += 1
    while end > start and flat[end - 1] == " ":
        end -= 1
    return "".join(flat[start:end]), origins[start:end]


def _trimmed_span(text: str, start: int, end: int):
    """``text[start:end]`` stripped, as a ``(start, end)`` pair; ``None`` when empty."""
    piece = text[start:end]
    body = piece.strip()
    if not body:
        return None
    begin = start + (len(piece) - len(piece.lstrip()))
    return begin, begin + len(body)


def _sentence_spans(flat: str) -> list:
    """``(start, end)`` of every sentence of an already flattened text."""
    if not flat:
        return []
    if _QUESTION_LINE_RE.match(flat):
        return [(0, len(flat))]
    masked = mask_abbreviations(flat)
    spans = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(masked):
        span = _trimmed_span(masked, start, match.end())
        if span is not None:
            spans.append(span)
        start = match.end()
    span = _trimmed_span(masked, start, len(masked))
    if span is not None:
        spans.append(span)
    return spans


def split_sentences(text: str) -> list:
    """Split one manifest 原文 cell into sentences (abbreviation-safe).

    Sentence-final punctuation is kept, each sentence is stripped, empty
    fragments are dropped.  ``Question N. ...`` is returned as exactly one
    sentence so a question row always has one Chinese line (第二期接口 §3.4).
    """
    flat = norm_ws(text)
    return [unmask_abbreviations(flat[start:end])
            for start, end in _sentence_spans(flat)]


def split_sentences_with_speakers(text: str) -> tuple:
    """Split a manifest 原文 cell into sentences plus one speaker per sentence.

    Sentences are cut by punctuation over the **whole** cell (speaker markers
    removed), exactly like :func:`sentence_count`, so
    ``len(sentences) == sentence_count(text)`` always holds.  That is the 040
    fix: E8 counts the Chinese lines with :func:`sentence_count`, so E9 must
    not get a different number just because a speaker changed mid-sentence
    (``W: ...he's—`` + ``W: What's ...?`` is one sentence said by ``W``).

    ``**M:**`` / ``**W:**`` therefore only decide *who* says a sentence: each
    sentence belongs to the speaker of the block whose text its first
    character was written in.  A line broken mid-sentence merges with the
    following block and keeps the speaker that started it; a block whose text
    is empty is skipped; a trailing fragment without sentence-final
    punctuation stays as the last sentence.  The returned speakers are ``M`` /
    ``W`` and ``""`` for narration; E9 uses them to put ``M: `` / ``W: `` only
    on the line where the speaker changes (第二期接口 §5).
    """
    source = str(text)
    blocks = []  # (speaker, first index, end index) of each speaker block
    position = 0
    speaker = ""
    for match in _SPEAKER_MARK_RE.finditer(source):
        blocks.append((speaker, position, match.start()))
        speaker = match.group(1)
        position = match.end()
    blocks.append((speaker, position, len(source)))
    stripped = []
    source_index = []
    for _, start, end in blocks:
        stripped.append(source[start:end])
        source_index.extend(range(start, end))
    flat, origins = _flatten_origin("".join(stripped))
    sentences = []
    speakers = []
    cursor = 0
    for start, end in _sentence_spans(flat):
        origin = source_index[origins[start]]
        while cursor + 1 < len(blocks) and blocks[cursor + 1][1] <= origin:
            cursor += 1
        sentences.append(unmask_abbreviations(flat[start:end]))
        speakers.append(blocks[cursor][0])
    return sentences, speakers


def sentence_count(text: str) -> int:
    """How many sentences a manifest 原文 cell holds (speaker markers ignored)."""
    return len(split_sentences(strip_speaker_markers(text)))


def question_number(text: str):
    """``Question 12. ...`` -> 12; ``None`` when the text is not a question row."""
    match = _QUESTION_LINE_RE.match(norm_ws(text))
    return int(match.group(1)) if match else None


def ends_sentence(text: str) -> bool:
    """True when one (prefix-stripped) line ends a sentence — abbreviation-safe.

    ``... around 1480 B.C.`` -> True (the final period survives the mask),
    ``... from U.S.`` -> False (every dot of ``U.S.`` is masked), and a line
    ending in a comma, dash or word -> False.  ``cards_apply`` uses this to
    find the line where each sentence starts.
    """
    flat = str(text).rstrip().rstrip(_TRAILING_CLOSERS).rstrip()
    if not flat or flat[-1] not in ".!?":
        return False
    if flat[-1] == ".":
        return mask_abbreviations(flat)[-1] in ".!?"
    return True


def sentence_start_lines(lines) -> list:
    """Line indices that start a sentence: line 0 and every line after a period.

    ``lines`` are the receipt's English lines with the layout prefixes
    (``- `` / ``-- ``) already removed; trailing spaces are ignored.
    """
    lines = list(lines)
    return [index for index in range(len(lines))
            if index == 0 or ends_sentence(lines[index - 1])]


# --- manifest with the 译文 column (E8) -------------------------------------
#
# ``render_manifest_6col`` appends the 6th column to the *output of*
# ``render_manifest``, so the first five columns are byte-identical by
# construction (第二期接口 §2 第 1 条: 一列不改五列).  ``strip_…`` is the exact
# inverse, used to recover the 5-column baseline from an already extended pack
# manifest.

_MANIFEST6_HEAD = _MANIFEST_TABLE_HEAD + " 译文 |"
_MANIFEST6_SEP = "|---|---|---|---|---|---|"
_MANIFEST6_NOTE = (
    "> **译文**列只填「进卡组＝是」的 {keep} 行；中文按句断行（一格内用 `<br>` 换行），"
    "对话保留 `M:` / `W:` 说话人标识；译文为贴合语境的听说大白话，代词已按上下文还原。"
    "不进卡组的 {skip} 行留空。"
)
_MANIFEST6_NOTE_RE = re.compile(
    r"^> \*\*译文\*\*列只填「进卡组＝是」的 \d+ 行；中文按句断行（一格内用 `<br>` 换行），"
    r"对话保留 `M:` / `W:` 说话人标识；译文为贴合语境的听说大白话，代词已按上下文还原。"
    r"不进卡组的 \d+ 行留空。$"
)
_MANIFEST_SUFFIX = "_切分清单.md"


def manifest_translation_note(keep: int, skip: int) -> str:
    """The extra quote line inserted after line 5 of the 6-column manifest."""
    return _MANIFEST6_NOTE.format(keep=int(keep), skip=int(skip))


def manifest_columns(text: str) -> int:
    """5 for the frozen manifest, 6 once the 译文 column has been appended."""
    return 6 if _MANIFEST6_HEAD in str(text) else 5


def render_manifest_6col(stem: str, rows, translations) -> str:
    """Render ``切分清单.md`` with the 译文 column, byte-for-byte per §2.

    ``translations`` maps a row number to its list of Chinese lines (keep rows
    only; a no-keep row renders as an empty cell ``|  |``).  The first five
    columns come from :func:`render_manifest`, so they cannot drift.
    """
    rows = list(rows)
    lines = render_manifest(stem, rows).split("\n")
    note_index = next((index for index, line in enumerate(lines)
                       if line.startswith("> 报幕（")), None)
    if note_index is None:
        raise Failure(EXIT_FAIL, "清单缺少第 5 行引用块（> 报幕（…）），无法插入译文说明行")
    keep_count = sum(1 for row in rows if row.get("keep"))
    lines.insert(note_index + 1,
                 manifest_translation_note(keep_count, len(rows) - keep_count))
    head_index = lines.index(_MANIFEST_TABLE_HEAD)
    lines[head_index] = _MANIFEST6_HEAD
    lines[head_index + 1] = _MANIFEST6_SEP
    for offset, row in enumerate(rows):
        cell = _cell("<br>".join(translations.get(row["n"], ())))
        index = head_index + 2 + offset
        lines[index] = lines[index] + " " + cell + " |"
    return "\n".join(lines)


def strip_manifest_translation_column(text: str) -> str:
    """Undo :func:`render_manifest_6col` exactly (6 columns -> 5 columns).

    Used when a pack manifest was already extended but the first-run snapshot
    (``manifest-5col.md``) is missing, and by the tests that prove the first
    five columns did not change.
    """
    lines = str(text).split("\n")
    note_indices = [index for index, line in enumerate(lines)
                    if _MANIFEST6_NOTE_RE.match(line.strip())]
    if len(note_indices) != 1:
        raise Failure(EXIT_FAIL,
                      "6 列清单的「译文」说明行不是恰好一行（找到 %d 行）" % len(note_indices))
    head_indices = [index for index, line in enumerate(lines)
                    if line.strip() == _MANIFEST6_HEAD]
    if len(head_indices) != 1:
        raise Failure(EXIT_FAIL, "6 列清单缺少表头（… 原文（含说话人标识） | 译文 |）")
    head_index = head_indices[0]
    if lines[head_index + 1].strip() != _MANIFEST6_SEP:
        raise Failure(EXIT_FAIL, "6 列清单的分隔行不是 %r" % _MANIFEST6_SEP)
    out = []
    for index, line in enumerate(lines):
        if index in note_indices:
            continue
        if index == head_index:
            out.append(_MANIFEST_TABLE_HEAD)
            continue
        if index == head_index + 1:
            out.append(_MANIFEST_TABLE_SEP)
            continue
        if index > head_index + 1 and line.startswith("|"):
            cells = _split_cells(line)
            if len(cells) != 6:
                raise Failure(EXIT_FAIL, "6 列清单第 %d 行不是 6 列（%d 列）：%r"
                              % (index + 1, len(cells), line[:80]))
            out.append("| " + " | ".join(cells[:5]) + " |")
            continue
        out.append(line)
    return "\n".join(out)


def parse_manifest_6col(path: str) -> dict:
    """Parse the extended manifest and keep the 译文 cells on every row.

    The structural checks run on the 5-column projection (so the 5-column
    parser stays the single source of truth) and the Chinese cells are read
    from the 6-column table itself.  Each row gains ``zh`` (list of lines).
    """
    text = read_text(path)
    if manifest_columns(text) != 6:
        raise Failure(EXIT_FAIL, "清单没有「译文」列：%s（先跑 translate_apply）" % path)
    parsed = parse_manifest_text(strip_manifest_translation_column(text), path)
    lines = text.split("\n")
    head_index = lines.index(_MANIFEST6_HEAD)
    translations = {}
    for index in range(head_index + 2, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            break
        cells = _split_cells(line)
        if len(cells) != 6 or not _ROW_CELL_RE.match(cells[0]):
            raise Failure(EXIT_FAIL, "6 列清单第 %d 行不是合法表格行：%r"
                          % (index + 1, line[:80]))
        number = int(cells[0])
        zh_text = cells[5].replace("\\|", "|")
        translations[number] = [part.strip() for part in zh_text.split("<br>")]
        if not zh_text.strip():
            translations[number] = []
    if len(translations) != len(parsed["rows"]):
        raise Failure(EXIT_FAIL, "6 列清单的表格行数（%d）与 5 列部分（%d）不一致"
                      % (len(translations), len(parsed["rows"])))
    for row in parsed["rows"]:
        row["zh"] = translations.get(row["n"], [])
    parsed["columns"] = 6
    parsed["translations"] = translations
    parsed["text"] = text
    return parsed


def manifest_row_hashes(text: str) -> list:
    """Per-row sha256 for ``approve.json.rows`` (0.0.5).

    Formula (byte-stable; approve.py and gate e9 both call this one helper):

    * cells = the six table cells of one 6-column manifest row, as split by
      :func:`split_table_cells` (outer whitespace trimmed; escaped pipes are
      turned back into plain pipes);
    * canonical = ``json.dumps([n, type, keep, position, display, zh],
      ensure_ascii=False, separators=(",", ":"))`` (n = int, everything else
      the cell string, zh keeps its <br> separators);
    * sha256 = sha256 of the canonical UTF-8 bytes.

    Returns rows in manifest order: ``[{"n": 1, "sha256": "…"}, …]``.
    """
    lines = str(text).split("\n")
    if manifest_columns(text) != 6:
        raise Failure(EXIT_FAIL, "清单不是 6 列，不能生成行哈希")
    try:
        head = lines.index(_MANIFEST6_HEAD)
    except ValueError:
        raise Failure(EXIT_FAIL, "6 列清单缺少表头（… 原文（含说话人标识） | 译文 |）")
    rows = []
    for index in range(head + 2, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            break
        cells = _split_cells(line)
        if len(cells) != 6 or not _ROW_CELL_RE.match(cells[0]):
            raise Failure(EXIT_FAIL, "6 列清单第 %d 行不是合法表格行：%r"
                          % (index + 1, line[:80]))
        number = int(cells[0])
        values = [str(number), cells[1], cells[2],
                  cells[3].replace("\\|", "|"),
                  cells[4].replace("\\|", "|"),
                  cells[5].replace("\\|", "|")]
        rows.append({
            "n": number,
            "sha256": sha256_text(json.dumps(
                values, ensure_ascii=False, separators=(",", ":"))),
        })
    return rows


def adopted_translations(parsed: dict) -> tuple:
    """Read the 译文 column back as ``{row n: zh lines}`` (only for keep rows).

    Returns ``(translations, problems)``.  ``problems`` is empty exactly when
    the extended manifest is complete per AMENDMENTS-2 A2-6: every keep row has
    Chinese and every no-keep row is blank.  The caller decides whether an
    incomplete manifest is exit 1 or exit 3.
    """
    translations = {}
    problems = []
    for row in parsed.get("rows", []):
        number = fmt_row(row["n"])
        zh = list(row.get("zh") or [])
        if row.get("keep"):
            if not zh:
                problems.append("keep 行 %s 没有译文" % number)
            elif any(not str(line).strip() for line in zh):
                problems.append("keep 行 %s 有空的译文行" % number)
            else:
                translations[row["n"]] = zh
        elif zh:
            problems.append("否行 %s 不该有译文（应留空）" % number)
    return translations, problems


def translations_document(doc_id: str, rows, translations,
                          manifest_sha256_v2: str,
                          manifest_5col_sha256: str, fixes: int = 0) -> dict:
    """Build ``translations.json`` exactly one way (E8 prepare + apply share it)."""
    keep_rows = [row for row in rows if row.get("keep")]
    return {
        "version": 1,
        "doc": doc_id,
        "manifest_sha256_v2": manifest_sha256_v2,
        "manifest_5col_sha256": manifest_5col_sha256,
        "rows": {str(row["n"]): list(translations[row["n"]]) for row in keep_rows},
        "stats": {
            "translated": len(keep_rows),
            "blank": len(list(rows)) - len(keep_rows),
            "sentences": sum(len(translations[row["n"]]) for row in keep_rows),
            "fixes": int(fixes),
        },
    }


def resolve_final_manifest_path(work: str, doc: dict) -> str:
    """Find the **6-column** manifest inside the delivered pack (E8 定稿).

    Only the pack copy counts: ``doc.json.pack_manifest`` (written by E8) →
    ``doc.pack_dir/<stem>_切分清单.md``.  The E4 copy in 产出区 is deliberately
    not a candidate here: it keeps the frozen five columns (and cleanup may
    have deleted it).  Used by ``approve.py`` and by the ``e9`` gate, so both
    bind and verify the very same bytes.
    """
    candidates = []
    if doc.get("pack_manifest"):
        candidates.append(("doc.json.pack_manifest",
                           resolve_work_path(work, str(doc["pack_manifest"]))))
    if doc.get("pack_dir") and doc.get("stem"):
        pack_dir = resolve_work_path(work, str(doc["pack_dir"]))
        candidates.append(("doc.pack_dir/<stem>%s" % _MANIFEST_SUFFIX,
                           os.path.join(pack_dir, "%s%s" % (doc["stem"], _MANIFEST_SUFFIX))))
    for _, path in candidates:
        if os.path.isfile(path):
            return path
    tried = ("\n按顺序找过：\n"
             + "\n".join("  - %s：%s" % (label, path) for label, path in candidates)
             ) if candidates else ""
    raise Failure(
        EXIT_ASK,
        "找不到包内 6 列清单%s\n先跑 E8（translate_apply.py 会把 6 列清单写进产出包）。" % tried,
        reason="manifest6_missing",
    )


def resolve_manifest_path(work: str, doc: dict) -> str:
    """Find the pack manifest in the frozen order (第二期接口 §1).

    ``doc.json.pack_manifest`` (written by E8) → ``doc.pack_dir/<stem>_切分清单.md``
    → ``doc.manifest`` (the E4 copy in 产出区).  Never assembles a path by hand
    because ``cleanup`` may have deleted the E4 copy already.
    """
    candidates = []
    if doc.get("pack_manifest"):
        candidates.append(("doc.json.pack_manifest",
                           resolve_work_path(work, str(doc["pack_manifest"]))))
    if doc.get("pack_dir") and doc.get("stem"):
        pack_dir = resolve_work_path(work, str(doc["pack_dir"]))
        candidates.append(("doc.pack_dir/<stem>%s" % _MANIFEST_SUFFIX,
                           os.path.join(pack_dir, "%s%s" % (doc["stem"], _MANIFEST_SUFFIX))))
    if doc.get("manifest"):
        candidates.append(("doc.json.manifest",
                           resolve_work_path(work, str(doc["manifest"]))))
    for _, path in candidates:
        if os.path.isfile(path):
            return path
    if not candidates:
        raise Failure(EXIT_USAGE,
                      "doc.json 里没有 pack_manifest / pack_dir / manifest，找不到清单。"
                      "先跑 E4（assemble）。", reason="manifest_missing")
    raise Failure(
        EXIT_USAGE,
        "找不到切分清单。按顺序找过：\n"
        + "\n".join("  - %s：%s" % (label, path) for label, path in candidates)
        + "\n先跑 E4（assemble）并确认产出包还在。",
        reason="manifest_missing",
    )


# --- card content (E9, 逐字冻结模板) ---------------------------------------

#: Where the uploaded audio id goes; ``markji_push`` replaces it before submit.
CARD_AUDIO_PLACEHOLDER = "{{AUDIO_ID}}"


def render_card_content(title: str, zh_lines, en_lines,
                        audio: str = CARD_AUDIO_PLACEHOLDER) -> str:
    """Render one card per 第二期接口 §5 (no trailing newline).

    ``title`` already carries the ``\\[`` / ``\\]`` escapes, ``zh_lines``
    already carry ``M:`` / ``W:`` on speaker changes (the translator writes
    them), and ``en_lines`` already carry ``- `` / ``-- `` layout prefixes and
    the speaker marks that ``cards_apply`` inserts.
    """
    lines = [
        "[T#!!fff895#%s]" % title,
        "[T#!275bd1#先听听看是什么意思]",
        "---",
        "[Audio#A,ID/%s#]" % audio,
        "",
        "[T#!275bd1#尽可能复述一下意思，完成后再往下点]",
        "---",
    ]
    lines.extend(zh_lines)
    lines.extend([
        "---",
        "[T#!275bd1,!!#最后对着原文听一遍，对一下单词和发音：]",
        "",
    ])
    lines.extend(en_lines)
    lines.extend([
        "---",
        "[Audio#A,ID/%s#]" % audio,
    ])
    return "\n".join(lines)


# --- Markji channel wrapper + content normalisation (E10–E12) ---------------
#
# 传输层永远是 markji-channel 的 ``markji_channel.py``（本 Skill 不重写 API 客户端，
# 也不打印 Key / token / 带签名的 url）。路径优先级：
#   ``--mk PATH`` > 环境变量 ``LISTENING_CARD_MK`` > 默认安装位置。
# 施工/测试用假通道时把 ``LISTENING_CARD_MK`` 指向测试夹具即可，不需要联网。

DEFAULT_MARKJI_CHANNEL = os.path.join(
    os.path.expanduser("~"), ".agents", "skills", "markji-channel",
    "scripts", "markji_channel.py",
)
MARKJI_CHANNEL_ENV = "LISTENING_CARD_MK"
#: 429 退避秒数；测试可把它调小（``LISTENING_CARD_MK_BACKOFF``），生产默认 10 秒。
MARKJI_BACKOFF_ENV = "LISTENING_CARD_MK_BACKOFF"
MARKJI_BACKOFF_SECONDS = 10.0
MARKJI_MAX_RETRY = 6
MARKJI_TIMEOUT_SECONDS = 180

_AUTH_HINTS = ("common_unauthorized", "unauthorized", "api key 已失效",
               "还没配置 api key", "401", "403")
_RATE_HINTS = ("too_many_request", "429", "rate limit")
_NETWORK_HINTS = ("网络错误", "urlopen error", "timed out", "connection reset",
                  "connection refused", "name or service not known")
_URL_RE = re.compile(r"https?://\S+")


def markji_channel_path(explicit: str | None = None) -> str:
    """Resolve + validate the markji-channel script (never invents one)."""
    raw = explicit or os.environ.get(MARKJI_CHANNEL_ENV) or DEFAULT_MARKJI_CHANNEL
    path = os.path.abspath(os.path.expanduser(str(raw).strip()))
    if not os.path.isfile(path):
        raise Failure(
            EXIT_USAGE,
            "找不到墨墨通道脚本：%s\n"
            "本 Skill 不重写 API 客户端，依赖 markji-channel skill"
            "（默认 ~/.agents/skills/markji-channel/scripts/markji_channel.py）。\n"
            "请先安装该 skill，或用 --mk / LISTENING_CARD_MK 指定 markji_channel.py。"
            % path,
            reason="markji_channel_missing",
        )
    return path


def _redact(text: str) -> str:
    """Strip signed URLs from channel output before it reaches a log/message."""
    return _URL_RE.sub("<url>", str(text))


def redact_urls(value):
    """Recursively replace signed URLs before anything is written to disk.

    Real cloud ``files[].url`` values carry a signature token; receipts, logs
    and JSON artifacts must only keep ``id`` / ``mime`` / ``size``.
    """
    if isinstance(value, str):
        return _URL_RE.sub("<url>", value)
    if isinstance(value, list):
        return [redact_urls(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_urls(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_urls(item) for key, item in value.items()}
    return value


def safe_file_meta(file_obj) -> dict:
    """Only the non-sensitive part of a ``files[]`` entry (never the url)."""
    if not isinstance(file_obj, dict):
        return {"id": None, "mime": None, "size": None}
    return {"id": file_obj.get("id"), "mime": file_obj.get("mime"),
            "size": file_obj.get("size")}


def _looks_like(message: str, hints) -> bool:
    lowered = str(message).lower()
    return any(hint in lowered for hint in hints)


def _parse_channel_json(stdout: str, command: str):
    """``markji_channel.py --json`` prints exactly one JSON value."""
    text = (stdout or "").strip()
    if not text:
        raise Failure(EXIT_FAIL, "墨墨通道没有输出（%s）" % command, reason="markji_output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise Failure(EXIT_FAIL, "墨墨通道输出不是 JSON（%s）：%r"
                  % (command, _redact(text[:300])), reason="markji_output")


class MarkjiChannel:
    """Run ``markji_channel.py`` with 429 backoff and redacted error messages."""

    def __init__(self, path: str | None = None, *, log=None, interval: float = 0.6,
                 backoff: float | None = None, max_retry: int = MARKJI_MAX_RETRY,
                 sleep=None):
        self.path = markji_channel_path(path)
        self.log = log
        self.interval = max(0.0, float(interval))
        if backoff is None:
            try:
                backoff = float(os.environ.get(MARKJI_BACKOFF_ENV, ""))
            except ValueError:
                backoff = MARKJI_BACKOFF_SECONDS
        self.backoff = max(0.0, float(backoff))
        self.max_retry = int(max_retry)
        self.sleep = sleep or time.sleep
        self.calls = 0
        self.retries = 0

    def _log(self, message: str) -> None:
        if self.log:
            self.log(message)

    def call(self, *args, retry: bool = False):
        """One channel command; ``retry=True`` for writes (429 退避 ≤6 次)."""
        command = [sys.executable, self.path] + [str(arg) for arg in args] + ["--json"]
        attempt = 0
        while True:
            self.calls += 1
            try:
                proc = subprocess.run(command, capture_output=True, text=True,
                                      timeout=MARKJI_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                raise Failure(EXIT_USAGE,
                              "墨墨通道超时（%d 秒）：%s" % (MARKJI_TIMEOUT_SECONDS, args[0]),
                              reason="markji_timeout")
            if proc.returncode == 0:
                return _parse_channel_json(proc.stdout, str(args[0]))
            message = _redact((proc.stderr or proc.stdout or "").strip())
            if _looks_like(message, _AUTH_HINTS):
                raise Failure(
                    EXIT_USAGE,
                    "墨墨 API Key 失效或缺失，通道拒绝了请求。把下面这段原样转达用户：\n%s"
                    % message, reason="markji_auth")
            if _looks_like(message, _RATE_HINTS):
                if retry and attempt < self.max_retry:
                    attempt += 1
                    self.retries += 1
                    self._log("WARN 频控 429，%.1f 秒后重试（第 %d/%d 次）"
                              % (self.backoff, attempt, self.max_retry))
                    self.sleep(self.backoff)
                    continue
                raise Failure(
                    EXIT_USAGE,
                    "墨墨频控（429/common_too_many_request）重试 %d 次仍失败：%s\n"
                    "请等一会儿再用 --limit 缩小批次重跑。" % (attempt, message),
                    reason="rate_limited")
            if _looks_like(message, _NETWORK_HINTS):
                raise Failure(EXIT_USAGE, "连不上墨墨（网络错误）：%s\n"
                                         "检查网络后重跑；本 Skill 不绕过通道。" % message,
                              reason="markji_network")
            raise Failure(EXIT_FAIL, "墨墨通道报错（%s）：%s"
                          % (args[0], message[:600]), reason="markji_error")

    def pause(self) -> None:
        """Inter-request interval for writes (default 0.6 s, 频控要求)."""
        if self.interval > 0:
            self.sleep(self.interval)


# --- card content normalisation (A2-1, plan/verify/sync 共用) ---------------

_LAYOUT_PREFIX_RE = re.compile(r"^(?:-- |- |[MW]:\s*)+")
AUDIO_ID_RE = re.compile(r"\[Audio#A,ID/([^#]+)#\]")


def normalize_card_content(text: str) -> str:
    """A2-1 归一化：换行统一、去 ``- ``/``-- ``、去 ``M:``/``W:``、压空白。

    云端与本地都用它，所以「用户只改了排版」的卡会判成 ``skip_identical``
    （AMENDMENTS-2 A2-1 的核心：不覆盖用户手改）。
    """
    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_LAYOUT_PREFIX_RE.sub("", line) for line in value.split("\n")]
    return " ".join("\n".join(lines).split())


def card_content_sections(text: str) -> list:
    """Split card content on standalone ``---`` lines (5 sections for the template)."""
    return str(text).split("\n---\n")


def card_audio_ids(text: str) -> list:
    """Audio ids referenced by a card content (``mkjf_…``), in order."""
    return AUDIO_ID_RE.findall(str(text))


def markji_plan_sha(plan: dict) -> str:
    """Stable sha of a plan (the ``plan_sha256`` field itself is excluded)."""
    payload = {key: value for key, value in plan.items()
               if key != "plan_sha256" and not str(key).startswith("_")}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")))


def markji_plan_sha_from_file(path: str) -> str:
    return markji_plan_sha(read_json(path))


def card_title(content: str):
    """Extract the card title from the first template line (or ``None``)."""
    match = re.match(r"^\[T#!!fff895#(.+)\]\s*$", str(content).split("\n")[0])
    return match.group(1) if match else None


def content_preview(text: str, limit: int = 60) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# --- workspace / run discovery ---------------------------------------------


def looks_like_work(path: str) -> bool:
    """A workspace has both ``系统/tools/cut`` and a ``材料区`` directory."""
    return (
        os.path.isfile(os.path.join(path, "系统", "tools", "cut"))
        and os.path.isdir(os.path.join(path, "材料区"))
    )


def resolve_work(argument: str | None = None) -> str:
    """Find the workspace root by walking up from ``argument`` (or the cwd)."""
    start = os.path.abspath(argument) if argument else os.getcwd()
    current = start
    while True:
        if looks_like_work(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise Failure(
        EXIT_USAGE,
        "没找到工作区：从 %s 往上都没有同时含 系统/tools/cut 与 材料区 的目录；请用 --work 指定。" % start,
    )


def default_run_root() -> str:
    """Parent directory for ``$RUN``; ``/tmp`` unless the test hook overrides it."""
    override = os.environ.get("LISTENING_CARD_RUN_ROOT")
    candidates = [override] if override else ["/tmp", tempfile.gettempdir()]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    raise Failure(
        EXIT_USAGE,
        "找不到可写的临时目录（试过 %s）；请设置 TMPDIR 或用 --run 指定。" % "、".join(candidates),
    )


def run_key(pairs) -> str:
    """0.0.3 $RUN identity (path/size/mtime) -- kept for old callers only.

    ``pairs`` is an ordered list of ``(audio_abs, text_abs)``.  The key hashes
    ``[[audio, size, mtime_ns], [text, size, mtime_ns], ...]``, so touching a
    file changes it.  0.0.4 stage code must use :func:`content_run_key`.
    """
    facts = []
    for audio, text in pairs:
        for path in (audio, text):
            stat = os.stat(path)
            facts.append([os.path.abspath(path), stat.st_size, stat.st_mtime_ns])
    payload = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(payload)


def content_run_key(pairs, work: str | None = None) -> str:
    """0.0.4 $RUN identity: content sha, never mtime (DESIGN §2.1).

    ``pairs`` is ``[(audio_abs, text_abs), ...]`` and must contain only the
    documents **selected** for this run.  The key is the sha256 of the JSON
    ``[[rel_audio, audio_sha256, rel_text, text_sha256], ...]`` sorted by the
    workspace-relative paths, so:

    * touching a file without changing its bytes resumes the same run;
    * re-saving the same bytes elsewhere inside the workspace keeps the key;
    * a path outside the workspace stays absolute (never silently rebased).
    """
    facts = []
    for audio, text in pairs:
        facts.append([
            rel_to(work, audio) if work else os.path.abspath(audio),
            sha256_file(audio),
            rel_to(work, text) if work else os.path.abspath(text),
            sha256_file(text),
        ])
    facts.sort(key=lambda row: (row[0], row[2]))
    payload = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(payload)


def default_run_dir(key: str) -> str:
    return os.path.join(default_run_root(), "listening-card-run-%s" % key[:8])


def doc_dir(run_dir: str, doc_id: str) -> str:
    return os.path.join(os.path.abspath(run_dir), "docs", doc_id)


def load_run(run_dir: str) -> dict:
    """Load and lightly validate ``$RUN/run.json``."""
    run_dir = os.path.abspath(run_dir)
    path = os.path.join(run_dir, "run.json")
    data = read_json(path)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise Failure(EXIT_FAIL, "run.json 不是 version 1 的结构：%s" % path)
    if not isinstance(data.get("docs"), list):
        raise Failure(EXIT_FAIL, "run.json 缺少 docs 数组：%s" % path)
    data.setdefault("run", run_dir)
    return data


def load_doc(run_dir: str, doc_id: str) -> dict:
    """Load and lightly validate ``$RUN/docs/<id>/doc.json``."""
    path = os.path.join(doc_dir(run_dir, doc_id), "doc.json")
    data = read_json(path)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise Failure(EXIT_FAIL, "doc.json 不是 version 1 的结构：%s" % path)
    if data.get("id") != doc_id:
        raise Failure(EXIT_FAIL, "doc.json 的 id 与目录不符：%s（id=%r）" % (path, data.get("id")))
    for key in ("audio", "text", "stem"):
        if not data.get(key):
            raise Failure(EXIT_FAIL, "doc.json 缺少 %s：%s" % (key, path))
    return data


# --- logging ----------------------------------------------------------------


class StageLog:
    """Per-stage log file under ``$RUN/logs/`` (接口约定 §2).

    Lines are buffered and flushed atomically in :meth:`flush`, so a crash in
    the middle of a stage never leaves a half-written log.  The log path is
    ``logs/<stage>[-<doc>]-<ts>.log``.
    """

    def __init__(self, run_dir: str, stage: str, doc: str | None = None):
        self.run_dir = os.path.abspath(run_dir)
        name = stage if not doc else "%s-%s" % (stage, doc)
        self.path = os.path.join(self.run_dir, "logs", "%s-%s.log" % (name, now_compact()))
        self._lines = []

    def __call__(self, message: str) -> None:
        self._lines.append("[%s] %s" % (now_iso(), message))

    def debug(self, message: str) -> None:  # pragma: no cover - convenience alias
        self(message)

    def flush(self) -> None:
        if not self._lines:
            return
        atomic_write_text(self.path, "\n".join(self._lines) + "\n")


# --- CLI helpers ------------------------------------------------------------


def emit_json(obj) -> None:
    """Print the machine result as the last stdout line: ``JSON {...}``."""
    print("JSON " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")), flush=True)


class SkillArgumentParser(argparse.ArgumentParser):
    """argparse that keeps the exit-code contract for usage errors (exit 2)."""

    def error(self, message):  # noqa: D401 - argparse API
        self.print_usage(sys.stderr)
        print("ERROR: %s" % message, file=sys.stderr)
        emit_json({"ok": False, "exit_code": EXIT_USAGE, "error": message})
        raise SystemExit(EXIT_USAGE)


def new_parser(prog: str, description: str, epilog: str | None = None,
               append_exit_help: bool = True) -> SkillArgumentParser:
    if not append_exit_help:
        pass
    elif epilog is None:
        epilog = EXIT_CODES_HELP
    else:
        epilog = "%s\n%s" % (epilog, EXIT_CODES_HELP)
    return SkillArgumentParser(
        prog=prog,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )


def report_failure(stage: str, failure: Failure, extra: dict | None = None) -> int:
    """Print a human error line and emit the failure JSON; returns the exit code."""
    print("ERROR: %s" % failure.message, file=sys.stderr)
    payload = {"ok": False, "exit_code": failure.code, "stage": stage, "error": failure.message}
    if extra:
        payload.update(extra)
    payload.update(failure.extra)
    emit_json(payload)
    return failure.code


def format_size(num_bytes: int) -> str:
    """Human readable size for the stdout summary."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (value, unit)
        value /= 1024
    return "%.1f GB" % value  # pragma: no cover - unreachable
