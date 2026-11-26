#!/usr/bin/env python3
"""preflight_choices.py -- E1 choice gate: read-only materials + deck candidates.

0.1.0 (D3) puts one question gate in front of the pipeline: *which materials*
and *which deck*.  This script gathers the data for that question without
starting anything:

* ``docs`` reuses :func:`bootstrap.scan_materials` + :func:`bootstrap.pair_documents`
  (0.0.9 recursive ``材料区/`` scan, symlink/same-content folding), so every
  entry is a pairable "audio + transcript" with the same stable ``d01``.. id
  bootstrap would assign.  ``audio``/``text``/``existing_pack`` are
  workspace-relative paths; ``existing_pack`` is the 产出区 pack with the same
  stem (or ``null``) and is only inspected, never created.
* ``needs_user`` is the pairing-level "ask the user" list (missing transcript,
  several candidates, same-stem audio conflict...), same kinds and wording as
  bootstrap, plus ``no_audio``/``no_text`` when 材料区 is empty.
* ``decks`` lists the cloud decks (``id``/``name``/``card_count``/
  ``chapter_count``) so the root agent can pick 3-5 fitted ones for the user.
  A missing Key, dead network or absent markji-channel skill is **not** fatal:
  ``decks`` stays ``[]`` and ``deck_error`` carries one human sentence; the
  materials list is unaffected and phase one can still run with a hand-typed
  deck name.  ``--no-decks`` skips the cloud entirely (offline/tests).
* ``--json`` prints only the trailing ``JSON {...}`` line.

This script is read-only: it never creates ``$RUN``, never writes a file and
never modifies ``材料区/``.  Exit codes: 0 scan done (cloud failure and an empty
材料区 are still 0 -- ask the user) / 2 usage or workspace problem.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bootstrap  # noqa: E402
import common  # noqa: E402

STAGE = "e1"

#: How many cloud decks one call may list; E1 only needs the candidates.
DECK_LIMIT = 100


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "preflight_choices.py",
        "E1 选择闸门：只读列出材料区的「音频+原文」配对与云端牌组候选"
        "（不建 run、不写文件），供根 agent 组装提问。",
    )
    parser.add_argument("--work", default=None, help="工作区根（默认从 cwd 向上找）")
    parser.add_argument("--no-decks", action="store_true",
                        help="跳过云端牌组（离线/测试）：decks=[]、deck_error=null")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- cloud decks ------------------------------------------------------------


def _as_int(value) -> int:
    """Cloud counters as ints; a missing/odd field counts as 0 (never guesses)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


#: One human sentence per failure reason; the raw channel message follows as 明细.
_DECK_ERROR_HINTS = {
    "markji_auth": "墨墨 Key 失效或缺失，这次读不到云端牌组；材料选择不受影响，"
                   "牌组名让用户直接回复即可。",
    "markji_channel_missing": "找不到 markji_channel.py（markji-channel skill 没装），"
                              "这次读不到云端牌组；材料选择不受影响，牌组名让用户直接回复。",
    "markji_network": "连不上墨墨（网络错误），这次读不到云端牌组；"
                      "材料选择不受影响，牌组名让用户直接回复。",
    "markji_timeout": "墨墨通道超时，这次读不到云端牌组；"
                       "材料选择不受影响，牌组名让用户直接回复。",
    "rate_limited": "墨墨频控（429），这次没取到云端牌组；可稍后重跑本脚本，"
                    "或让用户直接回复牌组名。",
}


def _deck_error_text(failure) -> str:
    """One readable line: what happened + how the pipeline continues."""
    reason = str((failure.extra or {}).get("reason") or "")
    head = _DECK_ERROR_HINTS.get(reason)
    if not head:
        head = "墨墨通道报错（%s），这次读不到云端牌组；材料选择不受影响，牌组名让用户直接回复。" % (
            reason or "unknown")
    detail = common.norm_ws(str(failure.message))[:240]
    return "%s 明细：%s" % (head, detail) if detail else head


