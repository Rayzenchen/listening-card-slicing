#!/usr/bin/env python3
"""zh_fix.py -- E9 中文校对「表格 + 直接改」（0.1.0 D1）。

校对员（``#proofreader``）发现中文问题时不再只能回 E5 重跑：它把建议写进回执
的 ``zh_fixes`` 数组（整张卡的中文行数组，``len(zh) == 英文句数``）。根 agent 用
本脚本渲染表格交给用户逐条敲定，然后**直接改中文**，不重跑 E3/E5/E6/E7/E8：

    # 1) 只读渲染表格（编号 / 当前中文 / 建议中文 / 理由；不写文件）
    python3 zh_fix.py table --run "$RUN" --doc d01 \\
        --review "$RUN/docs/d01/cards-review-1.json" --json

    # 2) 用户敲定：全部接受
    python3 zh_fix.py apply --run "$RUN" --doc d01 \\
        --review "$RUN/docs/d01/cards-review-1.json" --accept all --json

    # 3) 只接受某几张 / 用户自己改措辞：用批准文件
    python3 zh_fix.py apply --run "$RUN" --doc d01 \\
        --fixes /tmp/zh-approved.json --json

批准文件（``{"version":1,"doc":"d01","fixes":[{"no":"012","zh":["…"]}]}``）里的
``zh`` 就是用户最终措辞，原样采用。

``apply`` 只写这几处，且全部校验通过后才写：

* ``$RUN/docs/<doc>/切分清单.md``（6 列，主真源）；
* 包内 6 列清单（``doc.json.pack_manifest`` 或 ``doc.pack_dir/<stem>_切分清单.md``，
  存在才写；存在时顺手刷新 ``pack.json`` 的 manifest sha）；
* 产出区 6 列清单（``doc.json.manifest`` 指向的副本，存在且是 6 列才写）；
* ``translations.json``：行替换 + ``stats.fixes`` 累加 + ``manifest_sha256_v2``；
* ``doc.json.manifest_sha256_v2``。

**不写 approve.json**（用户批准后由根 agent 跑 ``approve.py``），输出
``changed_rows`` / ``manifest_sha256_v2`` / ``need_approve=true`` /
``need_refresh=true`` 提示下一步：``approve.py`` → ``cards_apply.py
--refresh-zh`` → ``gate e9`` → E10。不碰音频、cut-report、E10 产物。

Exit codes: 0 成功 / 1 回执或行校验失败（不写任何产物）/ 2 用法 /
3 缺 6 列清单或 translations.json（先跑 E5）。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cards_apply  # noqa: E402
import common  # noqa: E402
import translate_apply  # noqa: E402

STAGE = "e9"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "zh_fix.py",
        "E9：中文校对表格 + 直改（改 6 列清单 / translations.json，不写 approve.json）。",
        epilog="回执/行校验失败 → exit 1（不写任何产物）；"
               "缺 6 列清单或 translations.json → exit 3（先跑 E5）。"
               "子命令 table / apply 都支持 --json（末行 JSON；表格也在 table 字段）。",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=common.SkillArgumentParser)

    table = sub.add_parser(
        "table",
        help="只读渲染「卡片 / 当前中文 / 建议中文 / 理由」表格",
        description="读取校对回执的 zh_fixes，渲染给用户敲定的表格（不写任何文件）。",
    )
    table.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    table.add_argument("--doc", required=True, help="文档 id，例如 d01")
    table.add_argument("--review", required=True, help="校对回执 JSON（含 zh_fixes）")
    table.add_argument("--json", action="store_true",
                       help="只输出末行 JSON（表格文本在 table 字段）")

    apply_parser = sub.add_parser(
        "apply",
        help="用户敲定后直接改中文（6 列清单 + translations.json）",
        description="把敲定的 zh_fixes 写进 6 列清单与 translations.json；"
                    "不写 approve.json，不重跑上游。",
    )
    apply_parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    apply_parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    source = apply_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--review", default=None,
                        help="校对回执 JSON；与 --accept all|<no1,no2> 一起用")
    source.add_argument("--fixes", default=None,
                        help="用户敲定的批准文件 JSON（自带 fixes；可改措辞）")
    apply_parser.add_argument("--accept", default=None, metavar="all|N,N",
                              help="接受哪些中文建议：all 或 005,006（配 --review）")
    apply_parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


# --- receipt / approved-file readers ---------------------------------------


def load_review(path: str, doc_id: str) -> list:
    """Read a proofreader receipt and return its raw ``zh_fixes`` array."""
    review = common.read_json(path)
    if not isinstance(review, dict):
        raise common.Failure(common.EXIT_FAIL, "校对回执不是 JSON 对象：%s" % path)
    if review.get("doc") not in (None, doc_id):
        raise common.Failure(common.EXIT_FAIL, "校对回执 doc 不是 %r：%s（%r）"
                             % (doc_id, path, review.get("doc")))
    raw = review.get("zh_fixes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise common.Failure(common.EXIT_FAIL,
                             "校对回执的 zh_fixes 必须是数组：%s" % path)
    return raw


def load_approved(path: str, doc_id: str) -> list:
    """Read the user-settled approved file and return its raw ``fixes`` array."""
    data = common.read_json(path)
    if not isinstance(data, dict):
        raise common.Failure(common.EXIT_FAIL, "批准文件不是 JSON 对象：%s" % path)
    if data.get("doc") not in (None, doc_id):
        raise common.Failure(common.EXIT_FAIL, "批准文件 doc 不是 %r：%s（%r）"
                             % (doc_id, path, data.get("doc")))
    fixes = data.get("fixes")
    if fixes is None:
        raise common.Failure(common.EXIT_FAIL, "批准文件缺少 fixes 数组：%s" % path)
    if not isinstance(fixes, list):
        raise common.Failure(common.EXIT_FAIL, "批准文件的 fixes 必须是数组：%s" % path)
    return fixes


# --- shared context ---------------------------------------------------------


def load_context(run_dir: str, doc_id: str) -> dict:
    """Load run/doc + the current 6-column manifest (``$RUN`` first)."""
    run = common.load_run(run_dir)
    doc = common.load_doc(run_dir, doc_id)
    doc_path = common.doc_dir(run_dir, doc_id)
    work = run.get("work", "")
    manifest_path = cards_apply.resolve_six_manifest(doc_path, work, doc)
    parsed = common.parse_manifest_6col(manifest_path)
    return {
        "run": run,
        "doc": doc,
        "doc_path": doc_path,
        "work": work,
        "manifest_path": manifest_path,
        "parsed": parsed,
        "manifest_sha": common.sha256_text(common.read_text(manifest_path)),
    }


def manifest_rows(parsed: dict) -> list:
    return [{"n": row["n"], "type": row["type"], "keep": row["keep"],
             "position": row["position"], "display": row["display"]}
            for row in parsed["rows"]]


def rows_by_no(parsed: dict) -> dict:
    return {common.fmt_row(row["n"]): row for row in parsed["rows"]}


def current_zh(doc_path: str, parsed: dict) -> tuple:
    """Current Chinese for the table: cards-base → translations → manifest."""
    base_path = os.path.join(doc_path, "cards-base.json")
    if os.path.isfile(base_path):
        base = common.read_json(base_path)
        return ({str(card.get("no")): list(card.get("zh") or [])
                 for card in base.get("cards") or []}, "cards-base.json")
    translations_path = os.path.join(doc_path, "translations.json")
    if os.path.isfile(translations_path):
        data = common.read_json(translations_path)
        return ({common.fmt_row(int(key)): list(value)
                 for key, value in (data.get("rows") or {}).items()},
                "translations.json")
    return ({common.fmt_row(row["n"]): list(row.get("zh") or [])
             for row in parsed["rows"] if row["keep"]}, "6 列清单")


# --- fix validation ---------------------------------------------------------


def validate_fixes(raw: list, where_prefix: str, by_no: dict) -> list:
    """Validate ``[{no, zh, why?}]`` against the manifest keep rows.

    Returns ``[(number, zh_lines, why)]`` in input order.  One entry per row;
    ``len(zh) == sentence_count``; non-empty lines.  Only keep rows may be
    translated (the 6-column manifest leaves 否 rows blank).
    """
    fixes = []
    seen = set()
    for index, fix in enumerate(raw, start=1):
        where = "%s 第 %d 条" % (where_prefix, index)
        if not isinstance(fix, dict):
            raise common.Failure(common.EXIT_FAIL, "%s 不是对象：%r" % (where, fix))
        number = cards_apply.normalise_card_no(fix.get("no"), where)
        row = by_no.get(number)
        if row is None:
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 的行 %r 不在清单里" % (where, number))
        if not row["keep"]:
            raise common.Failure(
                common.EXIT_FAIL,
                "%s 的行 %s 是「进卡组＝否」的 %s 行（中文只能改 keep 行）"
                % (where, number, row["type"]),
                reason="zh_not_keep_row",
            )
        if number in seen:
            raise common.Failure(common.EXIT_FAIL,
                                 "%s：行 %s 出现了两次（一行只能改一次）"
                                 % (where, number))
        expected = common.sentence_count(row["display"])
        zh = cards_apply.check_zh_lines(
            fix.get("zh"), "%s（行 %s）" % (where, number), expected)
        why = fix.get("why") or ""
        if not isinstance(why, str):
            raise common.Failure(common.EXIT_FAIL, "%s 的 why 不是字符串：%r"
                                 % (where, why))
        fixes.append((number, zh, why.strip()))
        seen.add(number)
    return fixes


def select_from_review(raw: list, accept: str) -> list:
    """Pick the ``--accept`` subset (``all`` or ``005,006``) from a receipt."""
    if accept.strip() == "all":
        return list(raw)
    wanted = []
    for part in accept.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise common.Failure(common.EXIT_USAGE,
                                 "--accept 只能是 all 或逗号分隔的卡号：%r" % accept)
        number = common.fmt_row(int(part))
        if number not in wanted:
            wanted.append(number)
    if not wanted:
        raise common.Failure(common.EXIT_USAGE,
                             "--accept 没有给出任何卡号：%r" % accept)
    by_no = {}
    for fix in raw:
        if isinstance(fix, dict):
            try:
                number = cards_apply.normalise_card_no(fix.get("no"), "--review")
            except common.Failure:
                continue
            by_no[number] = fix
    missing = [number for number in wanted if number not in by_no]
    if missing:
        raise common.Failure(
            common.EXIT_FAIL,
            "--accept 里的卡号不在校对回执 zh_fixes 里：%s" % "、".join(missing))
    return [by_no[number] for number in wanted]


# --- table ------------------------------------------------------------------


def command_table(args) -> int:
    """Read-only: print the table, write nothing (no log file either)."""
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "doc": args.doc}
    try:
        run_dir = os.path.abspath(args.run)
        ctx = load_context(run_dir, args.doc)
        raw = load_review(args.review, args.doc)
        fixes = validate_fixes(raw, "校对回执的 zh_fixes", rows_by_no(ctx["parsed"]))
        current_by_no, source = current_zh(ctx["doc_path"], ctx["parsed"])
        entries = [{"no": number, "zh": zh, "why": why}
                   for number, zh, why in fixes]
        text = cards_apply.render_zh_fixes_table(
            args.doc, entries, current_by_no, os.path.basename(args.review))
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "zh_fixes": len(entries),
            "source": source,
            "manifest": ctx["manifest_path"],
            "manifest_sha256_v2": ctx["manifest_sha"],
            "review": os.path.abspath(args.review),
            "rows": entries,
            "table": text,
        })
        if not args.json:
            print(text.rstrip("\n"))
            print()
            print("共 %d 条中文建议（当前中文来自 %s）；只报不改。"
                  % (len(entries), source))
        common.emit_json(result)
        return common.EXIT_OK
    except common.Failure as failure:
        return common.report_failure(STAGE, failure, {"doc": args.doc})
    except OSError as exc:
        return common.report_failure(
            STAGE,
            common.Failure(common.EXIT_FAIL, "文件读写失败：%s" % exc, reason="os_error"),
            {"doc": args.doc},
        )


# --- apply ------------------------------------------------------------------


def write_six_column(ctx: dict, six_text: str) -> list:
    """Write the new 6-column manifest to $RUN, the pack (if any) and 产出区."""
    doc = ctx["doc"]
    doc_path = ctx["doc_path"]
    work = ctx["work"]
    written = []

    def add(path):
        path = os.path.abspath(path)
        if any(os.path.abspath(existing) == path for existing in written):
            return
        common.ensure_dir(os.path.dirname(path))
        common.atomic_write_text(path, six_text)
        written.append(path)

    add(os.path.join(doc_path, "切分清单.md"))
    pack_manifest = None
    if doc.get("pack_manifest"):
        candidate = common.resolve_work_path(work, str(doc["pack_manifest"]))
        if os.path.isfile(candidate):
            pack_manifest = candidate
    if pack_manifest is None and doc.get("pack_dir") and doc.get("stem"):
        candidate = os.path.join(
            common.resolve_work_path(work, str(doc["pack_dir"])),
            "%s_切分清单.md" % doc["stem"])
        if os.path.isfile(candidate):
            pack_manifest = candidate
    if pack_manifest:
        add(pack_manifest)
    out_manifest = None
    if doc.get("manifest"):
        candidate = common.resolve_work_path(work, str(doc["manifest"]))
        if os.path.isfile(candidate) \
                and common.manifest_columns(common.read_text(candidate)) == 6:
            out_manifest = candidate
            add(out_manifest)
    return written, pack_manifest, out_manifest


def command_apply(args) -> int:
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "doc": args.doc}
    log = None
    try:
        run_dir = os.path.abspath(args.run)
        log = common.StageLog(run_dir, STAGE, args.doc)
        if args.fixes is None and not args.accept:
            raise common.Failure(
                common.EXIT_USAGE,
                "--review 必须配 --accept all|005,006；或改用 --fixes 批准文件。")
        if args.fixes is not None and args.accept:
            raise common.Failure(common.EXIT_USAGE,
                                 "--fixes 批准文件不能与 --accept 同时用。")
        ctx = load_context(run_dir, args.doc)
        parsed = ctx["parsed"]
        by_no = rows_by_no(parsed)
        translations_path = os.path.join(ctx["doc_path"], "translations.json")
        if not os.path.isfile(translations_path):
            raise common.Failure(
                common.EXIT_ASK,
                "缺 translations.json：%s（先跑 E5 translate_apply）"
                % translations_path,
                reason="translations_missing",
            )
        translations_doc = common.read_json(translations_path)
        if not isinstance(translations_doc, dict):
            raise common.Failure(common.EXIT_FAIL,
                                 "translations.json 不是 JSON 对象：%s" % translations_path)
        if translations_doc.get("doc") not in (None, args.doc):
            raise common.Failure(common.EXIT_FAIL,
                                 "translations.json 的 doc 不是 %r：%s"
                                 % (args.doc, translations_path))
        recorded = translations_doc.get("manifest_sha256_v2")
        if recorded and recorded != ctx["manifest_sha"]:
            raise common.Failure(
                common.EXIT_FAIL,
                "清单与 translations.json 不一致（清单在 E5 之后被改过？）：\n"
                "  清单：%s\n  translations.json 记录：%s\n先跑 E5 重出清单，不要手改。"
                % (ctx["manifest_sha"], recorded),
                reason="manifest_changed",
            )
        translations = {}
        for key, value in (translations_doc.get("rows") or {}).items():
            if not isinstance(value, list):
                raise common.Failure(common.EXIT_FAIL,
                                     "translations.json 的行 %r 不是数组：%r"
                                     % (key, value))
            try:
                number = int(key)
            except (TypeError, ValueError):
                raise common.Failure(common.EXIT_FAIL,
                                     "translations.json 的行号不是数字：%r" % key)
            translations[str(number)] = list(value)
        six_rows = manifest_rows(parsed)
        live_base = common.strip_manifest_translation_column(
            common.read_text(ctx["manifest_path"]))
        if live_base != common.render_manifest(parsed["stem"], six_rows):
            raise common.Failure(
                common.EXIT_FAIL,
                "6 列清单的前 5 列不是脚本标准渲染（被手改过？）：%s\n"
                "先回 E5 重新出清单，再改中文。" % ctx["manifest_path"],
                reason="manifest_changed",
            )
        keep_rows = [row for row in parsed["rows"] if row["keep"]]
        for row in keep_rows:
            number = str(int(row["n"]))
            if number not in translations:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "translations.json 缺行 %s（先跑 E5 translate_apply）"
                    % common.fmt_row(row["n"]),
                    reason="translations_mismatch",
                )
            if list(row.get("zh") or []) != translations[number]:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "行 %s 的清单译文与 translations.json 不一致（清单被手改过？）"
                    % common.fmt_row(row["n"]),
                    reason="manifest_changed",
                )

        if args.fixes is not None:
            raw = load_approved(args.fixes, args.doc)
            where = "批准文件的 fixes"
        else:
            raw = select_from_review(load_review(args.review, args.doc), args.accept)
            where = "校对回执的 zh_fixes"
        fixes = validate_fixes(raw, where, by_no)

        changed_rows = []
        for number, zh, _why in fixes:
            key = str(int(number))
            if translations.get(key) != zh:
                changed_rows.append(number)
            translations[key] = zh
        six_text = common.render_manifest_6col(
            parsed["stem"], six_rows,
            {int(key): value for key, value in translations.items()})
        sha = common.sha256_text(six_text)

        written, pack_manifest, out_manifest = write_six_column(ctx, six_text)
        for path in written:
            if common.sha256_file(path) != sha:
                raise common.Failure(common.EXIT_FAIL,
                                     "6 列清单写盘后 sha 不一致：%s" % path)
        check = common.parse_manifest_6col(
            os.path.join(ctx["doc_path"], "切分清单.md"))
        for row in check["rows"]:
            want = translations.get(str(int(row["n"])), []) if row["keep"] else []
            if list(row.get("zh") or []) != want:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "6 列清单读回后行 %s 的译文对不上：%r ≠ %r"
                    % (common.fmt_row(row["n"]), row.get("zh"), want))

        translations_doc["rows"] = {str(int(key)): list(value)
                                    for key, value in translations.items()}
        translations_doc["manifest_sha256_v2"] = sha
        stats = translations_doc.get("stats")
        if not isinstance(stats, dict):
            stats = {}
            translations_doc["stats"] = stats
        stats["fixes"] = int(stats.get("fixes") or 0) + len(changed_rows)
        common.atomic_write_json(translations_path, translations_doc)
        ctx["doc"]["manifest_sha256_v2"] = sha
        if pack_manifest:
            ctx["doc"]["pack_manifest"] = pack_manifest
        common.atomic_write_json(os.path.join(ctx["doc_path"], "doc.json"), ctx["doc"])
        warnings = []
        if pack_manifest:
            try:
                translate_apply.refresh_pack_manifest_sha(
                    ctx["doc_path"], pack_manifest, sha)
            except (common.Failure, OSError) as exc:
                warnings.append("包内清单已更新，但 pack.json 的 sha 刷新失败：%s" % exc)

        log("applied %d fixes: changed_rows=%s sha=%s"
            % (len(fixes), ",".join(changed_rows), sha))
        for path in written:
            log("wrote %s" % path)
        for warning in warnings:
            log("WARN " + warning)
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "applied": len(fixes),
            "changed": len(changed_rows),
            "changed_rows": changed_rows,
            "manifest_sha256_v2": sha,
            "manifest": os.path.join(ctx["doc_path"], "切分清单.md"),
            "pack_manifest": pack_manifest,
            "out_manifest": out_manifest,
            "written": written,
            "translations_path": translations_path,
            "need_approve": True,
            "need_refresh": True,
            "warnings": warnings,
        })
        if not args.json:
            print("DOC=%s  中文直改：应用 %d 条（真正变更 %d 行）"
                  % (args.doc, len(fixes), len(changed_rows)))
            print("6 列清单：%s" % os.path.join(ctx["doc_path"], "切分清单.md"))
            if pack_manifest:
                print("包内清单：%s（pack.json sha 已同步）" % pack_manifest)
            if out_manifest:
                print("产出区清单：%s" % out_manifest)
            print("清单 sha256：%s" % sha)
            print("下一步：approve.py（用户已敲定）→ cards_apply.py --refresh-zh → gate e9")
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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "table":
        return command_table(args)
    return command_apply(args)


if __name__ == "__main__":
    sys.exit(main())
