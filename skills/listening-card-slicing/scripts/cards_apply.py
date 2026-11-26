#!/usr/bin/env python3
"""cards_apply.py -- E9: merge layouter receipts, validate, render cards.json.

The layouter subagents only decide where the English lines break; everything
else is deterministic.  This script therefore *only* accepts English lines and
adds the speaker marks itself, because the golden 96 cloud cards prove that
``M: `` / ``W: `` goes on the line where a sentence starts **and** the speaker
changes (第二期接口 §5) — a model must not retype that.

For every card the receipt's lines are checked against the manifest's English
sentences before anything is rendered:

1. first line must not start with ``-``; only ``- `` / ``-- `` prefixes are
   allowed, and a line may not carry two of them;
2. text integrity: strip the layout prefixes, join the lines, collapse
   whitespace — must equal the manifest's sentences joined by one space;
3. sentence starts (abbreviation-safe, ``... 1480 B.C.`` ends a sentence,
   ``... U.S.`` does not) must be exactly one per sentence, and each start
   line must begin with that very sentence;
4. ``-- `` before any ``- `` in the same card is a soft flag, not a failure;
5. a speaker mark the layouter copied onto a receipt line (``M: `` / ``W: ``,
   with or without the ``**…**`` decoration) is stripped and recorded in
   ``warnings`` (0.0.2, T3) — only a line that is nothing but the marker is an
   error, because nothing would be left to check.

Then the frozen template (第二期接口 §5) is rendered with
``{{AUDIO_ID}}`` as the audio placeholder and written to ``cards.json``;
``cards-preview.md`` is only a human sample-card preview (0.0.5: first 1-3
cards, ``--preview N`` / ``--full-preview`` for debugging).  The proofreader
must read the full machine truth (``cards.json`` + the 6-column manifest), not
the truncated preview.  A missing receipt is exit 3 and writes nothing; a
broken receipt is exit 1 and writes nothing.

0.0.4: ``cards.json`` also carries a top-level ``cards_sha256`` — the sha of
``[[no, content_sha256], ...]`` in card order (``cards_sha256()`` below).  The
plan / approve2 token / sample receipt / push receipt all bind to that one
value, so editing one card invalidates every downstream approval.

0.1.0 (D1 中文校对表格 + 直改): the proofreader may now return ``zh_fixes`` —
whole-card Chinese line arrays (``len(zh) == len(card["en"])``), never a diff.
``--review`` validates them and renders ``$RUN/docs/<doc>/zh-fixes.md``
(卡片 / 当前中文 / 建议中文 / 理由) for the user to settle, but ``cards.json``
keeps its Chinese byte-for-byte (只报不改; ``fixes[].zh`` is still rejected so
the English and Chinese channels stay separate).  ``--refresh-zh`` reads the
current 6-column manifest (``$RUN`` first, then the pack copy), re-checks the
sentence count and rewrites ``cards-base.json``'s ``zh`` before re-rendering
``cards.json`` from the **existing layouter receipts** — the layouter is never
re-run.  ``zh_fix.py apply`` performs the actual Chinese edit on the manifest +
``translations.json``; this script only reports and refreshes.

Exit codes: 0 rendered / 1 a receipt or the card structure failed a check /
2 usage or missing prior artifacts / 3 layout receipts are still missing
(or, for ``--refresh-zh``, ``cards-base.json`` / the 6-column manifest).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402

STAGE = "e9"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "cards_apply.py",
        "E9：合并排版回执（+ 可选校对 fixes），硬校验文本完整性后渲染 cards.json 与预览。",
        epilog="回执缺失 → exit 3（只补派缺的任务）；文本/句数/格式不符 → exit 1（不写任何产物）。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--review", default=None,
                      help="校对回执 JSON（fixes 覆盖 en_lines；zh_fixes 只生成 "
                           "zh-fixes.md 表格，不改 cards.json）")
    mode.add_argument("--refresh-zh", action="store_true",
                      help="从当前 6 列清单（$RUN 优先，其次包内）刷新 cards-base.json 的 zh，"
                           "并用现有排版回执重渲染 cards.json（不重跑 layouter）")
    parser.add_argument("--preview", type=int, default=3, metavar="N",
                        help="样卡预览张数（默认 3；cards-preview.md 只写前 N 张）")
    parser.add_argument("--full-preview", action="store_true",
                        help="排障用：cards-preview.md 写全部卡片（覆盖 --preview）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- receipt helpers --------------------------------------------------------


def split_prefix(line: str) -> tuple:
    """Return ``(depth, body)``; depth is 0/1/2, or -1 for a bad prefix."""
    if line.startswith("-- "):
        return 2, line[3:]
    if line.startswith("- "):
        return 1, line[2:]
    if line.startswith("-"):
        return -1, line
    return 0, line


#: A layouter sometimes copies the speaker marker that the renderer adds
#: (``M: `` / ``W: `` / ``**M:** `` / ``**W:** ``) onto a receipt line.
_COPIED_MARKER_RE = re.compile(
    r"^(?P<prefix>-- |- )?(?P<marker>\*\*[MW]:\*\*|[MW]:)(?P<gap>\s*)(?P<rest>.*)$")


def strip_copied_markers(lines, card: dict, warnings: list) -> list:
    """Strip a copied ``M: `` / ``W: `` from each receipt line, recording warnings.

    ``cards_apply`` owns the speaker marks: the receipt is only the line
    breaks.  A copied mark would break the text-integrity check and send the
    whole card back to the layouter, so 0.0.2 (T3) removes it instead and
    records ``{no, line, removed}``.  The only hard error is a line that holds
    nothing but the marker (nothing is left to validate).
    """
    if not isinstance(lines, list):
        return lines  # check_en_lines reports the type error with its own message
    number = card["no"]
    cleaned = []
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, str):
            cleaned.append(line)
            continue
        match = _COPIED_MARKER_RE.match(line)
        if not match:
            cleaned.append(line)
            continue
        prefix = match.group("prefix") or ""
        marker = match.group("marker")
        gap = match.group("gap") or ""
        rest = match.group("rest")
        if not prefix and not rest.strip():
            raise common.Failure(
                common.EXIT_FAIL,
                "第 %s 张第 %d 行只有说话人标记（%r），剥掉后是空的；"
                "排版回执不要抄 M:/W: 标记" % (number, index, marker),
            )
        cleaned.append(prefix + rest)
        warnings.append({"no": number, "line": index, "removed": marker + gap})
    return cleaned


def first_difference(left: str, right: str) -> int:
    """Index of the first differing character (for human error messages)."""
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))


def check_en_lines(lines, card: dict) -> tuple:
    """Validate one card's English lines; return ``(bodies, depths)``."""
    number = card["no"]
    sentences = card["en"]
    if not isinstance(lines, list) or not lines:
        raise common.Failure(common.EXIT_FAIL, "第 %s 张：en_lines 必须是非空数组" % number)
    bodies = []
    depths = []
    for index, line in enumerate(lines, start=1):
        where = "第 %s 张第 %d 行" % (number, index)
        if not isinstance(line, str):
            raise common.Failure(common.EXIT_FAIL, "%s 不是字符串：%r" % (where, line))
        if not line.strip():
            raise common.Failure(common.EXIT_FAIL, "%s 是空的" % where)
        if "\n" in line or "\r" in line:
            raise common.Failure(common.EXIT_FAIL, "%s 里有换行符：%r" % (where, line))
        depth, body = split_prefix(line)
        if depth < 0:
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 以 - 开头但不是 - /-- 前缀：%r" % (where, line))
        if depth and (body.startswith("- ") or body.startswith("-- ")):
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 有多重前缀（只允许一个 - /-- ）：%r" % (where, line))
        if not body.strip():
            raise common.Failure(common.EXIT_FAIL, "%s 去掉前缀后是空的：%r" % (where, line))
        bodies.append(body)
        depths.append(depth)
    if depths[0] != 0:
        raise common.Failure(common.EXIT_FAIL,
                             "第 %s 张：首行不得以 - 开头：%r" % (number, lines[0]))
    joined = common.norm_ws("\n".join(bodies))
    original = common.norm_ws(" ".join(sentences))
    if joined != original:
        index = first_difference(joined, original)
        start = max(0, index - 20)
        raise common.Failure(
            common.EXIT_FAIL,
            "第 %s 张：去掉断行前缀后与清单英文不一致（第 %d 个字符起）。\n"
            "  回执：%r\n  清单：%r"
            % (number, index + 1, joined[start:index + 40], original[start:index + 40]),
            reason="text_changed",
        )
    starts = common.sentence_start_lines(bodies)
    if len(starts) != len(sentences):
        raise common.Failure(
            common.EXIT_FAIL,
            "第 %s 张：断行后的句子起点 %d 个 ≠ 清单英文 %d 句"
            "（句子在句末标点处必须断行；1480 B.C. 算句末，U.S. 不算）"
            % (number, len(starts), len(sentences)),
            reason="sentence_count_mismatch",
        )
    for sentence_index, line_index in enumerate(starts):
        tail = common.norm_ws(" ".join(bodies[line_index:]))
        if not tail.startswith(common.norm_ws(sentences[sentence_index])):
            raise common.Failure(
                common.EXIT_FAIL,
                "第 %s 张第 %d 行：这个句子起点和清单第 %d 句对不上（断行位置错位）"
                % (number, line_index + 1, sentence_index + 1),
                reason="sentence_start_mismatch",
            )
    return bodies, depths


