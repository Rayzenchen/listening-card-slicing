#!/usr/bin/env python3
"""translate_apply.py -- E5: merge translator receipts into the 6-column manifest.

Reads every ``translate/<task>.json`` receipt plus the reviewer receipts
(``translate-review-<n>.json`` with ``fixes``; 0.0.4 applies **all** of them,
in numeric order, plus any explicit ``--review`` path — the union is
cumulative, so a later review can never drop an earlier correction), verifies
everything hard, and only then writes:

* ``translations.json`` -- row number -> Chinese lines, the single source of
  truth for E9 and for later 译文 revisions; 0.0.4 adds
  ``applied_reviews: [{file, sha256, fixes}]`` so gate e5 can prove every
  review with fixes is still reflected;
* ``$RUN/docs/<id>/切分清单.md`` and the 产出区 copy ``<stem>_切分清单.md`` --
  the 6-column manifest, byte-identical to 第二期接口 §2 (same sha).  E5 runs
  **before** the pack exists, so the pack copy is only written when a delivered
  pack is already there (phase-two entry / post-delivery 译文 revision); that
  rewrite also refreshes ``pack.json``'s manifest sha;
* ``manifest-5col.md`` -- the frozen E4 5-column baseline, written on the first
  run so 「一列不改五列」 can always be recomputed from disk;
* ``doc.json.pack_manifest`` (only when a pack copy exists) /
  ``doc.json.manifest_sha256_v2``.

Every check runs before the first write, so a bad receipt set leaves **no**
product behind (no half-merged manifest, no stale translations.json).

Checks (any failure -> exit 1, nothing written):
 * each keep row appears exactly once across all receipts, no duplicate row,
   no no-keep row, no unknown row number;
 * ``len(zh) == sentence_count(en)`` (第二期接口 §3.4, abbreviation-safe);
 * each line non-empty, no ``<br>``, no newline, no ``**``, no leading or
   trailing whitespace;
 * the 5-column baseline really is the manifest that E4 assembled, and any
   live 6-column copy still has those same five columns.

A missing receipt is different: the translators have not finished yet, so it
is exit 3 (dispatch the listed tasks, rerun) and still nothing is written.

Exit codes: 0 merged / 1 a receipt or the manifest failed a check / 2 usage or
missing prior artifacts / 3 receipts are still missing.

0.0.4 regression: runs 047/069/095 lost review-1's fix because each run only
applied the one ``--review`` it was given.  The cumulative union above plus
``applied_reviews`` are the fix (regression tests live in test_translate_e8).

0.0.10 (D2 译文记忆): rows that ``translate_prepare.py`` already translated
from the previous round are in ``translate-tasks.json.prefilled``.  They are
merged first with ``origin="memory"`` and no receipt is expected for them.
Coverage must be **exactly** prefilled ∪ receipts: a row that is both reused
and translated again is an error (exit 1, ``reused row also translated``);
``translations.json.stats`` gains ``reused`` and
``translated = len(merged) - reused``.  When every row was prefilled the task
list is empty and this still writes the 6-column manifest with zero translator
receipts.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402

STAGE = "e5"
RECEIPT_FORBIDDEN = ("<br>", "\n", "\r", "**")


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "translate_apply.py",
        "E5：合并翻译回执（+ 可选复核 fixes），校验后写 translations.json 与 6 列清单。",
        epilog="回执缺失 → exit 3（只补派缺的任务）；回执/清单校验失败 → exit 1（不写任何产物）。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--review", action="append", default=None, metavar="FILE",
                        help="译文复核回执 JSON，可重复。默认自动发现 $RUN/docs/<id>/"
                             "translate-review-<n>.json 并按 n 升序全部应用（累计，不丢旧 fix）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- receipt helpers --------------------------------------------------------


def normalise_no(value, where: str) -> str:
    """Accept ``5`` / ``"005"`` / ``5`` and return the canonical ``005``."""
    if isinstance(value, bool):
        raise common.Failure(common.EXIT_FAIL, "%s 的行号不是数字：%r" % (where, value))
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise common.Failure(common.EXIT_FAIL, "%s 的行号不是数字：%r" % (where, value))
    if number < 1:
        raise common.Failure(common.EXIT_FAIL, "%s 的行号必须 ≥ 1：%r" % (where, value))
    return common.fmt_row(number)


def check_zh_lines(lines, where: str, expected: int) -> list:
    """Validate one row's Chinese lines and return them unchanged."""
    if not isinstance(lines, list) or not lines:
        raise common.Failure(common.EXIT_FAIL, "%s 的 zh 必须是非空数组" % where)
    if len(lines) != expected:
        raise common.Failure(
            common.EXIT_FAIL,
            "%s 的中文行数 %d ≠ 英文句数 %d（一句一行；句子切分见接口 §3.4）"
            % (where, len(lines), expected),
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
        for needle in RECEIPT_FORBIDDEN:
            if needle in line:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "%s 第 %d 行包含 %r：%r（一格内用多行数组，不要写标记）"
                    % (where, index, needle, line),
                )
        checked.append(line)
    return checked


