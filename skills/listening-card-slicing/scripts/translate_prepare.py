#!/usr/bin/env python3
"""translate_prepare.py -- E5: build the translation work order (translate-tasks.json).

Reads the manifest in the frozen order (第二期接口 §1: ``doc.json.pack_manifest``
→ ``doc.pack_dir/<stem>_切分清单.md`` → ``doc.manifest``).  At E5 the pack does
not exist yet, so the 产出区 5-column manifest is the live source.  It keeps only the
「进卡组＝是」 rows (K rows, 96 for 卷1) and cuts them into translator tasks of
``--chunk`` rows (default 8).  Each row hands the translator exactly what the
receipt has to answer:

    {"no": "005", "type": "正文", "position": "Section A / News Report 1 (Q1-2)",
     "en": "Everything changed …", "sentence_count": 2}

``en`` is the manifest 原文 cell **verbatim** (with ``**M:**`` / ``**W:**``
markers): the translator needs them to keep ``M:`` / ``W:`` in the Chinese
column, and ``sentence_count`` is the number of Chinese lines the receipt must
carry (one sentence = one line, 第二期接口 §3.2).

0.0.10 (D2 译文记忆): before cutting tasks, rows whose ``display`` is
byte-for-byte identical to an ``en`` in ``translations-memory.json`` are
**prefilled** and never dispatched again — a regroup only has to translate the
rows the user actually changed.  ``--reuse-memory`` is on by default;
``--no-reuse-memory`` restores the 0.0.9 "translate everything" behavior.
Stage 3 adds sentence-level memory: a **single-sentence** new row that misses
the whole-row match is looked up by its sentence text (speaker marks removed)
and reused with the speaker prefix rewritten to the new cell; multi-sentence
rows are never stitched from sentence fragments.  Memory is snapshotted
automatically when it does not exist yet (from
``$RUN/docs/<id>/manifest-6col.prev-*.md`` or the current 6-column manifest).
``ALREADY_TRANSLATED=1`` keeps its 0.0.9 meaning: the manifest already carries
a complete 译文 column and is adopted instead of dispatched.  A translations.json
that no longer binds the current 5-column manifest means E4 re-assembled after
a regroup, so it must not make this stage "skip": tasks are regenerated and the
unchanged rows come back as ``prefilled``.  Whenever tasks are regenerated and
old artifacts exist (regroup, or a restart where translations.json is missing
but ``translate/`` / numbered reviews are still there), the old ``translate/``
directory **and** every ``translate-review-<n>.json`` are moved together into
``translate.prev-<ts>/`` first (never deleted) — stale review fixes would
otherwise be applied to different new rows (P1).  ``--keep-receipts`` turns
that off (explicit operator choice, apply then fails loudly).  ``--redo``
(revising translations, not a regroup) never reuses the memory and never
archives receipts/reviews; a first E5 with no old artifacts archives nothing.

Outputs (all under ``$RUN/docs/<id>/``): ``translate-tasks.json`` and an empty
``translate/`` directory.  Nothing else is written; the manifest is never
touched here.  ``translate-tasks.json`` gains ``prefilled`` (row number ->
Chinese lines, already done) and ``counts.prefilled``; when every row is
prefilled the task list is legitimately empty (``tasks=0``, file still
written) and ``translate_apply.py`` can render the 6-column manifest without
any translator receipt.

Exit codes: 0 task list written (or already merged, nothing to do) /
1 inconsistent input / 2 usage or missing prior artifacts / 3 the user has to
decide (the manifest already carries 译文 but there is no translations.json).
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
import translate_memory  # noqa: E402

STAGE = "e5"
DEFAULT_CHUNK = 8
BRIEF_ANCHOR = "references/subagent-briefs.md#translator"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "translate_prepare.py",
        "E5：读切分清单，把「进卡组＝是」的行按 --chunk 切成翻译任务，写 translate-tasks.json。",
        epilog="清单已经合入译文（有 translations.json）→ exit 0 提示；"
               "清单已加译文列但缺 translations.json → exit 3 请用户确认。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                        help="每个任务几行（默认 %d）" % DEFAULT_CHUNK)
    parser.add_argument("--redo", action="store_true",
                        help="已有 translations.json 也重新派任务（修订译文时用）")
    parser.add_argument("--reuse-memory", dest="reuse_memory", action="store_true",
                        default=True,
                        help="复用上一版 6 列清单的译文：原文逐字相同的行直接 prefilled，"
                             "不派翻译（默认开启）")
    parser.add_argument("--no-reuse-memory", dest="reuse_memory", action="store_false",
                        help="关闭译文复用，全部 keep 行重新派任务（0.0.9 行为）")
    parser.add_argument("--keep-receipts", action="store_true",
                        help="重编/中断重跑时不自动归档旧 translate/ 回执与旧 "
                             "translate-review-<n>.json（默认会归档为 "
                             "translate.prev-<ts>/，因为旧产物按行号已失效）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


REVIEW_RE = re.compile(r"^translate-review-(\d+)\.json$")


def stale_review_files(doc_path: str) -> list:
    """Numbered ``translate-review-<n>.json`` files that apply would auto-apply."""
    if not os.path.isdir(doc_path):
        return []
    return sorted(
        os.path.join(doc_path, name) for name in os.listdir(doc_path)
        if REVIEW_RE.match(name) and os.path.isfile(os.path.join(doc_path, name)))


def archive_stale_artifacts(doc_path: str) -> tuple:
    """Move the previous round's receipts **and** translation reviews aside.

    Called when this run is regenerating tasks and the old artifacts can no
    longer be trusted (D2 阶段 2/3, P1): a regroup changes row numbers and
    sentence counts, so an old ``translate-review-<n>.json`` fix may either
    blow up ``translate_apply`` (sentence-count mismatch) or — worse — silently
    overwrite a *different* new row when the counts happen to line up.  Both
    the ``translate/`` receipt directory and every numbered translation review
    are moved (never deleted) into one ``translate.prev-<ts>/`` directory, and
    an empty ``translate/`` is created for the new tasks.

    Returns ``(archive_path, moved_review_names)``; ``(None, [])`` when there
    was nothing to archive.
    """
    receipt_dir = os.path.join(doc_path, "translate")
    receipts = ([name for name in os.listdir(receipt_dir) if not name.startswith(".")]
                if os.path.isdir(receipt_dir) else [])
    reviews = stale_review_files(doc_path)
    if not receipts and not reviews:
        return None, []
    stamp = common.now_compact()
    target = os.path.join(doc_path, "translate.prev-%s" % stamp)
    suffix = 0
    while os.path.exists(target):
        suffix += 1
        target = os.path.join(doc_path, "translate.prev-%s-%d" % (stamp, suffix))
    moved_reviews = []
    try:
        if receipts:
            os.replace(receipt_dir, target)
        else:
            common.ensure_dir(target)
        for path in reviews:
            name = os.path.basename(path)
            os.replace(path, os.path.join(target, name))
            moved_reviews.append(name)
    except OSError as exc:
        raise common.Failure(
            common.EXIT_FAIL,
            "重编后旧翻译产物归档失败（→ %s）：%s\n"
            "请手动移走 translate/ 与 translate-review-<n>.json 后重跑，"
            "或加 --keep-receipts 自行处理。" % (target, exc),
            reason="receipts_archive_failed",
        )
    common.ensure_dir(receipt_dir)
    return target, moved_reviews


def _translations_sha_matches(translations_path: str, columns: int,
                             manifest_text: str) -> bool:
    """True when ``translations.json`` still binds the live manifest.

    A regroup goes E4 → assemble rewrites the **5-column** manifest while
    ``translations.json`` still describes the previous round; in that state
    ``translate_prepare`` must not take the 0.0.9 "already merged, skip" exit
    (SPEC §3.1: 重编分组后再 E5).  Old 0.0.9 files that lack the recorded sha
    keep the old skip semantics.
    """
    try:
        data = common.read_json(translations_path)
    except (common.Failure, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if columns == 6:
        recorded = data.get("manifest_sha256_v2")
    else:
        recorded = data.get("manifest_5col_sha256")
    if not recorded:
        return True
    return str(recorded) == common.sha256_text(manifest_text)


def display_speaker(display: str) -> str:
    """``M`` / ``W`` when the single-sentence cell starts with a speaker mark."""
    sentences, speakers = common.split_sentences_with_speakers(display)
    if len(sentences) == 1 and speakers and speakers[0] in ("M", "W"):
        return speakers[0]
    return ""


def split_reusable(keep_rows, memory: dict) -> tuple:
    """Split keep rows into ``(prefilled, pending)`` by memory match.

    Stage 1–2 rule first: a row whose ``display`` is byte-for-byte identical to
    a whole-row memory ``en`` reuses that Chinese as is.

    Stage 3 addition (SPEC §3 增补): when the whole row misses and the new row
    is a **single sentence**, look the sentence up in the sentence-level memory
    (``strip_speaker_markers(display)``).  A hit is reused with the speaker
    prefix rewritten to the new row's speaker: ``M: ``/``W: `` is added when
    the new cell carries a mark and removed when it does not.  Multi-sentence
    rows are never stitched together from sentence fragments — grouping can
    change where a sentence sits, and concatenating could silently mix
    translations, so those stay pending.

    ``prefilled`` is ``{str(row_n): zh_lines}`` (the shape written to
    ``translate-tasks.json``); ``pending`` keeps manifest order so task
    numbering is deterministic.  A memory entry whose Chinese line count no
    longer matches the English sentence count is **not** reused — the
    translator redoes that row instead of writing a bad receipt.
    """
    index = memory["index"]
    sentence_index = memory.get("sentence_index") or {}
    prefilled, pending = {}, []
    for row in keep_rows:
        display = row["display"]
        count = common.sentence_count(display)
        zh = index.get(display)
        if zh and len(zh) == count:
            prefilled[str(row["n"])] = list(zh)
            continue
        if sentence_index and count == 1:
            hit = sentence_index.get(common.strip_speaker_markers(display))
            if hit:
                speaker = display_speaker(display)
                chinese = hit["zh"]
                prefilled[str(row["n"])] = [
                    ("%s: %s" % (speaker, chinese)) if speaker else chinese]
                continue
        pending.append(row)
    return prefilled, pending


def task_payload(doc_id: str, keep_rows, chunk: int) -> list:
    """Cut the keep rows into ``d01-t01``-style tasks (deterministic naming)."""
    tasks = []
    for start in range(0, len(keep_rows), chunk):
        index = len(tasks) + 1
        name = "%s-t%02d" % (doc_id, index)
        rows = []
        for row in keep_rows[start:start + chunk]:
            rows.append({
                "no": common.fmt_row(row["n"]),
                "type": row["type"],
                "position": row["position"],
                "en": row["display"],
                "sentence_count": common.sentence_count(row["display"]),
            })
        tasks.append({
            "task": name,
            "receipt": "translate/%s.json" % name,
            "brief": BRIEF_ANCHOR,
            "rows": rows,
        })
    return tasks


# --- AMENDMENTS-2 A2-6: adopt a finished 6-column package --------------------


def adopt_existing(doc_id: str, manifest_path: str, live_text: str,
                   translations: dict, doc: dict, doc_path: str) -> dict:
    """Take the 译文 column of a finished package as the E5 result.

    The 5-column baseline is recovered by stripping the 6th column, and the
    manifest is re-rendered from that baseline + the adopted Chinese.  Only if
    the bytes match the live manifest exactly (no hand edits) are
    ``manifest-5col.md`` / ``translations.json`` written, so a later
    ``translate_apply`` reproduces the very same manifest byte for byte.
    """
    base_text = common.strip_manifest_translation_column(live_text)
    base_parsed = common.parse_manifest_text(base_text, manifest_path)
    rows = base_parsed["rows"]
    render_rows = [
        {"n": row["n"], "type": row["type"], "keep": row["keep"],
         "position": row["position"], "display": row["display"]}
        for row in rows
    ]
    six_text = common.render_manifest_6col(base_parsed["stem"], render_rows, translations)
    if six_text != live_text:
        raise common.Failure(
            common.EXIT_FAIL,
            "6 列清单不是脚本的标准渲染结果（列距 / 说明行 / 表格被手改过？），"
            "无法据此重建 5 列基准（E5）：%s" % manifest_path,
            reason="manifest_changed",
        )
    six_sha = common.sha256_text(six_text)
    translations_doc = common.translations_document(
        doc_id, rows, translations, six_sha, common.sha256_text(base_text))
    snapshot_path = os.path.join(doc_path, "manifest-5col.md")
    if os.path.isfile(snapshot_path):
        existing = common.read_text(snapshot_path)
        if existing != base_text:
            raise common.Failure(
                common.EXIT_FAIL,
                "manifest-5col.md 与 6 列清单剥离出的 5 列不一致（被改过？）：%s"
                % snapshot_path,
                reason="manifest_changed",
            )
    else:
        common.atomic_write_text(snapshot_path, base_text)
    translations_path = os.path.join(doc_path, "translations.json")
    common.atomic_write_json(translations_path, translations_doc)
    # 0.0.8: E5 has no pack yet; only record pack_manifest when the extended
    # manifest really lives inside the delivered pack (phase-two entry).
    pack_dir = doc.get("pack_dir")
    if pack_dir:
        pack_abs = os.path.abspath(str(pack_dir))
        manifest_abs = os.path.abspath(manifest_path)
        if manifest_abs == pack_abs or manifest_abs.startswith(pack_abs + os.sep):
            doc["pack_manifest"] = manifest_path
    doc["manifest_sha256_v2"] = six_sha
    common.atomic_write_json(os.path.join(doc_path, "doc.json"), doc)
    check = common.parse_manifest_6col(manifest_path)
    for row in check["rows"]:
        want = translations.get(row["n"], [])
        if row["zh"] != want:
            raise common.Failure(common.EXIT_FAIL,
                                 "采纳译文后读回行 %s 对不上：%r ≠ %r"
                                 % (common.fmt_row(row["n"]), row["zh"], want))
    return {
        "translations_path": translations_path,
        "manifest_5col_path": snapshot_path,
        "manifest_sha256_v2": six_sha,
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.chunk < 1:
        print("ERROR: --chunk 必须 ≥ 1（收到 %r）" % args.chunk, file=sys.stderr)
        common.emit_json({"ok": False, "exit_code": common.EXIT_USAGE,
                          "stage": STAGE, "error": "chunk 必须 ≥ 1"})
        return common.EXIT_USAGE
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
        manifest_text = common.read_text(manifest_path)
        columns = common.manifest_columns(manifest_text)
        if columns == 6:
            parsed = common.parse_manifest_6col(manifest_path)
        else:
            parsed = common.parse_manifest(manifest_path)
        keep_rows = [row for row in parsed["rows"] if row["keep"]]
        if not keep_rows:
            raise common.Failure(common.EXIT_FAIL, "清单里没有任何「进卡组＝是」的行：%s"
                                 % manifest_path)

        translations_path = os.path.join(doc_path, "translations.json")
        already_merged = os.path.isfile(translations_path)
        adopted, adoption_problems = ({}, [])
        if columns == 6:
            adopted, adoption_problems = common.adopted_translations(parsed)
        already_translated = columns == 6 and not adoption_problems and bool(adopted)
        # D2: after a regroup, assemble.py rewrites the 5-column manifest while
        # the previous translations.json is still on disk.  Do not "skip" then:
        # regenerate tasks and let the memory prefill the unchanged rows.
        stale_translations = (already_merged and not args.redo
                              and not _translations_sha_matches(
                                  translations_path, columns, manifest_text))
        if already_merged and not args.redo and not stale_translations:
            log("skip: translations.json exists")
            result.update({"ok": True, "exit_code": common.EXIT_OK, "skipped": True,
                           "reason": "already_merged", "translations": translations_path,
                           "rows": len(keep_rows), "columns": columns,
                           "tasks": 0, "prefilled": 0, "reused": 0,
                           "already_translated": already_translated})
            if not args.json:
                if already_translated:
                    print("ALREADY_TRANSLATED=1")
                print("DOC=%s  已合入译文，跳过（加 --redo 可重派）" % args.doc)
                print("译文：%s" % translations_path)
            common.emit_json(result)
            return common.EXIT_OK
        if stale_translations:
            log("translations.json 与当前清单不再绑定（E4 重汇编 / 改过分组）："
                "重新生成任务，未变行走译文记忆")
        if columns == 6 and not args.redo:
            # AMENDMENTS-2 A2-6: the pack already carries a complete 译文
            # column (e.g. a finished 卷1 package) — adopt it instead of
            # asking a translator to redo work that is already on disk.
            if adoption_problems:
                raise common.Failure(
                    common.EXIT_ASK,
                    "清单已经加过「译文」列，但不完整（%d 个问题）：%s\n"
                    "先补全这些行，或恢复 5 列清单重做 E5；"
                    "确认要按现有清单重新派任务请加 --redo。"
                    % (len(adoption_problems),
                       "；".join(adoption_problems[:8])
                       + ("…" if len(adoption_problems) > 8 else "")),
                    reason="manifest_already_extended",
                )
            adopted_info = adopt_existing(
                args.doc, manifest_path, manifest_text, adopted, doc, doc_path)
            result.update({
                "ok": True,
                "exit_code": common.EXIT_OK,
                "already_translated": True,
                "columns": 6,
                "rows": len(keep_rows),
                "blank": len(parsed["rows"]) - len(keep_rows),
                "tasks": 0,
                "prefilled": 0,
                "reused": 0,
                "translations_path": adopted_info["translations_path"],
                "manifest_5col_path": adopted_info["manifest_5col_path"],
                "manifest_sha256_v2": adopted_info["manifest_sha256_v2"],
                "manifest": manifest_path,
            })
            log("adopted existing 译文 column: keep=%d sha=%s"
                % (len(keep_rows), adopted_info["manifest_sha256_v2"]))
            if not args.json:
                print("ALREADY_TRANSLATED=1")
                print("DOC=%s  清单已含完整译文（%d 行），直接采用，不派翻译"
                      % (args.doc, len(keep_rows)))
                print("5 列快照：%s" % adopted_info["manifest_5col_path"])
                print("译文：%s" % adopted_info["translations_path"])
                print("清单 sha256：%s" % adopted_info["manifest_sha256_v2"])
            common.emit_json(result)
            return common.EXIT_OK

        # --- D2 译文记忆：逐字命中 display 的行不再派翻译 ---------------------
        # 重新生成任务前，旧产物（translate/ 回执 + translate-review-<n>.json）
        # 按行号/句数已可能失效，先整体归档（不删除）：
        #   * P1：translations.json 存在但已不绑当前清单（E4 重汇编）→ 归档；
        #   * P2：translations.json 缺失但 translate/ 或旧复核还在（重编/中断
        #     重跑）→ 同样归档，避免 apply 套用失效 review；
        #   * 首次 E5 没有旧产物、--redo（改译文不是重编）→ 不动现场。
        receipt_dir = os.path.join(doc_path, "translate")
        has_old_receipts = (
            os.path.isdir(receipt_dir)
            and any(not name.startswith(".") for name in os.listdir(receipt_dir)))
        old_reviews = stale_review_files(doc_path)
        restart_artifacts = bool(
            not args.redo
            and (stale_translations
                 or (not already_merged and (has_old_receipts or old_reviews))))
        archived_receipts = None
        archived_reviews = []
        if restart_artifacts:
            if args.keep_receipts:
                log("WARN 保留旧翻译产物（--keep-receipts）：%s"
                    % ("、".join(["translate/"] + [os.path.basename(p) for p in old_reviews])))
                log("WARN 重编后旧产物按行号可能已失效；translate_apply 报 "
                    "reused_row_translated / 行数不符即按提示处理")
            else:
                archived_receipts, archived_reviews = archive_stale_artifacts(doc_path)
                if archived_receipts:
                    log("归档重编前的旧翻译产物：%s → %s（回执 + %d 份旧复核）"
                        % (os.path.join(doc_path, "translate"), archived_receipts,
                           len(archived_reviews)))
        memory = None
        memory_path = None
        memory_note = None
        prefilled, pending_rows = ({}, list(keep_rows))
        reuse_memory = bool(args.reuse_memory and not args.redo)
        if reuse_memory:
            try:
                memory_path = translate_memory.ensure_memory(
                    run_dir, args.doc, work, doc)
            except (common.Failure, OSError) as exc:
                memory_note = ("译文记忆快照失败（已忽略，全部按新译处理）：%s"
                               % getattr(exc, "message", exc))
                log("WARN " + memory_note)
            if memory_path:
                try:
                    memory = translate_memory.load_memory(run_dir, args.doc)
                except (common.Failure, OSError) as exc:
                    # 损坏/半写的 memory 不能把整条流水线卡死：回退为 0 复用。
                    memory = None
                    memory_note = ("译文记忆文件不可用（已忽略，全部按新译处理）：%s"
                                   % getattr(exc, "message", exc))
                    log("WARN " + memory_note)
            if memory:
                prefilled, pending_rows = split_reusable(keep_rows, memory)
                log("memory %s: entries=%d reused=%d pending=%d"
                    % (memory_path, len(memory["entries"]),
                       len(prefilled), len(pending_rows)))
        elif args.redo:
            # --redo 的语义是「已有 translations.json 也重新派任务」；复用记忆
            # 会把任务清零、把 redo 架空，所以 redo 一律绕过译文记忆。
            log("--redo: 绕过译文记忆，全部 %d 行重新派任务" % len(keep_rows))
        else:
            log("--no-reuse-memory: 全部 %d 行重新派任务" % len(keep_rows))

        tasks = task_payload(args.doc, pending_rows, args.chunk)
        task_doc = {
            "version": 1,
            "doc": args.doc,
            "stage": STAGE,
            "generated_at": common.now_iso(),
            "manifest": manifest_path,
            "manifest_columns": columns,
            "brief": BRIEF_ANCHOR,
            "doc_dir": doc_path,
            "receipt_dir": os.path.join(doc_path, "translate"),
            "chunk": args.chunk,
            "memory": memory_path if memory else None,
            "prefilled": prefilled,
            "counts": {
                "rows": len(keep_rows),
                "tasks": len(tasks),
                "prefilled": len(prefilled),
                "blank": sum(1 for row in parsed["rows"] if not row["keep"]),
            },
            "tasks": tasks,
        }
        tasks_path = os.path.join(doc_path, "translate-tasks.json")
        common.ensure_dir(os.path.join(doc_path, "translate"))
        common.atomic_write_json(tasks_path, task_doc)
        log("manifest=%s columns=%d keep=%d prefilled=%d tasks=%d chunk=%d"
            % (manifest_path, columns, len(keep_rows), len(prefilled),
               len(tasks), args.chunk))
        log("wrote %s" % tasks_path)

        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "manifest": manifest_path,
            "columns": columns,
            "rows": len(keep_rows),
            "blank": task_doc["counts"]["blank"],
            "tasks": len(tasks),
            "prefilled": len(prefilled),
            "reused": len(prefilled),
            "pending": len(pending_rows),
            "chunk": args.chunk,
            "tasks_path": tasks_path,
            "receipt_dir": task_doc["receipt_dir"],
            "task_ids": [task["task"] for task in tasks],
            "memory": memory_path if memory else None,
            "memory_note": memory_note,
            "receipts_archived": archived_receipts,
            "reviews_archived": archived_reviews,
            "receipts_kept": bool(restart_artifacts and args.keep_receipts),
        })
        if not args.json:
            print("DOC=%s  清单=%d 列  进卡组=%d 行（空 %d 行）"
                  % (args.doc, columns, len(keep_rows), task_doc["counts"]["blank"]))
            if prefilled:
                print("复用译文记忆：%d 行（本次不再派翻译）" % len(prefilled))
            if archived_receipts:
                print("旧翻译产物已归档：%s（旧复核 %d 份）"
                      % (archived_receipts, len(archived_reviews)))
            if restart_artifacts and args.keep_receipts:
                print("WARN 旧翻译产物按 --keep-receipts 保留（重编后通常会让 "
                      "translate_apply exit 1）")
            if tasks:
                print("任务：%d 个（每任务 ≤%d 行）%s…%s"
                      % (len(tasks), args.chunk, tasks[0]["task"], tasks[-1]["task"]))
            else:
                print("任务：0 个（所有 keep 行都有译文记忆）"
                      "→ 直接跑 translate_apply.py 出 6 列清单")
            print("回执目录：%s" % task_doc["receipt_dir"])
            print("已写：%s" % tasks_path)
            if memory_note:
                print("WARN %s" % memory_note)
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