def mark_speakers(lines, bodies, depths, starts, card: dict) -> list:
    """Insert ``M: `` / ``W: `` on sentence starts where the speaker changes."""
    number = card["no"]
    speakers = card["speakers"]
    sentences = card["en"]
    if len(speakers) != len(sentences):
        raise common.Failure(common.EXIT_FAIL,
                             "第 %s 张：speakers 数量 %d ≠ 英文 %d 句"
                             % (number, len(speakers), len(sentences)))
    start_to_sentence = {line_index: sentence_index
                         for sentence_index, line_index in enumerate(starts)}
    marked = []
    for line_index, line in enumerate(lines):
        sentence_index = start_to_sentence.get(line_index)
        if sentence_index is None:
            marked.append(line)
            continue
        speaker = speakers[sentence_index]
        previous = speakers[sentence_index - 1] if sentence_index > 0 else ""
        if speaker not in ("M", "W") or speaker == previous:
            marked.append(line)
            continue
        if depths[line_index] != 0:
            raise common.Failure(
                common.EXIT_FAIL,
                "第 %s 张第 %d 行：换说话人处必须另起一行（不能带 - 前缀）"
                % (number, line_index + 1),
                reason="speaker_on_clause_line",
            )
        marked.append("%s: %s" % (speaker, line))
    return marked