def load_receipts(receipt_dir: str, doc_id: str) -> list:
    """Read every ``translate/*.json`` in name order (task order is not trusted)."""
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
        if data.get("role") != "translator":
            raise common.Failure(common.EXIT_FAIL,
                                 "回执 role 不是 translator：%s（%r）" % (path, data.get("role")))
        receipts.append((path, data))
    return receipts


def load_prefilled(doc_path: str, doc_id: str, keep_by_no: dict,
                   no_keep: dict) -> dict:
    """Read ``translate-tasks.json.prefilled`` as validated ``{no: zh}``.

    D2: ``translate_prepare.py`` already merged the previous round's
    translations for the rows whose English did not change.  Those rows are
    real translations, so they go through exactly the same checks as a
    receipt (keep row, sentence count, no layout markers) — a hand-edited or
    stale task file must not smuggle bad Chinese into the manifest.
    """
    tasks_path = os.path.join(doc_path, "translate-tasks.json")
    if not os.path.isfile(tasks_path):
        return {}
    data = common.read_json(tasks_path)
    if not isinstance(data, dict):
        raise common.Failure(common.EXIT_FAIL,
                             "translate-tasks.json 不是 JSON 对象：%s" % tasks_path)
    if data.get("doc") not in (None, doc_id):
        raise common.Failure(common.EXIT_FAIL,
                             "translate-tasks.json 的 doc 不是 %r：%s（%r）"
                             % (doc_id, tasks_path, data.get("doc")))
    raw = data.get("prefilled") or {}
    if not isinstance(raw, dict):
        raise common.Failure(common.EXIT_FAIL,
                             "translate-tasks.json 的 prefilled 必须是对象：%s"
                             % tasks_path)
    prefilled = {}
    for key, value in raw.items():
        where = "translate-tasks.json 的 prefilled[%r]" % key
        number = normalise_no(key, where)
        if number in no_keep:
            raise common.Failure(
                common.EXIT_FAIL,
                "%s 是「进卡组＝否」的 %s 行，不该有译文"
                % (number, no_keep[number]["type"]))
        row = keep_by_no.get(number)
        if row is None:
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 不在清单里（或不是 keep 行）：%s" % (where, number))
        if number in prefilled:
            raise common.Failure(common.EXIT_FAIL,
                                 "prefilled 里行 %s 出现了两次：%s" % (number, tasks_path))
        prefilled[number] = check_zh_lines(
            value, "%s（行 %s）" % (where, number),
            common.sentence_count(row["display"]))
    return prefilled


