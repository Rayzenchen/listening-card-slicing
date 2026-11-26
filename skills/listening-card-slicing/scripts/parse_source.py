#!/usr/bin/env python3
r"""parse_source.py -- E2: parse one listening transcript into blocks + atoms.

Contract v1 only (0.0.7)
------------------------
This parser accepts **one** transcript format and never guesses another one.
The whole grammar is four layout labels on top of numbered atoms:

```markdown
## [开场]                    layout only, never spoken itself
1、College English Test, Band 4.
2、Part Two, Listening Comprehension.

## Section A                 layout marker, text is synthesized "Section A."
### Directions               spoken instructions, one numbered atom per line
1、In this section, you will hear three news reports.

### News Report 1 [Q1-2]     "[Qx-y]" is mandatory; y may be omitted/equal to x
1、A terrified cat ...
[META] Questions 1 and 2 are based on the news report you have just heard.
Question 1. Where was the black cat found?
Question 2. What do we learn about the cat?

## [结尾]                    layout only; the ending atoms live here
1、That's the end of Listening Comprehension.
```

kind -> segment type (DESIGN §2, no 试音 block any more)
-----------------------------------------------------------
```
## [开场] + numbered atoms        title          开场  (text = atoms joined by one space)
## Section X                    marker         报幕  (text = "Section X.")
### Directions + atoms          directions     指令  (text = "Directions: " + atoms)
### <label> [Qx-y]             marker         报幕  (text = "<label>.")
numbered atoms under a <label> body           正文  (no "text"; carries q_range)
[META] Questions ...           questions_meta 指令  (label stripped, carries q_range)
Question N. ...                question       题目  (carries q_range "QN")
## [结尾] + numbered atoms        ending         结尾  (text = atoms joined by one space)
```

Hard rules enforced here (DESIGN §1/§3):

* one numbered atom per source line; several atoms on one line are a hard
  failure (all inline-atom guessing was deleted in 0.0.7);
* numbering restarts at 1 inside every collecting block;
* exactly one `## [开场]` (first block) and one `## [结尾]` (last block), if
  present; both are optional and only WARN (`missing_title`/`missing_ending`);
* every body heading needs `[Qx-y]`; its `[META]` row is optional but, when
  present, must carry the same range and sits *after* the body atoms;
* question numbers run `+1` (they may start at any N); gaps/duplicates fail;
* speakers are `M:`/`W:` and are inherited inside one body block;
* Markdown escapes (`\.`, `\$`), bold speakers (`**M:**`), code fences, links
  and unknown layout headings are rejected with a migration hint;
* legacy `# Part II...`, `## 试音部分`, bare `That's the end of...`, bare or
  numbered `Questions...` and label-only `### News Report 1` are rejected --
  the error tells the user which contract v1 label to use instead.

Every non-empty source line is covered by an atom `source_spans` entry or by a
block's own `line`; `source_sha256` is written by the caller.

Exit codes: 0 ok / 1 the transcript does not fit contract v1 /
2 usage or missing run artifacts / 3 the input changed under the run.
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

import common  # noqa: E402  (path bootstrap above)

STAGE = "e2"
FORMAT = "contract-v1"

#: Contract v1 layout headings.  Level is checked where the label is used.
HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")
SECTION_RE = re.compile(r"^Section\s+([A-Za-z])\s*[.．]?$")
DIRECTIONS_RE = re.compile(r"^Directions\s*[:：]?\s*[.．]?$", re.I)
#: `### <label> <suffix>`; the suffix must be the mandatory `[Qx-y]`.
BODY_HEADING_RE = re.compile(r"^(News Report|Conversation|Passage)\s+(\d+)\s*(.*)$")
#: A body label without the range, used by validation and migration messages.
BODY_LABEL_RE = re.compile(r"^(News Report|Conversation|Passage)\s+\d+$")

#: `M: 1、...` / `1、...` (speaker optional, Chinese enumeration comma).
ATOM_RE = re.compile(r"^(?:([MW])\s*[:：]\s*)?(\d{1,3})\s*、\s*(.*)$")
#: Token start used only to *reject* a second atom on the same line.
ATOM_TOKEN_RE = re.compile(r"(?:[MW]\s*[:：]\s*)?\d{1,3}\s*、")
#: `Question 16. ...` -- one per line, never numbered.
QUESTION_RE = re.compile(r"^Question\s+(\d+)\s*[.．]\s*(.*)$")
#: `[META] Questions 1 and 2 are based on ...`
META_LINE_RE = re.compile(r"^\[META\]\s*(.*)$")
QUESTIONS_RE = re.compile(
    r"^Questions?\s+(\d+)(?:\s*(?:and|to|through|~|-|–|—)\s*(\d+))?"
    r"\s+(?:is|are)\s+based\s+on\b",
    re.I,
)
Q_RANGE_RE = re.compile(r"^Q(\d+)(?:\s*-\s*(\d+))?$")
BOLD_SPEAKER_RE = re.compile(r"^\*\*\s*([MW])\s*[:：]\s*\*\*")

#: Old-format fingerprints: these only exist to give a one-line migration hint.
OLD_TITLE_HINT_RE = re.compile(
    r"^(?:College English Test\b"
    r"|Part\s+(?:[IVX]+|One|Two|Three|Four|Five|Six)\b.*Listening\s+Comprehension"
    r"|Listening\s+Comprehension\b)",
    re.I,
)
OLD_TEST_INTRO_LABEL = "试音部分"
OLD_ENDING_HINT_RE = re.compile(r"^That[’']?s\s+the\s+end\s+of\b", re.I)
OLD_QUESTIONS_HINT_RE = re.compile(r"^Questions?\s+\d+\b", re.I)

#: Kinds whose atoms are consumed from the source (in file order).
COLLECTING_KINDS = ("title", "directions", "body", "ending")

_BACKSLASH = "\\"


def _fail(message: str, *, line: int | None = None):
    where = ("（第 %d 行）" % line) if line else ""
    raise common.Failure(common.EXIT_FAIL, "原文解析失败%s：%s" % (where, message))


def _atoms_by_id(atoms) -> dict:
    return {atom["id"]: atom for atom in atoms}


def _parse_q_range(raw: str, *, line: int, where: str) -> str:
    """Normalise `Q1-2` / `Q16` / `Q16-16` -> `Q1-2` / `Q16`."""
    match = Q_RANGE_RE.match(raw.strip())
    if not match:
        _fail("%s 的 Q 范围写法不对：%r（应为 `[Q1-2]` 或 `[Q16]`）"
              % (where, raw), line=line)
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if last < first:
        _fail("%s 的 Q 范围倒序：%r（开始不能大于结束）" % (where, raw), line=line)
    if last == first:
        return "Q%d" % first
    return "Q%d-%d" % (first, last)


def infer_default_section(lines) -> str:
    """Section letter for text before the first explicit `## Section X`.

    Contract v1 keeps Section headings optional: the implicit section is the
    one before the first explicit section (`Section C` -> B, none -> A).  It
    is grouping metadata only; no `Section A.` marker is fabricated.
    """
    for raw in lines:
        match = HEADING_RE.match(raw.strip())
        if not match:
            continue
        section = SECTION_RE.match(match.group(2).strip())
        if section:
            letter = section.group(1).upper()
            return chr(ord(letter) - 1) if letter > "A" else "A"
    return "A"


class _Parser:
    """One parse pass; `parse_text` wraps it so state never leaks between runs."""

    def __init__(self, source_text: str, source_name: str):
        self.source_text = source_text
        self.source_name = source_name
        self.lines = source_text.split("\n")
        self.blocks = []
        self.atoms = []
        self.spans = []
        self.warnings = []
        self.state = {
            "section": "Section " + infer_default_section(self.lines),
            "collector": None,   # block that accepts the following numbered atoms
            "speaker": None,     # current M/W, inherited inside one block
            "body": None,        # body block waiting for [META] / Question rows
            "meta_seen": False,  # current body already has its [META] row
            "questions_started": False,  # a Question row already followed this body
            "seen_title": False,
            "seen_ending": False,
        }

    # -- block helpers -----------------------------------------------------

    def add_block(self, kind: str, *, label=None, section=None, position=None,
                  raw=None, line=None, collects: bool = False,
                  q_range=None) -> dict:
        block = {
            "id": "b%02d" % (len(self.blocks) + 1),
            "kind": kind,
            "label": label,
            "section": section,
            "position": position,
            "atoms": [],
            "line": line,
            "_raw": raw,
        }
        if q_range is not None:
            block["q_range"] = q_range
        self.blocks.append(block)
        self.state["collector"] = block if collects else None
        return block

    def _reset_context(self) -> None:
        """Drop the current collector / speaker / body before a new block."""
        self.state["collector"] = None
        self.state["speaker"] = None
        self.state["body"] = None
        self.state["meta_seen"] = False
        self.state["questions_started"] = False

    def _require_level(self, level: int, expected: int, syntax: str,
                       line_no: int) -> None:
        if level != expected:
            _fail("标题层级不对：这里必须写成 `%s`（用了 %d 个 `#`）"
                  % (syntax, level), line=line_no)

    # -- headings ----------------------------------------------------------

    def handle_heading(self, level: int, text: str, line_no: int) -> None:
        title = text.strip()

        if title == "[开场]":
            self._require_level(level, 2, "## [开场]", line_no)
            self._start_title(line_no)
            return

        if title == "[结尾]":
            self._require_level(level, 2, "## [结尾]", line_no)
            self._start_ending(line_no)
            return

        if title == OLD_TEST_INTRO_LABEL:
            _fail("旧格式 `## 试音部分` 不再支持：把试音句写成 `## [开场]` "
                  "下的 `1、…` 编号原子", line=line_no)

        section = SECTION_RE.match(title)
        if section:
            self._require_level(level, 2, "## Section A", line_no)
            self._start_section("Section %s" % section.group(1).upper(), line_no)
            return

        if DIRECTIONS_RE.match(title):
            self._require_level(level, 3, "### Directions", line_no)
            self._start_directions(line_no)
            return

        body = BODY_HEADING_RE.match(title)
        if body:
            self._require_level(level, 3, "### News Report 1 [Q1-2]", line_no)
            self._start_body(title, body, line_no)
            return

        if OLD_TITLE_HINT_RE.match(title):
            _fail("旧格式标题 `%s` 不再支持：开场标题改用 `## [开场]`，"
                  "把开场句写成 `1、…` 编号原子" % title[:80], line=line_no)

        _fail("不认识的标题 `%s`：只允许 `## [开场]`、`## Section X`、"
              "`### Directions`、`### News Report/Conversation/Passage N [Qx-y]`、"
              "`## [结尾]`" % title[:80], line=line_no)

    def _start_title(self, line_no: int) -> None:
        if self.state["seen_title"]:
            _fail("`## [开场]` 重复出现（只允许一个）", line=line_no)
        if self.blocks:
            _fail("`## [开场]` 必须是原文的第一个块（前面已经有其他标题或内容）",
                  line=line_no)
        self.state["seen_title"] = True
        self._reset_context()
        self.add_block("title", position="开场", line=line_no, collects=True)

    def _start_ending(self, line_no: int) -> None:
        if self.state["seen_ending"]:
            _fail("`## [结尾]` 重复出现（只允许一个）", line=line_no)
        self.state["seen_ending"] = True
        self._reset_context()
        self.add_block("ending", position="录音结尾", line=line_no, collects=True)

    def _start_section(self, label: str, line_no: int) -> None:
        self._reset_context()
        self.state["section"] = label
        self.add_block("marker", label=label, section=label, position=label,
                       line=line_no)

    def _start_directions(self, line_no: int) -> None:
        section = self.state["section"]
        self._reset_context()
        self.add_block("directions", label="Directions", section=section,
                       position=section, line=line_no, collects=True)

    def _start_body(self, title: str, match, line_no: int) -> None:
        label = "%s %s" % (match.group(1), match.group(2))
        suffix = match.group(3).strip()
        if not suffix:
            _fail("篇目标题 `%s` 缺少 `[Qx-y]`：应写成 `### %s [Q1-2]`"
                  % (title, label), line=line_no)
        if not (suffix.startswith("[") and suffix.endswith("]")):
            _fail("篇目标题 `%s` 的 Q 范围后缀写法不对：只允许 `[Qx-y]` "
                  "或 `[QN]`，例如 `### %s [Q1-2]`" % (title, label),
                  line=line_no)
        q_range = _parse_q_range(suffix[1:-1], line=line_no,
                                 where="篇目 `%s`" % label)
        section = self.state["section"]
        self._reset_context()
        self.add_block("marker", label=label, section=section,
                       position="%s / %s" % (section, label), line=line_no)
        body = self.add_block("body", label=label, section=section,
                              position="%s / %s (%s)" % (section, label, q_range),
                              line=line_no, collects=True, q_range=q_range)
        self.state["body"] = body

    # -- numbered atoms ----------------------------------------------------

    def handle_atoms(self, clean: str, line_no: int) -> None:
        starts = [match.start() for match in ATOM_TOKEN_RE.finditer(clean)
                  if match.start() == 0 or clean[match.start() - 1].isspace()]
        if len(starts) > 1:
            _fail("这一行有 %d 个编号原子：一行只能放一个编号原子/一句话，"
                  "把后面的 `2、…` 另起一行" % len(starts),
                  line=line_no)
        match = ATOM_RE.match(clean)
        if not match:
            _fail("编号原子格式不认识：%r" % clean[:80], line=line_no)
        speaker = match.group(1)
        number = int(match.group(2))
        text = match.group(3).strip()
        if not text:
            _fail("编号原子没有正文：%r" % clean[:80], line=line_no)

        # 0.0.7 deleted the numbered-Questions/-Question routing on purpose.
        if text.startswith("[META]"):
            _fail("编号型 `[META]` 不再支持：`[META] Questions…` 单独一行，"
                  "不加 `1、` 编号", line=line_no)
        if QUESTION_RE.match(text):
            _fail("编号型 `Question` 不再支持：`Question N. …` 单独一行，"
                  "不加 `1、` 编号", line=line_no)
        if OLD_QUESTIONS_HINT_RE.match(text) and "based on" in text.lower():
            _fail("编号型 `Questions …` 不再支持：改成 `[META] Questions …`"
                  "（去掉编号）；音频没说这句就不要写", line=line_no)

        collector = self.state["collector"]
        if collector is None:
            _fail("编号原子前面没有可归属的标题：必须紧跟 `## [开场]` / "
                  "`### Directions` / `### News Report/Conversation/Passage N [Qx-y]`",
                  line=line_no)
        expected = len(collector["atoms"]) + 1
        if number != expected:
            _fail("编号原子必须从 1 连续编号：这里是 `%d、`，期望 `%d、`"
                  % (number, expected), line=line_no)

        if speaker:
            self.state["speaker"] = speaker
        atom = {
            "id": len(self.atoms) + 1,
            "speaker": self.state["speaker"],
            "text": text,
            "line": line_no,
        }
        self.atoms.append(atom)
        self.spans.append({"id": atom["id"], "line": line_no,
                           "col_start": 0, "col_end": len(clean)})
        collector["atoms"].append(atom["id"])

    # -- [META] / Question -------------------------------------------------

    def handle_meta(self, clean: str, line_no: int) -> None:
        match = META_LINE_RE.match(clean)
        sentence = match.group(1).strip() if match else ""
        if not sentence:
            _fail("`[META]` 行没有句子内容", line=line_no)
        body = self.state["body"]
        if body is None:
            _fail("`[META]` 必须紧跟某个篇目的正文（前面没有篇目）",
                  line=line_no)
        if self.state["meta_seen"]:
            _fail("同一个篇目出现了两条 `[META]`（只允许一条）", line=line_no)
        if self.state["questions_started"]:
            _fail("`[META]` 必须在 Question 行之前（紧跟篇目正文）", line=line_no)
        meta = QUESTIONS_RE.match(sentence)
        if not meta:
            _fail("`[META]` 后面必须是 `Questions N and M are based on …`；"
                  "如果音频没说这句，就不要写 `[META]`", line=line_no)
        first = int(meta.group(1))
        last = int(meta.group(2)) if meta.group(2) else first
        if last < first:
            _fail("`[META]` 的题目范围倒序：%d..%d" % (first, last),
                  line=line_no)
        q_range = "Q%d" % first if last == first else "Q%d-%d" % (first, last)
        if q_range != body["q_range"]:
            _fail("`[META]` 的题目范围 %s 与篇目 `%s` 的 %s 不一致"
                  % (q_range, body["label"], body["q_range"]), line=line_no)
        section = body["section"]
        self.add_block("questions_meta", section=section,
                       position="%s / %s" % (section, q_range),
                       raw=sentence, line=line_no, q_range=q_range)
        self.state["collector"] = None
        self.state["meta_seen"] = True
        self.state["speaker"] = None

    def handle_question(self, clean: str, line_no: int) -> None:
        match = QUESTION_RE.match(clean)
        if not match:
            _fail("`Question N.` 行格式不认识：%r" % clean[:80], line=line_no)
        number = int(match.group(1))
        text = match.group(2).strip()
        if not text:
            _fail("`Question %d.` 没有题干" % number, line=line_no)
        body = self.state["body"]
        if body is None:
            _fail("`Question N.` 必须跟在某个篇目的正文之后（前面没有篇目）",
                  line=line_no)
        section = body["section"]
        self.add_block("question", label="Q%d" % number, section=section,
                       position="%s / Q%d" % (section, number),
                       raw=common.norm_ws(clean), line=line_no,
                       q_range="Q%d" % number)
        self.state["collector"] = None
        self.state["speaker"] = None
        self.state["questions_started"] = True

    # -- driver ------------------------------------------------------------

    def run(self) -> dict:
        for line_no, raw_line in enumerate(self.lines, start=1):
            clean = raw_line.strip()
            if not clean:
                continue
            if _BACKSLASH in clean:
                _fail("原文不要 Markdown 转义（反斜杠）：删掉 `\\.`/`\\$` "
                      "这类反斜杠，直接写原字符", line=line_no)
            if BOLD_SPEAKER_RE.match(clean):
                _fail("说话人不要加粗：`**M:**` 改成 `M:`（去掉星号）",
                      line=line_no)
            if "**" in clean:
                _fail("原文不要 Markdown 加粗/斜体：去掉 `**…**`；说话人直接写 "
                      "`M:` / `W:`", line=line_no)
            if clean.startswith("```") or clean.startswith("~~~"):
                _fail("原文不要代码围栏（```）：一行一句编号原子即可",
                      line=line_no)
            if "](" in clean:
                _fail("原文不要 Markdown 链接：直接写句子文本", line=line_no)

            heading = HEADING_RE.match(clean)
            if heading:
                self.handle_heading(len(heading.group(1)), heading.group(2),
                                    line_no)
                continue

            if OLD_ENDING_HINT_RE.match(clean):
                _fail("旧格式结尾句不再支持：把结尾句放进 `## [结尾]` 块，写成 "
                      "`1、That's the end of …`", line=line_no)

            if META_LINE_RE.match(clean):
                self.handle_meta(clean, line_no)
                continue

            if OLD_QUESTIONS_HINT_RE.match(clean) and "based on" in clean.lower():
                _fail("裸 `Questions …` 行不再支持：句首加 `[META] ` 标签；"
                      "音频没说这句就不要写", line=line_no)

            if QUESTION_RE.match(clean):
                self.handle_question(clean, line_no)
                continue

            if re.match(r"^Question\s+\d+\b", clean):
                _fail("`Question N.` 行格式不对：应为 `Question 1. …`"
                      "（题号后跟英文句点，单独一行）", line=line_no)

            if ATOM_RE.match(clean):
                self.handle_atoms(clean, line_no)
                continue

            _fail("无法识别的行（既不是 contract v1 标题、编号原子、"
                  "`[META]` 行、`Question N.` 行，也不是结尾句）：%r"
                  % clean[:80], line=line_no)

        core = self._finalise()
        _validate(core, self.lines)
        return core

    def _finalise(self) -> dict:
        atoms_by_id = _atoms_by_id(self.atoms)
        for block in self.blocks:
            kind = block["kind"]
            raw = block.pop("_raw", None)
            if kind == "title":
                block["text"] = common.norm_ws(
                    " ".join(atoms_by_id[i]["text"] for i in block["atoms"]))
            elif kind == "ending":
                block["text"] = common.norm_ws(
                    " ".join(atoms_by_id[i]["text"] for i in block["atoms"]))
            elif kind == "marker":
                block["text"] = (block["label"] or "") + "."
            elif kind == "directions":
                joined = " ".join(atoms_by_id[i]["text"] for i in block["atoms"])
                # contract v1 keeps one tolerated shape: a numbered
                # `1、Directions.` row is the label, not repeated in the text.
                joined = re.sub(r"^Directions\s*[:：.]?\s*", "", joined,
                                flags=re.I)
                block["text"] = ("Directions: " + joined).strip() if joined else "Directions."
            elif kind in ("questions_meta", "question"):
                block["text"] = common.norm_ws(raw or "")
            elif kind == "body":
                block.pop("text", None)
            else:  # pragma: no cover - guarded by add_block call sites
                _fail("内部错误：未知块类型 %r" % kind)

        stats = {
            "blocks": len(self.blocks),
            "atoms": len(self.atoms),
            "by_kind": {},
            "body_blocks": [b["id"] for b in self.blocks if b["kind"] == "body"],
            "q_ranges": [b["q_range"] for b in self.blocks if b["kind"] == "body"],
        }
        for block in self.blocks:
            stats["by_kind"][block["kind"]] = stats["by_kind"].get(block["kind"], 0) + 1
        titles = [b for b in self.blocks if b["kind"] == "title"]
        endings = [b for b in self.blocks if b["kind"] == "ending"]
        return {
            "format": FORMAT,
            "title": {"text": titles[0]["text"] if titles else ""},
            "ending": {"text": endings[0]["text"] if endings else ""},
            "blocks": self.blocks,
            "atoms": self.atoms,
            "stats": stats,
            "warnings": self.warnings,
            "source_spans": self.spans,
        }


def parse_text(source_text: str, source_name: str = "<source>") -> dict:
    """Parse one contract v1 transcript into the ``blocks.json`` core.

    Returns ``{"format", "title", "ending", "blocks", "atoms", "stats",
    "warnings", "source_spans"}``.  Raises :class:`common.Failure` (exit 1)
    with the offending line number when the document is not contract v1.
    """
    return _Parser(source_text, source_name).run()


def _validate(core: dict, lines) -> None:
    """Every invariant E2 promises to gate e2, checked before writing files."""
    blocks = core["blocks"]
    atoms = core["atoms"]
    spans = core["source_spans"]
    if not blocks:
        _fail("没有任何块")
    if not atoms:
        _fail("没有解析出任何编号原子")

    expected_block_id = 1
    for block in blocks:
        if block["id"] != "b%02d" % expected_block_id:
            _fail("块 id 不连续：期望 b%02d，实际 %s"
                  % (expected_block_id, block["id"]))
        expected_block_id += 1
        kind = block["kind"]
        if kind not in common.BLOCK_KINDS:
            _fail("未知块类型 %r（%s）" % (kind, block["id"]))
        if not block.get("position"):
            _fail("块 %s 没有位置" % block["id"])
        if not isinstance(block.get("line"), int) or block["line"] < 1:
            _fail("块 %s 没有记录源行号 line" % block["id"])
        if kind not in COLLECTING_KINDS and block["atoms"]:
            _fail("块 %s（%s）不该消费原子" % (block["id"], kind))
        if kind == "body":
            if not block["atoms"]:
                _fail("正文块 %s（%s）没有原子" % (block["id"], block.get("label")))
            if not block.get("q_range"):
                _fail("正文块 %s 没有 q_range（篇目标题必须带 [Qx-y]）"
                      % block["id"])
            if not block.get("section"):
                _fail("正文块 %s 没有 Section" % block["id"])
            match = Q_RANGE_RE.match(block["q_range"])
            if not match:
                _fail("正文块 %s 的 Q 范围格式不对：%r"
                      % (block["id"], block["q_range"]))
            first = int(match.group(1))
            last = int(match.group(2)) if match.group(2) else first
            if last < first:
                _fail("正文块 %s 的 Q 范围倒序：%s" % (block["id"], block["q_range"]))
            continue
        if kind == "title" and not block["atoms"]:
            _fail("开场块 %s 至少要有一个编号原子（没有声音就整块不写）"
                  % block["id"])
        if kind == "ending" and not block["atoms"]:
            _fail("结尾块 %s 至少要有一个编号原子（没有声音就整块不写）"
                  % block["id"])
        if kind == "directions" and not block["atoms"]:
            _fail("Directions 块 %s 至少要有一个编号原子（没有声音就不要写 "
                  "### Directions）" % block["id"])
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            _fail("块 %s（%s）没有定型文本" % (block["id"], kind))
        if kind == "marker" and text != (block.get("label") or "") + ".":
            _fail("报幕块 %s 的文本不是「标签.」" % block["id"])

    for index, atom in enumerate(atoms, start=1):
        if atom["id"] != index:
            _fail("原子 id 不是从 1 连续：第 %d 个是 %s" % (index, atom["id"]))
        if not atom["text"]:
            _fail("原子 %d 的文本为空" % atom["id"])
        if atom["speaker"] not in (None, "M", "W"):
            _fail("原子 %d 的 speaker 非法：%r" % (atom["id"], atom["speaker"]))

    if len(spans) != len(atoms):
        _fail("source_spans 有 %d 项，原子有 %d 个" % (len(spans), len(atoms)))
    for index, (span, atom) in enumerate(zip(spans, atoms), start=1):
        if span.get("id") != index or span.get("line") != atom["line"]:
            _fail("source_spans 第 %d 项与原子 %d 不对应" % (index, atom["id"]))
        start = span.get("col_start", -1)
        end = span.get("col_end", -1)
        if not (0 <= start < end):
            _fail("原子 %d 的 source_spans 列号非法：%r" % (atom["id"], span))
        if end > len(lines[atom["line"] - 1].strip()):
            _fail("原子 %d 的 source_spans 超出该行：%r" % (atom["id"], span))

    collected = []
    for block in blocks:
        if block["kind"] in COLLECTING_KINDS:
            collected.extend(block["atoms"])
    if collected != [atom["id"] for atom in atoms]:
        _fail("原子顺序与块归属不一致（收集到 %d 个，实际 %d 个）"
              % (len(collected), len(atoms)))

    titles = [b for b in blocks if b["kind"] == "title"]
    endings = [b for b in blocks if b["kind"] == "ending"]
    if len(titles) > 1:
        _fail("开场块数量应为 0 或 1，实际 %d" % len(titles))
    if titles and blocks[0]["kind"] != "title":
        _fail("`## [开场]` 必须是第一个块，实际第一个块是 %s"
              % blocks[0]["kind"])
    if not titles:
        core["warnings"].append(
            "原文没有 `## [开场]` 块（missing_title），按开场可选处理")
    if len(endings) > 1:
        _fail("结尾块数量应为 0 或 1，实际 %d" % len(endings))
    if endings and blocks[-1]["kind"] != "ending":
        _fail("`## [结尾]` 必须是最后一个块，实际最后一个块是 %s"
              % blocks[-1]["kind"])
    if not endings:
        core["warnings"].append(
            "原文没有 `## [结尾]` 块（missing_ending），已按结尾可选处理")

    # every body block belongs to exactly one earlier 篇目 marker, and every
    # 篇目 marker owns exactly one body block
    markers = [b for b in blocks if b["kind"] == "marker"]
    bodies = [b for b in blocks if b["kind"] == "body"]
    for body in bodies:
        owners = [marker for marker in markers
                  if marker.get("label") == body.get("label")]
        if len(owners) != 1:
            _fail("正文块 %s 找不到唯一的篇目标题（label=%r）"
                  % (body["id"], body.get("label")))
    for marker in markers:
        if not BODY_LABEL_RE.match(marker.get("label") or ""):
            continue
        owned = [body for body in bodies if body.get("label") == marker["label"]]
        if len(owned) != 1:
            _fail("篇目标题 %r 对应 %d 个正文块（应为 1）"
                  % (marker["label"], len(owned)))

    # [META] is optional; when present it must match its body's range.  Every
    # Question row must fill the body range exactly (no missing/gap/duplicate).
    body = None
    body_meta = {}
    body_questions = {}
    question_numbers = []
    for block in blocks:
        kind = block["kind"]
        if kind == "body":
            body = block
            body_meta[body["id"]] = 0
            body_questions[body["id"]] = []
        elif kind == "questions_meta":
            if body is None:
                _fail("[META] 块 %s 前面没有篇目" % block["id"])
            body_meta[body["id"]] += 1
            if body_meta[body["id"]] > 1:
                _fail("篇目 %r 出现了多条 [META]" % body.get("label"))
            if block["q_range"] != body["q_range"]:
                _fail("[META] 块 %s 的 %s 与篇目 %r 的 %s 不一致"
                      % (block["id"], block["q_range"], body.get("label"),
                         body["q_range"]))
        elif kind == "question":
            if body is None:
                _fail("Question 块 %s 前面没有篇目" % block["id"])
            match = Q_RANGE_RE.match(block["q_range"])
            body_questions[body["id"]].append(int(match.group(1)))
            question_numbers.append(int(match.group(1)))
        elif kind in ("marker", "title", "directions", "ending"):
            body = None

    for body in bodies:
        match = Q_RANGE_RE.match(body["q_range"])
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        expected = list(range(first, last + 1))
        actual = body_questions[body["id"]]
        if actual != expected:
            _fail("篇目 %r（%s）的 Question 行不一致：期望 %r，实际 %r"
                  "（缺题/跳号/重复）"
                  % (body.get("label"), body["q_range"], expected, actual))
    if question_numbers:
        expected_numbers = list(range(question_numbers[0],
                                       question_numbers[0] + len(question_numbers)))
        if question_numbers != expected_numbers:
            _fail("题目编号不连续（要求严格递增且步长为 1）：%r"
                  % question_numbers)

    # every non-empty source line is covered by an atom span or a block line
    nonempty = {number for number, raw in enumerate(lines, start=1) if raw.strip()}
    atom_lines = {span["line"] for span in spans}
    block_lines = {block["line"] for block in blocks}
    covered = atom_lines | block_lines
    missing = sorted(nonempty - covered)
    extra = sorted(covered - nonempty)
    if missing or extra:
        _fail("原文有 %d 个非空行，覆盖到 %d 行；漏掉 %r，多出 %r"
              % (len(nonempty), len(covered & nonempty), missing[:10], extra[:10]))


def build_blocks_doc(core: dict, *, doc_id: str, source_path: str,
                     source_sha256: str) -> dict:
    """Compose the on-disk ``blocks.json`` (contract v1 shape)."""
    return {
        "version": 1,
        "doc": doc_id,
        "source": os.path.abspath(source_path),
        "source_sha256": source_sha256,
        "format": core.get("format") or FORMAT,
        "warnings": list(core.get("warnings") or []),
        "atoms_total": len(core["atoms"]),
        "title": core["title"],
        "ending": core["ending"],
        "blocks": core["blocks"],
        "atoms": core["atoms"],
        "source_spans": core.get("source_spans") or [],
    }


def build_dispatch(blocks_doc: dict, run_dir: str, doc_id: str) -> dict:
    """Short-task ready list: exactly one task per body block (接口约定 §4.4)."""
    atoms_by_id = _atoms_by_id(blocks_doc["atoms"])
    tasks = []
    for block in blocks_doc["blocks"]:
        if block["kind"] != "body":
            continue
        tasks.append(
            {
                "task_id": "%s-%s" % (doc_id, block["id"]),
                "block": block["id"],
                "role": "segmenter",
                "kind": "body",
                "atoms": [
                    {"id": atom_id, "speaker": atoms_by_id[atom_id]["speaker"],
                     "text": atoms_by_id[atom_id]["text"]}
                    for atom_id in block["atoms"]
                ],
                "position": block["position"],
                "receipt": os.path.join(common.doc_dir(run_dir, doc_id), "blocks",
                                        block["id"] + ".json"),
                "brief": "references/subagent-briefs.md#segmenter",
            }
        )
    return {"version": 1, "doc": doc_id, "tasks": tasks}


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "parse_source.py",
        "E2：把一份 contract v1 听力原文解析成 blocks.json（原文真源）+ "
        "dispatch.json（短任务就绪清单）。",
    )
    parser.add_argument("--run", required=True, help="运行目录 $RUN（含 run.json）")
    parser.add_argument("--doc", required=True, help="文档 id，例如 d01")
    parser.add_argument("--json", action="store_true", help="只输出 RUN/JSON 这类机器可读行")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = os.path.abspath(args.run)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE, "doc": args.doc}
    try:
        common.load_run(run_dir)
        doc = common.load_doc(run_dir, args.doc)
        log = common.StageLog(run_dir, STAGE, args.doc)

        text_path = doc["text"]
        if not os.path.isfile(text_path):
            raise common.Failure(common.EXIT_USAGE, "原文文件不存在：%s" % text_path)
        actual_sha = common.sha256_file(text_path)
        recorded_sha = doc.get("text_sha256") or ""
        if not recorded_sha:
            raise common.Failure(common.EXIT_USAGE, "doc.json 没有 text_sha256，请重跑 bootstrap")
        if actual_sha != recorded_sha:
            raise common.Failure(
                common.EXIT_ASK,
                "原文内容变了（doc.json 记的 sha 与当前不符）：%s\n"
                "请重跑 bootstrap.py 生成新 run（旧 run 会改名保留），不要在旧 run 上继续。"
                % text_path,
                reason="text_changed",
            )
        source_text = common.read_text(text_path)
        core = parse_text(source_text, source_name=text_path)
        blocks_doc = build_blocks_doc(core, doc_id=args.doc, source_path=text_path,
                                      source_sha256=actual_sha)
        dispatch_doc = build_dispatch(blocks_doc, run_dir, args.doc)

        paths = common.doc_dir(run_dir, args.doc)
        common.ensure_dir(paths)
        blocks_path = os.path.join(paths, "blocks.json")
        dispatch_path = os.path.join(paths, "dispatch.json")
        common.atomic_write_json(blocks_path, blocks_doc)
        common.atomic_write_json(dispatch_path, dispatch_doc)

        stats = core["stats"]
        result.update(
            {
                "ok": True,
                "exit_code": common.EXIT_OK,
                "format": core.get("format") or FORMAT,
                "blocks": stats["blocks"],
                "atoms": stats["atoms"],
                "warnings": list(core.get("warnings") or []),
                "need_task_blocks": len(dispatch_doc["tasks"]),
                "kind_counts": stats["by_kind"],
                "q_ranges": stats["q_ranges"],
                "blocks_path": blocks_path,
                "dispatch_path": dispatch_path,
            }
        )
        if log:
            log("parsed %s: format=%s atoms=%d blocks=%d body=%d"
                % (args.doc, core.get("format"), stats["atoms"], stats["blocks"],
                   len(dispatch_doc["tasks"])))
            for warning in core["warnings"]:
                log("WARN " + warning)
            log("wrote %s" % blocks_path)
            log("wrote %s" % dispatch_path)

        if not args.json:
            print("DOC=%s  原文=%s" % (args.doc, text_path))
            print("格式=%s  原子=%d  块=%d  正文块（需回执）=%d"
                  % (core.get("format"), stats["atoms"], stats["blocks"],
                     len(dispatch_doc["tasks"])))
            print("类型分布：" + "、".join(
                "%s %d" % (kind, count) for kind, count in stats["by_kind"].items()))
            print("Q 范围：" + "、".join(stats["q_ranges"]))
            for warning in core["warnings"]:
                print("WARN " + warning)
            print("已写：%s" % blocks_path)
            print("已写：%s" % dispatch_path)
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