def load_receipts(receipt_dir: str, doc_id: str) -> list:
    if not os.path.isdir(receipt_dir):
        return []
    names = sorted(name for name in os.listdir(receipt_dir) if name.endswith(".json"))
    receipts = []
    for name in names:
        path = os.path.join(receipt_dir, name)
        data = common.read_json(path)
        if not isinstance(data, dict):
            raise common.Failure(common.EXIT_FAIL, "回执不是 JSON 对象：%s" % path)
        if data.get("version") != 1:
            raise common.Failure(common.EXIT_FAIL, "回执 version 不是 1：%s" % path)
        if data.get("doc") != doc_id:
            raise common.Failure(common.EXIT_FAIL, "回执 doc 不是 %r：%s（%r）"
                                 % (doc_id, path, data.get("doc")))
        if data.get("role") != "layouter":
            raise common.Failure(common.EXIT_FAIL,
                                 "回执 role 不是 layouter：%s（%r）" % (path, data.get("role")))
        receipts.append((path, data))
    return receipts


def collect_lines(receipts: list, cards_by_no: dict) -> dict:
    """Merge receipts into ``{no: en_lines}`` (each card exactly once)."""
    collected = {}
    origin = {}
    for path, data in receipts:
        cards = data.get("cards")
        if not isinstance(cards, list):
            raise common.Failure(common.EXIT_FAIL, "回执缺少 cards 数组：%s" % path)
        for index, item in enumerate(cards, start=1):
            where = "%s 第 %d 张" % (os.path.basename(path), index)
            if not isinstance(item, dict):
                raise common.Failure(common.EXIT_FAIL, "%s 不是对象：%r" % (where, item))
            number = str(item.get("no") or "").strip()
            if number.isdigit():
                number = common.fmt_row(int(number))
            if number not in cards_by_no:
                raise common.Failure(common.EXIT_FAIL,
                                     "%s 的卡号 %r 不在 cards-base.json 里" % (where, number))
            if number in collected:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "第 %s 张被排版了两次：%s 与 %s（每张卡恰好一次）"
                    % (number, origin[number], path))
            collected[number] = item.get("en_lines")
            origin[number] = path
    return collected


def missing_receipts(doc_path: str) -> list:
    tasks_path = os.path.join(doc_path, "card-tasks.json")
    if not os.path.isfile(tasks_path):
        return []
    data = common.read_json(tasks_path)
    missing = []
    for task in data.get("tasks", []):
        receipt = task.get("receipt") or ""
        path = receipt if os.path.isabs(receipt) else os.path.join(doc_path, receipt)
        if not os.path.isfile(path):
            missing.append(task.get("task") or receipt)
    return missing