def collect_rows(receipts: list, keep_by_no: dict, no_keep: dict,
                 reserved: dict | None = None) -> tuple:
    """Merge receipts into ``{no: zh}`` while enforcing the coverage rules.

    ``reserved`` is the prefilled map: a receipt that touches the same row is
    exit 1 (``reused row also translated``) — every keep row gets exactly one
    origin, memory or translator.
    """
    merged = {}
    origin = {}
    reserved = reserved or {}
    for path, data in receipts:
        rows = data.get("rows")
        if not isinstance(rows, list):
            raise common.Failure(common.EXIT_FAIL, "回执缺少 rows 数组：%s" % path)
        for index, item in enumerate(rows, start=1):
            where = "%s 第 %d 行" % (os.path.basename(path), index)
            if not isinstance(item, dict):
                raise common.Failure(common.EXIT_FAIL, "%s 不是对象：%r" % (where, item))
            number = normalise_no(item.get("no"), where)
            if number in reserved:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "行 %s 已由译文记忆复用（prefilled），%s 又翻了一遍："
                    "同一行只能有一份（reused row also translated）"
                    % (number, where),
                    reason="reused_row_translated",
                )
            if number in no_keep:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "%s 是「进卡组＝否」的 %s 行（%s），不该翻"
                    % (number, no_keep[number]["type"], where),
                )
            row = keep_by_no.get(number)
            if row is None:
                raise common.Failure(common.EXIT_FAIL,
                                     "%s 不在清单里（或不是 keep 行）：%s" % (where, number))
            if number in merged:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "行 %s 被翻了两次：%s 与 %s（每个 keep 行恰好一次）"
                    % (number, origin[number], path),
                )
            expected = common.sentence_count(row["display"])
            merged[number] = check_zh_lines(
                item.get("zh"), "%s（行 %s）" % (where, number), expected)
            origin[number] = path
    return merged, origin


REVIEW_RE = r"^translate-review-(\d+)\.json$"


def discover_reviews(doc_path: str) -> list:
    """Every ``translate-review-<n>.json`` in the doc dir, ascending by n.

    0.0.4: a review receipt is a **cumulative** record, not a one-shot patch.
    ``translate_apply`` re-runs on every output change, and the old code only
    applied the ``--review`` handed to that one call, so a later run silently
    dropped review-1's fixes (runs 047/069/095).  All numbered receipts are now
    applied in numeric order, newest last (a fix is absolute, so the newest
    receipt wins per row).
    """
    if not os.path.isdir(doc_path):
        return []
    found = []
    for name in os.listdir(doc_path):
        match = re.match(REVIEW_RE, name)
        if match:
            found.append((int(match.group(1)), os.path.join(doc_path, name)))
    found.sort()
    return found


def resolve_reviews(doc_path: str, explicit) -> list:
    """Return the review paths to apply: numbered (ascending) ∪ explicit list.

    Explicit ``--review`` paths are a union, not a replacement: passing only
    ``translate-review-2.json`` must not forget review-1.  Unnumbered review
    files (e.g. ``/tmp/review.json``) apply after the numbered ones, in the
    order the CLI gave them, and win for the rows they touch.
    """
    ordered = [path for _, path in discover_reviews(doc_path)]
    seen = {os.path.realpath(path) for path in ordered}
    for raw in explicit or []:
        path = os.path.abspath(os.path.expanduser(str(raw)))
        if not os.path.isfile(path):
            raise common.Failure(common.EXIT_USAGE,
                                 "复核回执不存在：%s（--review 可重复；默认自动发现 "
                                 "translate-review-<n>.json）" % path)
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def apply_review(path: str, doc_id: str, merged: dict, keep_by_no: dict) -> dict:
    """Override ``merged`` with one reviewer receipt's ``fixes``.

    Returns the ``applied_reviews`` record for ``translations.json``:
    ``{"file": basename, "sha256": <bytes sha>, "fixes": <count applied>}``.
    """
    review = common.read_json(path)
    if not isinstance(review, dict):
        raise common.Failure(common.EXIT_FAIL, "复核回执不是 JSON 对象：%s" % path)
    if review.get("doc") not in (None, doc_id):
        raise common.Failure(common.EXIT_FAIL, "复核回执 doc 不是 %r：%s（%r）"
                             % (doc_id, path, review.get("doc")))
    fixes = review.get("fixes")
    applied = 0
    if fixes is not None:
        if not isinstance(fixes, list):
            raise common.Failure(common.EXIT_FAIL, "复核回执的 fixes 必须是数组：%s" % path)
        for index, fix in enumerate(fixes, start=1):
            where = "%s 的 fixes 第 %d 条" % (os.path.basename(path), index)
            if not isinstance(fix, dict):
                raise common.Failure(common.EXIT_FAIL, "%s 不是对象：%r" % (where, fix))
            number = normalise_no(fix.get("no"), where)
            row = keep_by_no.get(number)
            if row is None:
                raise common.Failure(common.EXIT_FAIL, "%s 的行 %s 不存在或不是 keep 行"
                                     % (where, number))
            expected = common.sentence_count(row["display"])
            merged[number] = check_zh_lines(
                fix.get("zh"), "%s（行 %s）" % (where, number), expected)
            applied += 1
    return {
        "file": os.path.basename(path),
        "sha256": common.sha256_file(path),
        "fixes": applied,
    }


