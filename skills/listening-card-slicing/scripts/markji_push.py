#!/usr/bin/env python3
"""markji_push.py -- E10/E11: plan, sample, push and verify the Markji write.

The transport layer is always ``markji_channel.py`` (never a second API client);
this script only decides *what* may be written:

* ``plan``   -- read-only.  Resolve the deck by exact name, resolve/create the
  chapter, read the whole chapter and classify every local card into the three
  AMENDMENTS-2 A2-1 states: ``create`` / ``skip_identical`` / ``diff_pending``
  (normalised comparison, so a card the user only reformatted stays
  ``skip_identical``).  Writes ``markji-plan.json`` + a receipt copy.  0.1.0:
  the plan also records ``deck.card_count`` and ``gate`` -- a **veteran** deck
  (``card_count > 0``) auto-passes the E10 sample gate, a **first** deck
  (0/missing) writes one sample card and waits for the App confirmation.
  Re-running plan after that sample keeps ``first`` (marker
  ``first_sample_pending``) until a push succeeds, so the gate cannot be
  bypassed by the sample card itself making the deck look "veteran".
* ``sample`` -- first deck: writes/read-checks exactly one ``create`` card
  (no ``approve2.json`` needed -- the App review happens *after* the sample);
  a read-back mismatch is exit 1 and stops the batch.  Veteran deck: writes a
  ``skipped=true`` receipt and does not touch the cloud.
* ``push``   -- first deck: requires the successful sample receipt **and** the
  post-App ``approve2.json``.  Veteran deck: needs neither; an optional
  ``approve2.json`` only decides ``updates_ok`` (``diff_pending`` stays
  untouched unless the user explicitly approved the overwrite).  Re-reads the
  cloud first (downgrades stale ``create`` to ``skip_identical``), uploads each
  wav (idempotent), creates missing cards, skips ``diff_pending`` unless
  ``--allow-update`` and ``updates_ok=true``, reorders via ``move --order``
  (display order is ``card_ids``, never the ``cards`` array order), then
  verifies and writes ``markji.json`` + receipt.
* ``verify`` -- read-only re-check, writes the push receipt.

0.0.4 内容绑定: the plan records ``decision_sha256`` (doc/stem/deck/chapter/
cards/counts only) and ``cards_sha256`` (the E9 card-set identity).  approve2
carries both, the sample receipt carries both + the sampled card's
``content_sha256``, and push refuses a sample that was taken against an older
plan or an older ``cards.json``.  Only a card that is ``diff_pending`` **in the
plan** may be updated; a diff that appears after the plan is skipped + WARN.
``--limit`` writes the first N cards without touching the chapter order and
records ``partial=true`` / ``order_verified=false``.  A failure mid-batch still
writes ``receipts/markji-push.json`` with ``result=fail`` and every card id that
was already created (Markji has no delete API).

Safety: no delete API, no deck creation, no second create for an existing
title, no Key/token/url printing.  Rate limits: 429 / ``common_too_many_request``
waits 10 s and retries at most 6 times, requests are spaced by ``--interval``.

Exit codes: 0 written/verified / 1 read-back or verification mismatch / 2
dependency or usage problem (channel missing, Key dead, network) / 3 the user
or the plan has to decide something (deck ambiguity, missing approval, sample
is a ``diff_pending`` card, ...).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cards_apply  # noqa: E402  (cards_sha256: the E9 card-set identity)
import common  # noqa: E402

STAGE = "e10"
DEFAULT_DECK = "【CET-4】声音→意思"
PLAN_NAME = "markji-plan.json"
APPROVE_NAME = "approve2.json"
MARKJI_NAME = "markji.json"
SAMPLE_RECEIPT = "receipts/markji-sample.json"
PLAN_RECEIPT = "receipts/markji-plan.json"
PUSH_RECEIPT = "receipts/markji-push.json"
TMP_DIR = ".tmp-markji"


# --- 0.1.0 gate helpers -----------------------------------------------------


def resolve_deck_name(run: dict, explicit: str | None = None) -> str:
    """The deck this run writes into (D3: E1 stores the user's choice in run).

    Priority: ``--deck-name`` > ``run.options.deck_name`` >
    ``run.deck_name`` > ``DEFAULT_DECK``.
    """
    if explicit:
        return str(explicit)
    options = (run or {}).get("options")
    if isinstance(options, dict) and options.get("deck_name"):
        return str(options["deck_name"])
    if (run or {}).get("deck_name"):
        return str(run["deck_name"])
    return DEFAULT_DECK


def deck_card_count(deck: dict) -> int:
    """``deck.card_count`` as an int (missing/malformed -> 0)."""
    try:
        return int((deck or {}).get("card_count") or 0)
    except (TypeError, ValueError):
        return 0


def gate_mode(plan: dict) -> str:
    """``veteran`` / ``first`` from ``plan.gate`` (fallback: deck.card_count).

    ``veteran`` = the target deck already has cards (this workflow ran before),
    so the E10 sample gate is skipped; ``first`` = empty/missing deck, so one
    sample card is written and the user confirms it in the App.
    """
    gate = (plan or {}).get("gate")
    if isinstance(gate, dict) and gate.get("mode") in ("veteran", "first"):
        return str(gate["mode"])
    return "veteran" if deck_card_count((plan or {}).get("deck") or {}) > 0 else "first"


def plan_gate(deck: dict, doc_path: str | None = None) -> dict:
    """The ``gate`` block recorded in ``markji-plan.json``.

    0.1.0 D2: once a first-mode sample has been written but the batch push has
    not succeeded yet, re-running ``plan`` must keep the gate at ``first`` --
    even though the deck now has the sample card.  Otherwise a re-run would
    silently flip to ``veteran`` and let ``push`` skip the App confirmation.
    """
    count = deck_card_count(deck)
    if count > 0 and doc_path and first_sample_pending(doc_path):
        return {
            "mode": "first",
            "sample_required": True,
            "marker": "first_sample_pending",
        }
    mode = "veteran" if count > 0 else "first"
    return {
        "mode": mode,
        "sample_required": mode == "first",
        "marker": "deck_card_count>0" if mode == "veteran" else "empty_or_missing",
    }


def push_succeeded(doc_path: str) -> bool:
    """True when a successful batch push evidence exists (0.1.0)."""
    for name in (MARKJI_NAME, PUSH_RECEIPT):
        path = os.path.join(doc_path, name)
        if not os.path.isfile(path):
            continue
        try:
            data = common.read_json(path)
        except common.Failure:
            continue
        if isinstance(data, dict) and data.get("result") == "ok":
            return True
    return False


def first_sample_pending(doc_path: str) -> bool:
    """A first-mode sample is written but the batch push never succeeded.

    ``sample`` writes exactly one card; a re-run of ``plan`` then sees
    ``deck.card_count == 1`` and would otherwise classify the deck as
    ``veteran``.  The pending marker keeps the gate at ``first`` until a push
    succeeds, so ``push`` still needs ``approve2.json`` + the sample receipt.
    """
    if not doc_path or push_succeeded(doc_path):
        return False
    path = os.path.join(doc_path, SAMPLE_RECEIPT)
    if not os.path.isfile(path):
        return False
    try:
        receipt = common.read_json(path)
    except common.Failure:
        return False
    return (isinstance(receipt, dict)
            and receipt.get("mode") == "first"
            and receipt.get("result") == "ok")


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "markji_push.py",
        "E10/E11：墨墨 plan（只读三态）/ sample（1 张样卡）/ push（批量 + 重排 + 核对）/ verify。",
        epilog="first 缺样卡/approve2、或要覆盖但缺 updates_ok → exit 3；"
               "读回或核对不一致 → exit 1；通道/Key/网络问题 → exit 2。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--stage", required=True, choices=("plan", "sample", "push", "verify"),
                        help="plan=只读计划；sample=写 1 张样卡；push=批量；verify=只读核对")
    parser.add_argument("--deck-name", default=None,
                        help="牌组精确名称（默认取 run.options.deck_name → "
                             "run.deck_name → %s）" % DEFAULT_DECK)
    parser.add_argument("--chapter-name", default=None,
                        help="章节精确名称（默认取 cards.json 的 chapter）")
    parser.add_argument("--no", default=None,
                        help="样卡行号（first：默认第一张 create 卡；veteran 忽略）")
    parser.add_argument("--interval", type=float, default=0.6,
                        help="写请求间隔秒数（默认 0.6）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只处理/核对前 N 张（排障用，回执记 partial=true）")
    parser.add_argument("--allow-update", action="store_true",
                        help="允许对 diff_pending 的卡做整张 update（还需 approve2.updates_ok=true）")
    parser.add_argument("--mk", default=None,
                        help="markji_channel.py 路径（默认 $LISTENING_CARD_MK 或安装位置）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- local artifacts --------------------------------------------------------


def load_cards(doc_path: str) -> dict:
    path = os.path.join(doc_path, "cards.json")
    if not os.path.isfile(path):
        raise common.Failure(common.EXIT_USAGE,
                             "缺 cards.json：%s（先跑 E9：cards_build + cards_apply）" % path)
    data = common.read_json(path)
    cards = data.get("cards") or []
    if not cards:
        raise common.Failure(common.EXIT_FAIL, "cards.json 里没有卡片：%s" % path)
    if not data.get("chapter"):
        raise common.Failure(common.EXIT_FAIL, "cards.json 缺 chapter：%s" % path)
    for card in cards:
        content = card.get("content_template") or ""
        if content.count(common.CARD_AUDIO_PLACEHOLDER) != 2:
            raise common.Failure(common.EXIT_FAIL,
                                 "第 %s 张的音频占位不是 2 个：%s" % (card.get("no"), path))
    return data


def load_plan(doc_path: str) -> dict:
    path = os.path.join(doc_path, PLAN_NAME)
    if not os.path.isfile(path):
        raise common.Failure(common.EXIT_USAGE,
                             "缺 markji-plan.json：%s（先跑 --stage plan）" % path)
    plan = common.read_json(path)
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise common.Failure(common.EXIT_FAIL, "markji-plan.json 不是 version 1：%s" % path)
    plan["_path"] = path
    plan["_sha256"] = common.markji_plan_sha(plan)
    return plan


# --- 0.0.4 content binding -------------------------------------------------

#: The plan fields that make up the user decision; everything else
#: (generated_at / policy / warnings / plan_sha256 / *_sha256) is bookkeeping.
DECISION_KEYS = ("doc", "stem", "deck", "chapter", "cards", "counts")


def decision_payload(plan: dict) -> dict:
    """Only the decision-relevant plan fields (missing -> ``None``)."""
    return {key: plan.get(key) for key in DECISION_KEYS}


def decision_sha256(plan: dict) -> str:
    """Stable sha of ``decision_payload`` (E10 binds approve2/sample to it)."""
    return common.sha256_text(
        json.dumps(decision_payload(plan), ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))


def cards_sha256_of(cards_doc: dict) -> str:
    """Current card-set identity of a ``cards.json`` document."""
    return cards_apply.cards_sha256(cards_doc.get("cards") or [])


def require_approval(doc_path: str, plan: dict,
                     require_chapter_ok: bool = True) -> dict:
    """Load and validate ``approve2.json`` against the *current* plan.

    ``require_chapter_ok=False`` is used for a veteran deck's optional token:
    there the user is only confirming ``updates_ok`` (overwrite hand-edited
    cards); the sample/chapter gate was auto-passed.
    """
    path = os.path.join(doc_path, APPROVE_NAME)
    if not os.path.isfile(path):
        raise common.Failure(
            common.EXIT_ASK,
            "缺 %s：先跑 approve2.py（first 牌组：App 看过样卡后；"
            "veteran 牌组：只有覆盖 diff_pending 才需要）。" % path,
            reason="approval_missing",
        )
    approve = common.read_json(path)
    if approve.get("mode") != "user":
        raise common.Failure(common.EXIT_ASK,
                             "approve2.json 的 mode 不是 user：%r" % approve.get("mode"),
                             reason="approval_invalid")
    if approve.get("plan_sha256") != plan.get("_sha256"):
        raise common.Failure(
            common.EXIT_ASK,
            "approve2.json 绑定的 plan sha 与当前计划不一致（计划重算过？）。\n"
            "请重新出 plan、重新请示用户、再跑 approve2.py。",
            reason="plan_changed",
        )
    # 0.0.4: the approval must also match the *decision* inside the plan.
    if not plan.get("decision_sha256"):
        raise common.Failure(
            common.EXIT_ASK,
            "markji-plan.json 没有 decision_sha256（0.0.3 的旧计划）。\n"
            "请重跑 markji_push.py --stage plan，再跑 approve2.py。",
            reason="plan_stale",
        )
    if approve.get("decision_sha256") != plan.get("decision_sha256"):
        raise common.Failure(
            common.EXIT_ASK,
            "approve2.json 绑定的 decision_sha256 与当前计划不一致（计划被改过/重算过？）。\n"
            "请重跑 markji_push.py --stage plan，重新请示用户，再跑 approve2.py。",
            reason="plan_changed",
        )
    if approve.get("cards_sha256") != plan.get("cards_sha256"):
        raise common.Failure(
            common.EXIT_ASK,
            "approve2.json 绑定的 cards_sha256 与计划不一致（cards.json 在批准之后变过？）。\n"
            "请重跑 --stage plan + approve2.py。",
            reason="cards_changed",
        )
    mode = gate_mode(plan)
    sample_mode = approve.get("sample_mode")
    if sample_mode and sample_mode != mode:
        raise common.Failure(
            common.EXIT_ASK,
            "approve2.json 的 sample_mode=%r 与当前 plan 闸门 %r 不一致"
            "（计划重算过？重新 plan 后再批准）。" % (sample_mode, mode),
            reason="sample_mode_mismatch",
        )
    if require_chapter_ok and not approve.get("chapter_ok"):
        raise common.Failure(common.EXIT_ASK,
                             "approve2.json 的 chapter_ok 不是 true：用户还没确认章节。",
                             reason="chapter_not_approved")
    return approve


def _pending_sample_rebind_ok(plan: dict, receipt: dict) -> bool:
    """Allow exactly the pre-sample plan sha after a ``first_sample_pending`` re-plan.

    ``sample`` writes 1 card; a later ``plan`` necessarily changes (deck
    card_count 0->1, that card create->skip_identical, counts shift), so the
    plan sha and the earlier sample receipt's plan sha differ.  The sample is
    still the same user-visible card, so accept it only when the re-planned
    card is now ``skip_identical`` with the same cloud ``card_id`` and title as
    the receipt.  Anything else (different deck/title, cards.json changed ->
    the cards_sha check below) still fails.
    """
    number = str(receipt.get("no") or "")
    entry = next((item for item in plan.get("cards") or []
                  if str(item.get("no")) == number), None)
    if entry is None or entry.get("action") != "skip_identical":
        return False
    if str(entry.get("card_id") or "") != str(receipt.get("card_id") or ""):
        return False
    return str(entry.get("title") or "") == str(receipt.get("title") or "")


def load_sample_receipt(doc_path: str, plan: dict, cards_doc: dict) -> dict:
    """Read + validate ``receipts/markji-sample.json`` against plan/cards.

    Shared by ``push`` (first mode) and ``approve2.py`` (the App confirmation
    must be for a sample that really exists for *this* plan and card set).
    """
    path = os.path.join(doc_path, SAMPLE_RECEIPT)
    if not os.path.isfile(path):
        raise common.Failure(common.EXIT_ASK,
                             "缺样卡回执：%s（先跑 --stage sample 并请用户在 App 里看）"
                             % path, reason="sample_missing")
    receipt = common.read_json(path)
    if receipt.get("result") != "ok":
        raise common.Failure(common.EXIT_ASK,
                             "样卡回执不是 ok：%r（先处理样卡问题）" % receipt.get("result"),
                             reason="sample_failed")
    plan_sha = plan.get("_sha256") or common.markji_plan_sha(plan)
    pending_marker = ((plan.get("gate") or {}).get("marker")
                      == "first_sample_pending")
    if receipt.get("plan_sha256") != plan_sha and not (
            pending_marker and receipt.get("mode") == "first"
            and _pending_sample_rebind_ok(plan, receipt)):
        raise common.Failure(common.EXIT_ASK,
                             "样卡回执绑定的 plan sha 不是当前计划（计划重算过？先重做样卡）。",
                             reason="plan_changed")
    if not receipt.get("cards_sha256"):
        raise common.Failure(
            common.EXIT_ASK,
            "样卡回执是 0.0.3 的旧版（没有 cards_sha256），无法证明用户看过当前卡片集。\n"
            "请重跑 --stage sample。",
            reason="sample_stale",
        )
    current_cards_sha = cards_sha256_of(cards_doc)
    if receipt.get("cards_sha256") != current_cards_sha:
        raise common.Failure(
            common.EXIT_ASK,
            "样卡回执对应的卡片集与当前 cards.json 不一致（cards.json 在样卡之后变过？）。\n"
            "请重跑 --stage plan → --stage sample → approve2.py。",
            reason="sample_stale",
        )
    return receipt


def require_sample(doc_path: str, plan: dict, approve: dict, cards_doc: dict) -> dict:
    """The first-mode sample must prove the user looked at *these* cards.

    Bindings from disk: the plan (sha), the current card set (cards_sha256) and
    the sample card the user confirmed in the App (approve2.sample_no).  A
    re-run of any upstream script invalidates the sample instead of silently
    accepting a card the user never saw.
    """
    receipt = load_sample_receipt(doc_path, plan, cards_doc)
    if receipt.get("mode") != "first":
        raise common.Failure(
            common.EXIT_ASK,
            "样卡回执不是 first 模式（mode=%r）：当前是新/空牌组，必须先写 1 张样卡。"
            % receipt.get("mode"), reason="sample_mode")
    approved_no = str(approve.get("sample_no") or "")
    if not approved_no:
        raise common.Failure(common.EXIT_ASK,
                             "approve2.json 缺 sample_no：新牌组必须先写样卡、App 确认后再批准。",
                             reason="sample_missing")
    if str(receipt.get("no")) != approved_no:
        raise common.Failure(
            common.EXIT_ASK,
            "样卡回执是第 %s 张，但用户批准的是第 %s 张（approve2.sample_no）。"
            % (receipt.get("no"), approved_no),
            reason="sample_mismatch",
        )
    return receipt


def _receipt_path(doc_path: str, name: str) -> str:
    return os.path.join(doc_path, name)


def write_receipt(doc_path: str, name: str, payload: dict) -> str:
    path = _receipt_path(doc_path, name)
    common.ensure_dir(os.path.dirname(path))
    # A2 安全口径：带签名的 files[].url 永不落盘（只留 id/mime/size）。
    common.atomic_write_json(path, common.redact_urls(payload))
    return path


# --- cloud reads ------------------------------------------------------------


def find_deck(channel: common.MarkjiChannel, name: str) -> dict:
    matches = []
    offset = 0
    total = None
    while True:
        payload = channel.call("decks", "--limit", "100", "--offset", str(offset))
        decks = payload.get("decks") or []
        total = payload.get("total")
        for deck in decks:
            if deck.get("name") == name:
                matches.append(deck)
        offset += len(decks)
        if not decks or (isinstance(total, int) and offset >= total):
            break
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise common.Failure(
            common.EXIT_ASK,
            "牌组里找不到精确名 %r 的牌组（Key 可访问 %s 个）。\n"
            "牌组只能由用户在 App 里建/改名，本 Skill 不建牌组。" % (name, total),
            reason="deck_missing",
        )
    raise common.Failure(
        common.EXIT_ASK,
        "牌组名 %r 匹配到 %d 个（%s），不唯一。请让用户确认要写哪个（本 Skill 不改名）。"
        % (name, len(matches), "、".join("%s(%s)" % (item.get("name"), item.get("id"))
                                        for item in matches)),
        reason="deck_ambiguous",
    )


def list_chapters(channel: common.MarkjiChannel, deck_id: str) -> list:
    return channel.call("chapters", deck_id).get("chapters") or []


def find_chapter(channel: common.MarkjiChannel, deck_id: str, name: str):
    matches = [chapter for chapter in list_chapters(channel, deck_id)
               if chapter.get("name") == name]
    if not matches:
        return None
    if len(matches) > 1:
        raise common.Failure(
            common.EXIT_ASK,
            "章节名 %r 在牌组里匹配到 %d 个（%s），不唯一；请用户确认。"
            % (name, len(matches), "、".join(str(item.get("id")) for item in matches)),
            reason="chapter_ambiguous",
        )
    return matches[0]


def read_chapter_cards(channel: common.MarkjiChannel, deck_id: str, chapter: dict):
    """Return ``(ordered_cards, by_id)`` in display order (``card_ids``)."""
    payload = channel.call("cards", deck_id, chapter["id"])
    by_id = {card.get("id"): card for card in payload.get("cards") or [] if card.get("id")}
    ordered = []
    for card_id in chapter.get("card_ids") or []:
        if card_id not in by_id:
            by_id[card_id] = channel.call("card", deck_id, card_id)
        ordered.append(by_id[card_id])
    return ordered, by_id


def cloud_title(card: dict):
    """Title from the content's first line (real cloud cards have no ``title``).

    Falls back to a ``title`` field only if a channel version provides one, so
    both the real API and the fixtures work.  Returns ``None`` when the card
    has no usable content (caller records a flag and skips it).
    """
    title = common.card_title(str((card or {}).get("content") or ""))
    if title is None:
        fallback = (card or {}).get("title")
        title = str(fallback) if fallback else None
    return title


def cloud_cards_by_title(cards) -> tuple:
    """``([(title, card)], [unreadable card ids])`` in display order."""
    pairs = []
    unreadable = []
    for card in cards:
        title = cloud_title(card)
        if title is None:
            unreadable.append(str(card.get("id")))
        else:
            pairs.append((title, card))
    return pairs, unreadable


def preflight(channel: common.MarkjiChannel, log=None) -> dict:
    """§6.2: ``whoami`` first, so a dead Key fails before any plan/read work."""
    info = channel.call("whoami")
    if log:
        log("whoami ok: total_decks=%s"
            % (info.get("total_decks") if isinstance(info, dict) else "?"))
    return info if isinstance(info, dict) else {}


def unreadable_warnings(card_ids) -> list:
    return ["云端卡 %s 没有可解析的标题（content 第一行不是卡片标题），已跳过" % card_id
            for card_id in card_ids]


def chapter_fresh(channel: common.MarkjiChannel, deck_id: str, chapter_id: str) -> dict:
    for chapter in list_chapters(channel, deck_id):
        if chapter.get("id") == chapter_id:
            return chapter
    raise common.Failure(common.EXIT_FAIL,
                         "云端找不到章节 %s（刚被删了？）" % chapter_id)


# --- plan -------------------------------------------------------------------


def classify_card(local: dict, cloud: dict | None) -> dict:
    """A2-1 三态 for one local card against its cloud twin (or ``None``)."""
    entry = {
        "no": str(local.get("no")),
        "order": local.get("order"),
        "title": local.get("title"),
        "audio": local.get("audio"),
        "action": "create",
        "card_id": None,
        "file_id": None,
        "diff_summary": None,
        "byte_equal": False,
        "structure_warn": [],
    }
    if cloud is None:
        return entry
    entry["card_id"] = cloud.get("id")
    file_ids = [item.get("id") for item in cloud.get("files") or [] if item.get("id")]
    cloud_id = file_ids[0] if len(file_ids) == 1 else common.CARD_AUDIO_PLACEHOLDER
    expected = str(local.get("content_template") or "").replace(
        common.CARD_AUDIO_PLACEHOLDER, cloud_id)
    actual = str(cloud.get("content") or "")
    entry["file_id"] = file_ids[0] if len(file_ids) == 1 else None
    entry["byte_equal"] = expected == actual
    if common.normalize_card_content(expected) == common.normalize_card_content(actual):
        entry["action"] = "skip_identical"
        return entry
    entry["action"] = "diff_pending"
    expected_sections = common.card_content_sections(expected)
    actual_sections = common.card_content_sections(actual)
    if len(expected_sections) == 5 and len(actual_sections) == 5:
        if common.normalize_card_content(expected_sections[2]) != \
                common.normalize_card_content(actual_sections[2]):
            entry["diff_summary"] = "中文不同：" + common.content_preview(actual_sections[2])
        elif common.normalize_card_content(expected_sections[3]) != \
                common.normalize_card_content(actual_sections[3]):
            entry["diff_summary"] = "英文不同：" + common.content_preview(actual_sections[3])
        else:
            entry["diff_summary"] = "结构/音频不同：" + common.content_preview(actual)
    else:
        entry["diff_summary"] = "结构不同：" + common.content_preview(actual)
    if len(file_ids) != 1:
        entry["structure_warn"].append("云端音频挂载 %d 个（应为 1）" % len(file_ids))
    if str(actual).count("mkjf_") != 2:
        entry["structure_warn"].append("云端 content 里 mkjf_ 不是 2 处")
    return entry


def chapter_name_for(cards_doc: dict, override: str | None) -> str:
    name = override or cards_doc.get("chapter")
    if not name:
        raise common.Failure(common.EXIT_USAGE, "cards.json 没有 chapter，且没给 --chapter-name")
    return str(name)


def build_plan(channel: common.MarkjiChannel, cards_doc: dict, deck_name: str,
               chapter_name: str, interval: float, allow_update: bool,
               doc_path: str | None = None) -> dict:
    deck = find_deck(channel, deck_name)
    chapter = find_chapter(channel, deck["id"], chapter_name)
    cloud_by_title = {}
    warnings = []
    if chapter is not None:
        ordered, _ = read_chapter_cards(channel, deck["id"], chapter)
        pairs, unreadable = cloud_cards_by_title(ordered)
        warnings.extend(unreadable_warnings(unreadable))
        for title, card in pairs:
            if title in cloud_by_title:
                raise common.Failure(
                    common.EXIT_ASK,
                    "云端章节里标题 %r 出现两次（历史重复卡）。请用户在 App 里删掉多余的一张后再 plan；"
                    "本 Skill 不删除、也不猜哪张是对的。" % title,
                    reason="duplicate_title",
                )
            cloud_by_title[title] = card
    entries = [classify_card(card, cloud_by_title.get(card.get("title")))
               for card in cards_doc["cards"]]
    counts = {"cards": len(entries), "create": 0, "skip_identical": 0, "diff_pending": 0}
    for entry in entries:
        counts[entry["action"]] += 1
    deck_entry = {"id": deck.get("id"), "name": deck.get("name"),
                  "card_count": deck_card_count(deck)}
    gate = plan_gate(deck_entry, doc_path)
    plan = {
        "version": 1,
        "doc": None,
        "stem": cards_doc.get("stem"),
        "generated_at": common.now_iso(),
        "deck": deck_entry,
        "chapter": {
            "id": chapter.get("id") if chapter else None,
            "name": chapter_name,
            "action": "use" if chapter else "create",
        },
        "gate": gate,
        "cards": entries,
        "counts": counts,
        "cards_sha256": cards_sha256_of(cards_doc),
        "warnings": warnings,
        "policy": {
            "interval": interval,
            "max_retry": common.MARKJI_MAX_RETRY,
            "allow_update": bool(allow_update),
            "sample_no": None,
        },
    }
    plan["plan_sha256"] = common.markji_plan_sha(plan)
    return plan


def gate_line(plan: dict) -> str:
    """One ``E10 闸门`` line for ``plan`` (no card text, just the mode)."""
    gate = plan.get("gate") or {}
    mode = gate_mode(plan)
    if mode == "veteran":
        return "E10 闸门：老牌组（已有 %d 张卡）→ 跳过样卡，直接写新卡" \
            % deck_card_count(plan.get("deck") or {})
    if gate.get("marker") == "first_sample_pending":
        return ("E10 闸门：样卡已写、等待 App 确认 → 仍按新牌组流程"
                "（approve2 后才写余量；不因重跑 plan 变老牌组）")
    return "E10 闸门：新/空牌组 → 先写 1 张样卡，App 确认后写余量"


def plan_lines(plan: dict) -> list:
    counts = plan["counts"]
    lines = [
        "牌组：%s（%s，已有 %d 张卡）"
        % (plan["deck"]["name"], plan["deck"].get("id"),
           deck_card_count(plan.get("deck") or {})),
        "章节：%s（%s）" % (plan["chapter"]["name"],
                          "已存在" if plan["chapter"]["action"] == "use" else "将新建"),
        "卡片：%d 张 = create %d + skip_identical %d + diff_pending %d"
        % (counts["cards"], counts["create"], counts["skip_identical"], counts["diff_pending"]),
        gate_line(plan),
    ]
    diffs = [card for card in plan["cards"] if card["action"] == "diff_pending"]
    if diffs:
        lines.append("diff_pending（默认不动；用户确认后才 update）：")
        for card in diffs:
            lines.append("  %s  %s  %s" % (card["no"], card["title"], card["diff_summary"]))
    return lines


# --- sample -----------------------------------------------------------------


def choose_sample(plan: dict, requested: str | None) -> dict:
    cards = plan.get("cards") or []
    if requested is not None:
        number = common.fmt_row(int(str(requested).strip())) \
            if str(requested).strip().isdigit() else str(requested).strip()
        card = next((item for item in cards if str(item.get("no")) == number), None)
        if card is None:
            raise common.Failure(common.EXIT_ASK,
                                 "plan 里没有行号 %s 的卡" % number, reason="sample_not_in_plan")
        if card["action"] == "diff_pending":
            raise common.Failure(
                common.EXIT_ASK,
                "第 %s 张是 diff_pending（云端已被手改过），默认不拿它当样卡、也不覆盖它。\n"
                "请换一张 create 卡：--no <行号>；确实要覆盖这张请先让用户点名确认。" % number,
                reason="sample_diff_pending",
            )
        return card
    for action in ("create", "skip_identical"):
        for card in cards:
            if card["action"] == action:
                return card
    raise common.Failure(
        common.EXIT_ASK,
        "整个章节没有 create/skip_identical 的卡（全是 diff_pending）：先请用户决定要不要覆盖，"
        "不要拿手改卡当样卡。", reason="sample_diff_pending")


def _write_temp_content(doc_path: str, no: str, content: str) -> str:
    directory = os.path.join(doc_path, TMP_DIR)
    common.ensure_dir(directory)
    path = os.path.join(directory, "%s.txt" % no)
    common.atomic_write_text(path, content)
    return path


def _readback_ok(doc_path: str, channel, deck_id: str, card_id: str,
                 expected: str, file_id: str) -> tuple:
    card = channel.call("card", deck_id, card_id)
    actual = str(card.get("content") or "")
    file_ids = [item.get("id") for item in card.get("files") or [] if item.get("id")]
    normalized_ok = common.normalize_card_content(actual) == \
        common.normalize_card_content(expected)
    byte_equal = actual == expected
    audio_ok = file_ids == [file_id] and str(actual).count("mkjf_") == 2
    return {
        "card": card,
        "normalized_ok": normalized_ok,
        "byte_equal": byte_equal,
        "audio_ok": audio_ok,
        "readback_sha256": common.sha256_text(actual),
    }


def _sample_veteran(args, plan: dict, doc_path: str, cards_sha: str) -> int:
    """D2: auto-pass the sample gate for a deck that already has cards."""
    count = deck_card_count(plan.get("deck") or {})
    receipt = {
        "version": 1,
        "doc": args.doc,
        "mode": "veteran",
        "skipped": True,
        "result": "ok",
        "plan_sha256": plan["_sha256"],
        "decision_sha256": plan.get("decision_sha256"),
        "cards_sha256": cards_sha,
        "deck_card_count": count,
        "at": common.now_iso(),
        "agent": "markji_push",
    }
    path = write_receipt(doc_path, SAMPLE_RECEIPT, receipt)
    result = {"ok": True, "exit_code": common.EXIT_OK, "stage": STAGE,
              "doc": args.doc, "gate": plan.get("gate"),
              "sample": {"mode": "veteran", "skipped": True},
              "receipt": path}
    if not args.json:
        print("DOC=%s  样卡：老牌组（已有 %d 张卡）→ 跳过，不写云端" % (args.doc, count))
        print("回执：%s" % path)
    common.emit_json(result)
    return common.EXIT_OK


def stage_sample(args, run: dict, doc: dict, doc_path: str, channel,
                 cards_doc: dict) -> int:
    plan = load_plan(doc_path)
    cards_sha = cards_sha256_of(cards_doc)
    if plan.get("cards_sha256") != cards_sha:
        raise common.Failure(
            common.EXIT_ASK,
            "cards.json 在 plan 之后变过（plan.cards_sha256 对不上）：先重跑 --stage plan。",
            reason="cards_changed",
        )
    mode = gate_mode(plan)
    if mode == "veteran":
        # D2：老牌组已经走过这套流程，样卡闸门自动放行，不写云端。
        return _sample_veteran(args, plan, doc_path, cards_sha)

    # first：样卡就是给用户在 App 看的那一张，所以先写、后 approve2。
    card = choose_sample(plan, args.no)
    local = next((item for item in cards_doc["cards"]
                  if str(item.get("no")) == str(card.get("no"))), None)
    if local is None:
        raise common.Failure(common.EXIT_FAIL,
                             "plan 里的第 %s 张在 cards.json 里找不到" % card.get("no"))
    deck = find_deck(channel, plan["deck"]["name"])
    chapter = find_chapter(channel, deck["id"], plan["chapter"]["name"])
    chapter_created = False
    if chapter is None:
        chapter = channel.call("mkchapter", deck["id"], plan["chapter"]["name"],
                               retry=True)
        chapter_created = True
    receipt = {
        "version": 1,
        "doc": args.doc,
        "mode": "first",
        "plan_sha256": plan["_sha256"],
        "decision_sha256": plan.get("decision_sha256"),
        "cards_sha256": cards_sha,
        "no": card["no"],
        "title": card["title"],
        "action": card["action"],
        "chapter_id": chapter.get("id"),
        "chapter_created": chapter_created,
        "card_id": card.get("card_id"),
        "file_id": card.get("file_id"),
        # identity of the EXPECTED content (cards.json), not of the cloud bytes
        "content_sha256": str(local.get("content_sha256") or ""),
        "result": "fail",
        "at": common.now_iso(),
        "agent": "markji_push",
    }
    try:
        if card["action"] == "skip_identical":
            # 只读核对：章里已经有这张卡，不写云端。
            if not card.get("card_id"):
                raise common.Failure(common.EXIT_FAIL,
                                     "第 %s 张标成 skip_identical 但缺 card_id" % card["no"])
            expected = str(local["content_template"]).replace(
                common.CARD_AUDIO_PLACEHOLDER, card["file_id"] or "")
            check = _readback_ok(doc_path, channel, deck["id"], card["card_id"],
                                 expected, card["file_id"] or "")
            receipt.update({"readback_sha256": check["readback_sha256"],
                            "byte_equal": check["byte_equal"]})
            if check["normalized_ok"] and check["audio_ok"]:
                receipt["result"] = "ok"
            else:
                receipt["reason"] = "读回内容/音频与本地不一致"
                return _sample_done(args, doc_path, receipt, ok=False)
            return _sample_done(args, doc_path, receipt, ok=True)

        wav = _audio_path(run.get("work", ""), doc, local["audio"])
        uploaded = channel.call("upload", wav, retry=True)
        file_id = uploaded.get("id")
        if not file_id or not str(file_id).startswith("mkjf_"):
            raise common.Failure(
                common.EXIT_FAIL,
                "上传没有返回 mkjf_ file id：%r" % common.safe_file_meta(uploaded),
                reason="upload_failed")
        content = str(local["content_template"]).replace(
            common.CARD_AUDIO_PLACEHOLDER, file_id)
        if content.count("mkjf_") != 2:
            raise common.Failure(common.EXIT_FAIL,
                                 "替换音频 id 后 content 里 mkjf_ 不是 2 处（A2-3）",
                                 reason="audio_placeholder")
        content_path = _write_temp_content(doc_path, card["no"], content)
        order = None
        if chapter_created or not (chapter.get("card_ids") or []):
            order = max(0, int(local.get("order") or 1) - 1)
        created = channel.call("create", deck["id"], chapter["id"], "--file", content_path,
                               *(["--order", str(order)] if order is not None else []),
                               retry=True)
        channel.pause()
        card_id = created.get("id")
        if not card_id:
            raise common.Failure(common.EXIT_FAIL,
                                 "建卡没有返回 card id：%r" % created.get("id"),
                                 reason="create_failed")
        if not receipt.get("content_sha256"):
            receipt["content_sha256"] = common.sha256_text(content)
        receipt.update({"card_id": card_id, "file_id": file_id,
                        "written_content_sha256": common.sha256_text(content)})
        check = _readback_ok(doc_path, channel, deck["id"], card_id, content, file_id)
        receipt.update({"readback_sha256": check["readback_sha256"],
                        "byte_equal": check["byte_equal"]})
        if not check["normalized_ok"]:
            receipt["reason"] = "读回正文与本地模板不一致"
            return _sample_done(args, doc_path, receipt, ok=False)
        if not check["audio_ok"]:
            receipt["reason"] = "读回音频挂载不对（files 或 mkjf_ 数量）"
            return _sample_done(args, doc_path, receipt, ok=False)
        receipt["result"] = "ok"
        return _sample_done(args, doc_path, receipt, ok=True)
    except common.Failure:
        raise
    finally:
        tmp = os.path.join(doc_path, TMP_DIR, "%s.txt" % card["no"])
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _audio_path(work: str, doc: dict, name: str) -> str:
    pack_dir = doc.get("pack_dir")
    if pack_dir:
        candidate = os.path.join(common.resolve_work_path(work or "", str(pack_dir)), name)
        if os.path.isfile(candidate):
            return candidate
    raise common.Failure(common.EXIT_FAIL,
                         "找不到音频 %s（先确认产出包还在；cleanup 不会删这个包）" % name,
                         reason="audio_missing")


def _sample_done(args, doc_path: str, receipt: dict, ok: bool) -> int:
    path = write_receipt(doc_path, SAMPLE_RECEIPT, receipt)
    result = {"ok": ok, "exit_code": common.EXIT_OK if ok else common.EXIT_FAIL,
              "stage": STAGE, "doc": args.doc, "sample": {
                  "mode": receipt.get("mode"), "no": receipt["no"],
                  "title": receipt["title"],
                  "action": receipt["action"], "card_id": receipt.get("card_id"),
                  "file_id": receipt.get("file_id"), "result": receipt["result"]},
              "receipt": path}
    if not args.json:
        print("DOC=%s  样卡 %s %s" % (args.doc, receipt["no"], receipt["title"]))
        if receipt["result"] == "ok":
            print("结果：ok  card=%s  file=%s" % (receipt.get("card_id"),
                                                  receipt.get("file_id")))
        else:
            print("结果：FAIL  %s" % receipt.get("reason"))
        print("回执：%s" % path)
    common.emit_json(result)
    return common.EXIT_OK if ok else common.EXIT_FAIL


# --- push -------------------------------------------------------------------


def reorder(channel, deck_id: str, chapter_id: str, want_ids: list,
            rounds: int = 2) -> tuple:
    """Sort the chapter display order with ``move --order`` (max two rounds)."""
    moves = 0
    current = chapter_fresh(channel, deck_id, chapter_id).get("card_ids") or []
    for _ in range(rounds):
        mismatch = [index for index, card_id in enumerate(want_ids)
                    if index < len(current) and current[index] != card_id]
        if not mismatch:
            break
        for index in mismatch:
            if index >= len(current):
                break
            channel.call("move", deck_id, chapter_id, want_ids[index],
                         "--to", chapter_id, "--order", str(index), retry=True)
            channel.pause()
            moves += 1
        current = chapter_fresh(channel, deck_id, chapter_id).get("card_ids") or []
    verified = all(index < len(current) and current[index] == card_id
                   for index, card_id in enumerate(want_ids))
    return verified, moves


def verify_payload(plan: dict, cards_doc: dict, channel,
                   limit: int | None = None, updates_ok: bool = False,
                   extra_pending=()) -> dict:
    """Read the cloud and compare it with ``cards.json`` (A2-5 rules).

    ``extra_pending`` lists card numbers whose cloud copy changed *after* the
    plan; they were skipped on purpose (not overwritten), so they count as
    ``diff_pending`` + WARN instead of a content FAIL.
    """
    cards = list(cards_doc["cards"])
    if limit is not None:
        cards = cards[:max(0, int(limit))]
    expected_titles = [card["title"] for card in cards]
    plan_by_no = {str(item.get("no")): item for item in plan.get("cards") or []}
    payload = {
        "counts": {"cloud": 0, "expected": len(cards), "diff_pending": 0},
        "diffs": [],
        "warnings": [],
        "order_verified": False,
        "partial": limit is not None and int(limit) < len(cards_doc["cards"]),
        "limit": limit,
        "chapter_id": plan["chapter"].get("id"),
    }
    deck = find_deck(channel, plan["deck"]["name"])
    chapter = find_chapter(channel, deck["id"], plan["chapter"]["name"])
    if chapter is None:
        payload["diffs"].append({"kind": "chapter", "detail": "云端没有章节 %r"
                                 % plan["chapter"]["name"]})
        return payload
    payload["chapter_id"] = chapter["id"]
    ordered, by_id = read_chapter_cards(channel, deck["id"], chapter)
    payload["counts"]["cloud"] = len(ordered)
    pairs, unreadable = cloud_cards_by_title(ordered)
    payload["warnings"].extend(unreadable_warnings(unreadable))
    titles = [title for title, _ in pairs]
    seen = {}
    duplicates = []
    for title in titles:
        seen[title] = seen.get(title, 0) + 1
        if seen[title] == 2:
            duplicates.append(title)
    if duplicates:
        payload["diffs"].append({"kind": "duplicate", "detail": "云端重复标题：%s"
                                 % "、".join(duplicates[:5])})
    if limit is None and len(ordered) != len(cards):
        payload["diffs"].append({"kind": "count", "detail": "云端 %d 张 ≠ 期望 %d 张"
                                 % (len(ordered), len(cards))})
    cloud_by_title = {title: card for title, card in pairs}
    missing = [title for title in expected_titles if title not in cloud_by_title]
    extra = [title for title in titles if title not in set(expected_titles)]
    if missing:
        payload["diffs"].append({"kind": "missing", "detail": "云端缺 %d 张：%s"
                                 % (len(missing), "、".join(missing[:5]))})
    if extra and limit is None:
        payload["diffs"].append({"kind": "extra", "detail": "云端多出 %d 张：%s"
                                 % (len(extra), "、".join(extra[:5]))})
    positions = {title: index for index, title in enumerate(titles)}
    if limit is None:
        if titles[:len(expected_titles)] != expected_titles:
            first = next((index for index, (a, b) in enumerate(zip(titles, expected_titles))
                          if a != b), min(len(titles), len(expected_titles)))
            payload["diffs"].append({"kind": "order", "detail": "章节 card_ids 顺序不对（第 %d 位）"
                                     % (first + 1)})
    else:
        for index, title in enumerate(expected_titles):
            if title in positions and positions[title] != index:
                payload["diffs"].append({"kind": "order", "detail": "第 %s 张的位置是 %d（应为 %d）"
                                         % (title, positions[title], index)})
                break
    for card in cards:
        cloud = cloud_by_title.get(card["title"])
        if cloud is None:
            continue
        number = str(card.get("no"))
        action = (plan_by_no.get(number) or {}).get("action", "create")
        content = str(cloud.get("content") or "")
        file_ids = [item.get("id") for item in cloud.get("files") or [] if item.get("id")]
        cloud_id = file_ids[0] if len(file_ids) == 1 else common.CARD_AUDIO_PLACEHOLDER
        expected = str(card["content_template"]).replace(
            common.CARD_AUDIO_PLACEHOLDER, cloud_id)
        equal = common.normalize_card_content(expected) == \
            common.normalize_card_content(content)
        if not equal:
            if (action == "diff_pending" and not updates_ok) or number in extra_pending:
                payload["counts"]["diff_pending"] += 1
            else:
                payload["diffs"].append({
                    "kind": "content", "no": number, "title": card["title"],
                    "detail": "内容归一后不一致",
                })
        if len(file_ids) != 1:
            payload["diffs"].append({"kind": "audio", "no": number,
                                     "detail": "云端音频挂载 %d 个（应为 1）" % len(file_ids)})
        elif content.count("mkjf_") != 2:
            payload["diffs"].append({"kind": "structure", "no": number,
                                     "detail": "content 里 mkjf_ 不是 2 处"})
        payload.setdefault("cards", {})[number] = {
            "card_id": cloud.get("id"), "file_id": cloud_id,
            "content_sha256": common.sha256_text(content),
        }
    if payload["counts"]["diff_pending"]:
        payload["warnings"].append("diff_pending=%d" % payload["counts"]["diff_pending"])
    payload["order_verified"] = not any(
        diff["kind"] in ("order", "count", "missing", "extra", "duplicate", "chapter")
        for diff in payload["diffs"])
    return payload


def _write_failed_push_receipt(args, doc_path: str, plan: dict, cards_doc: dict,
                               cards_sha: str, state: dict, limit, partial: bool,
                               allow_update: bool, approve: dict, error) -> None:
    """Persist what was already written when a push dies mid-flight (0.0.4).

    Markji has no delete API, so a failed batch must not lose the ids it did
    create: the receipt is written with ``result=fail`` and the completed
    ``cards[]``, so a retry sees them as existing instead of duplicating them.
    Never mask the original exception (write failures are swallowed).
    """
    results = list(state.get("results") or [])
    payload = {
        "version": 1,
        "doc": args.doc,
        "mode": "push",
        "plan_sha256": plan.get("_sha256"),
        "decision_sha256": plan.get("decision_sha256"),
        "cards_sha256": cards_sha,
        "failed_at": common.now_iso(),
        "partial": partial,
        "limit": limit,
        "updates_ok": bool((approve or {}).get("updates_ok")),
        "allow_update": bool(allow_update),
        "order_verified": False,
        "moves": 0,
        "counts": {
            "cards": len(cards_doc.get("cards") or []),
            "create": len([item for item in results if item["action"] == "create"]),
            "skip_identical": len([item for item in results
                                   if item["action"] == "skip_identical"]),
            "diff_pending": len([item for item in results
                                 if item["action"] == "diff_pending"]),
            "written": int(state.get("written") or 0),
        },
        "cards": results,
        "skipped": list(state.get("skipped") or []),
        "sample": _sample_summary(doc_path),
        "error": str(error),
        "result": "fail",
    }
    try:
        write_receipt(doc_path, PUSH_RECEIPT, payload)
    except Exception:  # noqa: BLE001 - 回执写失败也不能盖掉原始错误
        pass


def _push_cards(run, doc, doc_path, channel, cards, plan_by_no, cloud_by_title,
                chapter, chapter_created, allow_update, deck, warnings, state):
    """Write the planned cards; return ``(results, skipped, written)`` (0.0.4).

    Only a card whose **plan** action is ``diff_pending`` may be updated: a card
    that became diff_pending *after* the plan+approval (cloud edited in between)
    is skipped with a WARN, never overwritten under a stale ``updates_ok``.
    ``state`` collects the partial results so a mid-flight failure can still
    persist the card ids that were already created (no delete API in Markji).
    """
    results = state["results"]
    skipped = state["skipped"]
    written = 0
    for local in cards:
        number = str(local.get("no"))
        plan_entry = plan_by_no.get(number) or {}
        plan_action = plan_entry.get("action")
        cloud = cloud_by_title.get(local["title"])
        # push 前重读云端：计划里的 create 现在已存在 → 降级，不重复建卡
        action = classify_card(local, cloud)["action"] if cloud is not None else "create"
        record = {
            "no": number, "title": local["title"], "audio": local.get("audio"),
            "plan_action": plan_action,
            "action": action, "card_id": cloud.get("id") if cloud else None,
            "file_id": None, "content_sha256": None,
        }
        if action == "skip_identical":
            file_ids = [item.get("id") for item in (cloud or {}).get("files") or []
                        if item.get("id")]
            record["file_id"] = file_ids[0] if len(file_ids) == 1 else None
            results.append(record)
            continue
        if action == "diff_pending":
            if plan_action != "diff_pending":
                warnings.append(
                    "第 %s 张：plan 之后云端出现差异（%s），本批不覆盖；"
                    "如要覆盖请重新 plan + approve2 --updates-ok"
                    % (number, local["title"]))
                state.setdefault("new_diff", []).append(number)
            if not (plan_action == "diff_pending" and allow_update):
                file_ids = [item.get("id") for item in (cloud or {}).get("files") or []
                            if item.get("id")]
                record["file_id"] = file_ids[0] if len(file_ids) == 1 else None
                skipped.append({
                    "no": number, "title": local["title"],
                    "card_id": cloud.get("id") if cloud else None,
                    "reason": "diff_pending 默认不覆盖（要覆盖需 --allow-update + "
                              "updates_ok，且计划里就是这张 diff）",
                })
                results.append(record)
                continue
        wav = _audio_path(run.get("work", ""), doc, local["audio"])
        uploaded = channel.call("upload", wav, retry=True)
        file_id = uploaded.get("id")
        if not file_id:
            raise common.Failure(common.EXIT_FAIL, "上传 %s 没有返回 id" % wav,
                                 reason="upload_failed")
        content = str(local["content_template"]).replace(
            common.CARD_AUDIO_PLACEHOLDER, file_id)
        if content.count("mkjf_") != 2:
            raise common.Failure(common.EXIT_FAIL,
                                 "第 %s 张替换音频 id 后 mkjf_ 不是 2 处（A2-3）" % number,
                                 reason="audio_placeholder")
        content_path = _write_temp_content(doc_path, number, content)
        try:
            if action == "create":
                order = max(0, int(local.get("order") or 1) - 1) \
                    if chapter_created or not (chapter.get("card_ids") or []) else None
                created = channel.call(
                    "create", deck["id"], chapter["id"], "--file", content_path,
                    *(["--order", str(order)] if order is not None else []), retry=True)
                record["card_id"] = created.get("id")
                if not record["card_id"]:
                    raise common.Failure(common.EXIT_FAIL,
                                         "建卡没有返回 id：%r" % created, reason="create_failed")
            else:
                channel.call("update", deck["id"], record["card_id"],
                             "--file", content_path, retry=True)
            # 先记账再 pause：pause/网络失败时已写卡不能被回执丢掉
            record["file_id"] = file_id
            record["content_sha256"] = common.sha256_text(content)
            results.append(record)
            written += 1
            state["written"] = written
            channel.pause()
        finally:
            if os.path.isfile(content_path):
                try:
                    os.remove(content_path)
                except OSError:
                    pass
    return results, skipped, written


def _optional_approval(doc_path: str, plan: dict) -> dict:
    """Veteran decks need no token; an existing one only carries ``updates_ok``."""
    path = os.path.join(doc_path, APPROVE_NAME)
    if not os.path.isfile(path):
        return {}
    return require_approval(doc_path, plan, require_chapter_ok=False)


def stage_push(args, run: dict, doc: dict, doc_path: str, channel,
               cards_doc: dict) -> int:
    plan = load_plan(doc_path)
    mode = gate_mode(plan)
    if mode == "first":
        approve = require_approval(doc_path, plan)
        require_sample(doc_path, plan, approve, cards_doc)
    else:
        # D2：老牌组自动放行 -- 没令牌也能写；有令牌时它只决定 updates_ok。
        approve = _optional_approval(doc_path, plan)
    allow_update = bool(args.allow_update and approve.get("updates_ok"))
    cards_sha = cards_sha256_of(cards_doc)
    cards = list(cards_doc["cards"])
    limit = args.limit
    if limit is not None:
        if limit < 1:
            raise common.Failure(common.EXIT_USAGE, "--limit 必须 ≥ 1")
        cards = cards[:limit]
    partial = limit is not None and int(limit) < len(cards_doc["cards"])
    deck = find_deck(channel, plan["deck"]["name"])
    if deck.get("id") != plan["deck"].get("id"):
        raise common.Failure(common.EXIT_ASK,
                             "牌组 id 与计划不一致（重建过？）：%s ≠ %s"
                             % (deck.get("id"), plan["deck"].get("id")),
                             reason="deck_changed")
    chapter = find_chapter(channel, deck["id"], plan["chapter"]["name"])
    chapter_created = False
    if chapter is None:
        if mode == "first" and not approve.get("chapter_ok"):
            raise common.Failure(common.EXIT_ASK, "章节将新建但 chapter_ok 不是 true",
                                 reason="chapter_not_approved")
        chapter = channel.call("mkchapter", deck["id"], plan["chapter"]["name"], retry=True)
        chapter_created = True
    ordered, _ = read_chapter_cards(channel, deck["id"], chapter)
    pairs, unreadable = cloud_cards_by_title(ordered)
    cloud_by_title = {}
    for title, cloud_card in pairs:
        cloud_by_title.setdefault(title, cloud_card)

    plan_by_no = {str(item.get("no")): item for item in plan.get("cards") or []}
    warnings = list(unreadable_warnings(unreadable))
    planned_pending = int((plan.get("counts") or {}).get("diff_pending") or 0)
    if mode == "veteran" and planned_pending and not (
            approve.get("updates_ok") and args.allow_update):
        warnings.append(
            "老牌组：%d 张 diff_pending 已跳过（覆盖需 approve2.py --veteran --updates-ok "
            "+ --allow-update）" % planned_pending)
    if partial:
        warnings.append(
            "partial：--limit 模式不重排，顺序未验证（后续全量 push 再验证）")
    state = {"results": [], "skipped": [], "written": 0}
    try:
        results, skipped, written = _push_cards(
            run, doc, doc_path, channel, cards, plan_by_no, cloud_by_title,
            chapter, chapter_created, allow_update, deck, warnings, state)
        fresh_unreadable = []
        if partial:
            # --limit 只是排障：只写前 N 张时不要动章节顺序，也不能声称验过
            order_verified, moves = False, 0
        else:
            # 重排：展示顺序只认章节 card_ids，不认 cards 数组顺序
            fresh = chapter_fresh(channel, deck["id"], chapter["id"])
            fresh_cards, _ = read_chapter_cards(channel, deck["id"], fresh)
            fresh_pairs, fresh_unreadable = cloud_cards_by_title(fresh_cards)
            title_to_id = {}
            for title, cloud_card in fresh_pairs:
                title_to_id.setdefault(title, cloud_card.get("id"))
            want_ids = [title_to_id[card["title"]] for card in cards
                        if card["title"] in title_to_id]
            order_verified, moves = reorder(
                channel, deck["id"], chapter["id"], want_ids)
        verify = verify_payload(plan, cards_doc, channel, limit=limit,
                                updates_ok=bool(approve.get("updates_ok")),
                                extra_pending=state.get("new_diff") or ())
    except Exception as exc:  # noqa: BLE001 - 写到一半也要留下已写卡的回执
        _write_failed_push_receipt(
            args, doc_path, plan, cards_doc, cards_sha, state, limit, partial,
            allow_update, approve, exc)
        raise
    sample_summary = _sample_summary(doc_path)
    if not sample_summary:
        sample_summary = ({"mode": "veteran", "skipped": True}
                          if mode == "veteran" else {})
    markji = {
        "version": 1,
        "doc": args.doc,
        "stem": cards_doc.get("stem"),
        "plan_sha256": plan["_sha256"],
        "decision_sha256": plan.get("decision_sha256"),
        "cards_sha256": cards_sha,
        "pushed_at": common.now_iso(),
        "deck": {"id": deck.get("id"), "name": deck.get("name")},
        "chapter": {"id": chapter.get("id"), "name": plan["chapter"]["name"],
                    "action": "created" if chapter_created else "use"},
        "counts": {
            "cards": len(cards),
            "create": len([item for item in results if item["action"] == "create"]),
            "skip_identical": len([item for item in results
                                   if item["action"] == "skip_identical"]),
            "diff_pending": len([item for item in results
                                 if item["action"] == "diff_pending"]),
            "written": written,
        },
        "updates_ok": bool(approve.get("updates_ok")),
        "allow_update": bool(args.allow_update),
        "sample_mode": mode,
        "sample": sample_summary,
        "partial": partial,
        "limit": limit,
        "order_verified": False if partial else bool(
            verify.get("order_verified") and order_verified),
        "moves": moves,
        "cards": results,
        "skipped": skipped,
        "diffs": verify.get("diffs") or [],
        "warnings": warnings + unreadable_warnings(fresh_unreadable),
        "verify": {"counts": verify.get("counts"),
                   "warnings": verify.get("warnings") or []},
        "result": "ok" if not (verify.get("diffs") or []) else "fail",
    }
    common.atomic_write_json(os.path.join(doc_path, MARKJI_NAME),
                             common.redact_urls(markji))
    receipt = dict(markji)
    receipt["sample"] = sample_summary
    write_receipt(doc_path, PUSH_RECEIPT, receipt)
    ok = markji["result"] == "ok"
    payload = dict(markji)
    payload.update({"stage": STAGE, "ok": ok,
                    "exit_code": common.EXIT_OK if ok else common.EXIT_FAIL})
    if not args.json:
        print("DOC=%s  写入完成：create %d / skip %d / diff_pending 跳过 %d（实际写 %d）"
              % (args.doc, markji["counts"]["create"], markji["counts"]["skip_identical"],
                 markji["counts"]["diff_pending"], written))
        print("章节：%s（%s）  重排：%s（%d 次 move）"
              % (markji["chapter"]["name"], markji["chapter"]["action"],
                 "ok" if markji["order_verified"] else "FAIL", moves))
        for warning in markji["warnings"]:
            print("WARN %s" % warning)
        for warning in markji["verify"]["warnings"]:
            print("WARN %s" % warning)
        for diff in markji["diffs"][:10]:
            print("DIFF %s %s" % (diff.get("kind"), diff.get("detail") or diff.get("title")))
        if not ok:
            print("结果：FAIL（回执 %s）" % _receipt_path(doc_path, PUSH_RECEIPT))
    common.emit_json(payload)
    return common.EXIT_OK if ok else common.EXIT_FAIL





def _sample_summary(doc_path: str) -> dict:
    path = os.path.join(doc_path, SAMPLE_RECEIPT)
    if not os.path.isfile(path):
        return {}
    receipt = common.read_json(path)
    summary = {"mode": receipt.get("mode") or "first",
               "skipped": bool(receipt.get("skipped"))}
    if not summary["skipped"]:
        summary.update({"no": receipt.get("no"), "card_id": receipt.get("card_id"),
                        "file_id": receipt.get("file_id"),
                        "cards_sha256": receipt.get("cards_sha256"),
                        "content_sha256": receipt.get("content_sha256"),
                        "readback_sha256": receipt.get("readback_sha256")})
    return summary


def stage_verify(args, run: dict, doc: dict, doc_path: str, channel,
                 cards_doc: dict) -> int:
    plan = load_plan(doc_path)
    approve = {"updates_ok": False}
    approve_path = os.path.join(doc_path, APPROVE_NAME)
    if os.path.isfile(approve_path):
        try:
            approve = common.read_json(approve_path)
        except common.Failure:
            approve = {"updates_ok": False}
    verify = verify_payload(plan, cards_doc, channel, limit=args.limit,
                            updates_ok=bool(approve.get("updates_ok")))
    markji_path = os.path.join(doc_path, MARKJI_NAME)
    markji = {}
    if os.path.isfile(markji_path):
        try:
            markji = common.read_json(markji_path)
        except common.Failure:
            markji = {}
    cards_sha = cards_sha256_of(cards_doc)
    order_verified = bool(verify.get("order_verified"))
    if verify.get("partial"):
        # --limit 核对只覆盖前 N 张：不能声称整章顺序已验证
        order_verified = False
        verify.setdefault("warnings", []).append(
            "partial：--limit 核对只覆盖前 N 张，顺序未验证")
    if markji:
        markji["order_verified"] = order_verified
        markji["cards_sha256"] = cards_sha
        markji["decision_sha256"] = plan.get("decision_sha256")
        markji["diffs"] = verify.get("diffs") or []
        markji["verify"] = {"counts": verify.get("counts"),
                            "warnings": verify.get("warnings") or []}
        markji["result"] = "ok" if not (verify.get("diffs") or []) else "fail"
        common.atomic_write_json(markji_path, markji)
    receipt = {
        "version": 1,
        "doc": args.doc,
        "mode": "verify",
        "plan_sha256": plan["_sha256"],
        "decision_sha256": plan.get("decision_sha256"),
        "cards_sha256": cards_sha,
        "verified_at": common.now_iso(),
        "partial": verify.get("partial"),
        "order_verified": order_verified,
        "counts": verify.get("counts"),
        "warnings": verify.get("warnings") or [],
        "diffs": verify.get("diffs") or [],
        "cards": verify.get("cards") or {},
        "sample": _sample_summary(doc_path),
        "result": "ok" if not (verify.get("diffs") or []) else "fail",
    }
    path = write_receipt(doc_path, PUSH_RECEIPT, receipt)
    ok = receipt["result"] == "ok"
    payload = {"ok": ok, "exit_code": common.EXIT_OK if ok else common.EXIT_FAIL,
               "stage": STAGE, "doc": args.doc, "counts": receipt["counts"],
               "warnings": receipt["warnings"], "diffs": receipt["diffs"],
               "order_verified": receipt["order_verified"], "receipt": path}
    if not args.json:
        print("DOC=%s  核对：云端 %s 张 / 期望 %s 张"
              % (args.doc, receipt["counts"].get("cloud"), receipt["counts"].get("expected")))
        print("顺序：%s" % ("ok" if receipt["order_verified"] else "FAIL"))
        for warning in receipt["warnings"]:
            print("WARN %s" % warning)
        for diff in receipt["diffs"][:10]:
            print("DIFF %s %s" % (diff.get("kind"), diff.get("detail") or diff.get("title")))
        print("回执：%s" % path)
    common.emit_json(payload)
    return common.EXIT_OK if ok else common.EXIT_FAIL


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
        cards_doc = load_cards(doc_path)
        chapter_name = chapter_name_for(cards_doc, args.chapter_name)
        channel = common.MarkjiChannel(args.mk, log=log, interval=args.interval)
        preflight(channel, log)
        if args.stage == "plan":
            deck_name = resolve_deck_name(run, args.deck_name)
            plan = build_plan(channel, cards_doc, deck_name, chapter_name,
                              args.interval, args.allow_update, doc_path=doc_path)
            plan["doc"] = args.doc
            plan["decision_sha256"] = decision_sha256(plan)
            plan["plan_sha256"] = common.markji_plan_sha(plan)
            plan_path = os.path.join(doc_path, PLAN_NAME)
            common.atomic_write_json(plan_path, common.redact_urls(plan))
            receipt = dict(plan)
            receipt["readonly"] = True
            receipt_path = write_receipt(doc_path, PLAN_RECEIPT, receipt)
            counts = plan["counts"]
            log("plan deck=%s card_count=%s gate=%s chapter=%r action=%s counts=%r sha=%s"
                % (plan["deck"]["name"], plan["deck"].get("card_count"),
                   (plan.get("gate") or {}).get("mode"), plan["chapter"]["name"],
                   plan["chapter"]["action"], counts, plan["plan_sha256"]))
            result.update({
                "ok": True, "exit_code": common.EXIT_OK,
                "deck": plan["deck"], "chapter": plan["chapter"],
                "gate": plan.get("gate"),
                "counts": counts, "plan_sha256": plan["plan_sha256"],
                "decision_sha256": plan["decision_sha256"],
                "cards_sha256": plan["cards_sha256"],
                "plan_path": plan_path, "receipt_path": receipt_path,
                "warnings": plan.get("warnings") or [],
                "diff_pending": [{"no": card["no"], "title": card["title"],
                                  "diff_summary": card["diff_summary"]}
                                 for card in plan["cards"]
                                 if card["action"] == "diff_pending"],
            })
            if not args.json:
                for line in plan_lines(plan):
                    print(line)
                for warning in plan.get("warnings") or []:
                    print("WARN %s" % warning)
                print("plan sha256：%s" % plan["plan_sha256"])
                print("decision sha256：%s" % plan["decision_sha256"])
                print("卡片集 sha256：%s" % plan["cards_sha256"])
                print("已写：%s" % plan_path)
            common.emit_json(result)
            return common.EXIT_OK

        if args.stage == "sample":
            return stage_sample(args, run, doc, doc_path, channel, cards_doc)
        if args.stage == "push":
            result.update({"ok": True, "exit_code": common.EXIT_OK})
            return stage_push(args, run, doc, doc_path, channel, cards_doc)
        return stage_verify(args, run, doc, doc_path, channel, cards_doc)
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