def apply_review(path: str, doc_id: str, cards_by_no: dict, collected: dict) -> tuple:
    """Validate a proofreader receipt; return ``(fixes, flags, zh_fixes)``.

    English fixes replace ``en_lines``.  A Chinese fix must **not** travel in
    ``fixes``: the Chinese column lives in ``translations.json`` / the
    6-column manifest, and silently changing it here would make gate e9
    disagree with E5.  0.1.0 (D1) adds the separate ``zh_fixes`` array — whole
    card Chinese line arrays that this script only validates and renders as a
    table; the user settles them and ``zh_fix.py apply`` edits the real
    sources.  ``cards.json`` is never changed by ``zh_fixes``.
    """
    review = common.read_json(path)
    if not isinstance(review, dict):
        raise common.Failure(common.EXIT_FAIL, "校对回执不是 JSON 对象：%s" % path)
    if review.get("doc") not in (None, doc_id):
        raise common.Failure(common.EXIT_FAIL, "校对回执 doc 不是 %r：%s（%r）"
                             % (doc_id, path, review.get("doc")))
    flags = review.get("flags") or []
    if not isinstance(flags, list):
        raise common.Failure(common.EXIT_FAIL, "校对回执的 flags 必须是数组：%s" % path)
    zh_fixes = validate_zh_fixes(review.get("zh_fixes"), path, cards_by_no)
    fixes = review.get("fixes")
    if fixes is None:
        return 0, flags, zh_fixes
    if not isinstance(fixes, list):
        raise common.Failure(common.EXIT_FAIL, "校对回执的 fixes 必须是数组：%s" % path)
    applied = 0
    for index, fix in enumerate(fixes, start=1):
        where = "%s 的 fixes 第 %d 条" % (os.path.basename(path), index)
        if not isinstance(fix, dict):
            raise common.Failure(common.EXIT_FAIL, "%s 不是对象：%r" % (where, fix))
        number = str(fix.get("no") or "").strip()
        if number.isdigit():
            number = common.fmt_row(int(number))
        card = cards_by_no.get(number)
        if card is None:
            raise common.Failure(common.EXIT_FAIL, "%s 的卡号 %r 不存在" % (where, number))
        if "zh" in fix:
            # 0.1.0 D1: 英文/中文通道保持分离 —— 中文建议写 zh_fixes（整张卡），
            # 由 zh_fix.py 直接改清单 + translations.json；fixes 只收 en_lines。
            raise common.Failure(
                common.EXIT_FAIL,
                "%s（第 %s 张）在 fixes 里带了中文：fixes 只收 en_lines。\n"
                "中文改动请写 zh_fixes（整张卡的中文行数组），交用户敲定后"
                "由 zh_fix.py apply 直接改清单/cards.json。"
                % (where, number),
                reason="zh_fix_requires_e5",
            )
        if "en_lines" in fix:
            collected[number] = fix["en_lines"]
            applied += 1
    return applied, flags, zh_fixes


# --- 0.1.0 D1: zh_fixes（整卡中文校对表格 + --refresh-zh） -------------------

#: Same line rules as E5 (translate_apply.check_zh_lines): one array entry per
#: sentence, no layout markers, no surrounding whitespace.
_ZH_LINE_FORBIDDEN = ("<br>", "\n", "\r", "**")


def normalise_card_no(value, where: str) -> str:
    """Accept ``5`` / ``"005"`` and return the canonical ``005``."""
    number = str(value or "").strip()
    if number.isdigit():
        return common.fmt_row(int(number))
    raise common.Failure(common.EXIT_FAIL, "%s 的卡号不是数字：%r" % (where, value))


def check_zh_lines(lines, where: str, expected: int) -> list:
    """Validate one whole-card Chinese line array (``len == 英文句数``)."""
    if not isinstance(lines, list) or not lines:
        raise common.Failure(common.EXIT_FAIL,
                             "%s 的 zh 必须是非空数组（整张卡的中文行）" % where)
    if len(lines) != expected:
        raise common.Failure(
            common.EXIT_FAIL,
            "%s 的中文行数 %d ≠ 英文句数 %d"
            "（一句一行；zh 要写整张卡的中文行数组，不是 diff）"
            % (where, len(lines), expected),
            reason="zh_sentence_count_mismatch",
        )
    checked = []
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, str):
            raise common.Failure(common.EXIT_FAIL, "%s 第 %d 行不是字符串：%r"
                                 % (where, index, line))
        if not line.strip():
            raise common.Failure(common.EXIT_FAIL, "%s 第 %d 行是空的" % (where, index))
        if line != line.strip():
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 第 %d 行首尾有空白：%r" % (where, index, line))
        for needle in _ZH_LINE_FORBIDDEN:
            if needle in line:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "%s 第 %d 行包含 %r：%r（一格内用多行数组，不要写标记）"
                    % (where, index, needle, line),
                )
        checked.append(line)
    return checked