def apply_reviews(paths, doc_id: str, merged: dict, keep_by_no: dict) -> tuple:
    """Apply every review in order; return ``(total_fixes, applied_reviews)``."""
    total = 0
    entries = []
    for path in paths:
        entry = apply_review(path, doc_id, merged, keep_by_no)
        total += entry["fixes"]
        entries.append(entry)
    return total, entries


# --- baseline manifest ------------------------------------------------------


def _e4_reassembled(doc: dict, doc_path: str, live_text: str) -> bool:
    """True when the live 5-column manifest is a fresh E4 (regroup) output.

    ``assemble`` rewrites ``segments.json.manifest_sha256`` and
    ``doc.json.manifest_sha256`` together with the manifest, so an E4 re-run
    after E5 is provable from disk without trusting conversation history.
    """
    live_sha = common.sha256_text(live_text)
    if doc.get("manifest_sha256") != live_sha:
        return False
    try:
        segments = common.read_json(os.path.join(doc_path, "segments.json"))
    except (common.Failure, OSError):
        return False
    return isinstance(segments, dict) and segments.get("manifest_sha256") == live_sha


def resolve_baseline(manifest_path: str, doc: dict, doc_path: str) -> tuple:
    """Return ``(base_text, live_text, warnings, reset_snapshot)``.

    On the first E5 run the live manifest is 5 columns and becomes the baseline.
    On later runs ``manifest-5col.md`` is authoritative; if it is missing but
    the live manifest was already extended, the 6-column text is stripped back
    (documented warning) instead of guessing.  A regroup goes back to E4, which
    rewrites the live 5-column manifest; when that new manifest is exactly the
    one segments.json/doc.json bind, the old snapshot is replaced
    (``reset_snapshot=True``) instead of reporting a hand edit.
    """
    live_text = common.read_text(manifest_path)
    snapshot_path = os.path.join(doc_path, "manifest-5col.md")
    warnings = []
    if os.path.isfile(snapshot_path):
        base_text = common.read_text(snapshot_path)
        if common.manifest_columns(base_text) != 5:
            raise common.Failure(common.EXIT_FAIL,
                                 "manifest-5col.md 不是 5 列清单：%s" % snapshot_path)
        if common.manifest_columns(live_text) == 6:
            derived = common.strip_manifest_translation_column(live_text)
            if derived != base_text:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "6 列清单的前 5 列与 manifest-5col.md 不一致（清单被手改过？）：%s"
                    % manifest_path,
                    reason="manifest_changed",
                )
        elif live_text != base_text:
            if _e4_reassembled(doc, doc_path, live_text):
                warnings.append(
                    "manifest-5col.md 已按最新 E4 汇编刷新（分组变过？）："
                    "旧 6 列清单与译文作废，按新 5 列基线重做 E5")
                return live_text, live_text, warnings, True
            raise common.Failure(
                common.EXIT_FAIL,
                "清单还是 5 列，但与 manifest-5col.md 不一致（清单被手改过？）：%s"
                % manifest_path,
                reason="manifest_changed",
            )
        return base_text, live_text, warnings, False
    if common.manifest_columns(live_text) == 6:
        base_text = common.strip_manifest_translation_column(live_text)
        warnings.append("没有 manifest-5col.md：已从 6 列清单反推 5 列基准（前 5 列保持不变）")
    else:
        base_text = live_text
    recorded = doc.get("manifest_sha256")
    if recorded and common.manifest_columns(live_text) == 5 \
            and common.sha256_text(base_text) != recorded:
        raise common.Failure(
            common.EXIT_FAIL,
            "清单 sha 与 doc.json.manifest_sha256 不一致（E4 之后被手改过？）：%s\n"
            "请回 E4 重新 assemble，再重做 E5。" % manifest_path,
            reason="manifest_changed",
        )
    return base_text, live_text, warnings, False


