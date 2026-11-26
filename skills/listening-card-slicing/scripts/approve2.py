#!/usr/bin/env python3
"""approve2.py -- E10: the user's confirmation token (App sample / overwrite).

0.1.0 D2: the token is written **after** the sample stage, and its meaning
depends on the plan's gate mode:

* ``first`` (new/empty deck) -- the user has seen the single sample card in the
  App.  ``--sample-no`` and ``--chapter-ok`` are required.  The sample receipt
  must exist, be ``mode=first`` / ``result=ok`` and bind the same plan, card
  set and card number.  ``push`` refuses to run without this token.
* ``veteran`` (the deck already has cards) -- the sample gate auto-passes, so
  no token is needed.  ``--veteran --updates-ok`` is only the explicit
  permission to overwrite the ``diff_pending`` (hand-edited) cards; without
  ``--updates-ok`` they are skipped by default.

The token still binds ``plan_sha256`` / ``decision_sha256`` / ``cards_sha256``
(0.0.4), so re-running ``plan`` invalidates it, and ``sample_mode`` records
which gate produced it.  The root agent never hand-computes the sha: it always
calls this script.

Exit codes: 0 token written / 1 the plan looks tampered with / 2 usage, missing
required flags or no applicable ``--updates-ok`` / 3 the plan/sample state does
not allow this token yet (sample missing/stale/for another card, ...).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402
import markji_push  # noqa: E402  (gate mode / decision sha / sample helpers)

STAGE = "e10"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "approve2.py",
        "E10：写 approve2.json（first=App 看过样卡后的确认；veteran=仅授权覆盖差异卡）。",
        epilog="plan 不存在或解析不了 → exit 2；plan 内容与自身记录的 sha 不符 → exit 1；"
               "样卡回执缺失/过期/不是这张 → exit 3。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--mode", choices=("user",), default="user",
                        help="批准方式（目前只有 user）")
    parser.add_argument("--sample-no", default=None,
                        help="样卡行号（3 位，例如 005）；first 必填，veteran 可省略")
    parser.add_argument("--chapter-ok", action="store_true",
                        help="用户认可「章节存在/将新建」；first 必填")
    parser.add_argument("--updates-ok", action="store_true",
                        help="用户同意覆盖 diff_pending 的已有卡（默认 false）")
    parser.add_argument("--veteran", action="store_true",
                        help="老牌组：不绑样卡，只用于 --updates-ok（first 计划不能加）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


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
        plan_path = os.path.join(doc_path, "markji-plan.json")
        if not os.path.isfile(plan_path):
            raise common.Failure(common.EXIT_USAGE,
                                 "缺 markji-plan.json：%s（先跑 markji_push.py --stage plan）"
                                 % plan_path)
        plan = common.read_json(plan_path)
        if not isinstance(plan, dict) or plan.get("version") != 1:
            raise common.Failure(common.EXIT_FAIL, "markji-plan.json 不是 version 1：%s" % plan_path)
        sha = common.markji_plan_sha(plan)
        recorded = plan.get("plan_sha256")
        if recorded and recorded != sha:
            raise common.Failure(
                common.EXIT_FAIL,
                "markji-plan.json 记录的 plan_sha256 与内容不符（计划被手改过？）：%s\n"
                "请重跑 markji_push.py --stage plan 后再批准。" % plan_path,
                reason="plan_changed",
            )
        if not plan.get("decision_sha256") or not plan.get("cards_sha256"):
            raise common.Failure(
                common.EXIT_USAGE,
                "markji-plan.json 缺 decision_sha256 / cards_sha256（0.0.3 的旧计划）：%s\n"
                "请重跑 markji_push.py --stage plan，再让用户确认。" % plan_path,
                reason="plan_stale",
            )
        if plan.get("decision_sha256") != markji_push.decision_sha256(plan):
            raise common.Failure(
                common.EXIT_FAIL,
                "markji-plan.json 的 decision_sha256 与计划内容不符（被手改过？）：%s"
                % plan_path,
                reason="plan_changed",
            )
        mode = markji_push.gate_mode(plan)
        if args.veteran and mode != "veteran":
            raise common.Failure(
                common.EXIT_USAGE,
                "当前计划是新/空牌组（gate.mode=first）：先写 1 张样卡、"
                "请用户在 App 看过，再 approve2.py --sample-no <行号> --chapter-ok。\n"
                "不能加 --veteran 跳过样卡闸门。",
                reason="gate_mismatch",
            )
        counts = plan.get("counts") or {}
        diff_pending = int(counts.get("diff_pending") or 0)
        if args.updates_ok and diff_pending <= 0:
            raise common.Failure(
                common.EXIT_USAGE,
                "计划里没有 diff_pending（0 张），不需要、也不接受 --updates-ok：\n"
                "updates_ok 只在用户真正看过「要覆盖哪些卡」之后才成立。\n"
                "如果是要给 E12 markji_sync --update 授权，请先重跑 --stage plan，"
                "让云端差异出现在 diff_pending 里，再 approve2 --updates-ok。",
                reason="updates_not_applicable",
            )
        plan["_sha256"] = sha
        sample_no = None
        sample_action = None
        cards_doc = None
        if mode == "first":
            if not args.sample_no:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "first（新/空牌组）必须给 --sample-no：App 里看的是哪一张样卡。",
                    reason="sample_no_required",
                )
            if not args.chapter_ok:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "first（新/空牌组）必须给 --chapter-ok：用户确认章节对吗。",
                    reason="chapter_ok_required",
                )
            raw_no = str(args.sample_no).strip()
            if not raw_no.isdigit():
                raise common.Failure(
                    common.EXIT_USAGE,
                    "--sample-no 必须是行号（例如 005），收到 %r。" % args.sample_no,
                    reason="sample_no_invalid",
                )
            sample_no = common.fmt_row(int(raw_no))
            cards = plan.get("cards") or []
            card = next((item for item in cards
                         if str(item.get("no")) == sample_no), None)
            if card is None:
                raise common.Failure(
                    common.EXIT_ASK,
                    "plan 里没有行号 %s 的卡（共 %d 张）。请用 plan 里真实存在的行号，"
                    "例如 %s。" % (sample_no, len(cards),
                                   (cards[0].get("no") if cards else "005")),
                    reason="sample_not_in_plan",
                )
            cards_doc = markji_push.load_cards(doc_path)
            receipt = markji_push.load_sample_receipt(doc_path, plan, cards_doc)
            if receipt.get("mode") != "first":
                raise common.Failure(
                    common.EXIT_ASK,
                    "样卡回执不是 first 模式（mode=%r）：请先写 first 样卡。"
                    % receipt.get("mode"), reason="sample_mode")
            if str(receipt.get("no")) != sample_no:
                raise common.Failure(
                    common.EXIT_ASK,
                    "App 里看的是第 %s 张样卡，但 --sample-no 是 %s；"
                    "请用样卡回执里的行号，或重写样卡。"
                    % (receipt.get("no"), sample_no), reason="sample_mismatch")
            sample_action = card.get("action")
        approve = {
            "version": 1,
            "doc": args.doc,
            "mode": "user",
            "sample_mode": mode,
            "plan_sha256": sha,
            "decision_sha256": plan["decision_sha256"],
            "cards_sha256": plan["cards_sha256"],
            "sample_no": sample_no,
            "chapter_ok": bool(args.chapter_ok),
            "updates_ok": bool(args.updates_ok),
            "approved_at": common.now_iso(),
            "by": "user",
        }
        approve_path = os.path.join(doc_path, "approve2.json")
        common.atomic_write_json(approve_path, approve)
        log("approved plan %s mode=%s sample_no=%s chapter_ok=%s updates_ok=%s"
            % (sha, mode, sample_no, approve["chapter_ok"], approve["updates_ok"]))
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "sample_mode": mode,
            "plan_sha256": sha,
            "decision_sha256": approve["decision_sha256"],
            "cards_sha256": approve["cards_sha256"],
            "sample_no": sample_no,
            "chapter_ok": approve["chapter_ok"],
            "updates_ok": approve["updates_ok"],
            "sample_action": sample_action,
            "approve_path": approve_path,
        })
        if not args.json:
            print("DOC=%s  已写批准令牌（%s）" % (
                args.doc, "first：App 已看样卡" if mode == "first"
                else "veteran：老牌组，无需样卡"))
            print("plan sha256：%s" % sha)
            print("decision sha256：%s" % approve["decision_sha256"])
            print("卡片集 sha256：%s" % approve["cards_sha256"])
            if mode == "first":
                print("样卡：%s（plan 动作 %s）" % (sample_no, sample_action))
            else:
                print("样卡：跳过（老牌组自动放行）")
            print("章节确认：%s  覆盖差异：%s"
                  % ("是" if approve["chapter_ok"] else "否",
                     "是" if approve["updates_ok"] else "否（默认）"))
            print("已写：%s" % approve_path)
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