def validate_zh_fixes(raw, path: str, cards_by_no: dict) -> list:
    """Validate the proofreader's ``zh_fixes`` array; return normalised entries.

    ``zh`` is the **whole card** Chinese line array, so it must match the
    English sentence count; one entry per card; the card number must exist.
    ``current`` is not accepted from the model (the table reads it from
    ``cards-base.json``/``cards.json`` so it cannot be mistyped).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise common.Failure(common.EXIT_FAIL,
                             "校对回执的 zh_fixes 必须是数组：%s" % path)
    name = os.path.basename(path)
    fixes = []
    seen = set()
    for index, fix in enumerate(raw, start=1):
        where = "%s 的 zh_fixes 第 %d 条" % (name, index)
        if not isinstance(fix, dict):
            raise common.Failure(common.EXIT_FAIL, "%s 不是对象：%r" % (where, fix))
        number = normalise_card_no(fix.get("no"), where)
        card = cards_by_no.get(number)
        if card is None:
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 的卡号 %r 不在 cards.json 里" % (where, number))
        if number in seen:
            raise common.Failure(common.EXIT_FAIL,
                                 "%s：第 %s 张已有中文建议（一卡最多一条）"
                                 % (where, number))
        zh = check_zh_lines(fix.get("zh"), "%s（第 %s 张）" % (where, number),
                            len(card["en"]))
        why = fix.get("why") or ""
        if not isinstance(why, str):
            raise common.Failure(common.EXIT_FAIL, "%s 的 why 不是字符串：%r"
                                 % (where, why))
        fixes.append({"no": number, "zh": zh, "why": why.strip()})
        seen.add(number)
    return fixes


def _md_cell(text) -> str:
    """Escape one markdown table cell (``|`` -> ``\\|``)."""
    return str(text).replace("|", "\\|")


def render_zh_fixes_table(doc_id: str, fixes, current_by_no: dict,
                          source: str | None = None) -> str:
    """Render the ``zh-fixes.md`` table: 卡片 / 当前中文 / 建议中文 / 理由.

    ``current_by_no`` maps a card number to its current Chinese lines (read
    from ``cards-base.json``/``cards.json`` by the caller, never from the
    receipt).  Multiple lines join with ``<br>`` like the 6-column manifest.
    """
    lines = [
        "# 中文校对建议 · %s" % doc_id,
        "",
        "> 只报不改：本表不修改 cards.json。逐条敲定后跑 `zh_fix.py apply`，"
        "再 `approve.py` 重新定稿 + `cards_apply.py --refresh-zh`。",
    ]
    if source:
        lines.append("> 回执：%s" % source)
    lines.extend(["", "| 卡片 | 当前中文 | 建议中文 | 理由 |", "|---|---|---|---|"])
    for fix in fixes:
        current = current_by_no.get(fix["no"]) or []
        current_text = "<br>".join(_md_cell(line) for line in current) or "（未找到）"
        suggestion = "<br>".join(_md_cell(line) for line in fix["zh"])
        why = _md_cell(fix.get("why") or "") or "—"
        lines.append("| %s | %s | %s | %s |"
                     % (fix["no"], current_text, suggestion, why))
    return "\n".join(lines) + "\n"


def resolve_six_manifest(doc_path: str, work: str, doc: dict) -> str:
    """The 6-column manifest for E9: ``$RUN`` first, then the pack copy.

    Deliberately *not* the E4 产出区 copy (it keeps the frozen five columns).
    Exit 3 when neither exists or neither is 6 columns.
    """
    candidates = [("$RUN/docs/<doc>/切分清单.md",
                   os.path.join(doc_path, "切分清单.md"))]
    if doc.get("pack_manifest"):
        candidates.append(("doc.json.pack_manifest",
                           common.resolve_work_path(work, str(doc["pack_manifest"]))))
    if doc.get("pack_dir") and doc.get("stem"):
        candidates.append((
            "包内清单",
            os.path.join(common.resolve_work_path(work, str(doc["pack_dir"])),
                         "%s_切分清单.md" % doc["stem"]),
        ))
    tried = []
    for label, path in candidates:
        if not os.path.isfile(path):
            tried.append("%s：%s（不存在）" % (label, path))
            continue
        if common.manifest_columns(common.read_text(path)) == 6:
            return path
        tried.append("%s：%s（不是 6 列）" % (label, path))
    raise common.Failure(
        common.EXIT_ASK,
        "找不到 6 列清单，无法处理中文。\n"
        + "\n".join("  - " + line for line in tried)
        + "\n先跑 E5（translate_apply）或 E8 收尾。",
        reason="manifest6_missing",
    )


def cards_sha256(cards: list) -> str:
    """Card-set identity: sha over "[[no, content_sha256], ...]" in order.

    0.0.4 binds E10/E11/E12 to the *content* of cards.json instead of its
    mtime: the plan, approve2 token, sample receipt and push receipt all carry
    this one value, so a single changed card invalidates them.  ``order`` is
    sorted numerically (cards.json is already 1..N; sorting keeps the helper
    honest even when it is not).
    """
    def order_key(card):
        try:
            return int(card.get("order") or 0)
        except (TypeError, ValueError):
            return 0

    pairs = [[str(card.get("no")), str(card.get("content_sha256") or "")]
             for card in sorted(cards, key=order_key)]
    return common.sha256_text(
        json.dumps(pairs, ensure_ascii=False, separators=(",", ":")))


def render_preview(cards_doc: dict, limit: int | None = 3) -> str:
    """Render the human preview; ``limit=None`` means every card (0.0.5).

    The default is a *sample* preview (first 1-3 cards).  ``cards.json`` is the
    full machine truth and is what the proofreader / cloud push consume; the
    preview exists only so a human can eyeball the card shape quickly.
    """
    cards = cards_doc["cards"]
    total = len(cards)
    if limit is None:
        shown = total
        title = "# 卡片预览 · %d 张（章节：%s）" % (total, cards_doc["chapter"])
    else:
        shown = max(1, min(int(limit), total))
        title = "# 卡片预览 · 前 %d 张（共 %d 张；章节：%s）" % (
            shown, total, cards_doc["chapter"])
    lines = [
        title,
        "",
        "> 内容里的 `%s` 在写入云端时替换为实际上传的音频 id。"
        % common.CARD_AUDIO_PLACEHOLDER,
        "",
    ]
    if shown < total:
        lines.extend([
            "> 样卡预览：默认只列前 3 张；全量机器真源是 cards.json。"
            "排障要全部时加 --full-preview。",
            "",
        ])
    for card in cards[:shown]:
        lines.append("## %d. %s · %s" % (card["order"], card["no"], card["title"]))
        lines.append("")
        lines.append("```text")
        lines.extend(card["content_template"].split("\n"))
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "doc": args.doc}
    try:
        run = common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        log = common.StageLog(run_dir, STAGE, args.doc)
        doc_path = common.doc_dir(run_dir, args.doc)
        work = run.get("work", "")
        base_path = os.path.join(doc_path, "cards-base.json")
        if not os.path.isfile(base_path):
            if args.refresh_zh:
                raise common.Failure(
                    common.EXIT_ASK,
                    "缺 cards-base.json：%s（--refresh-zh 需要先跑 cards_build）"
                    % base_path,
                    reason="cards_base_missing",
                )
            raise common.Failure(common.EXIT_USAGE,
                                 "缺 cards-base.json：%s（先跑 cards_build）" % base_path)
        base = common.read_json(base_path)
        cards = base.get("cards") or []
        if not cards:
            raise common.Failure(common.EXIT_FAIL, "cards-base.json 里没有卡片：%s" % base_path)
        cards_by_no = {card["no"]: card for card in cards}

        # 0.1.0 D1: --refresh-zh 只刷新中文。读当前 6 列清单（$RUN 优先，其次包内），
        # 按 card no 取整卡中文行，校验行数 == 英文句数，然后照旧用现有排版回执
        # 重渲染 cards.json —— 不重跑 layouter，也不改清单/translations.json。
        six_path = None
        zh_refreshed = 0
        if args.refresh_zh:
            six_path = resolve_six_manifest(doc_path, work, doc)
            parsed = common.parse_manifest_6col(six_path)
            rows_by_no = {common.fmt_row(row["n"]): row for row in parsed["rows"]}
            for card in cards:
                number = card["no"]
                row = rows_by_no.get(number)
                if row is None:
                    raise common.Failure(
                        common.EXIT_FAIL,
                        "6 列清单里没有第 %s 张对应的行：%s" % (number, six_path))
                if not row["keep"]:
                    raise common.Failure(
                        common.EXIT_FAIL,
                        "第 %s 张对应清单行是「进卡组＝否」，不能刷新中文：%s"
                        % (number, six_path),
                        reason="zh_not_keep_row",
                    )
                zh = list(row.get("zh") or [])
                if len(zh) != len(card["en"]):
                    raise common.Failure(
                        common.EXIT_FAIL,
                        "第 %s 张：清单中文 %d 行 ≠ 英文 %d 句"
                        "（先跑 zh_fix.py apply 或回 E5；不要手改清单）"
                        % (number, len(zh), len(card["en"])),
                        reason="zh_sentence_count_mismatch",
                    )
                if any(not str(line).strip() for line in zh):
                    raise common.Failure(common.EXIT_FAIL,
                                         "第 %s 张：清单中文有空行" % number)
                if zh != list(card.get("zh") or []):
                    zh_refreshed += 1
                card["zh"] = zh
            base["manifest_sha256_v2"] = common.sha256_text(
                common.read_text(six_path))

        missing = missing_receipts(doc_path)
        if missing:
            raise common.Failure(
                common.EXIT_ASK,
                "还缺 %d 个排版回执，先派排版员再重跑：%s"
                % (len(missing), "、".join(missing)),
                reason="receipts_missing",
                missing=missing,
            )
        receipts = load_receipts(os.path.join(doc_path, "cards"), args.doc)
        if not receipts:
            raise common.Failure(common.EXIT_ASK,
                                 "还没有任何排版回执：%s（先跑 cards_build 再派排版员）"
                                 % os.path.join(doc_path, "cards"),
                                 reason="receipts_missing")
        collected = collect_lines(receipts, cards_by_no)
        missing_cards = [card["no"] for card in cards if card["no"] not in collected]
        if missing_cards:
            raise common.Failure(
                common.EXIT_FAIL,
                "回执没有覆盖 %d 张卡：%s（每张卡必须恰好一次）"
                % (len(missing_cards), "、".join(missing_cards[:20])
                   + ("…" if len(missing_cards) > 20 else "")),
                reason="cards_missing",
            )
        fixes = 0
        review_flags = []
        zh_fixes = []
        zh_table = None
        zh_fixes_path = None
        if args.review:
            fixes, review_flags, zh_fixes = apply_review(
                args.review, args.doc, cards_by_no, collected)
            zh_table = render_zh_fixes_table(
                args.doc, zh_fixes,
                {card["no"]: list(card.get("zh") or []) for card in cards},
                os.path.basename(args.review))
            zh_fixes_path = os.path.join(doc_path, "zh-fixes.md")

        warnings = []
        for card in cards:
            collected[card["no"]] = strip_copied_markers(
                collected[card["no"]], card, warnings)

        flags = []
        rendered = []
        for card in cards:
            lines = collected[card["no"]]
            bodies, depths = check_en_lines(lines, card)
            starts = common.sentence_start_lines(bodies)
            if depths and any(depth == 2 for depth in depths):
                first_depth_two = depths.index(2)
                if not any(depth == 1 for depth in depths[:first_depth_two]):
                    flags.append({"no": card["no"], "kind": "format",
                                  "why": "第 %d 行用了 -- 前缀，但同卡前面没有 - 行"
                                         % (first_depth_two + 1)})
            marked = mark_speakers(lines, bodies, depths, starts, card)
            content = common.render_card_content(card["title"], card["zh"], marked)
            if content.count(common.CARD_AUDIO_PLACEHOLDER) != 2:
                raise common.Failure(common.EXIT_FAIL,
                                     "第 %s 张：音频占位符不是恰好 2 个" % card["no"])
            rendered.append({
                "order": card["order"],
                "no": card["no"],
                "title": card["title"],
                "kind": card["kind"],
                "audio": card["audio"],
                "zh": list(card["zh"]),
                "en": list(marked),
                "content_template": content,
                "content_sha256": common.sha256_text(content),
            })
        rendered.sort(key=lambda card: card["order"])
        if len(rendered) != len(cards):
            raise common.Failure(common.EXIT_FAIL,
                                 "渲染出 %d 张 ≠ cards-base 的 %d 张"
                                 % (len(rendered), len(cards)))
        cards_doc = {
            "version": 1,
            "doc": args.doc,
            "stem": base.get("stem"),
            "chapter": base.get("chapter"),
            "audio_placeholder": common.CARD_AUDIO_PLACEHOLDER,
            "counts": {"cards": len(rendered)},
            "fixes": fixes,
            "flags": list(review_flags) + flags,
            "warnings": list(warnings),
            "cards": rendered,
            # 0.0.4: card-set identity (E10/E11/E12 bind to this, not mtime)
            "cards_sha256": cards_sha256(rendered),
        }
        cards_path = os.path.join(doc_path, "cards.json")
        preview_path = os.path.join(doc_path, "cards-preview.md")
        preview_limit = None if args.full_preview else max(1, int(args.preview))
        preview_count = (len(rendered) if preview_limit is None
                         else min(preview_limit, len(rendered)))
        preview_text = render_preview(cards_doc, preview_limit)
        common.atomic_write_json(cards_path, cards_doc)
        common.atomic_write_text(preview_path, preview_text)
        if zh_table is not None:
            common.atomic_write_text(zh_fixes_path, zh_table)
        if args.refresh_zh:
            # cards-base.json is the E9 source for zh; keep it in step with the
            # manifest we just re-rendered cards.json from.
            base["cards"] = cards
            common.atomic_write_json(base_path, base)

        check = common.read_json(cards_path)
        if len(check.get("cards") or []) != len(rendered):
            raise common.Failure(common.EXIT_FAIL, "读回 cards.json 后卡片数不对：%s" % cards_path)
        if check.get("cards_sha256") != cards_sha256(check.get("cards") or []):
            raise common.Failure(common.EXIT_FAIL,
                                 "读回 cards.json 后 cards_sha256 对不上：%s" % cards_path)
        if args.refresh_zh:
            base_check = common.read_json(base_path)
            base_by_no = {card.get("no"): card
                          for card in base_check.get("cards") or []}
            for card in rendered:
                if list(base_by_no.get(card["no"], {}).get("zh") or []) != list(card["zh"]):
                    raise common.Failure(
                        common.EXIT_FAIL,
                        "读回 cards-base.json 后第 %s 张中文与 cards.json 不一致：%s"
                        % (card["no"], base_path))
        preview_text = common.read_text(preview_path)
        if preview_text.count("```text") != preview_count:
            raise common.Failure(common.EXIT_FAIL,
                                 "cards-preview.md 的代码块数不对：%s" % preview_path)
        log("rendered cards=%d receipts=%d fixes=%d zh_fixes=%d flags=%d warnings=%d"
            % (len(rendered), len(receipts), fixes, len(zh_fixes),
               len(cards_doc["flags"]), len(warnings)))
        if args.refresh_zh:
            log("refresh-zh: %d 张按 %s 更新中文" % (zh_refreshed, six_path))
        if zh_fixes_path:
            log("wrote %s" % zh_fixes_path)
        for warning in warnings:
            log("WARN 第 %s 张第 %d 行：剥离误抄的说话人标记 %r"
                % (warning["no"], warning["line"], warning["removed"]))
        log("wrote %s" % cards_path)
        log("wrote %s" % preview_path)

        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "cards": len(rendered),
            "receipts": len(receipts),
            "fixes": fixes,
            "zh_fixes": len(zh_fixes),
            "zh_fixes_path": zh_fixes_path,
            "refresh_zh": bool(args.refresh_zh),
            "zh_refreshed": zh_refreshed,
            "manifest_path": six_path,
            "flags": cards_doc["flags"],
            "warnings": list(warnings),
            "cards_path": cards_path,
            "cards_sha256": cards_doc["cards_sha256"],
            "preview_path": preview_path,
            "preview_count": preview_count,
            "preview_full": preview_limit is None,
            "first_title": rendered[0]["title"],
            "last_title": rendered[-1]["title"],
        })
        if not args.json:
            print("DOC=%s  合入 %d 个排版回执：%d 张卡（fixes %d，zh_fixes %d，flags %d）"
                  % (args.doc, len(receipts), len(rendered), fixes, len(zh_fixes),
                     len(cards_doc["flags"])))
            print("卡片：%s" % cards_path)
            print("卡片集 sha256：%s" % cards_doc["cards_sha256"])
            print("预览：%s（%s）"
                  % (preview_path,
                     "%d 张全量" % preview_count if preview_limit is None
                     else "前 %d 张样卡" % preview_count))
            if zh_fixes_path:
                print("中文校对建议：%d 条 → %s（只报不改；敲定后跑 zh_fix.py apply）"
                      % (len(zh_fixes), zh_fixes_path))
            if args.refresh_zh:
                print("中文刷新：%d 张按 6 列清单更新（清单：%s）"
                      % (zh_refreshed, six_path))
            for flag in cards_doc["flags"]:
                print("FLAG 第 %s 张：%s" % (flag.get("no"), flag.get("why")))
            for warning in warnings:
                print("WARN 第 %s 张第 %d 行：已剥离误抄的说话人标记 %r"
                      % (warning["no"], warning["line"], warning["removed"]))
        common.emit_json(result)
        return common.EXIT_OK
    except common.Failure as failure:
        if log:
            log("FAIL " + failure.message)
        return common.report_failure(STAGE, failure, {"doc": args.doc})
    except OSError as exc:
        return common.report_failure(
            STAGE,
            common.Failure(common.EXIT_FAIL, "文件读写失败：%s" % exc, reason="os_error"),
            {"doc": args.doc},
        )
    finally:
        if log:
            log.flush()


if __name__ == "__main__":
    sys.exit(main())
