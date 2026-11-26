#!/usr/bin/env python3
"""markji_sync.py -- E12: compare the Markji cloud with the local cards, fix diffs.

``--check``  reads the cloud in chapter display order (``card_ids``) and compares
it with the local cards (``cards.json``; or, when ``$RUN`` was cleaned, rebuilt
from the 6-column manifest + ``translations.json`` — layout is then unknown, so
line breaks are ignored by the normalised comparison).

``--update`` is the explicit maintenance action: it only rewrites the cards that
differ, always with ``update`` (never ``create`` and never delete), requires an
``approve2.json`` with ``updates_ok == true`` (the user has seen the diff list)
**and** ``cards_sha256`` equal to the current cards (0.0.4: a token taken before
cards.json changed is stale), and re-reads the cloud afterwards.  Chapter order
is not touched here.

Comparison (AMENDMENTS-2 A2-1 normalisation): title set, count, display order,
Chinese/English text (layout prefixes and speaker marks ignored), and audio
mounted (exactly one file, two ``mkjf_`` references).

Exit codes: 0 no diff (or fixed) / 1 diffs were found (``--check``) or remain
(``--update``) / 2 dependency or usage problem / 3 the user must decide first
(missing approval / chapter or deck missing).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cards_apply  # noqa: E402  (cards_sha256: the E9 card-set identity)
import common  # noqa: E402
import markji_push  # noqa: E402  (deck/chapter/card helpers + cloud reads)

STAGE = "e12"
SYNC_RECEIPT = "receipts/markji-sync.json"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "markji_sync.py",
        "E12：比对云端与本地卡片；--check 只报告，--update 只对差异卡整张 update。",
        epilog="--check 有差异 → exit 1（不是崩了）；--update 缺 approve2.updates_ok → exit 3；"
               "只读核对缺牌组/章节 → exit 3。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只报告差异（不改云端）")
    mode.add_argument("--update", action="store_true", help="只更新差异卡（需 updates_ok）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只处理前 N 张（排障用）")
    parser.add_argument("--mk", default=None,
                        help="markji_channel.py 路径（默认 $LISTENING_CARD_MK 或安装位置）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- local cards ------------------------------------------------------------


def load_local(run: dict, doc: dict, doc_path: str) -> tuple:
    """Return ``(cards_doc, source)``; rebuild from E8/E9 artefacts if needed."""
    cards_path = os.path.join(doc_path, "cards.json")
    if os.path.isfile(cards_path):
        return markji_push.load_cards(doc_path), "cards.json"
    work = run.get("work", "")
    manifest_path = common.resolve_manifest_path(work, doc)
    if common.manifest_columns(common.read_text(manifest_path)) != 6:
        raise common.Failure(
            common.EXIT_USAGE,
            "既没有 cards.json，清单也不是 6 列：%s（先跑 E8/E9）" % manifest_path,
            reason="local_missing")
    translations_path = os.path.join(doc_path, "translations.json")
    if not os.path.isfile(translations_path):
        raise common.Failure(common.EXIT_USAGE,
                             "cards.json 与 translations.json 都不在，无法重建本地卡片：%s"
                             % translations_path, reason="local_missing")
    import cards_build  # local import keeps the CLI light

    parsed = common.parse_manifest_6col(manifest_path)
    translations_doc = common.read_json(translations_path)
    translations = {}
    for key, value in (translations_doc.get("rows") or {}).items():
        translations[common.fmt_row(int(key))] = list(value)
    scheme = cards_build.derive_scheme(doc["stem"])
    cards = cards_build.build_cards(parsed, translations, scheme)
    local = {
        "version": 1,
        "doc": doc["id"],
        "stem": doc["stem"],
        "chapter": scheme["chapter"],
        "cards": [],
    }
    for card in cards:
        content = common.render_card_content(card["title"], card["zh"], card["en"])
        local["cards"].append({
            "order": card["order"], "no": card["no"], "title": card["title"],
            "audio": card["audio"], "zh": card["zh"], "en": card["en"],
            "content_template": content,
            "content_sha256": common.sha256_text(content),
        })
    return local, "rebuilt"


# --- cloud ------------------------------------------------------------------


def resolve_target(run: dict, doc: dict, cards_doc: dict, channel):
    """Deck + chapter from ``markji.json`` → plan → defaults (exact names)."""
    doc_path = common.doc_dir(os.path.abspath(run["run"]), doc["id"])
    deck_name = markji_push.DEFAULT_DECK
    chapter_name = cards_doc.get("chapter")
    chapter_id = None
    markji_path = os.path.join(doc_path, markji_push.MARKJI_NAME)
    plan_path = os.path.join(doc_path, markji_push.PLAN_NAME)
    if os.path.isfile(markji_path):
        markji = common.read_json(markji_path)
        deck_name = (markji.get("deck") or {}).get("name") or deck_name
        chapter_name = (markji.get("chapter") or {}).get("name") or chapter_name
        chapter_id = (markji.get("chapter") or {}).get("id")
    elif os.path.isfile(plan_path):
        plan = common.read_json(plan_path)
        deck_name = (plan.get("deck") or {}).get("name") or deck_name
        chapter_name = (plan.get("chapter") or {}).get("name") or chapter_name
        chapter_id = (plan.get("chapter") or {}).get("id")
    if not chapter_name:
        raise common.Failure(common.EXIT_USAGE,
                             "cards.json 没有 chapter，且没有 markji.json/计划可参考")
    deck = markji_push.find_deck(channel, deck_name)
    chapter = markji_push.find_chapter(channel, deck["id"], chapter_name)
    if chapter is None:
        raise common.Failure(
            common.EXIT_ASK,
            "云端没有章节 %r（先在 App 里确认，或回 E11 建章节/写卡）。" % chapter_name,
            reason="chapter_missing")
    if chapter_id and chapter.get("id") != chapter_id:
        raise common.Failure(
            common.EXIT_ASK,
            "章节 id 与 markji.json 记录不一致（章节被重建过？）：%s ≠ %s"
            % (chapter.get("id"), chapter_id), reason="chapter_changed")
    return deck, chapter


def collect_diffs(cards_doc: dict, ordered: list, limit: int | None) -> dict:
    cards = list(cards_doc["cards"])
    if limit is not None:
        cards = cards[:max(0, int(limit))]
    pairs, unreadable = markji_push.cloud_cards_by_title(ordered)
    warnings = markji_push.unreadable_warnings(unreadable)
    titles = [title for title, _ in pairs]
    positions = {title: index for index, title in enumerate(titles)}
    diffs = []
    counts = {"cloud": len(ordered), "local": len(cards), "diffs": 0}
    expected_titles = [card["title"] for card in cards]
    if limit is None and len(ordered) != len(cards):
        diffs.append({"kind": "count", "detail": "云端 %d 张 ≠ 本地 %d 张"
                      % (len(ordered), len(cards))})
    seen = set()
    for title in titles:
        if title in seen:
            diffs.append({"kind": "duplicate", "title": title,
                          "detail": "云端同一标题出现两次"})
        seen.add(title)
    cloud_by_title = {title: card for title, card in pairs}
    for title in expected_titles:
        if title not in cloud_by_title:
            diffs.append({"kind": "missing", "title": title,
                          "detail": "云端没有这张卡（sync 不 create，回 E11 push）"})
    local_titles = set(expected_titles)
    for title in titles:
        if title not in local_titles and limit is None:
            diffs.append({"kind": "extra", "title": title,
                          "detail": "云端多出这张卡（sync 不删除，只能 App 手删）"})
    # 顺序：只看双方都有的卡，避免最后一分钟被 extra/missing 干扰
    shared_cloud = [title for title in titles if title in local_titles]
    shared_local = [title for title in expected_titles if title in positions]
    if shared_cloud != shared_local:
        first = next((index for index, (a, b) in enumerate(zip(shared_cloud, shared_local))
                      if a != b), min(len(shared_cloud), len(shared_local)))
        diffs.append({"kind": "order", "detail": "展示顺序不对（第 %d 位：%r ≠ %r）"
                      % (first + 1,
                         shared_cloud[first] if first < len(shared_cloud) else None,
                         shared_local[first] if first < len(shared_local) else None)})
    cards_detail = {}
    for card in cards:
        cloud = cloud_by_title.get(card["title"])
        if cloud is None:
            continue
        number = str(card.get("no"))
        content = str(cloud.get("content") or "")
        file_ids = [item.get("id") for item in cloud.get("files") or [] if item.get("id")]
        cloud_id = file_ids[0] if len(file_ids) == 1 else common.CARD_AUDIO_PLACEHOLDER
        expected = str(card["content_template"]).replace(
            common.CARD_AUDIO_PLACEHOLDER, cloud_id)
        if common.normalize_card_content(expected) != common.normalize_card_content(content):
            expected_sections = common.card_content_sections(expected)
            actual_sections = common.card_content_sections(content)
            kind = "content"
            if len(expected_sections) == 5 and len(actual_sections) == 5:
                if common.normalize_card_content(expected_sections[2]) != \
                        common.normalize_card_content(actual_sections[2]):
                    kind = "zh"
                elif common.normalize_card_content(expected_sections[3]) != \
                        common.normalize_card_content(actual_sections[3]):
                    kind = "en"
            diffs.append({"kind": kind, "no": number, "title": card["title"],
                          "detail": "内容归一后不一致"})
        if len(file_ids) != 1:
            diffs.append({"kind": "audio", "no": number, "title": card["title"],
                          "detail": "云端音频挂载 %d 个（应为 1）" % len(file_ids)})
        elif content.count("mkjf_") != 2:
            diffs.append({"kind": "structure", "no": number, "title": card["title"],
                          "detail": "content 里 mkjf_ 不是 2 处"})
        cards_detail[number] = {
            "title": card["title"], "card_id": cloud.get("id"),
            "file_id": cloud_id, "content_sha256": common.sha256_text(content),
        }
    counts["diffs"] = len(diffs)
    return {"counts": counts, "diffs": diffs, "cards": cards_detail,
            "warnings": warnings}


def write_sync_receipt(doc_path: str, payload: dict) -> str:
    path = os.path.join(doc_path, SYNC_RECEIPT)
    common.ensure_dir(os.path.dirname(path))
    common.atomic_write_json(path, common.redact_urls(payload))
    return path


# --- main -------------------------------------------------------------------


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "doc": args.doc, "mode": "check" if args.check else "update"}
    try:
        run = common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        log = common.StageLog(run_dir, STAGE, args.doc)
        doc_path = common.doc_dir(run_dir, args.doc)
        cards_doc, source = load_local(run, doc, doc_path)
        cards_sha = cards_apply.cards_sha256(cards_doc["cards"])
        channel = common.MarkjiChannel(args.mk, log=log)
        if args.update:
            markji_push.preflight(channel, log)
        deck, chapter = resolve_target(run, doc, cards_doc, channel)
        ordered, _ = markji_push.read_chapter_cards(channel, deck["id"], chapter)
        if args.update:
            approve_path = os.path.join(doc_path, markji_push.APPROVE_NAME)
            if not os.path.isfile(approve_path):
                raise common.Failure(
                    common.EXIT_ASK,
                    "缺 approve2.json：--update 要先让用户看过差异并确认覆盖（updates_ok=true）。",
                    reason="approval_missing")
            approve = common.read_json(approve_path)
            if approve.get("mode") != "user" or not approve.get("updates_ok"):
                raise common.Failure(
                    common.EXIT_ASK,
                    "approve2.json 没有 updates_ok=true（mode=%r, updates_ok=%r）："
                    "不能覆盖云端已有卡。请先给用户看差异，再 approve2.py --updates-ok。"
                    % (approve.get("mode"), approve.get("updates_ok")),
                    reason="updates_not_approved")
            # 0.0.4: 批准令牌还必须绑当前卡片集（cards.json 变了就重新 plan+approve）
            current_cards_sha = cards_apply.cards_sha256(cards_doc["cards"])
            if approve.get("cards_sha256") != current_cards_sha:
                raise common.Failure(
                    common.EXIT_ASK,
                    "approve2.json 绑定的 cards_sha256 与当前 cards.json 不一致"
                    "（卡片在批准之后改过？）：\n当前 %s\n批准 %s\n"
                    "请重跑 markji_push.py --stage plan → approve2.py（用户重新确认差异）"
                    " → markji_sync.py --update。"
                    % (current_cards_sha, approve.get("cards_sha256")),
                    reason="cards_changed")
            before = collect_diffs(cards_doc, ordered, args.limit)
            cloud_by_title = {}
            for cloud_card in ordered:
                title = cloud_card.get("title") or common.card_title(
                    cloud_card.get("content") or "")
                cloud_by_title.setdefault(title, cloud_card)
            updated = []
            failed = []
            work = run.get("work", "")
            for card in list(cards_doc["cards"])[:args.limit] if args.limit else \
                    list(cards_doc["cards"]):
                cloud = cloud_by_title.get(card["title"])
                if cloud is None:
                    continue
                number = str(card.get("no"))
                card_diffs = [diff for diff in before["diffs"]
                              if str(diff.get("no")) == number]
                if not card_diffs:
                    continue
                file_ids = [item.get("id") for item in cloud.get("files") or []
                            if item.get("id")]
                try:
                    if len(file_ids) == 1:
                        file_id = file_ids[0]
                    else:
                        uploaded = channel.call("upload",
                                                markji_push._audio_path(work, doc, card["audio"]),
                                                retry=True)
                        file_id = uploaded.get("id")
                    content = str(card["content_template"]).replace(
                        common.CARD_AUDIO_PLACEHOLDER, file_id)
                    if content.count("mkjf_") != 2:
                        raise common.Failure(common.EXIT_FAIL,
                                             "第 %s 张替换后 mkjf_ 不是 2 处" % number,
                                             reason="audio_placeholder")
                    content_path = markji_push._write_temp_content(doc_path, number, content)
                    try:
                        channel.call("update", deck["id"], cloud["id"],
                                     "--file", content_path, retry=True)
                    finally:
                        if os.path.isfile(content_path):
                            try:
                                os.remove(content_path)
                            except OSError:
                                pass
                    channel.pause()
                    updated.append({"no": number, "title": card["title"],
                                    "card_id": cloud.get("id"), "file_id": file_id,
                                    "content_sha256": common.sha256_text(content)})
                except common.Failure as failure:
                    if failure.code in (common.EXIT_USAGE, common.EXIT_ASK):
                        raise
                    failed.append({"no": number, "title": card["title"],
                                   "error": failure.message})
            ordered, _ = markji_push.read_chapter_cards(channel, deck["id"], chapter)
            after = collect_diffs(cards_doc, ordered, args.limit)
            receipt = {
                "version": 1,
                "doc": args.doc,
                "mode": "update",
                "checked_at": common.now_iso(),
                "deck": {"id": deck.get("id"), "name": deck.get("name")},
                "chapter": {"id": chapter.get("id"), "name": chapter.get("name")},
                "local_source": source,
                "cards_sha256": cards_sha,
                "partial": args.limit is not None and int(args.limit) < len(cards_doc["cards"]),
                "counts": after["counts"],
                "diffs_before": before["diffs"],
                "diffs": after["diffs"],
                "warnings": after.get("warnings") or [],
                "updated": updated,
                "failed": failed,
                "result": "ok" if not after["diffs"] else "fail",
            }
            path = write_sync_receipt(doc_path, receipt)
            ok = receipt["result"] == "ok"
            log("sync update: updated=%d diffs_before=%d diffs_after=%d"
                % (len(updated), len(before["diffs"]), len(after["diffs"])))
            result.update({
                "ok": ok, "exit_code": common.EXIT_OK if ok else common.EXIT_FAIL,
                "updated": len(updated), "failed": len(failed),
                "counts": after["counts"], "diffs": after["diffs"],
                "receipt": path,
            })
            if not args.json:
                print("DOC=%s  已更新 %d 张（失败 %d）" % (args.doc, len(updated), len(failed)))
                print("剩余差异：%d" % len(after["diffs"]))
                for diff in after["diffs"][:10]:
                    print("DIFF %s %s %s" % (diff.get("kind"), diff.get("title") or diff.get("no"),
                                             diff.get("detail") or ""))
                print("回执：%s" % path)
            common.emit_json(result)
            return common.EXIT_OK if ok else common.EXIT_FAIL

        state = collect_diffs(cards_doc, ordered, args.limit)
        receipt = {
            "version": 1,
            "doc": args.doc,
            "mode": "check",
            "checked_at": common.now_iso(),
            "deck": {"id": deck.get("id"), "name": deck.get("name")},
            "chapter": {"id": chapter.get("id"), "name": chapter.get("name")},
            "local_source": source,
            "cards_sha256": cards_sha,
            "partial": args.limit is not None and int(args.limit) < len(cards_doc["cards"]),
            "counts": state["counts"],
            "diffs": state["diffs"],
            "warnings": state.get("warnings") or [],
            "result": "ok" if not state["diffs"] else "diff",
        }
        path = write_sync_receipt(doc_path, receipt)
        ok = not state["diffs"]
        log("sync check: cloud=%d local=%d diffs=%d"
            % (state["counts"]["cloud"], state["counts"]["local"], len(state["diffs"])))
        result.update({
            "ok": ok, "exit_code": common.EXIT_OK if ok else common.EXIT_FAIL,
            "counts": state["counts"], "diffs": state["diffs"], "receipt": path,
        })
        if not args.json:
            print("DOC=%s  本地来源=%s  云端 %d 张 / 本地 %d 张"
                  % (args.doc, source, state["counts"]["cloud"], state["counts"]["local"]))
            if ok:
                print("没有差异。")
            else:
                print("差异 %d 条：" % len(state["diffs"]))
                for diff in state["diffs"][:10]:
                    print("DIFF %s %s %s"
                          % (diff.get("kind"), diff.get("title") or diff.get("no"),
                             diff.get("detail") or ""))
            print("回执：%s" % path)
        common.emit_json(result)
        return common.EXIT_OK if ok else common.EXIT_FAIL
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
