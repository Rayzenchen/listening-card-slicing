#!/usr/bin/env python3
"""report.py -- E8: summarise the run into ``$RUN/report.md`` + ``report.json``.

This is the human-facing receipt of one run: per document it records the
inputs, the manifest sha, the cut status, the pack contents and the **gate
verdicts already recorded on disk** by ``gate phase1``/``gate e8``.  It is a
summary only: it never re-runs a gate (0.0.4 瘦身), so the numbers do not
silently disagree with the verdict the user was shown.

Two things the callers rely on:

* **F1 risk line** (FINDINGS): when the cutter's first segment looks like it
  swallowed speech the transcript does not cover (``asr_similarity < 0.90`` or
  a non-zero start), that goes into the 「风险」 section so the agent must ask
  the user to listen before shipping.
* **A11 downstream-extension line**: ``下游扩展：yes/no``.  0.0.8 packs
  already carry the E5 6-column manifest, so the comparison is byte-for-byte
  against **that** manifest (``$RUN`` copy, then pack.json's recorded sha);
  ``yes`` only when the delivered pack was extended on the right (a 7th
  column / extra quote line) while the six approved columns and the tail
  stayed frozen.

The manifest is written atomically; stdout is one human line ``REPORT <path>``
and the last line is ``JSON {...}``.  Exit 0 when the report was written (even
if gates still fail — the failures are the point of the report).

Exit codes: 0 written / 1 write failure / 2 usage or unreadable run.
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
import gates  # noqa: E402

STAGE = "e8"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "report.py",
        "E8：从磁盘复算关卡并写 $RUN/report.md（含 F1 首段风险与 A11 下游扩展行）。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


def _safe(loader, default=None):
    try:
        return loader()
    except Exception:  # noqa: BLE001 - a report must survive missing artifacts
        return default


def _first_segment_risk(report) -> str | None:
    detail = ((report or {}).get("segments_detail") or [None])[0]
    if not isinstance(detail, dict):
        return None
    notes = []
    similarity = detail.get("asr_similarity")
    if isinstance(similarity, (int, float)) and similarity < 0.90:
        notes.append("asr_similarity=%.2f（第 1 段可能含未覆盖语音）" % similarity)
    start_sec = detail.get("start_sec")
    start_sample = detail.get("start_sample")
    if (isinstance(start_sec, (int, float)) and start_sec > 0) or \
            (isinstance(start_sample, (int, float)) and start_sample > 0):
        notes.append("start=%s（第 1 段起点不是 0）"
                     % (start_sec if isinstance(start_sec, (int, float)) else start_sample))
    return "；".join(notes) if notes else None


def _other_runs(run_dir: str, work: str) -> int:
    root = os.path.dirname(os.path.abspath(run_dir))
    count = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or os.path.abspath(path) == os.path.abspath(run_dir):
            continue
        run_file = os.path.join(path, "run.json")
        if not os.path.isfile(run_file):
            continue
        data = _safe(lambda: common.read_json(run_file), None)
        if isinstance(data, dict) and os.path.abspath(data.get("work") or "") == work:
            count += 1
    return count


def _pack_container(pack: dict) -> str:
    """Report label for a delivered pack: format first, else infer like gate e7."""
    fmt = pack.get("format") if isinstance(pack, dict) else None
    if isinstance(fmt, dict):
        container = str(fmt.get("container") or "").strip().lower()
        if container in ("mp3", "wav"):
            return container
    pack_dir = str(pack.get("pack_dir") or "") if isinstance(pack, dict) else ""
    return common.pack_audio_ext(pack_dir).lstrip(".")


def _pack_manifest_extension(doc, pack: dict) -> bool:
    """A11: was the delivered pack manifest extended over the E5 6 columns?

    A normal 0.0.8 pack already contains the 6-column manifest, so the old
    "compare with the 5-column baseline" test marked every normal pack as
    "下游扩展：yes".  Compare byte-for-byte against the E5 6-column manifest
    ($RUN copy first, then pack.json's recorded sha); only an additive
    extension (extra right-side column / extra quote line) counts.
    """
    package_manifest = os.path.join(os.path.abspath(doc.pack_dir),
                                    os.path.basename(doc.manifest))
    if not os.path.isfile(package_manifest):
        return False
    package_text = common.read_text(package_manifest)
    package_sha = common.sha256_text(package_text)
    header_cells = []
    for line in package_text.split("\n"):
        if line.startswith("| 行 |"):
            header_cells = common.split_table_cells(line)
            break
    if len(header_cells) < 6:
        return False  # a 5-column (or broken) copy is not a downstream extension
    six_text = None
    six_sha = None
    run_six = os.path.join(doc.dir, "切分清单.md")
    if os.path.isfile(run_six):
        text = common.read_text(run_six)
        if common.manifest_columns(text) == 6:
            six_text = text
            six_sha = common.sha256_text(text)
    if six_sha is None:
        recorded = pack.get("manifest_sha256")
        if recorded and pack.get("manifest_columns") in (None, 6):
            six_sha = str(recorded)
    if six_sha is None:
        return False
    if package_sha == six_sha:
        return False
    if six_text is None:
        # No reference text: only a real 7+-column header proves an extension.
        return len(header_cells) > 6
    compatible, _reason = gates.manifest_extension_compatible(package_text, six_text)
    return bool(compatible)


def _gate_receipts(doc) -> dict:
    """Per-stage verdicts recorded by ``gate phase1``/``gate e8`` (no recompute)."""
    result = {}
    for stage in gates.DOC_STAGES:
        data = _safe(lambda: common.read_json(
            os.path.join(doc.dir, "receipts", "gate-%s.json" % stage)), None)
        if not isinstance(data, dict):
            result[stage] = {"verdict": "未运行", "warnings": []}
            continue
        warnings = [check.get("message") for check in data.get("checks") or []
                    if isinstance(check, dict) and check.get("status") == "WARN"
                    and check.get("message")]
        result[stage] = {
            "verdict": str(data.get("verdict")
                           or ("PASS" if data.get("pass") else "FAIL")),
            "warnings": warnings,
            "at": data.get("at"),
        }
    return result


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE}
    try:
        gate = gates.GateRun(run_dir)
        log = common.StageLog(run_dir, STAGE)
        other = _other_runs(run_dir, gate.work)
        # 0.0.4: summary only -- the E1 verdict is read from the receipt written
        # by gate phase1/e8, never recomputed here.
        e1_receipt = gates.read_receipt(run_dir, "e1")
        e1_pass = (bool(e1_receipt.get("pass"))
                   if isinstance(e1_receipt, dict) else None)
        recorded_scripts_sha = str((gate.run.get("package") or {})
                                   .get("scripts_sha256") or "")
        drift = bool(recorded_scripts_sha
                     and recorded_scripts_sha != common.scripts_tree_sha())

        docs = []
        risks = []
        extension_any = False
        for doc_id in gate.doc_ids:
            doc = _safe(lambda: gate.doc(doc_id), None)
            if doc is None:
                docs.append({"id": doc_id, "missing": True})
                risks.append("%s：doc.json 不可读" % doc_id)
                continue
            segments = _safe(doc.segments, {}) or {}
            stats = segments.get("stats") or {}
            flags = segments.get("flags") or []
            # 0.0.5: the manifest sha is recorded in segments.json, not a file.
            manifest_sha = segments.get("manifest_sha256")
            cut = _safe(doc.cut_report, {}) or {}
            pack = _safe(doc.pack_report, {}) or {}
            gate_results = _gate_receipts(doc)

            # A11: was the delivered pack manifest extended over the E5 6 columns?
            extended = False
            try:
                extended = _pack_manifest_extension(doc, pack)
            except Exception:  # noqa: BLE001 - a report must survive bad manifests
                extended = False
            extension_any = extension_any or extended

            first_risk = _first_segment_risk(cut)
            if first_risk:
                risks.append("%s F1 首段：%s" % (doc_id, first_risk))
            for flag in flags:
                if isinstance(flag, dict) and flag.get("kind") == "long_atom":
                    risks.append("%s 第 %03d 行 %d 词（long_atom，不自动拆）"
                                 % (doc_id, flag.get("n", 0), flag.get("words", 0)))
            if cut.get("status") == "needs_review":
                risks.append("%s cut=needs_review（%d 个 issue，停下问用户）"
                             % (doc_id, len(cut.get("issues") or [])))
            failed = [stage for stage, info in gate_results.items()
                      if str(info["verdict"]).startswith("FAIL")]
            unrun = [stage for stage, info in gate_results.items()
                     if info["verdict"] == "未运行"]
            if failed:
                risks.append("%s 关卡未过：%s" % (doc_id, "、".join(failed)))
            if unrun:
                risks.append("%s 关卡回执缺失：%s（先跑 gate phase1 或 gate e8）"
                             % (doc_id, "、".join(unrun)))
            if extended:
                risks.append("%s 包内清单被下游扩展（前 5 列与尾部未变，A11 兼容）" % doc_id)
            if not os.path.isfile(os.path.join(doc.dir, "receipts", "cleanup.json")):
                risks.append("%s 还没有 cleanup 回执（E8 收尾未完成）" % doc_id)

            docs.append({
                "id": doc_id,
                "stem": doc.stem,
                "audio": doc.audio,
                "text": doc.text,
                "audio_sha256": doc.doc.get("audio_sha256"),
                "text_sha256": doc.doc.get("text_sha256"),
                "segments": stats,
                "manifest_sha256": manifest_sha,
                "cut": cut,
                "pack": pack,
                "gates": gate_results,
                "extended": extended,
                "first_risk": first_risk,
                "flags": flags,
                "pack_dir": doc.pack_dir,
                "manifest": doc.manifest,
            })

        if any(item.get("missing") for item in docs):
            pass
        if e1_pass is False:
            risks.append("E1 未过：%s"
                         % (e1_receipt.get("verdict") or "见 gate e1"))
        elif e1_pass is None:
            risks.append("E1 关卡回执缺失（先跑 gate phase1 或 gate e8）")

        lines = []
        lines.append("# 听力卡切分报告 · %s" % os.path.basename(gate.run_dir))
        lines.append("")
        lines.append("> 生成时间：%s" % common.now_iso())
        lines.append("> 工作区：`%s`" % gate.work)
        lines.append("> 运行目录：`%s`" % gate.run_dir)
        lines.append("> 输入 sha：`%s`" % (gate.run.get("input_sha") or ""))
        lines.append("> 切片确认：E5 交付 6 列清单后由 approve.py 定稿（切分前★闸门）")
        lines.append("> 脚本漂移：%s" % ("yes" if drift else "no"))
        lines.append("")
        lines.append("## 总览")
        lines.append("")
        lines.append("| 文档 | stem | 行数 | 进卡组 | 切分 | 包 | 关卡 | 下游扩展 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for item in docs:
            if item.get("missing"):
                lines.append("| %s | — | — | — | — | — | — | — |" % item["id"])
                continue
            stats = item["segments"]
            cut = item["cut"]
            pack = item["pack"]
            verdicts = [str(info["verdict"]) for info in item["gates"].values()]
            if any(value.startswith("FAIL") for value in verdicts):
                gate_label = "FAIL"
            elif verdicts and all(value.startswith("PASS") for value in verdicts):
                gate_label = "PASS"
            else:
                gate_label = "未运行"
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                item["id"], item["stem"], stats.get("total", "—"),
                stats.get("keep", "—"), cut.get("status", "—"),
                "%s %s" % (pack.get("count"), _pack_container(pack))
                if pack.get("count") is not None else "—",
                gate_label,
                "yes" if item["extended"] else "no",
            ))
        lines.append("")
        for item in docs:
            if item.get("missing"):
                lines.append("## %s" % item["id"])
                lines.append("")
                lines.append("- doc.json 不可读，无法汇总。")
                lines.append("")
                continue
            stats = item["segments"]
            by_type = stats.get("by_type") or {}
            cut = item["cut"]
            pack = item["pack"]
            lines.append("## %s · %s" % (item["id"], item["stem"]))
            lines.append("")
            lines.append("- 音频：`%s`（sha256 `%s`）"
                         % (item["audio"], item["audio_sha256"]))
            lines.append("- 原文：`%s`（sha256 `%s`）"
                         % (item["text"], item["text_sha256"]))
            lines.append("- 清单：%s 行 / 进卡组 %s（正文 %s + 题目 %s）"
                         % (stats.get("total"), stats.get("keep"),
                            by_type.get("正文", 0), by_type.get("题目", 0)))
            lines.append("- 清单文件：`%s`" % item["manifest"])
            lines.append("- 清单 sha256：`%s`" % (item["manifest_sha256"] or "缺失"))
            lines.append("- cut：status=%s，segments=%s，zip verified=%s，issues=%s"
                         % (cut.get("status"), cut.get("segments"),
                            (cut.get("zip") or {}).get("verified"),
                            len(cut.get("issues") or [])))
            zip_path = (cut.get("zip") or {}).get("path")
            zip_cleaned = bool(cut.get("status") == "ok" and zip_path
                               and not os.path.isfile(zip_path)
                               and pack.get("zip_path")
                               and pack.get("count") is not None)
            if zip_cleaned:
                lines.append("- 切分 ZIP：已清理（A8）")
                risks.append("%s 切分 ZIP 已被 cleanup 清理（A8）" % item["id"])
            if cut.get("status") == "needs_review":
                lines.append("- 需要复核：见 `$RUN/docs/%s/issues.md`" % item["id"])
            lines.append("- 包：`%s`（%s %s）"
                         % (item["pack_dir"], pack.get("count", "—"),
                            _pack_container(pack)))
            if item["flags"]:
                lines.append("- 长原子：%s"
                             % "、".join("%03d（%s 词）" % (flag.get("n"), flag.get("words"))
                                        for flag in item["flags"]))
            else:
                lines.append("- 长原子：无")
            lines.append("- 首段告警：%s" % (item["first_risk"] or "无"))
            gate_parts = []
            for stage, info in item["gates"].items():
                warnings = [value for value in info.get("warnings") or [] if value]
                suffix = "（WARN: %s）" % "；".join(warnings) if warnings else ""
                gate_parts.append("%s %s%s" % (stage, info["verdict"], suffix))
            gate_text = " / ".join(gate_parts)
            if all(info["verdict"] == "未运行" for info in item["gates"].values()):
                lines.append("- 关卡：未运行（本报告只汇总；先跑 gate phase1 / gate e8）")
            else:
                lines.append("- 关卡（gate 回执）：%s" % gate_text)
            lines.append("- 下游扩展：%s" % ("yes" if item["extended"] else "no"))
            lines.append("")
        lines.append("## 风险")
        lines.append("")
        if risks:
            for risk in risks:
                lines.append("- %s" % risk)
        else:
            lines.append("- 无")
        lines.append("")
        lines.append("下游扩展：%s" % ("yes" if extension_any else "no"))
        lines.append("")
        lines.append("## 其它 run")
        lines.append("")
        lines.append("- 其它 run：%d 个（不影响本次）" % other)
        lines.append("")
        report_text = "\n".join(lines) + "\n"
        report_path = os.path.join(gate.run_dir, "report.md")
        common.atomic_write_text(report_path, report_text)
        log("report written: %s" % report_path)
        report_json_path = os.path.join(gate.run_dir, "report.json")
        common.atomic_write_json(report_json_path, {
            "version": 1,
            "generated_at": common.now_iso(),
            "run": gate.run_dir,
            "work": gate.work,
            "input_sha": gate.run.get("input_sha"),
            "e1_pass": e1_pass,
            "extension": extension_any,
            "risks": risks,
            "docs": docs,
        })
        log("report.json written: %s" % report_json_path)
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "report": report_path,
            "report_json": report_json_path,
            "docs": [item.get("id") for item in docs],
            "extension": extension_any,
            "risks": risks,
            "e1_pass": e1_pass,
        })
        if not args.json:
            print("REPORT %s" % report_path)
            print("文档：%s" % "、".join(item.get("id", "?") for item in docs))
            print("风险：%d 条" % len(risks))
            for risk in risks[:8]:
                print("- %s" % risk)
            print("下游扩展：%s" % ("yes" if extension_any else "no"))
        common.emit_json(result)
        return common.EXIT_OK
    except common.Failure as failure:
        if log:
            log("FAIL " + failure.message)
        return common.report_failure(STAGE, failure)
    except OSError as exc:
        return common.report_failure(
            STAGE, common.Failure(common.EXIT_FAIL, "写报告失败：%s" % exc,
                                  reason="os_error"))
    finally:
        if log:
            log.flush()


if __name__ == "__main__":
    sys.exit(main())
