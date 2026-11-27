#!/usr/bin/env python3
"""cleanup.py -- E8 收尾：清本 run 在 ``产出区`` 的残留（默认只清本文档）。

Rules (接口约定 §3.6 + 接口修订 A8 + 0.0.4 文档作用域):

* **document scope by default**: remove only paths derived from this run's
  documents (``<stem>_切分结果/``, ``<stem>_切分结果*.zip``,
  ``<stem>_切分文本.txt``, legacy ``_不进卡组/``) and the ``<stem>_切分清单.md``
  review copy.  Never touch another doc/run and never call anything that globs
  the whole output area.  With ``--doc d01`` only that document is cleaned
  (use it before an E6 re-cut); without it every doc of this run is cleaned
  (the E8 global wrap-up);
* ``--global`` additionally calls ``<work>/系统/tools/cleanup_outputs.py`` (a
  workspace-wide sweep, default ``<work>/产出区`` only) and removes generic
  ``.tmp_*`` / ``.DS_Store`` / ``*.partial`` residue.  Use it only when no
  other run is cutting in parallel;
* delete the review-period ``<stem>_切分清单.md`` **only** when a delivered
  pack already contains a manifest with the same file name;
* never delete ``*_音频切分（进牌组）/`` (current or ``.prev-`` archived), any
  ``*.mp3`` outside a pack, ``材料区/`` or ``系统/``;
* every candidate is resolved and must sit directly inside the output area —
  anything else is refused and reported;
* never deletes ``approve.json``, ``pack.json`` / ``receipts/pack.json`` or an
  in-pack 6-column manifest (they live in ``$RUN`` / inside the delivered pack,
  so they are not even candidates; the guard makes that explicit).

``--dry-run`` only lists what would be removed and writes no receipts.
``--purge-run`` deletes ``$RUN`` **after** the cleanup and only when
``run.json.run`` points at exactly that directory; that removes approve/pack
receipts too, so use it only after delivery is confirmed.

Exit codes: 0 ok (or nothing to clean) / 1 the workspace tool or a removal
failed / 2 usage, bad run, or an unsafe path.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bootstrap  # noqa: E402
import common  # noqa: E402

STAGE = "e8"
PACK_SUFFIX = "_音频切分（进牌组）"
TEMP_PATTERNS = (".tmp_*", ".tmp-*", ".DS_Store", "*.partial")
#: 0.0.8: tokens/artifacts that must survive every cleanup (they live in $RUN,
#: never in 产出区; the guard is belt-and-braces for custom output roots).
PROTECTED_NAMES = ("approve.json", "pack.json")


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "cleanup.py",
        "E8：清本 run 在产出区的切分残留，写 cleanup 回执；不删包、不删 approve/pack、默认不碰别的文档。",
        epilog="--dry-run 只列清单；--purge-run 在清理后删 $RUN（交付确认后再用）。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", default=None,
                        help="只清该文档的残留（E6 重切前用）；省略=清本 run 全部 docs（E8 收尾）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将删除的内容")
    parser.add_argument("--global", dest="global_mode", action="store_true",
                        help="额外调工作区 cleanup_outputs.py，并清通用临时文件"
                        "（会遍历整个产出区；并行切分时不要用）")
    parser.add_argument("--purge-run", action="store_true",
                        help="清理后删除 $RUN（含回执/日志；交付确认后再用）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


def _is_pack_name(name: str) -> bool:
    return name.endswith(PACK_SUFFIX) or (".prev-" in name and PACK_SUFFIX in name)


def _area_for(doc: dict, work: str) -> str:
    pack_dir = common.resolve_work_path(work, doc["pack_dir"])
    return os.path.dirname(os.path.abspath(pack_dir))


def _doc_candidates(doc: dict, work: str, area: str) -> list:
    """Only paths derived from this document; never globs into other jobs."""
    stem = doc["stem"]
    result = os.path.abspath(common.resolve_work_path(work, doc["result_dir"]))
    candidates = [
        result,
        result + ".zip",
        result + "_review.zip",
        os.path.join(area, stem + "_切分文本.txt"),
        os.path.join(area, stem + "_纯卡组.zip"),
        os.path.join(area, stem + "_不进卡组"),
    ]
    return list(dict.fromkeys(candidates))


def _area_candidates(area: str) -> list:
    found = []
    if not os.path.isdir(area):
        return found
    for name in sorted(os.listdir(area)):
        if any(fnmatch.fnmatch(name, pattern) for pattern in TEMP_PATTERNS):
            found.append(os.path.join(area, name))
    return found


def _removable(path: str, area: str) -> bool:
    if not os.path.exists(path):
        return False
    area_real = os.path.realpath(area)
    real = os.path.realpath(path)
    if os.path.dirname(real) != area_real:
        return False
    name = os.path.basename(real)
    if _is_pack_name(name):
        return False
    if name in PROTECTED_NAMES:
        return False
    return True


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _packed_manifest_names(area: str) -> set:
    names = set()
    if not os.path.isdir(area):
        return names
    for name in os.listdir(area):
        pack = os.path.join(area, name)
        if not os.path.isdir(pack) or not _is_pack_name(name):
            continue
        try:
            for item in os.listdir(pack):
                if item.endswith("_切分清单.md"):
                    names.add(item)
        except OSError:
            continue
    return names


def _run_work_tool(work: str, dry_run: bool, log) -> dict:
    tool = os.path.join(work, "系统", "tools", "cleanup_outputs.py")
    receipt = {"path": tool, "ok": False, "exit_code": None, "stdout": "", "stderr": ""}
    if not os.path.isfile(tool):
        receipt["error"] = "cleanup_outputs.py 不存在，改用本 skill 的清理逻辑"
        log(receipt["error"])
        return receipt
    try:
        python = bootstrap.probe_env(work)["python"]["path"]
    except Exception:  # noqa: BLE001
        python = sys.executable
    command = [python, tool]
    if dry_run:
        command.append("--dry-run")
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        receipt["error"] = str(exc)
        log("cleanup tool failed to start: %s" % exc)
        return receipt
    receipt.update({
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-20000:],
        "stderr": proc.stderr[-20000:],
    })
    log("cleanup tool exit=%d" % proc.returncode)
    if proc.stdout:
        log("tool stdout:\n" + proc.stdout[-20000:])
    if proc.stderr:
        log("tool stderr:\n" + proc.stderr[-20000:])
    return receipt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "dry_run": args.dry_run, "doc": args.doc}
    try:
        run = common.load_run(run_dir)
        work = os.path.abspath(run.get("work") or "")
        if args.dry_run and args.purge_run:
            raise common.Failure(common.EXIT_USAGE, "--dry-run 不能与 --purge-run 同用")
        if args.doc and args.purge_run:
            raise common.Failure(common.EXIT_USAGE,
                                 "--purge-run 会删整个 $RUN，不能与 --doc 同用")
        log = common.StageLog(run_dir, STAGE, args.doc)
        docs = [common.load_doc(run_dir, entry["id"]) for entry in run.get("docs") or []]
        if args.doc:
            ids = [doc["id"] for doc in docs]
            if args.doc not in ids:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "run.json.docs 里没有 %s（可选：%s）" % (args.doc, "、".join(ids)))
            docs = [doc for doc in docs if doc["id"] == args.doc]
        areas = []
        for doc in docs:
            area = _area_for(doc, work)
            if area not in areas:
                areas.append(area)
        default_area = os.path.abspath(os.path.join(work, "产出区"))
        if not args.global_mode:
            work_tool = {"path": None, "ok": True,
                         "error": "默认只清本文档；--global 才调工作区 cleanup_outputs.py"}
        elif areas and areas[0] == default_area:
            work_tool = _run_work_tool(work, args.dry_run, log)
        else:
            work_tool = {"path": None, "ok": True,
                         "error": "自定义产出根，跳过工作区 cleanup_outputs.py"}

        removed = []
        skipped = []
        kept_packs = []
        remove_failed = False
        for area in areas:
            if not os.path.isdir(area):
                skipped.append({"path": area, "reason": "产出区不存在"})
                continue
            packed = _packed_manifest_names(area)
            stems = {doc["stem"] for doc in docs if _area_for(doc, work) == area}
            candidates = []
            for doc in docs:
                if _area_for(doc, work) == area:
                    candidates.extend(_doc_candidates(doc, work, area))
            if args.global_mode:
                candidates.extend(_area_candidates(area))
            for name in sorted(os.listdir(area)):
                # only this run's review-period manifest; never another doc's
                if (name.endswith("_切分清单.md")
                        and name[:-len("_切分清单.md")] in stems
                        and name in packed):
                    candidates.append(os.path.join(area, name))
            for candidate in dict.fromkeys(candidates):
                path = os.path.abspath(candidate)
                if not os.path.exists(path):
                    continue
                if not _removable(path, area):
                    skipped.append({"path": path, "reason": "不在产出区顶层或是交付包"})
                    continue
                if args.dry_run:
                    removed.append(path)
                    log("would remove %s" % path)
                else:
                    try:
                        _remove(path)
                    except OSError as exc:
                        skipped.append({"path": path, "reason": str(exc)})
                        remove_failed = True
                        log("remove failed: %s (%s)" % (path, exc))
                        continue
                    removed.append(path)
                    log("removed %s" % path)
            for name in sorted(os.listdir(area)):
                path = os.path.join(area, name)
                if os.path.isdir(path) and _is_pack_name(name):
                    kept_packs.append(path)

        if not args.dry_run:
            for doc in docs:
                receipt = {
                    "version": 1,
                    "doc": doc["id"],
                    "at": common.now_iso(),
                    "dry_run": False,
                    "global": bool(args.global_mode),
                    "area": _area_for(doc, work),
                    "work_tool": work_tool,
                    "removed": removed,
                    "skipped": skipped,
                    "kept_packs": kept_packs,
                    # 0.0.8: prove the tokens/audit files are still there after E8.
                    "kept_protected": [
                        name for name in PROTECTED_NAMES
                        if os.path.isfile(os.path.join(
                            common.doc_dir(run_dir, doc["id"]), name))],
                }
                common.atomic_write_json(
                    os.path.join(common.doc_dir(run_dir, doc["id"]), "receipts",
                                 "cleanup.json"), receipt)
            log("cleanup receipt written for %d doc(s)" % len(docs))

        ok = work_tool.get("ok", True) and not remove_failed
        result.update({
            "ok": ok,
            "exit_code": common.EXIT_OK if ok else common.EXIT_FAIL,
            "global": bool(args.global_mode),
            "removed": removed,
            "skipped": skipped,
            "kept_packs": kept_packs,
            "work_tool": {key: value for key, value in work_tool.items()
                          if key in ("path", "ok", "exit_code", "error")},
            "purged_run": None,
        })
        if args.purge_run:
            data = common.read_json(os.path.join(run_dir, "run.json"))
            if os.path.abspath(data.get("run") or "") != run_dir:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "$RUN 与 run.json.run 不一致，拒绝删除：%s" % run_dir)
            log("purged run: %s" % run_dir)
            log.flush()
            log = None
            shutil.rmtree(run_dir)
            result["purged_run"] = run_dir
        if not args.json:
            if args.dry_run:
                print("（dry-run）将删除 %d 项" % len(removed))
            elif removed:
                print("已删除 %d 项，保留 %d 个交付包" % (len(removed), len(kept_packs)))
            else:
                print("nothing to clean")
            for path in removed[:20]:
                print("%s %s" % ("would remove" if args.dry_run else "removed", path))
            for pack in kept_packs:
                print("keep %s" % pack)
            if result["purged_run"]:
                print("PURGED %s" % result["purged_run"])
        common.emit_json(result)
        return common.EXIT_OK if ok else common.EXIT_FAIL
    except common.Failure as failure:
        if log:
            log("FAIL " + failure.message)
        return common.report_failure(STAGE, failure)
    except OSError as exc:
        return common.report_failure(
            STAGE, common.Failure(common.EXIT_FAIL, "清理失败：%s" % exc,
                                  reason="os_error"))
    finally:
        if log:
            log.flush()


if __name__ == "__main__":
    sys.exit(main())
