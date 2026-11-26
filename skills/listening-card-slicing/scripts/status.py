#!/usr/bin/env python3
"""status.py -- read-only "where am I" for one $RUN: one step at a time.

0.0.8 order: E1 → E2 → E3 → E4 → E5（译文 + 6 列 + approve）→ E6 切分 →
E7 分包 → E8 收尾；阶段二 E9 → E10 → E11 → E12（不再有 skip e8 开关）。

This is the breakpoint helper (0.0.4): it reads ``run.json``, ``doc.json``,
the artifacts and the gate receipts already on disk and prints

    DONE=e1,e2,e3,...        # steps whose evidence is present
    WARN=...                 # hints that need a human look (never blocking)
    NEXT=<exactly one step>  # the next command/action to run

It never prints PASS/FAIL, never writes a file and never talks to the cloud.
Missing artifacts are a normal state (that is what NEXT is for), so the
exit code is 0 whenever ``run.json`` is readable; an unreadable $RUN still
returns 0 with NEXT pointing back at bootstrap (a status probe must never
become the thing that blocks the run).  ``--json`` keeps the machine result
as the last stdout line.

Exit codes: 0 always (informational) / 2 usage error from argparse.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402
import gates  # noqa: E402

STAGE = "status"

# --- 0.0.10 (D3): E6/E7 serial lock + live cutter log -----------------------

LOCK_RELATIVE = os.path.join(".locks", "e6-e7.lock")
CUT_LOG_NAME = "cut-cutter.log"
CUT_LOG_LIMIT = 200


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "status.py",
        "只读扫描 $RUN 的 run/doc/回执，输出 NEXT=下一步、DONE=已完成、WARN=提示；不做判定。",
        epilog="只读、不写文件、不联网；NEXT 每次只给一步。\n--json 只输出末行 JSON。"
               "\n退出码：0 只读成功（NEXT 指向任何一步）/ 2 用法错误",
        append_exit_help=False,
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


def _load_json(path: str):
    try:
        return common.read_json(path)
    except (common.Failure, OSError):
        return None


def _sha(path: str):
    try:
        return common.sha256_file(path) if os.path.isfile(path) else None
    except OSError:
        return None


def _numbered_receipts(doc_dir: str, pattern: str) -> list:
    found = []
    try:
        names = sorted(os.listdir(doc_dir))
    except OSError:
        return found
    for name in names:
        if re.match(pattern, name):
            found.append(os.path.join(doc_dir, name))
    return found


def _pid_alive(pid) -> bool:
    """Whether ``pid`` names a live process (``os.kill(pid, 0)``; never raises)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by another user
        return True
    except OSError:
        return False
    return True


def _elapsed_sec(data: dict):
    started_ts = data.get("started_ts")
    if isinstance(started_ts, (int, float)) and not isinstance(started_ts, bool):
        return max(0, int(time.time() - float(started_ts)))
    started_at = data.get("started_at")
    if isinstance(started_at, str):
        try:
            return max(0, int(time.time()
                               - datetime.fromisoformat(started_at).timestamp()))
        except ValueError:
            return None
    return None


def _cut_lock_state(run_dir: str):
    """``(info, warning)`` for the E6/E7 lock; ``(None, None)`` when absent.

    The lock is written by ``cut_stage.py`` (0.0.10).  It is never deleted
    here: a live pid means a cut/pack is really running, a dead pid means a
    stale file a human should look at.  Read-only helpers, like the rest of
    status.py.
    """
    path = os.path.join(run_dir, LOCK_RELATIVE)
    if not os.path.isfile(path):
        return None, None
    data = _load_json(path)
    if not isinstance(data, dict):
        data = {}
    doc = data.get("doc")
    doc = doc if isinstance(doc, str) and doc else None
    info = {
        "running": _pid_alive(data.get("pid")),
        "stale": False,
        "pid": data.get("pid"),
        "doc": doc,
        "stage": data.get("stage"),
        "started_at": data.get("started_at"),
        "elapsed_sec": _elapsed_sec(data),
        "log": (os.path.join(common.doc_dir(run_dir, doc), CUT_LOG_NAME)
                if doc else None),
        "path": path,
    }
    info["stale"] = not info["running"]
    if info["running"]:
        return info, None
    return info, ("stale lock：%s 的 pid=%r 已不在（E6/E7 没在跑）；"
                  "不自动删，确认没有其它 cut/pack 后再手动删掉重试"
                  % (path, data.get("pid")))


