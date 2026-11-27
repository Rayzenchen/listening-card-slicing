#!/usr/bin/env python3
"""cards_build.py -- E9: derive the card structure (cards-base.json + card-tasks.json).

Everything in a card is deterministic except the English line breaks, so E9
splits in two: this script derives order / title / family / question number /
Chinese lines / English sentences from the 6-column manifest +
``translations.json``, and the layouter subagents only add line breaks later.

Card IDs (第二期接口 §4.1):

* stem ``【CET-4】25年06月-卷1`` -> ``chapter`` = ``精听：25年06月-卷1``,
  ``prefix`` = ``Y25M06``, ``volume`` = roman numeral (``I``);
* body rows: 位置 ``Section A / News Report 1 (Q1-2)`` -> family ``NR1`` and
  the family's own sequence number in manifest order;
  ``Conversation k`` -> ``Cs``, ``Passage k`` -> ``Pa``;
* question rows: ``Q<NN>`` from 位置 (``Q1``) or from ``Question N.``;
* title = ``【<prefix>-\\[<volume>\\]_<family>】`` (the escapes are stored as
  they are rendered, 第二期接口 §5).

If the stem cannot be parsed, the script exits 3 and asks the user for
``--id-scheme FILE`` instead of guessing.

0.0.4 E9 定稿前置: a six-column manifest is not enough — ``approve.json`` must
bind its **current bytes** with ``mode=user`` (written by ``approve.py`` at the
end of E5).  Missing/stale token = exit 3 and *nothing* is written, so cards can
never be derived from a manifest the user did not finalize.

Exit codes: 0 base + tasks written / 1 the manifest or translations are
inconsistent / 2 usage or unreadable files / 3 the ID scheme cannot be derived,
or the 6-column manifest / translations.json / approve.json are not there yet.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402

STAGE = "e9"
DEFAULT_CHUNK = 6
BRIEF_ANCHOR = "references/subagent-briefs.md#layouter"

_STEM_RE = re.compile(r"^【(?P<exam>[^】]+)】(?P<yy>\d{2})年(?P<mm>\d{2})月-卷(?P<volume>\d+)$")
_BODY_FAMILY_RE = re.compile(r"^(News Report|Conversation|Passage)\s+(\d+)\b")
_Q_POSITION_RE = re.compile(r"\bQ(\d+)\b")
_ROMAN_PREFIX_RE = re.compile(r"^[IVXLC]+$")
_PREFIX_RE = re.compile(r"^Y\d{2}M\d{2}$")

_FAMILY_CODE = {"News Report": "NR", "Conversation": "Cs", "Passage": "Pa"}
_ROMAN = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "cards_build.py",
        "E9：由 6 列清单 + translations.json 派生卡片 ID/顺序/家族/题目号，"
        "写 cards-base.json 与断行任务 card-tasks.json。",
        epilog="名字里推不出卷号 → exit 3（用 --id-scheme 指定 chapter/prefix/volume）；"
               "清单或译文不一致 → exit 1。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                        help="每个排版任务几张卡（默认 %d）" % DEFAULT_CHUNK)
    parser.add_argument("--id-scheme", default=None,
                        help='覆盖 ID 方案：{"chapter":"…","prefix":"Y25M06","volume":"I"}')
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


def roman(number: int) -> str:
    """1 -> I, 2 -> II, 4 -> IV (subtractive, enough for 卷号 1..3999)."""
    if number < 1 or number > 3999:
        raise common.Failure(common.EXIT_ASK, "卷号 %d 超出罗马数字范围（1–3999）" % number)
    value = number
    parts = []
    for amount, numeral in _ROMAN:
        while value >= amount:
            parts.append(numeral)
            value -= amount
    return "".join(parts)


def derive_scheme(stem: str) -> dict:
    """Derive ``{chapter, prefix, volume}`` from the frozen stem shape."""
    match = _STEM_RE.match(str(stem))
    if not match:
        raise common.Failure(
            common.EXIT_ASK,
            "从名字 %r 推不出卷号：应形如「【CET-4】25年06月-卷1」。\n"
            '请用 --id-scheme FILE 指定 {"chapter":"精听：25年06月-卷1",'
            '"prefix":"Y25M06","volume":"I"}。' % stem,
            reason="id_scheme_unknown",
        )
    yy, mm = match.group("yy"), match.group("mm")
    volume_number = int(match.group("volume"))
    return {
        "chapter": "精听：%s年%s月-卷%d" % (yy, mm, volume_number),
        "prefix": "Y%sM%s" % (yy, mm),
        "volume": roman(volume_number),
    }


def load_scheme(path: str, stem: str) -> dict:
    """``--id-scheme FILE`` overrides the stem-derived scheme (all keys used)."""
    data = common.read_json(path)
    if not isinstance(data, dict):
        raise common.Failure(common.EXIT_USAGE, "--id-scheme 不是 JSON 对象：%s" % path)
    scheme = {}
    for key in ("chapter", "prefix", "volume"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise common.Failure(common.EXIT_USAGE,
                                 "--id-scheme 缺少字符串字段 %r：%s" % (key, path))
        scheme[key] = value.strip()
    if not _PREFIX_RE.match(scheme["prefix"]):
        raise common.Failure(common.EXIT_ASK,
                             "--id-scheme 的 prefix 应形如 Y25M06：%r" % scheme["prefix"])
    if not _ROMAN_PREFIX_RE.match(scheme["volume"]):
        raise common.Failure(common.EXIT_ASK,
                             "--id-scheme 的 volume 应是罗马数字（I/II/III…）：%r"
                             % scheme["volume"])
    scheme["source"] = "--id-scheme:%s" % os.path.basename(path)
    scheme["stem"] = stem
    return scheme


def derive_family(row: dict, family_counts: dict) -> tuple:
    """Return ``(family, question_number)`` for one keep row."""
    position = row["position"]
    last_part = position.rsplit(" / ", 1)[-1].strip()
    if row["type"] == "题目":
        question = common.question_number(row["display"])
        from_position = _Q_POSITION_RE.search(position)
        position_number = int(from_position.group(1)) if from_position else None
        if question is None and position_number is None:
            raise common.Failure(
                common.EXIT_ASK,
                "行 %s 是题目，但位置 %r 和原文里都找不到题目号（Q1 / Question 1.）"
                % (common.fmt_row(row["n"]), position),
                reason="question_number_unknown",
            )
        if question is not None and position_number is not None and question != position_number:
            raise common.Failure(
                common.EXIT_FAIL,
                "行 %s 的题目号不一致：位置里是 Q%d，原文里是 Question %d"
                % (common.fmt_row(row["n"]), position_number, question),
            )
        number = question if question is not None else position_number
        return "Q%02d" % number, number
    match = _BODY_FAMILY_RE.match(last_part)
    if not match:
        raise common.Failure(
            common.EXIT_ASK,
            "行 %s 的位置 %r 不是「Section X / News Report 1 (Q1-2)」「Conversation 1」"
            "「Passage 1」这种形状，推不出卡片家族（NR/Cs/Pa）。请让 E4 重新给出位置列。"
            % (common.fmt_row(row["n"]), position),
            reason="family_unknown",
        )
    code = _FAMILY_CODE[match.group(1)]
    base = "%s%s" % (code, int(match.group(2)))
    family_counts[base] = family_counts.get(base, 0) + 1
    return "%s-%d" % (base, family_counts[base]), None


def build_cards(parsed: dict, translations: dict, scheme: dict,
                audio_ext: str = common.SEGMENT_EXT) -> list:
    """One card per keep row, in manifest order, with IDs and sentences."""
    family_counts = {}
    cards = []
    for order, row in enumerate([row for row in parsed["rows"] if row["keep"]], start=1):
        number = common.fmt_row(row["n"])
        family, question = derive_family(row, family_counts)
        sentences, speakers = common.split_sentences_with_speakers(row["display"])
        if not sentences:
            raise common.Failure(common.EXIT_FAIL, "行 %s 的英文切不出句子" % number)
        zh = translations[number]
        if len(zh) != len(sentences):
            raise common.Failure(
                common.EXIT_FAIL,
                "行 %s 的中文 %d 行 ≠ 英文 %d 句（先修 E5 回执，不要手改清单）"
                % (number, len(zh), len(sentences)),
            )
        cards.append({
            "order": order,
            "no": number,
            "kind": row["type"],
            "title": "【%s-\\[%s\\]_%s】" % (scheme["prefix"], scheme["volume"], family),
            "family": family,
            "question": question,
            "zh": zh,
            "en": sentences,
            "speakers": speakers,
            "audio": "%s%s" % (number, audio_ext),
            "position": row["position"],
        })
    return cards


def check_cards(cards: list, parsed: dict) -> dict:
    """Re-derive the invariants gate e9 will recompute from disk."""
    keep_count = sum(1 for row in parsed["rows"] if row["keep"])
    if len(cards) != keep_count:
        raise common.Failure(common.EXIT_FAIL,
                             "卡片数 %d ≠ keep 行数 %d" % (len(cards), keep_count))
    orders = [card["order"] for card in cards]
    if orders != list(range(1, len(cards) + 1)):
        raise common.Failure(common.EXIT_FAIL, "卡片顺序不是 1..%d：%r"
                             % (len(cards), orders[:10]))
    titles = [card["title"] for card in cards]
    if len(set(titles)) != len(titles):
        duplicates = sorted({title for title in titles if titles.count(title) > 1})
        raise common.Failure(common.EXIT_FAIL, "卡片标题重复：%s" % "、".join(duplicates))
    by_base = {}
    for card in cards:
        family = card["family"]
        if family.startswith("Q"):
            continue
        base, sequence = family.rsplit("-", 1)
        by_base.setdefault(base, set()).add(int(sequence))
    for base, sequences in by_base.items():
        if sequences != set(range(1, len(sequences) + 1)):
            raise common.Failure(
                common.EXIT_FAIL,
                "家族 %s 的序号不是从 1 连续：%s"
                % (base, "、".join("%d" % number for number in sorted(sequences))))
    questions = [card["question"] for card in cards if card["question"] is not None]
    if len(set(questions)) != len(questions):
        raise common.Failure(common.EXIT_FAIL, "题目号重复：%r" % questions)
    groups = sorted({
        "Q" if card["family"].startswith("Q") else card["family"].rsplit("-", 1)[0]
        for card in cards
    })
    return {"cards": len(cards), "families": len(groups), "questions": len(questions)}


def require_manifest_finalized(doc_path: str, doc_id: str, manifest_path: str) -> dict:
    """E9 定稿前置：没有 E5 用户令牌就不许派生卡片结构（0.0.4）。

    The 6-column manifest is the E5 deliverable; approve.json is the token that
    the **user** accepted those very bytes.  Deriving cards from a manifest that
    was never finalized (or was edited afterwards) would silently push a
    different text to the cloud, so this is exit 3 (ask the user), before any
    product is written.
    """
    approve_path = os.path.join(doc_path, "approve.json")
    if not os.path.isfile(approve_path):
        raise common.Failure(
            common.EXIT_ASK,
            "缺 approve.json（E5 定稿令牌）：%s\n"
            "先把 6 列清单交给用户定稿，在 E5 末尾跑 approve.py，再跑 cards_build。"
            % approve_path,
            reason="manifest_not_finalized",
        )
    approve = common.read_json(approve_path)
    if not isinstance(approve, dict):
        raise common.Failure(common.EXIT_ASK,
                             "approve.json 不是 JSON 对象：%s" % approve_path,
                             reason="manifest_not_finalized")
    if approve.get("doc") != doc_id:
        raise common.Failure(
            common.EXIT_ASK,
            "approve.json 的 doc=%r 不是 %s；在 E5 末尾重新跑 approve.py"
            % (approve.get("doc"), doc_id),
            reason="manifest_not_finalized",
        )
    if approve.get("mode") != "user":
        raise common.Failure(
            common.EXIT_ASK,
            "approve.json 的 mode=%r（应为 user）：清单只能由用户定稿；"
            "在 E5 末尾重新跑 approve.py" % approve.get("mode"),
            reason="manifest_not_finalized",
        )
    current_sha = common.sha256_file(manifest_path)
    if approve.get("manifest_sha256") != current_sha:
        raise common.Failure(
            common.EXIT_ASK,
            "approve.json 绑定的清单 sha 与当前 6 列清单不符（定稿后清单被改过？）。\n"
            "当前：%s\n批准：%s\n请回 E5 重新交付定稿，再跑 cards_build。"
            % (current_sha, approve.get("manifest_sha256")),
            reason="manifest_not_finalized",
        )
    return approve


def task_payload(doc_id: str, cards: list, chunk: int) -> list:
    """Cut the cards into ``d01-c01``-style layouter tasks (deterministic)."""
    tasks = []
    for start in range(0, len(cards), chunk):
        index = len(tasks) + 1
        name = "%s-c%02d" % (doc_id, index)
        tasks.append({
            "task": name,
            "receipt": "cards/%s.json" % name,
            "brief": BRIEF_ANCHOR,
            "cards": [
                {"no": card["no"], "order": card["order"],
                 "title": card["title"], "en": card["en"]}
                for card in cards[start:start + chunk]
            ],
        })
    return tasks


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.chunk < 1:
        print("ERROR: --chunk 必须 ≥ 1（收到 %r）" % args.chunk, file=sys.stderr)
        common.emit_json({"ok": False, "exit_code": common.EXIT_USAGE,
                          "stage": STAGE, "error": "chunk 必须 ≥ 1"})
        return common.EXIT_USAGE
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "doc": args.doc}
    try:
        run = common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        log = common.StageLog(run_dir, STAGE, args.doc)
        doc_path = common.doc_dir(run_dir, args.doc)
        work = run.get("work", "")
        manifest_path = common.resolve_manifest_path(work, doc)
        if common.manifest_columns(common.read_text(manifest_path)) != 6:
            raise common.Failure(
                common.EXIT_ASK,
                "清单还没有「译文」列：%s（先跑 translate_apply 再跑 cards_build）"
                % manifest_path,
                reason="manifest_not_extended",
            )
        # 0.0.4 E9 定稿前置：清单是 6 列还不够，必须有用户定稿令牌 approve.json
        approve = require_manifest_finalized(doc_path, args.doc, manifest_path)
        parsed = common.parse_manifest_6col(manifest_path)
        translations_path = os.path.join(doc_path, "translations.json")
        if not os.path.isfile(translations_path):
            raise common.Failure(
                common.EXIT_ASK,
                "缺 translations.json：%s（先跑 translate_apply）" % translations_path,
                reason="translations_missing",
            )
        translations_doc = common.read_json(translations_path)
        manifest_sha = common.sha256_file(manifest_path)
        recorded = translations_doc.get("manifest_sha256_v2")
        if recorded and recorded != manifest_sha:
            raise common.Failure(
                common.EXIT_FAIL,
                "清单与 translations.json 不一致（清单在 E5 之后被改过？）：sha %s ≠ %s"
                % (manifest_sha, recorded),
                reason="manifest_changed",
            )
        translations = {}
        for key, value in (translations_doc.get("rows") or {}).items():
            if not isinstance(value, list):
                raise common.Failure(common.EXIT_FAIL,
                                     "translations.json 的行 %r 不是数组：%r" % (key, value))
            try:
                translations[common.fmt_row(int(key))] = list(value)
            except (TypeError, ValueError):
                raise common.Failure(common.EXIT_FAIL,
                                     "translations.json 的行号不是数字：%r" % key)
        keep_rows = [row for row in parsed["rows"] if row["keep"]]
        for row in keep_rows:
            number = common.fmt_row(row["n"])
            if number not in translations:
                raise common.Failure(common.EXIT_FAIL,
                                     "translations.json 缺行 %s" % number)
            if row["zh"] != translations[number]:
                raise common.Failure(
                    common.EXIT_FAIL,
                    "行 %s 的清单译文与 translations.json 不一致（清单被手改过？）" % number,
                )

        scheme = load_scheme(args.id_scheme, doc["stem"]) if args.id_scheme \
            else derive_scheme(doc["stem"])
        # E9 audio names follow the delivered pack (mp3 for new packs, wav for
        # old ones); an absent pack falls back to the new default.
        audio_ext = common.cards_audio_ext(work, doc)
        cards = build_cards(parsed, translations, scheme, audio_ext)
        stats = check_cards(cards, parsed)
        tasks = task_payload(args.doc, cards, args.chunk)

        base_doc = {
            "version": 1,
            "doc": args.doc,
            "stem": doc["stem"],
            "chapter": scheme["chapter"],
            "id_scheme": {
                "prefix": scheme["prefix"],
                "volume": scheme["volume"],
                "source": scheme.get("source", "stem"),
            },
            "manifest": manifest_path,
            "manifest_sha256_v2": manifest_sha,
            "counts": stats,
            "cards": cards,
        }
        task_doc = {
            "version": 1,
            "doc": args.doc,
            "stage": STAGE,
            "generated_at": common.now_iso(),
            "brief": BRIEF_ANCHOR,
            "doc_dir": doc_path,
            "receipt_dir": os.path.join(doc_path, "cards"),
            "chunk": args.chunk,
            "counts": {"cards": len(cards), "tasks": len(tasks)},
            "tasks": tasks,
        }
        base_path = os.path.join(doc_path, "cards-base.json")
        tasks_path = os.path.join(doc_path, "card-tasks.json")
        common.atomic_write_json(base_path, base_doc)
        common.ensure_dir(os.path.join(doc_path, "cards"))
        common.atomic_write_json(tasks_path, task_doc)
        log("scheme=%r cards=%d tasks=%d chunk=%d"
            % (scheme, len(cards), len(tasks), args.chunk))
        log("wrote %s" % base_path)
        log("wrote %s" % tasks_path)

        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "chapter": scheme["chapter"],
            "id_scheme": base_doc["id_scheme"],
            "approve_manifest_sha256": approve.get("manifest_sha256"),
            "cards": len(cards),
            "tasks": len(tasks),
            "chunk": args.chunk,
            "families": stats["families"],
            "questions": stats["questions"],
            "first_title": cards[0]["title"],
            "last_title": cards[-1]["title"],
            "cards_base": base_path,
            "tasks_path": tasks_path,
            "receipt_dir": task_doc["receipt_dir"],
            "task_ids": [task["task"] for task in tasks],
        })
        if not args.json:
            print("DOC=%s  章节=%s  前缀=%s  卷=%s"
                  % (args.doc, scheme["chapter"], scheme["prefix"], scheme["volume"]))
            print("卡片：%d 张（家族 %d 个，题目 %d 道）  任务：%d 个（每任务 ≤%d 张）"
                  % (len(cards), stats["families"], stats["questions"],
                     len(tasks), args.chunk))
            print("首/末标题：%s / %s" % (cards[0]["title"], cards[-1]["title"]))
            print("已写：%s" % base_path)
            print("任务单：%s" % tasks_path)
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