def deck_candidates(limit: int = DECK_LIMIT) -> tuple:
    """``(decks, deck_error)``: the cloud list never blocks the materials list."""
    try:
        channel = common.MarkjiChannel()
        payload = channel.call("decks", "--limit", str(limit))
    except common.Failure as failure:
        return [], _deck_error_text(failure)
    except OSError as exc:  # vanished channel script / unresolvable interpreter
        return [], ("读云端牌组失败（文件或进程错误：%s）；材料选择不受影响，"
                    "牌组名让用户直接回复。" % exc)
    raw = payload.get("decks") if isinstance(payload, dict) else None
    decks = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        decks.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "card_count": _as_int(item.get("card_count")),
            "chapter_count": _as_int(item.get("chapter_count")),
        })
    return decks, None


# --- main -------------------------------------------------------------------


def _doc_choices(work: str, docs) -> list:
    """The question-ready view of the paired docs (relative paths, read-only)."""
    out_base = os.path.join(work, "产出区")
    payload = []
    for doc in docs:
        pack = os.path.join(out_base, doc["stem"] + bootstrap.PACK_SUFFIX)
        payload.append({
            "id": doc["id"],
            "stem": doc["stem"],
            "audio": common.rel_to(work, doc["audio"]),
            "text": common.rel_to(work, doc["text"]),
            "existing_pack": common.rel_to(work, pack) if os.path.isdir(pack) else None,
        })
    return payload


def _print_summary(result: dict, *, skipped: bool = False) -> None:
    print("工作区：%s" % result["work"])
    print("材料：%d 对（%s） · 待用户 %d"
          % (len(result["docs"]),
             "、".join(doc["id"] for doc in result["docs"]) or "无",
             len(result["needs_user"])))
    for doc in result["docs"]:
        print("  文档：%s %s%s" % (doc["id"], doc["stem"],
                                  "（产出区已有同名包）" if doc["existing_pack"] else ""))
    for need in result["needs_user"]:
        print("  待办：%s" % need.get("message"))
    if result["deck_error"]:
        print("牌组：读不到云端 —— %s" % result["deck_error"])
    elif skipped:
        print("牌组：--no-decks，跳过云端（牌组名让用户自填精确名称）")
    elif not result["decks"]:
        print("牌组：云端没有可列出的牌组（用户可自填精确名称）")
    else:
        print("牌组：%d 个（按内容特征挑 3-5 个发给用户，末尾给「自己填」）"
              % len(result["decks"]))
        for deck in result["decks"]:
            print("  %s（已有 %d 张卡 / %d 章）id=%s"
                  % (deck["name"], deck["card_count"], deck["chapter_count"],
                     deck["id"]))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        work = common.resolve_work(args.work)
        scanned = bootstrap.scan_materials(work)
        audios, texts = scanned["audio"], scanned["text"]
        docs, needs = bootstrap.pair_documents(audios, texts, work)
        needs.extend(bootstrap.materials_presence_needs(audios, texts))
        if args.no_decks:
            decks, deck_error = [], None
        else:
            decks, deck_error = deck_candidates()
        result = {
            "ok": True,
            "exit_code": common.EXIT_OK,
            "stage": STAGE,
            "work": work,
            "docs": _doc_choices(work, docs),
            "needs_user": needs,
            "decks": decks,
            "deck_error": deck_error,
        }
        if not args.json:
            _print_summary(result, skipped=args.no_decks)
        common.emit_json(result)
        return common.EXIT_OK
    except common.Failure as failure:
        return common.report_failure(STAGE, failure)
    except OSError as exc:
        return common.report_failure(
            STAGE,
            common.Failure(common.EXIT_FAIL, "扫描材料区失败：%s" % exc, reason="os_error"),
        )


if __name__ == "__main__":
    sys.exit(main())