def _latest_cut_log_line(run_dir: str, docs) -> str | None:
    """Last line of the newest ``docs/<doc>/cut-cutter.log`` (<=200 chars)."""
    newest = None
    for doc_id in docs or []:
        path = os.path.join(common.doc_dir(run_dir, doc_id), CUT_LOG_NAME)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    if newest is None:
        return None
    try:
        with open(newest[1], encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.read().splitlines()
                     if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    line = lines[-1]
    return line[:CUT_LOG_LIMIT] + ("…" if len(line) > CUT_LOG_LIMIT else "")


def _doc_state(run_dir: str, work: str, doc_id: str) -> dict:
    """Collect artifacts/receipts for one document (never raises)."""
    state = {"id": doc_id, "doc": None, "doc_dir": common.doc_dir(run_dir, doc_id),
             "done": [], "warn": [], "next": None}
    doc = _load_json(os.path.join(state["doc_dir"], "doc.json"))
    if not isinstance(doc, dict):
        state["next"] = "bootstrap.py --work %s（doc.json 缺失，回 E1 重建）" % work
        return state
    state["doc"] = doc
    if not all(doc.get(key) for key in ("audio", "text", "stem", "result_dir",
                                        "pack_dir", "manifest")):
        state["next"] = "bootstrap.py --work %s（doc.json 缺字段，回 E1 重建）" % work
        return state
    state["done"].append("e1")
    return state


def _stage_one(state: dict, run_dir: str, work: str,
               cut_running: bool = False) -> None:
    """Fill DONE/NEXT for E1-E8 (stage one); stops at the first gap.

    0.0.8 order: E5 译文 + 6 列清单 + approve ★ → E6 切分 → E7 分包 →
    E8 cleanup/phase1/report.  Somebody else (cut_stage) enforces the gates;
    this helper only tells the operator the next command.

    0.0.10 (D3 stage 2): while the E6/E7 lock names this doc, an **old**
    ``cut-report.json`` is ignored for NEXT/WARN purposes — a re-cut leaves the
    previous (often ``needs_review``) report on disk until the new one lands,
    and reporting "停下问用户" during a healthy running cut is misleading.  The
    lock being released restores the normal "read the report" behaviour.
    """
    doc = state["doc"]
    doc_dir = state["doc_dir"]
    text = os.path.abspath(doc["text"])
    done = state["done"]

    def stop(step: str) -> None:
        state["next"] = step

    # E2 / E3: the parse + dispatcher receipts.
    blocks = _load_json(os.path.join(doc_dir, "blocks.json"))
    dispatch = _load_json(os.path.join(doc_dir, "dispatch.json"))
    if not isinstance(blocks, dict):
        stop("parse_source.py --run %s --doc %s（E2）" % (run_dir, state["id"]))
        return
    if blocks.get("source_sha256") != _sha(text):
        state["warn"].append("%s 原文已变：blocks 绑定的 sha 与当前不符" % state["id"])
        stop("parse_source.py --run %s --doc %s（原文变了，重跑 E2）"
             % (run_dir, state["id"]))
        return
    if not isinstance(dispatch, dict):
        stop("parse_source.py --run %s --doc %s（E2 还会写 dispatch）"
             % (run_dir, state["id"]))
        return
    done.append("e2")
    missing = [task.get("block") for task in dispatch.get("tasks") or []
               if not os.path.isfile(str(task.get("receipt") or ""))]
    if missing:
        stop("派裁定员（E3）：补 %d 个 blocks/<block>.json" % len(missing))
        return
    done.append("e3")

    # E4: segments/cut-text/manifest + reviewer receipt.
    segments = _load_json(os.path.join(doc_dir, "segments.json"))
    cut_text = os.path.join(doc_dir, "切分文本.txt")
    reviews = _numbered_receipts(doc_dir, r"^review-(\d+)\.json$")
    if (not isinstance(segments, dict) or not os.path.isfile(cut_text)
            or not segments.get("manifest_sha256")):
        stop("assemble.py --run %s --doc %s（E4）" % (run_dir, state["id"]))
        return
    if not reviews:
        stop("派复核员 review-1.json，再按需 assemble.py（E4）")
        return
    e4 = gates.read_receipt(run_dir, "e4", state["id"])
    if e4 is not None and not e4.get("pass"):
        stop("gate.py e4 --run %s --doc %s（看 FAIL 修）" % (run_dir, state["id"]))
        return
    done.append("e4")

    # E5 ★: translations + 6-column manifest + user approval, before any cut.
    five = os.path.join(doc_dir, "manifest-5col.md")
    six = os.path.join(doc_dir, "切分清单.md")
    translations = _load_json(os.path.join(doc_dir, "translations.json"))
    translate_reviews = _numbered_receipts(
        doc_dir, r"^translate-review-(\d+)\.json$")
    approve = _load_json(os.path.join(doc_dir, "approve.json"))
    if (not os.path.isfile(five) or not os.path.isfile(six)
            or not isinstance(translations, dict)):
        stop("translate_prepare.py --run %s --doc %s → 派翻译员 → translate_apply.py"
             "（E5 译文 + 6 列清单）" % (run_dir, state["id"]))
        return
    if not translate_reviews:
        stop("派译文复核员，再 translate_apply.py --review（E5）")
        return
    if not isinstance(approve, dict):
        stop("E5 把 6 列清单交用户定稿，再 approve.py（★闸门 1；没有定稿不会切分）")
        return
    done.append("e5")

    # E6: cut (cut_stage refuses unless gate e5 passes).
    if cut_running:
        # The lock is alive and names this doc: a cut/pack is really running.
        # Do not read the stale cut-report.json (it is the previous attempt's),
        # do not append e6, and keep NEXT on E6 (wait for the lock to clear).
        state["next"] = ("等 E6 cut 跑完：status.py --run %s（CUT_RUNNING；"
                         "锁释放后重跑 status 看结果）" % run_dir)
        return
    cut = _load_json(os.path.join(doc_dir, "cut-report.json"))
    if not isinstance(cut, dict):
        stop("cut_stage.py --run %s --doc %s --stage cut（E6，后台作业）"
             % (run_dir, state["id"]))
        return
    if cut.get("status") == "needs_review":
        state["warn"].append("%s cut=needs_review：停下问用户" % state["id"])
        stop("停下：E6 needs_review，把 issues 交用户复核（不硬闯）")
        return
    if cut.get("status") != "ok":
        stop("看 cut 日志修复后重跑 cut_stage.py --stage cut（E6）")
        return
    # A delivered pack proves the cut even after E8 cleanup removed the ZIP.
    pack = _load_json(os.path.join(doc_dir, "pack.json"))
    pack_dir = common.resolve_work_path(work, doc["pack_dir"])
    pack_ok = (isinstance(pack, dict) and os.path.isdir(pack_dir)
               and bool(os.listdir(pack_dir)))
    if cut.get("text_sha256") != _sha(cut_text):
        if pack_ok:
            state["warn"].append(
                "%s cut-report 没有绑定当前切分文本（0.0.3 旧回执）；交付包已在，"
                "按旧版继续或按需重切" % state["id"])
        else:
            stop("cut_stage.py --run %s --doc %s --stage cut --reset"
                 "（切分文本变了，E6）" % (run_dir, state["id"]))
            return
    zip_info = cut.get("zip") or {}
    zip_ok = (zip_info.get("verified") is True
              and os.path.isfile(zip_info.get("path") or ""))
    if not (zip_ok or pack_ok):
        stop("cut_stage.py --run %s --doc %s --stage cut --reset"
             "（ZIP 丢失/未验证，E6）" % (run_dir, state["id"]))
        return
    done.append("e6")

    # E7: pack (cut_stage pack requires gate e4 + e5 + e6).
    if not pack_ok:
        stop("cut_stage.py --run %s --doc %s --stage pack（E7）"
             % (run_dir, state["id"]))
        return
    done.append("e7")

    # E8: cleanup -> phase1 -> report -> final e8 gate.
    cleanup = _load_json(os.path.join(doc_dir, "receipts", "cleanup.json"))
    if not isinstance(cleanup, dict):
        stop("cleanup.py --run %s（E8 收尾；默认清本 run 全部 docs）" % run_dir)
        return
    phase1 = gates.read_receipt(run_dir, gates.PHASE1_STAGE)
    if phase1 is None:
        stop("gate.py phase1 --run %s（一次复算 E1 + 逐文档 E2–E7）" % run_dir)
        return
    if not phase1.get("pass"):
        state["warn"].append("%s 阶段一关卡未过：%s"
                             % (state["id"], phase1.get("verdict")))
        stop("gate.py phase1 --run %s（看 FAIL 修）" % run_dir)
        return
    if not os.path.isfile(os.path.join(run_dir, "report.md")):
        stop("report.py --run %s（E8 只汇总）" % run_dir)
        return
    e8 = gates.read_receipt(run_dir, "e8")
    if e8 is None or not e8.get("pass"):
        stop("gate.py e8 --run %s（最后一道阶段一关）" % run_dir)
        return
    done.append("e8")


def _stage_two(state: dict, run_dir: str) -> None:
    """Fill DONE/NEXT for E9-E12 (stage two); stops at the first gap.

    Phase two starts at E9: E1-E8 belong to stage one, so there is no e8 skip
    switch any more.  The one stage-one token cards_build needs is approve.json.
    """
    doc_dir = state["doc_dir"]
    done = state["done"]

    def stop(step: str) -> None:
        state["next"] = step

    # E5 定稿 is the gate into phase two (cards_build checks approve.json too).
    approve = _load_json(os.path.join(doc_dir, "approve.json"))
    if not isinstance(approve, dict):
        stop("E5 定稿缺失：把 6 列清单交用户确认，再 approve.py（★闸门 1）")
        return

    # E9: cards.
    cards = _load_json(os.path.join(doc_dir, "cards.json"))
    cards_reviews = _numbered_receipts(doc_dir, r"^cards-review-(\d+)\.json$")
    if not isinstance(cards, dict):
        stop("cards_build.py → 派排版员 → cards_apply.py（E9）")
        return
    if not cards_reviews:
        stop("派校对员，改完后 cards_apply.py --run %s --doc %s（E9）"
             % (run_dir, state["id"]))
        return
    e9 = gates.read_receipt(run_dir, "e9", state["id"])
    if e9 is None or not e9.get("pass"):
        stop("gate.py e9 --run %s --doc %s（看 FAIL 修）"
             % (run_dir, state["id"]))
        return
    done.append("e9")

    # E10: plan gate (veteran auto-pass / first sample + App approval).
    plan = _load_json(os.path.join(doc_dir, "markji-plan.json"))
    if not isinstance(plan, dict):
        stop("markji_push.py --run %s --doc %s --stage plan（E10，只读云端）"
             % (run_dir, state["id"]))
        return
    plan_gate = plan.get("gate")
    gate_mode = plan_gate.get("mode") if isinstance(plan_gate, dict) else None
    if gate_mode not in ("first", "veteran"):
        stop("markji-plan.json 缺 gate.mode（0.0.10 旧计划）→ 重跑 "
             "markji_push.py --run %s --doc %s --stage plan（E10）"
             % (run_dir, state["id"]))
        return
    if gate_mode == "veteran":
        # 老牌组：无需样本/approve2，e10 通过后直接 push。
        e10 = gates.read_receipt(run_dir, "e10", state["id"])
        if e10 is None or not e10.get("pass"):
            stop("gate.py e10 --run %s --doc %s（需联网只读；老牌组样卡闸门自动放行）"
                 % (run_dir, state["id"]))
            return
        done.append("e10")
    else:
        sample = _load_json(os.path.join(doc_dir, "receipts", "markji-sample.json"))
        if not isinstance(sample, dict) or sample.get("result") != "ok":
            stop("markji_push.py --run %s --doc %s --stage sample（E10 样卡：写 1 张）"
                 % (run_dir, state["id"]))
            return
        approve2 = _load_json(os.path.join(doc_dir, "approve2.json"))
        if not isinstance(approve2, dict):
            sample_no = sample.get("no") or "<行号>"
            stop("请用户在 App 看过样卡 %s → approve2.py --run %s --doc %s "
                 "--mode user --sample-no %s --chapter-ok（★闸门 E10）"
                 % (sample_no, run_dir, state["id"], sample_no))
            return
        e10 = gates.read_receipt(run_dir, "e10", state["id"])
        if e10 is None or not e10.get("pass"):
            stop("gate.py e10 --run %s --doc %s（需联网只读）"
                 % (run_dir, state["id"]))
            return
        done.append("e10")

    # E11: push.
    markji = _load_json(os.path.join(doc_dir, "markji.json"))
    push = _load_json(os.path.join(doc_dir, "receipts", "markji-push.json"))
    if not isinstance(markji, dict) or not isinstance(push, dict):
        hint = ""
        if ((plan.get("gate") or {}).get("mode") == "veteran"):
            hint = "；老牌组：无需样卡/approve2，diff_pending 默认跳过"
        stop("markji_push.py --run %s --doc %s --stage push（E11，慢，后台）%s"
             % (run_dir, state["id"], hint))
        return
    e11 = gates.read_receipt(run_dir, "e11", state["id"])
    if e11 is None or not e11.get("pass"):
        stop("gate.py e11 --run %s --doc %s（需联网只读）"
             % (run_dir, state["id"]))
        return
    done.append("e11")

    # E12: maintenance sync.
    sync = _load_json(os.path.join(doc_dir, "receipts", "markji-sync.json"))
    if not isinstance(sync, dict):
        stop("markji_sync.py --run %s --doc %s --check（E12 维护步，可选）"
             % (run_dir, state["id"]))
        return
    e12 = gates.read_receipt(run_dir, "e12", state["id"])
    if e12 is None or not e12.get("pass"):
        stop("gate.py e12 --run %s --doc %s（看 FAIL 修）"
             % (run_dir, state["id"]))
        return
    done.append("e12")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    result = {"ok": True, "exit_code": common.EXIT_OK, "stage": STAGE,
              "run": run_dir, "next": "", "done": [], "warnings": [],
              "docs": []}
    run = _load_json(os.path.join(run_dir, "run.json"))
    if not isinstance(run, dict):
        result["next"] = "bootstrap.py --work <工作区>（$RUN 里没有 run.json；先按 E1 建 run）"
        result["warnings"].append("run.json 不存在或不可读：%s" % run_dir)
        if not args.json:
            print("RUN=%s" % run_dir)
            print("NEXT=%s" % result["next"])
        common.emit_json(result)
        return common.EXIT_OK
    if run.get("skill_version"):
        result["skill_version"] = run.get("skill_version")
    work = os.path.abspath(run.get("work") or "")
    recorded = ((run.get("package") or {}).get("scripts_sha256") or "")
    if recorded and recorded != common.scripts_tree_sha():
        result["warnings"].append(
            "脚本已漂移（run 记录 %s… ≠ 当前 %s…）；不要跨版本混跑"
            % (recorded[:8], common.scripts_tree_sha()[:8]))
    phase = str((run.get("options") or {}).get("phase") or "")
    docs = [entry.get("id") for entry in run.get("docs") or []
            if isinstance(entry, dict) and entry.get("id")]
    if not docs:
        result["next"] = "bootstrap.py --work %s（run.json.docs 为空）" % work
        if not args.json:
            print("RUN=%s" % run_dir)
            print("NEXT=%s" % result["next"])
        common.emit_json(result)
        return common.EXIT_OK

    # 0.0.10 (D3): read-only view of the E6/E7 serial lock + live cutter log.
    cut_info, cut_warn = _cut_lock_state(run_dir)
    result["cut_running"] = cut_info
    if cut_warn:
        result["warnings"].append(cut_warn)
    cut_last_log = _latest_cut_log_line(run_dir, docs)
    if cut_last_log:
        result["cut_last_log"] = cut_last_log

    # Only a running **cut** invalidates the old cut-report.json; a running pack
    # has a fresh report (E7 requires E6 PASS), so it is read normally.
    running_doc = (cut_info.get("doc")
                   if (isinstance(cut_info, dict) and cut_info.get("running")
                       and cut_info.get("stage") == "cut")
                   else None)
    states = []
    for doc_id in docs:
        state = _doc_state(run_dir, work, doc_id)
        if state["doc"] is not None:
            try:
                if phase == "2":
                    # --phase 2 reuses a delivered stage-one pack: E1-E8 were
                    # already done by the stage-one run, so start at E9.
                    _stage_two(state, run_dir)
                else:
                    _stage_one(state, run_dir, work,
                               cut_running=doc_id == running_doc)
                    if state["next"] is None:
                        _stage_two(state, run_dir)
            except Exception as exc:  # noqa: BLE001 - status must never crash
                state["next"] = "检查 doc %s 的产物（status 读取出错：%s）" % (doc_id, exc)
        states.append(state)
        result["done"].extend(state["done"])
        result["warnings"].extend(state["warn"])
        result["docs"].append({"id": doc_id, "done": state["done"],
                               "next": state["next"], "warnings": state["warn"]})
    pending = [state for state in states if state["next"]]
    if pending:
        result["next"] = pending[0]["next"]
        if len(pending) > 1:
            result["next"] += "（另有 %d 个文档待处理）" % (len(pending) - 1)
    else:
        result["next"] = ("阶段一/阶段二已完成。交付确认后：cleanup.py --run %s "
                          "--purge-run（删 $RUN；删了就不能再复算）" % run_dir)
    result["done"] = list(dict.fromkeys(result["done"]))
    if not args.json:
        print("RUN=%s" % run_dir)
        if work:
            print("WORK=%s" % work)
        if cut_info and cut_info["running"]:
            elapsed = cut_info["elapsed_sec"]
            print("CUT_RUNNING doc=%s stage=%s elapsed=%s log=%s" % (
                cut_info["doc"] or "?", cut_info["stage"] or "?",
                "%ss" % elapsed if elapsed is not None else "?",
                cut_info["log"] or "?"))
        if cut_last_log:
            print("CUT_LAST_LOG=%s" % cut_last_log)
        for item in result["docs"]:
            print("DOC=%s DONE=%s" % (item["id"], ",".join(item["done"]) or "-"))
        for warning in result["warnings"]:
            print("WARN=%s" % warning)
        print("NEXT=%s" % result["next"])
    common.emit_json(result)
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
