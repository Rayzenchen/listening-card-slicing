#!/usr/bin/env python3
"""translate_memory.py -- D2: collect previous 译文 into a reusable memory file.

0.0.10 P4: after the user changes the grouping, E4 rewrites the 5-column
manifest and E5 used to re-translate **every** keep row.  The translations of
the previous round are still on disk, though: ``assemble.py`` copies the old
6-column 产出区 manifest to ``$RUN/docs/<doc>/manifest-6col.prev-<ts>.md``
before overwriting it (SPEC §2.3).  This script turns such a manifest into
``$RUN/docs/<doc>/translations-memory.json``:

    {"version":1, "doc":"d01",
     "entries":[{"en":"<原文 cell 逐字>", "zh":["…","…"],
                 "source":"manifest-6col.prev-20261126-112502.md",
                 "sentences":[{"en":"<句子，去掉 **M:**/**W:**>",
                               "speaker":"M|W|", "zh":"<中文行，去掉 M:/W:>"}]}],
     "stats":{"entries":N, ...}}

``sentences`` 是 0.0.10 阶段 3 的句级记忆（每行可选）：拆分后的单句新行
可以按句复用中文；``len(sentences) != len(zh)`` 的行不写 ``sentences``，
只保留整行配对，绝不对不上硬配。

Rules (SPEC §3.2):

* the source is the **latest** ``manifest-6col.prev-*.md``; ``--from PATH``
  names one explicitly, and as a last resort the current 产出区
  ``<stem>_切分清单.md`` is used when it is already 6 columns;
* parse/render reuse ``common.parse_manifest_6col`` / friends — no second
  manifest format is invented here;
* the same ``en`` appearing twice keeps the **last** entry; an empty ``zh``
  is not recorded;
* nothing is written when no readable 6-column manifest exists: exit 3 with
  「没有可复用译文，正常派翻译即可」.  A bad memory file is never created.

``translate_prepare.py`` calls :func:`ensure_memory` automatically (it is the
default path); this CLI exists for troubleshooting and for re-snapshotting by
hand, e.g. after checking which prev archive was used.

Exit codes: 0 memory written / 1 the source manifest is broken / 2 usage or
missing prior artifacts / 3 no 6-column manifest at all (just dispatch the
translators).
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

STAGE = "e5"
MEMORY_NAME = "translations-memory.json"
MEMORY_VERSION = 1
#: ``assemble.py`` (D1) names the archived 6-column manifest like this.
PREV_RE = re.compile(r"^manifest-6col\.prev-.*\.md$")


def memory_path(run_dir: str, doc_id: str) -> str:
    """``$RUN/docs/<doc>/translations-memory.json`` (the one frozen path)."""
    return os.path.join(common.doc_dir(run_dir, doc_id), MEMORY_NAME)


def prev_manifests(doc_path: str) -> list:
    """Every archived 6-column manifest, newest first (mtime, then name)."""
    if not os.path.isdir(doc_path):
        return []
    found = []
    for name in os.listdir(doc_path):
        path = os.path.join(doc_path, name)
        if not PREV_RE.match(name) or not os.path.isfile(path):
            continue
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            continue
        found.append((stamp, name, path))
    found.sort(reverse=True)
    return [path for _stamp, _name, path in found]


def is_six_column(path: str) -> bool:
    """True when ``path`` is readable and carries the 译文 column."""
    try:
        return common.manifest_columns(common.read_text(path)) == 6
    except (common.Failure, OSError):
        return False


def current_manifest_candidates(work: str, doc: dict) -> list:
    """The live manifests to fall back on: doc.json.manifest → pack copy."""
    candidates = []
    for key in ("manifest", "pack_manifest"):
        raw = doc.get(key)
        if raw:
            candidates.append(common.resolve_work_path(work, str(raw)))
    if doc.get("pack_dir") and doc.get("stem"):
        pack_dir = common.resolve_work_path(work, str(doc["pack_dir"]))
        candidates.append(os.path.join(pack_dir, "%s_切分清单.md" % doc["stem"]))
    unique = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def choose_source(run_dir: str, doc_id: str, work: str, doc: dict,
                  from_path: str | None = None) -> tuple:
    """Return ``(path, why)`` of the 6-column manifest to snapshot.

    Priority (SPEC §3.2): newest ``manifest-6col.prev-*`` → ``--from PATH`` →
    current 产出区 manifest (only when already 6 columns).  An explicit
    ``--from`` is treated as authoritative so the troubleshooting entrance can
    point at any file on purpose.
    """
    doc_path = common.doc_dir(run_dir, doc_id)
    tried = []
    if from_path:
        path = os.path.abspath(os.path.expanduser(str(from_path)))
        if not os.path.isfile(path):
            raise common.Failure(common.EXIT_USAGE, "找不到 --from 清单：%s" % path)
        if not is_six_column(path):
            raise common.Failure(
                common.EXIT_ASK,
                "没有可复用译文（--from 清单不是 6 列，缺「译文」列）：%s\n"
                "正常派翻译即可（本次全部按新译处理）。" % path,
                reason="memory_unavailable",
            )
        return path, "--from"
    for path in prev_manifests(doc_path):
        if is_six_column(path):
            return path, "prev"
        tried.append("不是 6 列：%s" % path)
    for path in current_manifest_candidates(work, doc):
        if os.path.isfile(path):
            if is_six_column(path):
                return path, "current"
            tried.append("不是 6 列：%s" % path)
    detail = ("\n找过：\n  " + "\n  ".join(tried)) if tried else ""
    raise common.Failure(
        common.EXIT_ASK,
        "没有可复用译文：没有找到 6 列旧清单"
        "（$RUN/docs/%s/manifest-6col.prev-*.md 或已带译文列的当前清单）。%s\n"
        "正常派翻译即可（本次全部按新译处理）。" % (doc_id, detail),
        reason="memory_unavailable",
    )


_ZH_SPEAKER_RE = re.compile(r"^(?:M|W):\s*")


def strip_zh_speaker(line: str) -> str:
    """Drop a leading ``M: `` / ``W: `` from a Chinese line (口径: 前缀不是译文)."""
    return _ZH_SPEAKER_RE.sub("", str(line), count=1)


def sentences_for_row(display: str, zh) -> list | None:
    """Sentence-level memory of one 6-column row, or ``None`` when unusable.

    D2 阶段 3 (SPEC §3 增补): when a merged row is later split into atomic
    rows, the whole-row ``en`` no longer matches anything but the individual
    sentences (and their Chinese lines) do.  ``common.split_sentences_with_speakers``
    is the single source of truth for the split, so the sentence count always
    agrees with the E5 receipt rule (``len(zh) == sentence_count``); when it
    does not, the whole row is skipped (整行记忆仍保留) — never guess a pairing.

    Each sentence is ``{"en": <sentence without **M:**/**W:**>,
    "speaker": "M"|"W"|"", "zh": <Chinese line without the leading M:/W:>}``.
    """
    sentences, speakers = common.split_sentences_with_speakers(display)
    if not sentences or len(sentences) != len(zh):
        return None
    pairs = []
    for sentence, speaker, line in zip(sentences, speakers, zh):
        text = str(sentence).strip()
        chinese = strip_zh_speaker(line).strip()
        if not text or not chinese:
            return None
        pairs.append({"en": text,
                      "speaker": speaker if speaker in ("M", "W") else "",
                      "zh": chinese})
    return pairs


def build_memory(doc_id: str, source_path: str) -> dict:
    """Parse one 6-column manifest into the memory document (SPEC §3.2)."""
    parsed = common.parse_manifest_6col(source_path)
    entries = {}
    keep = empty = duplicates = 0
    for row in parsed["rows"]:
        if not row.get("keep"):
            continue
        keep += 1
        zh = [str(line) for line in (row.get("zh") or [])]
        if not zh or any(not line.strip() for line in zh):
            empty += 1
            continue
        en = row["display"]
        if en in entries:
            duplicates += 1
        # 同一 en 保留最后一条：dict 赋值天然做到（顺序保留第一次出现的位置）
        entry = {"en": en, "zh": zh,
                 "source": os.path.basename(source_path)}
        sentences = sentences_for_row(en, zh)
        if sentences:
            entry["sentences"] = sentences
        entries[en] = entry
    return {
        "version": MEMORY_VERSION,
        "doc": doc_id,
        "generated_at": common.now_iso(),
        "source": os.path.basename(source_path),
        "entries": list(entries.values()),
        "stats": {"entries": len(entries), "keep": keep,
                  "empty_zh": empty, "duplicates": duplicates},
    }


def write_memory(run_dir: str, doc_id: str, memory: dict) -> str:
    path = memory_path(run_dir, doc_id)
    common.atomic_write_json(path, memory)
    return path


def load_memory(run_dir: str, doc_id: str) -> dict:
    """Read + validate ``translations-memory.json``; index it two ways.

    Returns ``{"path", "doc", "entries", "index", "sentence_index"}``:

    * ``index``: verbatim whole-row ``en`` → Chinese line list (stages 1–2);
    * ``sentence_index``: sentence ``en`` (speaker markers already removed) →
      ``{"en", "speaker", "zh"}`` (stage 3; entries written before this
      extension simply contribute nothing).

    Raises :class:`Failure` when the file is missing or malformed (callers
    treat that as "0 reuse").
    """
    path = memory_path(run_dir, doc_id)
    data = common.read_json(path)
    if not isinstance(data, dict) or data.get("version") != MEMORY_VERSION:
        raise common.Failure(common.EXIT_FAIL,
                             "译文记忆 version 不是 %d：%s" % (MEMORY_VERSION, path))
    if data.get("doc") not in (None, doc_id):
        raise common.Failure(common.EXIT_FAIL,
                             "译文记忆 doc 不是 %r：%s（%r）"
                             % (doc_id, path, data.get("doc")))
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise common.Failure(common.EXIT_FAIL, "译文记忆缺少 entries 数组：%s" % path)
    index = {}
    sentence_index = {}
    for position, entry in enumerate(entries, start=1):
        where = "%s 的 entries 第 %d 条" % (os.path.basename(path), position)
        if not isinstance(entry, dict):
            raise common.Failure(common.EXIT_FAIL, "%s 不是对象" % where)
        en = entry.get("en")
        zh = entry.get("zh")
        if not isinstance(en, str) or not en:
            raise common.Failure(common.EXIT_FAIL, "%s 的 en 不是非空字符串" % where)
        if not isinstance(zh, list) or not zh \
                or any(not isinstance(line, str) or not line.strip() for line in zh):
            raise common.Failure(common.EXIT_FAIL, "%s 的 zh 不是非空字符串数组" % where)
        index[en] = list(zh)
        raw_sentences = entry.get("sentences")
        if raw_sentences is None:
            continue
        if not isinstance(raw_sentences, list):
            raise common.Failure(common.EXIT_FAIL,
                                 "%s 的 sentences 不是数组" % where)
        for offset, sentence in enumerate(raw_sentences, start=1):
            swhere = "%s 的 sentences 第 %d 条" % (where, offset)
            if not isinstance(sentence, dict):
                raise common.Failure(common.EXIT_FAIL, "%s 不是对象" % swhere)
            sentence_en = sentence.get("en")
            speaker = sentence.get("speaker")
            chinese = sentence.get("zh")
            if not isinstance(sentence_en, str) or not sentence_en:
                raise common.Failure(common.EXIT_FAIL,
                                     "%s 的 en 不是非空字符串" % swhere)
            if speaker not in ("", "M", "W"):
                raise common.Failure(common.EXIT_FAIL,
                                     "%s 的 speaker 不是 ''/M/W（%r）"
                                     % (swhere, speaker))
            if not isinstance(chinese, str) or not chinese.strip():
                raise common.Failure(common.EXIT_FAIL,
                                     "%s 的 zh 不是非空字符串" % swhere)
            sentence_index[sentence_en] = {
                "en": sentence_en, "speaker": speaker, "zh": chinese}
    return {"path": path, "doc": doc_id, "entries": entries,
            "index": index, "sentence_index": sentence_index}


def ensure_memory(run_dir: str, doc_id: str, work: str, doc: dict) -> str | None:
    """Auto-snapshot for ``translate_prepare.py`` (silent-entry point).

    Called with the default ``--reuse-memory``:

    * no memory file yet → snapshot the newest prev archive, else the current
      6-column manifest; returns ``None`` when neither exists (fresh run);
    * memory exists but a **newer** prev archive appeared (a regroup happened
      since it was built) → rebuild it from that archive;
    * otherwise keep the existing file untouched.

    Raises :class:`Failure` when a chosen source cannot be parsed; the caller
    decides whether that is a hard error (CLI snapshot) or a silent fallback
    (prepare → 0 reuse).
    """
    path = memory_path(run_dir, doc_id)
    doc_path = common.doc_dir(run_dir, doc_id)
    prevs = [item for item in prev_manifests(doc_path) if is_six_column(item)]
    source = None
    if not os.path.isfile(path):
        source = prevs[0] if prevs else _first_current_six(work, doc)
    elif prevs:
        try:
            memory_mtime = os.stat(path).st_mtime_ns
        except OSError:
            memory_mtime = 0
        try:
            prev_mtime = os.stat(prevs[0]).st_mtime_ns
        except OSError:
            prev_mtime = 0
        if prev_mtime > memory_mtime:
            source = prevs[0]
    if source is None:
        return path if os.path.isfile(path) else None
    memory = build_memory(doc_id, source)
    return write_memory(run_dir, doc_id, memory)


def _first_current_six(work: str, doc: dict) -> str | None:
    for path in current_manifest_candidates(work, doc):
        if is_six_column(path):
            return path
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "translate_memory.py",
        "E5（译文记忆，排障入口）：把上一版 6 列清单的译文收成 "
        "translations-memory.json；translate_prepare.py 默认会自动做同样的事。",
        epilog="没有任何 6 列清单 → exit 3（正常派翻译即可）；"
               "--from PATH 可指定某份 6 列清单；不写坏文件。",
    )
    parser.add_argument("command", nargs="?", default="snapshot",
                        choices=("snapshot",),
                        help="目前只有 snapshot（省略也可以）")
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--from", dest="from_path", default=None, metavar="PATH",
                        help="指定一份 6 列清单（默认：最新 manifest-6col.prev-*.md "
                             "→ 当前已带译文列的产出区清单）")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE,
              "doc": args.doc}
    try:
        run_dir = os.path.abspath(args.run)
        run = common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        log = common.StageLog(run_dir, STAGE, args.doc)
        work = run.get("work", "")
        source, why = choose_source(run_dir, args.doc, work, doc, args.from_path)
        memory = build_memory(args.doc, source)
        path = write_memory(run_dir, args.doc, memory)
        stats = memory["stats"]
        result.update({
            "ok": True,
            "exit_code": common.EXIT_OK,
            "memory": path,
            "source": source,
            "source_kind": why,
            "entries": stats["entries"],
            "keep": stats["keep"],
            "empty_zh": stats["empty_zh"],
            "duplicates": stats["duplicates"],
        })
        log("snapshot %s -> %s entries=%d keep=%d empty_zh=%d duplicates=%d"
            % (source, path, stats["entries"], stats["keep"],
               stats["empty_zh"], stats["duplicates"]))
        if not args.json:
            print("DOC=%s  译文记忆 %d 条（来源：%s）" % (args.doc, stats["entries"], why))
            print("来源清单：%s" % source)
            print("已写：%s" % path)
            if stats["empty_zh"]:
                print("跳过空译文行：%d" % stats["empty_zh"])
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
