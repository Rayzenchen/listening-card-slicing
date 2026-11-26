#!/usr/bin/env python3
"""approve.py -- E5 定稿: bind the 6-column manifest sha into approve.json.

The human checkpoint sits at the end of E5, **before** E6 cuts any audio: the
agent presents the whole six-column manifest (translations included), the user
accepts it (or asks for fixes), and only then does this script write the token
that lets E6 cut.

The script is deliberately tiny and dumb:

* it reads the **$RUN** 6-column manifest (``$RUN/docs/<id>/切分清单.md``,
  written by translate_apply before any pack exists); only when that file does
  not exist does it fall back to the delivered pack copy
  (``doc.json.pack_manifest`` → ``doc.pack_dir/<stem>_切分清单.md``);
* it refuses with exit 3 unless that manifest is the six-column one (so E5 has
  really produced the translations);
* it writes ``approve.json`` atomically:

      {"version":1,"doc":"d01","mode":"user","manifest_sha256":"…",
       "rows":[{"n":1,"sha256":"…"},…],"approved_at":"…","by":"user"}

``manifest_sha256`` is re-hashed from the current manifest, so any later edit
invalidates the token (gate ``e9`` compares it again).  0.0.5 also records
``rows``: one sha256 per 6-column table row (see
``common.manifest_row_hashes`` for the exact formula: six cells as JSON
``[n, type, keep, position, display, zh]``, compact separators, UTF-8).
Gate ``e9`` recomputes those hashes and reports the first changed row.  When
``doc.json.manifest_sha256_v2`` disagrees the approval still succeeds but a
warning is printed; that field is only the E5 bookkeeping copy.

Re-running is idempotent (same sha; ``approved_at`` is refreshed).  Only the
user can finalise the manifest, so there is no ``--mode`` switch: the token is
always written with ``"mode":"user"`` / ``"by":"user"``.

Exit codes: 0 written / 1 I/O or doc.json failure / 2 usage / 3 the 6-column
manifest is missing or still five columns (run E5 translate_apply first).
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

STAGE = "e5"


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "approve.py",
        "E5 定稿：把当前 6 列清单的 sha256 绑定进 approve.json（切分前的★闸门）。",
        epilog="清单还不是 6 列 → exit 3（先跑 E5 translate_apply）。"
               "定稿只有用户一种方式，approve.json 固定写 mode=user。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
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
        work = run.get("work", "")
        # 0.0.8: E5 在切分/分包之前，$RUN 6 列清单才是定稿对象；只有它不存在时
        # 才回退包内 6 列（入口 B / cleanup 之后）。
        run_manifest = os.path.join(common.doc_dir(run_dir, args.doc), "切分清单.md")
        if os.path.isfile(run_manifest):
            manifest_path = run_manifest
        else:
            try:
                manifest_path = common.resolve_final_manifest_path(work, doc)
            except common.Failure as exc:
                raise common.Failure(
                    exc.code,
                    "找不到 6 列清单（$RUN 没有、包内也没有）。先跑 E5："
                    "translate_prepare → 翻译员 → translate_apply 会写 $RUN 与产出区。",
                    reason="manifest6_missing",
                )
        manifest_text = common.read_text(manifest_path)
        if common.manifest_columns(manifest_text) != 6:
            raise common.Failure(
                common.EXIT_ASK,
                "清单还不是 6 列：%s\n先跑 E5（translate_prepare → 翻译员 → "
                "translate_apply），定稿只能发生在 6 列清单上。" % manifest_path,
                reason="manifest_not_6col",
            )
        manifest_sha = common.sha256_text(manifest_text)
        rows = common.manifest_row_hashes(manifest_text)
        warnings = []
        recorded_sha = doc.get("manifest_sha256_v2")
        if recorded_sha and recorded_sha != manifest_sha:
            warnings.append(
                "doc.json.manifest_sha256_v2（%s）与当前 6 列清单（%s）不一致；"
                "本次按当前清单绑定，建议回 E5 重跑 translate_apply 核对"
                % (recorded_sha, manifest_sha))
        approve = {
            "version": 1,
            "doc": args.doc,
            "mode": "user",
            "manifest_sha256": manifest_sha,
            "rows": rows,
            "approved_at": common.now_iso(),
            "by": "user",
        }
        approve_path = os.path.join(common.doc_dir(run_dir, args.doc), "approve.json")
        common.atomic_write_json(approve_path, approve)
        log("mode=user manifest_sha256=%s rows=%d" % (manifest_sha, len(rows)))
        for warning in warnings:
            log("WARN " + warning)
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "mode": "user",
            "by": "user",
            "manifest_sha256": manifest_sha,
            "rows": len(rows),
            "manifest_path": manifest_path,
            "approve_path": approve_path,
            "approved_at": approve["approved_at"],
            "warnings": list(warnings),
        })
        if not args.json:
            print("DOC=%s  已定稿（E5 用户确认；E6 可以切分了）" % args.doc)
            print("清单：%s" % manifest_path)
            print("清单 sha256：%s（%d 行行哈希）" % (manifest_sha, len(rows)))
            print("已写：%s" % approve_path)
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


if __name__ == "__main__":
    sys.exit(main())