def missing_receipts(doc_path: str) -> list:
    """Task ids from translate-tasks.json whose receipt file does not exist yet."""
    tasks_path = os.path.join(doc_path, "translate-tasks.json")
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


def refresh_pack_manifest_sha(doc_path: str, manifest_path: str, sha: str) -> None:
    """Rebind pack.json / receipts/pack.json to a rewritten in-pack 6-column copy.

    A post-delivery 译文 fix rewrites the pack manifest byte-for-byte; the pack
    report is the E7 snapshot, so its manifest sha must follow or the audit
    would disagree with the delivered file.
    """
    for relative in ("pack.json", os.path.join("receipts", "pack.json")):
        path = os.path.join(doc_path, relative)
        if not os.path.isfile(path):
            continue
        data = common.read_json(path)
        if not isinstance(data, dict):
            continue
        if data.get("manifest_sha256") == sha and data.get("manifest_columns") == 6:
            continue
        data["manifest"] = manifest_path
        data["manifest_sha256"] = sha
        data["manifest_columns"] = 6
        common.atomic_write_json(path, data)


# --- main -------------------------------------------------------------------


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
        manifest_path = common.resolve_manifest_path(work, doc)
        pack_dir = doc.get("pack_dir")
        stem = doc.get("stem")
        if not pack_dir or not stem:
            raise common.Failure(common.EXIT_USAGE,
                                 "doc.json 缺 pack_dir / stem，无法定位产出包：%s"
                                 % os.path.join(doc_path, "doc.json"))

        missing = missing_receipts(doc_path)
        if missing:
            raise common.Failure(
                common.EXIT_ASK,
                "还缺 %d 个翻译回执，先派翻译员再重跑：%s"
                % (len(missing), "、".join(missing)),
                reason="receipts_missing",
                missing=missing,
            )

        base_text, live_text, warnings, reset_snapshot = resolve_baseline(
            manifest_path, doc, doc_path)
        parsed = common.parse_manifest_text(base_text, manifest_path)
        rows = parsed["rows"]
        keep_rows = [row for row in rows if row["keep"]]
        if not keep_rows:
            raise common.Failure(common.EXIT_FAIL, "清单里没有任何 keep 行：%s" % manifest_path)
        keep_by_no = {common.fmt_row(row["n"]): row for row in keep_rows}
        no_keep = {common.fmt_row(row["n"]): row for row in rows if not row["keep"]}

        receipts = load_receipts(os.path.join(doc_path, "translate"), args.doc)
        prefilled = load_prefilled(doc_path, args.doc, keep_by_no, no_keep)
        adopted_existing = False
        adopted, adoption_problems = {}, []
        if common.manifest_columns(live_text) == 6:
            live_parsed = common.parse_manifest_6col(manifest_path)
            adopted, adoption_problems = common.adopted_translations(live_parsed)
        # D2: memory first (origin="memory"), then the receipts.
        merged = {number: list(zh) for number, zh in prefilled.items()}
        origin = {number: "memory" for number in prefilled}
        if receipts:
            receipt_rows, receipt_origin = collect_rows(
                receipts, keep_by_no, no_keep, reserved=prefilled)
            merged.update(receipt_rows)
            origin.update(receipt_origin)
        elif not prefilled:
            # AMENDMENTS-2 A2-6: a pack that already carries a *complete* 译文
            # column (keep rows filled, no-keep rows blank) is adopted as is —
            # the 「只制卡」 entry must not need a translator round-trip.
            if not adoption_problems:
                adopted_existing = True
                merged = {common.fmt_row(row["n"]): adopted[row["n"]] for row in keep_rows}
            else:
                raise common.Failure(
                    common.EXIT_ASK,
                    "还没有任何翻译回执：%s\n清单已有「译文」列但不完整：%s\n"
                    "先补翻这些行（或恢复 5 列清单重做 E5）。"
                    % (os.path.join(doc_path, "translate"),
                       "；".join(adoption_problems[:8])
                       + ("…" if len(adoption_problems) > 8 else "")),
                    reason="receipts_missing",
                )
        missing_rows = [number for number in keep_by_no if number not in merged]
        if missing_rows:
            raise common.Failure(
                common.EXIT_FAIL,
                "译文没有覆盖 %d 个 keep 行：%s（prefilled + 回执必须恰好覆盖每一行）"
                % (len(missing_rows), "、".join(missing_rows[:20])
                   + ("…" if len(missing_rows) > 20 else "")),
                reason="rows_missing",
            )
        reused = len(prefilled)
        review_paths = resolve_reviews(doc_path, args.review)
        fixes, applied_reviews = apply_reviews(
            review_paths, args.doc, merged, keep_by_no)

        translations = {row["n"]: merged[common.fmt_row(row["n"])] for row in keep_rows}
        render_rows = [
            {"n": row["n"], "type": row["type"], "keep": row["keep"],
             "position": row["position"], "display": row["display"]}
            for row in rows
        ]
        six_text = common.render_manifest_6col(parsed["stem"], render_rows, translations)
        six_bytes = six_text.encode("utf-8")
        sha = common.sha256_bytes(six_bytes)
        if adopted_existing and six_text != live_text:
            raise common.Failure(
                common.EXIT_FAIL,
                "6 列清单不是脚本的标准渲染结果（列距/说明行被手改过？），"
                "无法幂等重建：%s" % manifest_path,
                reason="manifest_changed",
            )

        translations_doc = common.translations_document(
            args.doc, rows, translations, sha, common.sha256_text(base_text), fixes)
        translations_doc["applied_reviews"] = applied_reviews
        # D2 stats: reused rows came from the memory (no translator work this
        # round); everything else was translated now (or adopted from the pack).
        translations_doc["stats"]["reused"] = reused
        translations_doc["stats"]["translated"] = len(merged) - reused

        # Everything validated: now write (and only now).
        snapshot_path = os.path.join(doc_path, "manifest-5col.md")
        if reset_snapshot or not os.path.isfile(snapshot_path):
            common.atomic_write_text(snapshot_path, base_text)
        translations_path = os.path.join(doc_path, "translations.json")
        common.atomic_write_json(translations_path, translations_doc)
        doc_manifest_path = os.path.join(doc_path, "切分清单.md")
        out_manifest_path = common.resolve_work_path(work, str(doc["manifest"]))
        pack_manifest_path = os.path.join(
            common.resolve_work_path(work, str(pack_dir)),
            "%s_切分清单.md" % parsed["stem"])
        # 0.0.8: E5 writes $RUN, then the 产出区 review copy.  The pack does not
        # exist yet; if it already does (phase-two / post-delivery 译文 fix) the
        # same bytes are written there too and pack.json's sha is refreshed.
        written = [doc_manifest_path]
        common.atomic_write_text(doc_manifest_path, six_text)
        if os.path.abspath(out_manifest_path) != os.path.abspath(doc_manifest_path):
            common.ensure_dir(os.path.dirname(out_manifest_path))
            common.atomic_write_text(out_manifest_path, six_text)
            written.append(out_manifest_path)
        pack_exists = (os.path.isdir(os.path.dirname(pack_manifest_path))
                       and os.path.isfile(pack_manifest_path))
        if pack_exists and os.path.abspath(pack_manifest_path) not in {
                os.path.abspath(path) for path in written}:
            common.atomic_write_text(pack_manifest_path, six_text)
            written.append(pack_manifest_path)
        doc["manifest_sha256_v2"] = sha
        if pack_exists:
            doc["pack_manifest"] = pack_manifest_path
        else:
            doc.pop("pack_manifest", None)
        common.atomic_write_json(os.path.join(doc_path, "doc.json"), doc)
        if pack_exists:
            try:
                refresh_pack_manifest_sha(doc_path, pack_manifest_path, sha)
            except (common.Failure, OSError) as exc:
                warnings.append("包内清单已更新，但 pack.json 的 sha 刷新失败：%s" % exc)

        # Read back: every written copy must be byte-identical, the extension
        # must keep the first five columns, and every row must carry the merged zh.
        for path in written:
            if common.sha256_file(path) != sha:
                raise common.Failure(common.EXIT_FAIL,
                                     "6 列清单写盘后 sha 不一致：%s" % path)
        check = common.parse_manifest_6col(doc_manifest_path)
        for row in check["rows"]:
            want = translations.get(row["n"], [])
            if row["zh"] != want:
                raise common.Failure(common.EXIT_FAIL,
                                     "6 列清单读回后行 %s 的译文对不上：%r ≠ %r"
                                     % (common.fmt_row(row["n"]), row["zh"], want))
            if row["keep"] and not row["zh"]:
                raise common.Failure(common.EXIT_FAIL,
                                     "keep 行 %s 读回后译文为空" % common.fmt_row(row["n"]))

        for warning in warnings:
            log("WARN " + warning)
        log("merged %d receipts: keep=%d reused=%d fixes=%d reviews=%d sha=%s"
            % (len(receipts), len(keep_rows), reused, fixes,
               len(applied_reviews), sha))
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "manifest": manifest_path,
            "manifest_columns": 6,
            "manifest_sha256_v2": sha,
            "manifest_5col_sha256": translations_doc["manifest_5col_sha256"],
            "rows": len(rows),
            "translated": len(keep_rows),
            "reused": reused,
            "prefilled": reused,
            "newly_translated": len(merged) - reused,
            "blank": len(rows) - len(keep_rows),
            "receipts": len(receipts),
            "fixes": fixes,
            "reviews": [entry["file"] for entry in applied_reviews],
            "applied_reviews": applied_reviews,
            "already_translated": adopted_existing,
            "sentences": translations_doc["stats"]["sentences"],
            "translations_path": translations_path,
            "manifest_path": doc_manifest_path,
            "out_manifest": out_manifest_path,
            "pack_manifest": pack_manifest_path if pack_exists else None,
            "pack_manifest_synced": bool(pack_exists),
            "manifest_5col_path": snapshot_path,
            "manifest_5col_reset": bool(reset_snapshot),
            "warnings": warnings,
        })
        if not args.json:
            print("DOC=%s  合入 %d 个回执：keep %d 行（复用 %d 行）/ 空 %d 行 / fixes %d"
                  "（%d 份复核）"
                  % (args.doc, len(receipts), len(keep_rows), reused,
                     len(rows) - len(keep_rows), fixes, len(applied_reviews)))
            print("6 列清单：%s" % doc_manifest_path)
            print("产出区清单：%s" % out_manifest_path)
            if pack_exists:
                print("包内清单：%s（已同步，pack.json sha 一并刷新）" % pack_manifest_path)
            print("清单 sha256：%s" % sha)
            for warning in warnings:
                print("WARN %s" % warning)
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
