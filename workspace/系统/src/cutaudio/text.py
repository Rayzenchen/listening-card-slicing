"""Parse the user's transcript file into segments.

Rules (documented in AGENTS.md):
  * UTF-8 text; every non-empty line is one segment, by default.
  * With --segments blank_line, an empty line separates segments and a segment may
    span several lines (the lines are joined with a single space / no space for zh).
  * Original text is preserved verbatim (BOM stripped, trailing whitespace trimmed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class TextError(RuntimeError):
    pass


@dataclass
class Segment:
    index: int
    text: str


def looks_chinese(text: str) -> bool:
    cjk = len(CJK_RE.findall(text))
    letters = len(re.findall(r"[A-Za-z]", text))
    return cjk > letters


def join_lines(lines: list[str]) -> str:
    if looks_chinese("\n".join(lines)):
        return "".join(lines)
    return " ".join(lines)


def load_text(path: str, mode: str = "line") -> tuple[list[Segment], str]:
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw:
        raise TextError(f"text file is empty: {path}")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TextError(f"text file is not valid UTF-8 ({exc}); re-save it as UTF-8") from exc

    if not content.strip():
        raise TextError(f"text file has no non-empty text: {path}")

    segs: list[str] = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if mode == "blank_line":
        buf: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped:
                buf.append(stripped)
            elif buf:
                segs.append(join_lines(buf))
                buf = []
        if buf:
            segs.append(join_lines(buf))
    else:
        for line in lines:
            stripped = line.strip()
            if stripped:
                segs.append(stripped)

    if not segs:
        raise TextError(f"no segments found in {path}")
    language_hint = "zh" if looks_chinese(content) else "en"
    return [Segment(i + 1, s) for i, s in enumerate(segs)], language_hint


def normalize_for_match(text: str) -> str:
    """Lowercase, drop punctuation/spaces - used only for comparison."""
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text.lower())
