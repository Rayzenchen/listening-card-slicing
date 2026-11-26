#!/usr/bin/env python3
"""gate.py -- the E1-E12 checkpoint CLI.

Every gate recomputes its verdict from the files on disk (see ``gates.py``);
this module only owns the stdout contract:

    check <label>: ok
    check <label>: WARN <原因>
    check <label>: FAIL <原因>
    PASS                     # last line on success
    FAIL: <摘要>              # last line on failure

WARN never fails a gate.  With ``--json`` the per-check human lines are
replaced by one ``JSON {...}`` line that is printed *before* the verdict, so
the verdict stays the last line (接口约定 §3.4).

Usage: ``gate.py <e1…e12|phase1> --run RUN [--doc d01] [--json]``.
``e1``/``e8`` are global; ``e2``-``e7`` and ``e9``-``e12`` require
``--doc``.  ``phase1`` is the stage-one aggregate (E1, then per-document
E2-E7, stopping at the first FAIL).  ``e5`` is the translation/approval gate
(humans finalise the 6-column manifest before any audio is cut); ``e6`` is
the cut gate (re-reads ``cut-report.json`` and checks it is bound to the
current ``切分文本.txt``/``manifest-5col.md``); ``e7`` is the pack gate; ``e8``
re-checks E1 + E2–E7 + cleanup/report.  ``e9`` is local-only;
``e10``/``e11``/``e12`` read the cloud through ``markji_channel.py``
(read-only; offline → FAIL, which is not a bug).

Every verdict is also written to ``receipts/gate-<stage>.json`` so
``report.py``/``status.py`` can read it instead of recomputing.

Exit codes: 0 PASS / 1 FAIL / 2 usage or bad arguments.
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

import common  # noqa: E402
import gates  # noqa: E402

STAGE = "gate"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "gate.py",
        "从磁盘重算 E1–E12 关卡：每条检查一行，最后一行 PASS 或 FAIL。",
        epilog="e1/e8 是全局关卡；e2–e7、e9–e12 需要 --doc。\n"
               "e5 是译文/6 列/定稿关卡；E6 才是切分（没 approve 不切），E7 分包。\n"
               "--json 时在 PASS/FAIL 之前打一行 JSON。\n"
               "退出码：0 PASS / 1 FAIL / 2 用法错误（3 需要用户决定，本脚本保留不用）。",
        append_exit_help=False,
    )
    parser.add_argument("stage",
                        help="关卡名 e1–e12 或 phase1（e1/e8/phase1 全局，其余需 --doc）")
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", default=None,
                        help="文档 id（e2–e7 / e9–e12 必填；e1、e8 是全局关卡，不带 --doc）")
    parser.add_argument("--json", action="store_true",
                        help="用一行 JSON 代替逐条检查行（PASS/FAIL 仍是最后一行）")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stage not in gates.ALL_STAGES:
        parser.error("未知关卡：%r（可选：%s）"
                     % (args.stage, "、".join(gates.ALL_STAGES)))
    if args.stage in gates.DOC_STAGES + gates.PHASE2_STAGES and not args.doc:
        parser.error("%s 关卡需要 --doc" % args.stage)
    run_dir = os.path.abspath(args.run)
    log = None
    if os.path.isdir(run_dir):
        log = common.StageLog(run_dir, STAGE, args.doc or args.stage)
    checker = gates.run_stage(args.stage, run_dir, args.doc)
    try:
        if not args.json:
            for line in checker.lines():
                print(line)
        else:
            print("JSON " + json.dumps(checker.payload(), ensure_ascii=False,
                                       separators=(",", ":")))
        verdict = checker.verdict()
        print(verdict)
        try:
            gates.write_receipt(run_dir, args.stage, args.doc, checker)
        except OSError as exc:  # the verdict is still valid without its receipt
            print("WARN 关卡回执写入失败：%s" % exc, file=sys.stderr)
        if log:
            log("stage=%s doc=%s" % (args.stage, args.doc or "-"))
            for line in checker.lines():
                log(line)
            log(verdict)
        return common.EXIT_OK if checker.passed else common.EXIT_FAIL
    finally:
        if log:
            try:
                log.flush()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
