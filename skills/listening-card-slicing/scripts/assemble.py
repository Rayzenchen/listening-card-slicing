#!/usr/bin/env python3
"""assemble.py -- E4: merge receipts into numbered rows, cut text and manifest.

Inputs (all under ``$RUN``): ``blocks.json`` from E2 and one receipt
``docs/<id>/blocks/<block>.json`` per body block (接口约定 §4.5), or
``--overrides FILE`` that replaces whole blocks (接口修订 A2: a reviewer
receipt carrying an ``overrides`` array, or a pure
``{"version":1,"doc":"d01","overrides":[...]}`` file).

The assembler is deliberately paranoid because everything downstream trusts
its output:

* every body block must have a plan; a missing receipt is **exit 3** and no
  output file is written at all (no half products);
* a plan must cover its block's atoms exactly once, in order, with no foreign
  atoms, no duplicates and no reordering (**exit 1**);
* v1 has no atom splitting (接口修订 A9): a ``split`` field in a receipt is
  rejected with ``split_not_supported`` instead of being half-honoured;
* rows are numbered 1..N, ``keep`` is derived from the type only, and the
  display column must reduce to the cut text by dropping ``**M:**``/``**W:**``
  markers and collapsing whitespace;
* body rows longer than 44 words are recorded in ``segments.json.flags`` as
  ``{"n":…, "kind":"long_atom", "words":…}`` for report.py (A9).

0.0.10 adds the group-merge contract (SPEC §2): a multi-atom body segment may
carry ``merge`` = ``pure_response | fragment | compact_qa | dialogue_beat``;
the value is machine-checked (word caps, reply/fragment tables, ``?`` order,
speakers) and an illegal value is exit 1 with ``reason="merge_invalid"``.  Old
receipts without ``merge`` still assemble: every such multi-atom row lands in
``segments.json.merge_candidates`` with ``reason_missing=true`` so gate e4 can
require the reviewer to cover it.  ``flags`` stays exactly the ``long_atom``
list (the old gate equality is kept on purpose).

Outputs (all rewritten atomically per run): ``segments.json``,
``切分文本.txt``, ``docs/<id>/切分清单.md``, ``产出区/<stem>_切分清单.md``,
and the manifest sha is bound into ``segments.json.manifest_sha256`` and
``doc.json.manifest_sha256`` (A11).  0.0.5 dropped the separate
``manifest-sha.txt``: gates read those two fields instead.

Exit codes: 0 ok / 1 a plan or a check failed / 2 usage or missing prior
artifacts / 3 a body block has no receipt yet (dispatch it, then rerun).
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

STAGE = "e4"
KEEP_TYPES = frozenset(common.KEEP_TYPES)
#: 接口修订 A9: a body row longer than this many words is worth reporting.
LONG_ATOM_WORDS = 44

#: SPEC §2.1: the five legal reasons to put several atoms on one card.
#: A multi-atom segment must name one of these; a single-atom segment must not.
#: ``turn`` is dialogue-only (one speaker's consecutive run); mono blocks may
#: only use the first four.  See :func:`validate_merge_group`.
MERGE_KINDS = ("pure_response", "fragment", "compact_qa", "dialogue_beat", "turn")

#: SPEC §2.1: a ``pure_response`` tail atom must be one of these fixed
#: acknowledgements (case-insensitive, optional final punctuation).  A short
#: sentence that carries new information ("Yeah, it's more convenient.") is
#: deliberately not here: it is a normal card, not a pure response.
PURE_RESPONSE_RE = re.compile(
    r"^(yeah|yes|no|okay|ok|sure|sure thing|right|oh|all right|thanks|"
    r"thank you|i agree|exactly|absolutely|of course|what|why|really|"
    r"not really|that's true|sounds good|me too|you too)[.!?]*$",
    re.IGNORECASE,
)

#: SPEC §2.1: a ``fragment`` tail atom starts with one of these cues (or is
#: short enough — ≤4 words — that the cue test is waived).
FRAGMENT_CUES = frozenset({
    "because", "so", "but", "and", "or", "if", "when", "while", "which",
    "that", "to", "for", "with", "without", "yes", "no", "not", "sure",
    "okay", "right", "well",
})

#: ``pure_response`` / ``fragment`` still obey the global 44-word row cap.
MERGE_LINE_CAP = LONG_ATOM_WORDS
#: ``compact_qa`` / ``dialogue_beat`` have their own caps (SPEC §2.1).
COMPACT_QA_CAP = 24
DIALOGUE_BEAT_CAP = 30
DIALOGUE_BEAT_MAX_ATOMS = 6


def _fail(message: str, **extra):
    raise common.Failure(common.EXIT_FAIL, message, **extra)


def _ids(numbers) -> str:
    return "、".join(str(int(number)) for number in numbers)


def _merge_fail(message: str):
    """A merge contract violation: exit 1 with a human sentence (SPEC §2.2)."""
    raise common.Failure(common.EXIT_FAIL, "check merge: " + message,
                         reason="merge_invalid")


def _atom_text(atom: dict) -> str:
    return common.norm_ws(common.strip_speaker_markers(atom.get("text") or ""))


def _words(text: str) -> int:
    return len(common.norm_ws(text).split())


def _first_word(text: str) -> str:
    match = re.match(r"[A-Za-z']+", common.norm_ws(text))
    return match.group(0).lower() if match else ""


def _block_speakers(block: dict, atoms_by_id: dict) -> set:
    speakers = set()
    for atom_id in block.get("atoms") or []:
        atom = atoms_by_id.get(atom_id)
        if atom and atom.get("speaker") in ("M", "W"):
            speakers.add(atom["speaker"])
    return speakers


def validate_merge_group(block: dict, atom_ids, merge, atoms_by_id: dict) -> None:
    """Machine-check one multi-atom group against SPEC §2.1.

    ``pure_response`` / ``fragment`` constrain the **tail atom** (the one being
    pulled into the previous row); ``compact_qa`` / ``dialogue_beat`` constrain
    the whole row.  Raises :class:`common.Failure` (exit 1, reason
    ``merge_invalid``) with a sentence naming the violated clause.
    """
    if merge not in MERGE_KINDS:
        _merge_fail("merge 枚举非法：%r（只允许 %s）"
                    % (merge, " / ".join(MERGE_KINDS)))
    ids = list(atom_ids)
    if len(ids) < 2:
        _merge_fail("单原子组不能带 merge（原子 %s）；单原子请省略 merge 或写 null"
                    % _ids(ids))
    missing = [atom_id for atom_id in ids if atom_id not in atoms_by_id]
    if missing:
        _merge_fail("merge 组引用了不存在的原子 %s" % _ids(missing))
    texts = [_atom_text(atoms_by_id[atom_id]) for atom_id in ids]
    counts = [_words(text) for text in texts]
    total = sum(counts)
    label = "原子 %s" % _ids(ids)

    if merge == "pure_response":
        if counts[-1] > 6:
            _merge_fail("%s 的 pure_response 被并入回应原子 %d 是 %d 词，超过 ≤6 词"
                        % (label, ids[-1], counts[-1]))
        if not PURE_RESPONSE_RE.match(texts[-1]):
            _merge_fail("%s 的 pure_response 被并入回应原子 %d 不在固定回应短语表：%r"
                        % (label, ids[-1], texts[-1]))
        if total > MERGE_LINE_CAP:
            _merge_fail("%s 的 pure_response 整行 %d 词，超过 %d 词硬上限"
                        % (label, total, MERGE_LINE_CAP))
        return

    if merge == "fragment":
        if counts[-1] > 6:
            _merge_fail("%s 的 fragment 被并入片段原子 %d 是 %d 词，超过 ≤6 词（0.0.10 收紧）"
                        % (label, ids[-1], counts[-1]))
        first = _first_word(texts[-1])
        if first not in FRAGMENT_CUES and counts[-1] > 4:
            _merge_fail("%s 的 fragment 片段原子 %d 首词 %r 不在线索表，且整句 %d 词 >4"
                        % (label, ids[-1], first, counts[-1]))
        if total > MERGE_LINE_CAP:
            _merge_fail("%s 的 fragment 整行 %d 词，超过 %d 词硬上限"
                        % (label, total, MERGE_LINE_CAP))
        return

    if merge == "compact_qa":
        q_index = next((index for index, text in enumerate(texts) if "?" in text), None)
        if q_index is None:
            _merge_fail("%s 的 compact_qa 组内没有任何含 ? 的问句" % label)
        if q_index == len(texts) - 1:
            _merge_fail("%s 的 compact_qa 最后一个原子仍是问句，问句必须在回答之前"
                        % label)
        if total > COMPACT_QA_CAP:
            _merge_fail("%s 的 compact_qa 整行 %d 词，超过 %d 词"
                        % (label, total, COMPACT_QA_CAP))
        return

    if merge == "turn":
        if _block_speakers(block, atoms_by_id) != {"M", "W"}:
            _merge_fail("%s 的 turn 所在块 %s 不是对话块（块内没有同时出现 M/W）；"
                        "独白块禁止 turn" % (label, block.get("id")))
        speakers = {atoms_by_id[atom_id].get("speaker") for atom_id in ids}
        if len(speakers) != 1 or not speakers <= {"M", "W"}:
            _merge_fail("%s 的 turn 组内 speaker 不一致（%r）；"
                        "turn 只合并同一个说话人的连续回合"
                        % (label, sorted(str(item) for item in speakers)))
        if total > MERGE_LINE_CAP:
            _merge_fail("%s 的 turn 整行 %d 词，超过 %d 词硬上限"
                        % (label, total, MERGE_LINE_CAP))
        return

    # dialogue_beat
    if _block_speakers(block, atoms_by_id) != {"M", "W"}:
        _merge_fail("%s 的 dialogue_beat 所在块 %s 没有同时出现 M/W 两种说话人"
                    % (label, block.get("id")))
    speakers = {atoms_by_id[atom_id].get("speaker") for atom_id in ids}
    speakers &= {"M", "W"}
    if len(speakers) < 2:
        _merge_fail("%s 的 dialogue_beat 组内只有 %d 种 speaker（需要 ≥2）"
                    % (label, len(speakers)))
    if len(ids) > DIALOGUE_BEAT_MAX_ATOMS:
        _merge_fail("%s 的 dialogue_beat 组内 %d 个原子，超过 %d 个"
                    % (label, len(ids), DIALOGUE_BEAT_MAX_ATOMS))
    if total > DIALOGUE_BEAT_CAP:
        _merge_fail("%s 的 dialogue_beat 整行 %d 词，超过 %d 词"
                    % (label, total, DIALOGUE_BEAT_CAP))



# --- inputs -----------------------------------------------------------------


def load_blocks(run_dir: str, doc: dict, doc_id: str) -> dict:
    path = os.path.join(common.doc_dir(run_dir, doc_id), "blocks.json")
    blocks_doc = common.read_json(path)
    if not isinstance(blocks_doc, dict) or blocks_doc.get("version") != 1:
        _fail("blocks.json 不是 version 1 的结构：%s" % path)
    if blocks_doc.get("doc") != doc_id:
        _fail("blocks.json 的 doc 与命令不符：%s（doc=%r）" % (path, blocks_doc.get("doc")))
    current_sha = common.sha256_file(doc["text"])
    if doc.get("text_sha256") and current_sha != doc["text_sha256"]:
        raise common.Failure(
            common.EXIT_ASK,
            "原文内容变了（doc.json 记的 sha 与当前不符）：%s\n"
            "请重跑 bootstrap.py 生成新 run，不要在旧 run 上继续。" % doc["text"],
            reason="text_changed",
        )
    if blocks_doc.get("source_sha256") != current_sha:
        _fail("blocks.json 与原文 sha 不一致（blocks=%r，原文=%r）；请重跑 parse_source.py"
              % (blocks_doc.get("source_sha256"), current_sha))
    atoms = blocks_doc.get("atoms")
    blocks = blocks_doc.get("blocks")
    if not isinstance(atoms, list) or not isinstance(blocks, list) or not blocks:
        _fail("blocks.json 缺少 atoms / blocks：%s" % path)
    if len(atoms) != blocks_doc.get("atoms_total"):
        _fail("blocks.json 的 atoms_total（%r）与实际原子数（%d）不符"
              % (blocks_doc.get("atoms_total"), len(atoms)))
    for index, atom in enumerate(atoms, start=1):
        if atom.get("id") != index:
            _fail("blocks.json 的原子 id 不是从 1 连续：第 %d 个是 %r" % (index, atom.get("id")))
        if not atom.get("text"):
            _fail("blocks.json 的原子 %d 没有文本" % index)
        if atom.get("speaker") not in (None, "M", "W"):
            _fail("blocks.json 的原子 %d speaker 非法：%r" % (index, atom.get("speaker")))
    body_ids = []
    for block in blocks:
        if not block.get("id") or not block.get("kind"):
            _fail("blocks.json 有缺 id/kind 的块：%r" % (block,))
        if block.get("kind") not in common.BLOCK_KINDS:
            _fail("blocks.json 的块 %s 类型未知：%r" % (block.get("id"), block.get("kind")))
        # 0.0.7 contract v1: only these four kinds carry numbered atoms.
        # title/ending are layout blocks whose text is the single-space join
        # of their atoms; the old `test_intro` kind was removed.
        if block["kind"] not in ("title", "body", "directions", "ending") and block.get("atoms"):
            _fail("blocks.json 的块 %s（%s）不该带原子" % (block["id"], block["kind"]))
        if block["kind"] == "body":
            body_ids.append(block["id"])
            if not block.get("atoms"):
                _fail("blocks.json 的正文块 %s 没有原子" % block["id"])
            if not block.get("position"):
                _fail("blocks.json 的正文块 %s 没有位置" % block["id"])
    if not body_ids:
        _fail("blocks.json 没有任何正文块，无法汇编")
    return blocks_doc


def load_receipts(run_dir: str, doc_id: str, body_ids) -> tuple:
    """Read ``blocks/<block>.json`` for every body block that has one.

    Returns ``(receipts, ignored)``; ``ignored`` lists receipts that do not
    belong to a body block (they are reported, never used).
    """
    receipt_dir = os.path.join(common.doc_dir(run_dir, doc_id), "blocks")
    receipts = {}
    ignored = []
    if not os.path.isdir(receipt_dir):
        return receipts, ignored
    wanted = set(body_ids)
    for name in sorted(os.listdir(receipt_dir)):
        if not name.endswith(".json"):
            continue
        block_id = name[:-5]
        path = os.path.join(receipt_dir, name)
        if block_id not in wanted:
            ignored.append(path)
            continue
        receipts[block_id] = common.read_json(path)
    return receipts, ignored


def load_overrides(path: str, doc_id: str) -> tuple:
    """Read ``--overrides`` per 接口修订 A2, return ``(map, review_file)``.

    Exactly two spellings are accepted, because guessing a third one is how
    silent data loss happens:

    * a reviewer receipt ``review-<n>.json`` (any object carrying an
      ``overrides`` array);
    * a pure override file ``{"version": 1, "doc": "d01", "overrides": [...]}``.

    Unknown block ids, a block listed twice, and entries without ``segments``
    are exit 1 (A2), never merged "best effort".
    """
    data = common.read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("overrides"), list):
        _fail("--overrides 格式不认识：%s\n"
              "只接受两种：复核员回执（含 overrides 数组），或 "
              "{\"version\":1,\"doc\":\"d01\",\"overrides\":[...]}。" % path)
    recorded_doc = data.get("doc")
    if recorded_doc is not None and recorded_doc != doc_id:
        _fail("--overrides 的 doc 与命令不符：%s（doc=%r，期望 %s）"
              % (path, recorded_doc, doc_id))
    map_out = {}
    for item in data["overrides"]:
        if not isinstance(item, dict) or "block" not in item or "segments" not in item:
            _fail("--overrides 的某一项缺少 block/segments：%r（%s）" % (item, path))
        block_id = str(item["block"])
        if block_id in map_out:
            _fail("--overrides 里同一个块出现了两次：%s（%s）" % (block_id, path))
        map_out[block_id] = item["segments"]
    return map_out, path


def receipt_segments(receipt: dict, block: dict, doc_id: str, path: str,
                     warnings: list) -> list:
    """Schema check the envelope; the atom coverage check happens later."""
    if not isinstance(receipt, dict):
        _fail("回执不是 JSON 对象：%s" % path)
    if receipt.get("version") != 1:
        _fail("回执不是 version 1 的结构：%s" % path)
    if receipt.get("doc") != doc_id:
        _fail("回执的 doc 与命令不符：%s（doc=%r）" % (path, receipt.get("doc")))
    if receipt.get("block") != block["id"]:
        _fail("回执的 block 与文件名不符：%s（block=%r，期望 %s）"
              % (path, receipt.get("block"), block["id"]))
    role = receipt.get("role")
    if role not in (None, "segmenter"):
        _fail("回执的 role 应为 segmenter：%s（role=%r）" % (path, role))
    if not receipt.get("agent") or not receipt.get("finished_at"):
        warnings.append("回执 %s 缺 agent/finished_at，e3 关卡会要求补齐" % os.path.basename(path))
    segments = receipt.get("segments")
    if not isinstance(segments, list):
        _fail("回执缺少 segments 数组：%s" % path)
    return segments


# --- plan validation --------------------------------------------------------


def validate_plan(block: dict, segments, atoms_by_id: dict, doc_id: str,
                  where: str, warnings: list) -> list:
    """Validate one body block's plan and return ``[{"atoms":…, "merge":…}, …]``.

    接口修订 A9: v1 has no atom splitting.  A segment may only group
    consecutive atoms; a ``split`` field is rejected outright instead of being
    silently ignored, because ignoring it would assemble the wrong text.

    0.0.10 (SPEC §2): a multi-atom segment may name a ``merge`` reason; the
    value is enum-checked here and machine-checked by
    :func:`validate_merge_group` (word caps, reply/fragment tables, ``?``
    order, speakers).  A missing ``merge`` is *not* fatal — it marks the row
    as ``reason_missing`` in ``segments.json.merge_candidates`` so gate e4 can
    require the reviewer to justify it (old 0.0.9 receipts stay usable).

    The flattened atom list must equal the block's atoms exactly (same order,
    nothing missing, nothing repeated, nothing from another block).  Raises
    :class:`common.Failure` (exit 1) with a message that names the problem.
    """
    block_id = block["id"]
    expected = list(block["atoms"])
    label = "%s 的块 %s（%s，%s）" % (doc_id, block_id, block.get("label"), where)
    if not isinstance(segments, list) or not segments:
        _fail("%s 的 segments 必须是非空数组" % label)
    seen = []
    plan = []
    for index, segment in enumerate(segments):
        where_seg = "%s 第 %d 段" % (label, index + 1)
        if not isinstance(segment, dict):
            _fail("%s 不是对象：%r" % (where_seg, segment))
        if "split" in segment:
            _fail("check atom: split_not_supported（%s 出现了 split 字段；"
                  "v1 只允许连续原子分组，不允许把一个原子拆成两段。"
                  "请改回执；要拆就先让用户改原文分段、另起一次 run）" % where_seg,
                  reason="split_not_supported")
        atom_ids = segment.get("atoms")
        if (not isinstance(atom_ids, list) or not atom_ids
                or not all(isinstance(value, int) and not isinstance(value, bool)
                           for value in atom_ids)):
            _fail("%s 的 atoms 必须是非空整数数组：%r" % (where_seg, atom_ids))
        for atom_id in atom_ids:
            if atom_id not in atoms_by_id:
                _fail("%s 引用了不存在的原子 %d" % (where_seg, atom_id))
        seen.extend(atom_ids)
        plan.append(list(atom_ids))
    if seen != expected:
        detail = _coverage_diff(expected, seen, atoms_by_id)
        _fail("%s 的覆盖门失败：%s" % (label, detail))
    long_atoms = [atom_id for atom_ids in plan for atom_id in atom_ids
                  if len(atoms_by_id[atom_id]["text"].split()) > LONG_ATOM_WORDS]
    if long_atoms:
        warnings.append("%s 含超过 %d 词的原子 %s（A9：记 flag=long_atom，"
                        "要拆请用户改原文分段后另起 run）"
                        % (label, LONG_ATOM_WORDS, _ids(sorted(set(long_atoms)))))

    plan_with_merges = []
    for index, (segment, atom_ids) in enumerate(zip(segments, plan)):
        where_seg = "%s 第 %d 段" % (label, index + 1)
        if "merge" not in segment or segment.get("merge") is None:
            plan_with_merges.append({"atoms": atom_ids})
            continue
        merge = segment.get("merge")
        if not isinstance(merge, str) or merge not in MERGE_KINDS:
            _merge_fail("%s 的 merge 枚举非法：%r（只允许 %s）"
                        % (where_seg, merge, " / ".join(MERGE_KINDS)))
        validate_merge_group(block, atom_ids, merge, atoms_by_id)
        plan_with_merges.append({"atoms": atom_ids, "merge": merge})
    return plan_with_merges



def _coverage_diff(expected, seen, atoms_by_id) -> str:
    """Explain a flattened-atom mismatch in one human sentence."""
    missing = [atom for atom in expected if atom not in seen]
    extra = [atom for atom in seen if atom not in expected]
    duplicated = sorted({atom for atom in seen if seen.count(atom) > 1})
    parts = []
    if missing:
        parts.append("少原子 %s" % _ids(missing))
    if duplicated:
        parts.append("重复原子 %s" % _ids(duplicated))
    if extra:
        parts.append("多出原子 %s" % _ids(extra))
    if not parts:
        # Same multiset, different order: report the first divergence.
        position = next(index for index, (left, right) in enumerate(zip(expected, seen))
                        if left != right) if len(expected) == len(seen) else 0
        parts.append(
            "原子顺序被打乱（第 %d 处：期望 %s，实际 %s；期望序列 %s…，实际序列 %s…）"
            % (position + 1,
               expected[position] if position < len(expected) else "-",
               seen[position] if position < len(seen) else "-",
               _ids(expected[:8]), _ids(seen[:8]))
        )
    return "；".join(parts)


def _deterministic_row(block: dict, doc_id: str) -> dict:
    kind = block["kind"]
    seg_type = common.KIND_TO_TYPE[kind]
    text = common.norm_ws(block.get("text") or "")
    if not text:
        _fail("%s 的块 %s（%s）缺少定型文本，请重跑 parse_source.py"
              % (doc_id, block["id"], kind))
    if "\n" in text:
        _fail("%s 的块 %s 的文本里还有换行，请重跑 parse_source.py" % (doc_id, block["id"]))
    return {
        "type": seg_type,
        "keep": seg_type in KEEP_TYPES,
        "position": block["position"],
        "block": block["id"],
        "atoms": [],
        "text_cut": text,
        "text_display": text,
        "words": len(text.split()),
    }


def _body_rows(block: dict, plan, atoms_by_id: dict, doc_id: str) -> list:
    """One row per segment: its atoms joined with a single space (A9).

    0.0.10: a multi-atom segment's legal ``merge`` reason is carried onto the
    row (single-atom rows never get the key), so ``segments.json`` and gate e4
    can re-check it without re-reading the receipts.
    """
    rows = []
    for entry in plan:
        atom_ids = entry["atoms"] if isinstance(entry, dict) else entry
        merge = entry.get("merge") if isinstance(entry, dict) else None
        pieces = [atoms_by_id[atom_id]["text"] for atom_id in atom_ids]
        cut = common.norm_ws(" ".join(pieces))
        display = " ".join(
            common.speaker_mark(atoms_by_id[atom_id]["speaker"]) + piece
            for atom_id, piece in zip(atom_ids, pieces)
        )
        if not cut:
            _fail("%s 的块 %s 有一段切出空文本，请检查回执"
                  % (doc_id, block["id"]))
        row = {
            "type": "正文",
            "keep": True,
            "position": block["position"],
            "block": block["id"],
            "atoms": list(atom_ids),
            "text_cut": cut,
            "text_display": display,
            "words": len(cut.split()),
        }
        if merge:
            row["merge"] = merge
        rows.append(row)
    return rows


def build_rows(blocks_doc: dict, doc_id: str, plans: dict) -> list:
    """One row per audio piece, in block order; numbers are assigned later."""
    atoms_by_id = {atom["id"]: atom for atom in blocks_doc["atoms"]}
    rows = []
    for block in blocks_doc["blocks"]:
        if block["kind"] == "body":
            rows.extend(_body_rows(block, plans[block["id"]], atoms_by_id, doc_id))
        else:
            rows.append(_deterministic_row(block, doc_id))
    return rows


def check_rows(rows, blocks_doc: dict, doc_id: str) -> None:
    """Re-derive every invariant gate e4 will recompute from disk."""
    if not rows:
        _fail("%s 汇编出 0 行" % doc_id)
    atoms_by_id = {atom["id"]: atom for atom in blocks_doc["atoms"]}
    body_by_id = {block["id"]: block for block in blocks_doc["blocks"]
                  if block["kind"] == "body"}
    for index, row in enumerate(rows, start=1):
        row["n"] = index
        row_type = row.get("type")
        if row_type not in common.SEGMENT_TYPES:
            _fail("%s 第 %d 行的类型非法：%r" % (doc_id, index, row_type))
        if row.get("keep") != (row_type in KEEP_TYPES):
            _fail("%s 第 %d 行 keep 与类型不符（%s：keep=%r）"
                  % (doc_id, index, row_type, row.get("keep")))
        if row_type == "正文":
            block = body_by_id.get(row.get("block"))
            if block is None:
                _fail("%s 第 %d 行的块 %r 不是正文块"
                      % (doc_id, index, row.get("block")))
            merge = row.get("merge")
            if merge is not None:
                validate_merge_group(block, row.get("atoms") or [], merge,
                                     atoms_by_id)
        elif "merge" in row:
            _fail("%s 第 %d 行不是正文，不能带 merge 字段" % (doc_id, index))
        text = row.get("text_cut")
        if not isinstance(text, str) or not text:
            _fail("%s 第 %d 行的切分文本为空" % (doc_id, index))
        if "\n" in text or text != common.norm_ws(text):
            _fail("%s 第 %d 行的切分文本没有归一（%r）" % (doc_id, index, text))
        display = row.get("text_display")
        if not isinstance(display, str) or not display:
            _fail("%s 第 %d 行的清单原文为空" % (doc_id, index))
        reduced = common.norm_ws(common.strip_speaker_markers(display))
        if reduced != text:
            _fail("%s 第 %d 行：去掉说话人标记后的原文与切分文本不一致（%r vs %r）"
                  % (doc_id, index, reduced, text))
        if not row.get("position"):
            _fail("%s 第 %d 行没有位置" % (doc_id, index))
        if "split" in row:
            _fail("check atom: split_not_supported（%s 第 %d 行仍带 split 字段；"
                  "v1 不允许拆分原子）" % (doc_id, index), reason="split_not_supported")

    # body blocks must partition their atoms, in order, with no overlap
    body_blocks = [block for block in blocks_doc["blocks"] if block["kind"] == "body"]
    cursor = 0
    consumed = []
    for block in body_blocks:
        if block["atoms"] and block["atoms"][0] < cursor:
            _fail("%s 的正文块 %s 与前一个块共用原子（跨块重叠）" % (doc_id, block["id"]))
        consumed.extend(block["atoms"])
        cursor = max(cursor, max(block["atoms"]))
    expected = [atom["id"] for atom in blocks_doc["atoms"]
                if atom["id"] in set(consumed)]
    if consumed != expected:
        _fail("%s 的正文块没有按顺序划分全部正文原子" % doc_id)


# --- outputs ----------------------------------------------------------------


def _preserve_6col_manifest(paths: str, out_manifest_path: str, warnings: list):
    """Copy a 6-column 产出区 manifest to ``$RUN/docs/<doc>/manifest-6col.prev-*``.

    SPEC §2.3 / D2: E5's translation memory is rebuilt from this snapshot, so
    a regrouped E4 never has to retranslate unchanged rows.  We only copy the
    bytes — parsing the 译文 column is translate_memory.py's job.
    """
    if not os.path.isfile(out_manifest_path):
        return None
    try:
        text = common.read_text(out_manifest_path)
    except common.Failure as exc:
        warnings.append("读不了产出区清单，跳过 6 列快照：%s" % exc.message)
        return None
    if common.manifest_columns(text) != 6:
        return None
    stamp = common.now_compact()
    target = os.path.join(paths, "manifest-6col.prev-%s.md" % stamp)
    suffix = 0
    while os.path.exists(target):
        suffix += 1
        target = os.path.join(paths, "manifest-6col.prev-%s-%d.md" % (stamp, suffix))
    common.ensure_dir(paths)
    common.atomic_write_text(target, text)
    warnings.append("已将旧 6 列清单复制为 %s（D2 译文记忆来源，只复制不解析）" % target)
    return target


def stats_of(rows) -> dict:
    by_type = {}
    keep_by_type = {}
    for row in rows:
        by_type[row["type"]] = by_type.get(row["type"], 0) + 1
        if row["keep"]:
            keep_by_type[row["type"]] = keep_by_type.get(row["type"], 0) + 1
    ordered = {name: by_type[name] for name in common.SEGMENT_TYPES if name in by_type}
    keep_ordered = {name: keep_by_type[name] for name in common.SEGMENT_TYPES
                    if name in keep_by_type}
    return {
        "total": len(rows),
        "keep": sum(1 for row in rows if row["keep"]),
        "by_type": ordered,
        "keep_by_type": keep_ordered,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "assemble.py",
        "E4：合并正文块回执（或 --overrides 整块替换）+ 确定性片段，校验覆盖门后编号，"
        "写 segments.json / 切分文本.txt / 切分清单.md（清单 sha 记入 segments/doc）。",
        epilog="缺回执 → exit 3（只重派缺的块）；覆盖门/回执非法 → exit 1；产物缺失 → exit 2。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--overrides", default=None,
                        help="整块替换：复核员回执（含 overrides 数组），或 "
                             "{\"version\":1,\"doc\":\"d01\",\"overrides\":[...]} 纯覆盖文件")
    parser.add_argument("--json", action="store_true", help="只输出末行 JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE, "doc": args.doc}
    try:
        run = common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        log = common.StageLog(run_dir, STAGE, args.doc)
        blocks_doc = load_blocks(run_dir, doc, args.doc)
        atoms_by_id = {atom["id"]: atom for atom in blocks_doc["atoms"]}
        body_blocks = [block for block in blocks_doc["blocks"] if block["kind"] == "body"]
        body_ids = [block["id"] for block in body_blocks]

        receipts, ignored = load_receipts(run_dir, args.doc, body_ids)
        overrides = {}
        override_source = None
        if args.overrides:
            overrides, override_source = load_overrides(args.overrides, args.doc)
            unknown = [block_id for block_id in overrides if block_id not in set(body_ids)]
            if unknown:
                _fail("--overrides 里的块不在本文档：%s（正文块：%s）"
                      % ("、".join(unknown), "、".join(body_ids)))

        # 1) missing receipts first: no half products, exit 3.
        missing = []
        plans = {}
        warnings = []
        for block in body_blocks:
            block_id = block["id"]
            if block_id in overrides:
                segments = overrides[block_id]
                where = "overrides"
            elif block_id in receipts:
                segments = receipt_segments(
                    receipts[block_id], block, args.doc,
                    os.path.join(common.doc_dir(run_dir, args.doc), "blocks",
                                 block_id + ".json"), warnings)
                where = "回执"
            else:
                missing.append("%s-%s" % (args.doc, block_id))
                continue
            plans[block_id] = validate_plan(block, segments, atoms_by_id,
                                            args.doc, where, warnings)
        if missing:
            receipt_paths = [os.path.join(common.doc_dir(run_dir, args.doc), "blocks",
                                          task_id.split("-", 1)[1] + ".json")
                             for task_id in missing]
            raise common.Failure(
                common.EXIT_ASK,
                "缺 %d 个正文块的裁定回执，本次没有生成任何产物：\n  %s\n"
                "请只重派这些块（把回执写到对应路径），然后重跑 assemble.py。\n"
                "回执路径：\n  %s"
                % (len(missing), "、".join(missing), "\n  ".join(receipt_paths)),
                missing=missing,
                receipt_paths=receipt_paths,
            )

        # 2) rows + numbering + invariant checks (all in memory).
        rows = build_rows(blocks_doc, args.doc, plans)
        check_rows(rows, blocks_doc, args.doc)
        stats = stats_of(rows)
        applied = [block["id"] for block in body_blocks if block["id"] in overrides]
        flags = [
            {"n": row["n"], "kind": "long_atom", "words": row["words"]}
            for row in rows
            if row["type"] == "正文" and row["words"] > LONG_ATOM_WORDS
        ]
        merge_candidates = [
            {
                "n": row["n"],
                "block": row["block"],
                "atoms": list(row["atoms"]),
                "words": row["words"],
                "merge": row.get("merge"),
                "reason_missing": not row.get("merge"),
            }
            for row in rows
            if row["type"] == "正文" and len(row["atoms"]) > 1
        ]
        missing_merges = [item for item in merge_candidates if item["reason_missing"]]
        if missing_merges:
            warnings.append(
                "merge_unreviewed: %d 个多原子行没有 merge 理由（旧回执兼容；"
                "gate e4 会要求复核员逐条给 merge_review）：%s"
                % (len(missing_merges),
                   "、".join("%03d" % item["n"] for item in missing_merges[:12])))
        paths = common.doc_dir(run_dir, args.doc)
        segments_path = os.path.join(paths, "segments.json")
        cut_text_path = os.path.join(paths, "切分文本.txt")
        doc_manifest_path = os.path.join(paths, "切分清单.md")
        out_manifest_path = common.resolve_work_path(run.get("work", ""), doc["manifest"])

        # SPEC §2.3: review receipts from a ``fixed`` verdict talk about the
        # rows *before* this override run, so keep that candidate snapshot for
        # gate e4.  A re-run with the same --overrides must not overwrite it
        # with the post-override candidates (otherwise the gate would compare
        # two different generations).
        review_base = []
        if applied and os.path.isfile(segments_path):
            try:
                previous = common.read_json(segments_path)
            except common.Failure:
                previous = None
            if isinstance(previous, dict):
                preserved = previous.get("review_base_merge_candidates")
                if isinstance(preserved, list) and preserved:
                    review_base = preserved
                elif isinstance(previous.get("merge_candidates"), list):
                    review_base = previous["merge_candidates"]

        segments_doc = {
            "version": 1,
            "doc": args.doc,
            "source_sha256": blocks_doc["source_sha256"],
            "overrides_applied": applied,
            "review_file": override_source,
            "flags": flags,
            "merge_candidates": merge_candidates,
            "review_base_merge_candidates": review_base,
            "rows": rows,
            "stats": stats,
        }
        cut_text = "\n".join(row["text_cut"] for row in rows) + "\n"
        manifest_rows = [
            {
                "n": row["n"],
                "type": row["type"],
                "keep": row["keep"],
                "position": row["position"],
                "display": row["text_display"],
            }
            for row in rows
        ]
        manifest_text = common.render_manifest(doc["stem"], manifest_rows)
        manifest_bytes = manifest_text.encode("utf-8")
        manifest_sha = common.sha256_bytes(manifest_bytes)
        segments_doc["manifest_sha256"] = manifest_sha

        common.ensure_dir(paths)
        common.ensure_dir(os.path.dirname(out_manifest_path))

        # D2 hook (SPEC §2.3): E5 may have extended the 产出区 manifest to the
        # 6-column form (译文).  Before this run overwrites it, keep a byte
        # copy under $RUN so translate_memory.py can snapshot the translations.
        # We only copy; parsing/merging is D2's job.
        manifest_6col_prev = _preserve_6col_manifest(paths, out_manifest_path, warnings)

        # 3) write everything only after every check passed.
        common.atomic_write_json(segments_path, segments_doc)
        common.atomic_write_text(cut_text_path, cut_text)
        common.atomic_write_text(doc_manifest_path, manifest_text)
        common.atomic_write_text(out_manifest_path, manifest_text)

        # 4) A11: the manifest sha is also recorded on the document itself so
        # later stages can detect a downstream extension (e.g. a 译文 column)
        # without reading the live pack.
        doc["manifest_sha256"] = manifest_sha
        common.atomic_write_json(os.path.join(paths, "doc.json"), doc)

        # 5) read back: the two manifest copies must be byte-identical.
        if common.sha256_file(doc_manifest_path) != common.sha256_file(out_manifest_path):
            _fail("两份切分清单 sha 不一致（%s / %s），写盘出了问题"
                  % (doc_manifest_path, out_manifest_path))

        for path in ignored:
            warnings.append("忽略不属于正文块的回执：%s" % os.path.basename(path))

        result.update(
            {
                "ok": True,
                "exit_code": common.EXIT_OK,
                "rows": stats["total"],
                "keep": stats["keep"],
                "by_type": stats["by_type"],
                "manifest_sha256": manifest_sha,
                "flags": flags,
                "merge_candidates": len(merge_candidates),
                "merge_unreviewed": len(missing_merges),
                "overrides_applied": applied,
                "segments_path": segments_path,
                "cut_text_path": cut_text_path,
                "manifest_path": doc_manifest_path,
                "output_manifest": out_manifest_path,
                "manifest_6col_prev": manifest_6col_prev,
                "overrides": override_source,
                "warnings": warnings,
            }
        )
        log("assembled %s: rows=%d keep=%d by_type=%r"
            % (args.doc, stats["total"], stats["keep"], stats["by_type"]))
        for warning in warnings:
            log("WARN " + warning)
        log("wrote %s" % segments_path)
        log("wrote %s" % cut_text_path)
        log("wrote %s" % doc_manifest_path)
        log("wrote %s (sha %s)" % (out_manifest_path, manifest_sha))

        if not args.json:
            print("DOC=%s  行=%d  进卡组=%d（正文 %d + 题目 %d）"
                  % (args.doc, stats["total"], stats["keep"],
                     stats["keep_by_type"].get("正文", 0),
                     stats["keep_by_type"].get("题目", 0)))
            print("类型分布：" + "、".join("%s %d" % (key, value)
                                       for key, value in stats["by_type"].items()))
            print("切分文本：%s（%d 行）" % (cut_text_path, stats["total"]))
            print("清单：%s" % doc_manifest_path)
            print("产出区清单：%s" % out_manifest_path)
            print("清单 sha256：%s" % manifest_sha)
            if merge_candidates:
                print("merge 候选（多原子行）：%d 条，其中缺理由 %d 条（旧回执兼容）"
                      % (len(merge_candidates), len(missing_merges)))
            if manifest_6col_prev:
                print("旧 6 列清单快照：%s" % manifest_6col_prev)
            if flags:
                print("WARN 正文行超过 %d 词（long_atom，报给用户决定）：%s"
                      % (LONG_ATOM_WORDS,
                         "、".join("%03d（%d 词）" % (item["n"], item["words"])
                                   for item in flags)))
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
