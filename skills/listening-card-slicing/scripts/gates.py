#!/usr/bin/env python3
"""gates.py -- every checkpoint E1-E12, recomputed from the files on disk.

Nothing here trusts a recorded "done".  Each check opens the artifacts again,
recomputes the numbers and compares them with what the stage claimed.  A check
that cannot run is a FAIL, never a silent pass.  WARN lines (script drift, a
downstream-extended pack manifest, an F1 first-segment hint) do not fail the
gate.

The stages are numbered continuously E1-E12.  ``phase1`` is the stage-one
aggregate entry (E1, then E2-E7 per document, stopping at the first FAIL).
``e8`` is the final global stage-one gate (cleanup/report wrap-up); ``e5``
is the translation + 6-column manifest approval gate, ``e6`` is the cut and
``e7`` the pack.  The module is imported by
``gate.py`` (the CLI), by ``cut_stage.py`` (it must refuse to cut before E4
passes) and by ``report.py`` (it reads the gate receipts, never re-runs a
check).  ``gate.py`` owns the stdout contract; this module only produces
:class:`Checker` objects.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import zipfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import assemble  # noqa: E402  (reuse the E4 coverage/schema checks)
import bootstrap  # noqa: E402  (reuse the E1 environment probe)
import cards_apply  # noqa: E402  (the one cards_sha256 implementation, E9-E11)
import common  # noqa: E402
import markji_push  # noqa: E402  (E11 re-reads the cloud through the channel)

STAGES = ("e1", "e2", "e3", "e4", "e5", "e6", "e7",
          "e8", "e9", "e10", "e11", "e12")
#: 阶段一聚合入口（不属于 E 编号；E8 仍是最后一道全局关）。
PHASE1_STAGE = "phase1"
ALL_STAGES = STAGES + (PHASE1_STAGE,)
#: 阶段一的文档级关卡 e2-e7（e8 的聚合只复算阶段一，见 PHASE2_STAGES）。
DOC_STAGES = ("e2", "e3", "e4", "e5", "e6", "e7")
PHASE2_STAGES = ("e9", "e10", "e11", "e12")

OK = "ok"
WARN = "WARN"
FAIL = "FAIL"

MANIFEST_KEEP_HEAD = "## 进卡组清单"
PACK_SUFFIX = "_音频切分（进牌组）"
ROW_NUMBER_RE = re.compile(r"^\d{3,}$")
#: A delivered segment: old packs are ``<NNN>.wav``, new packs ``<NNN>.mp3``.
AUDIO_SEGMENT_RE = re.compile(r"^(\d{3,})\.(wav|mp3)$")


# --- checker ----------------------------------------------------------------


class Checker:
    """Collects ``check <label>: ok|WARN|FAIL`` lines and the final verdict."""

    def __init__(self, stage: str):
        self.stage = stage
        self.checks = []

    # recording ---------------------------------------------------------
    def ok(self, label: str, message: str | None = None) -> None:
        self.checks.append({"label": label, "status": OK, "message": message})

    def warn(self, label: str, message: str) -> None:
        self.checks.append({"label": label, "status": WARN, "message": message})

    def fail(self, label: str, message: str) -> None:
        self.checks.append({"label": label, "status": FAIL, "message": message})

    def expect(self, label: str, condition, message: str) -> bool:
        if condition:
            self.ok(label)
        else:
            self.fail(label, message)
        return bool(condition)

    def check(self, label: str, condition, message: str) -> bool:
        return self.expect(label, condition, message)

    # results -----------------------------------------------------------
    @property
    def failures(self):
        return [check for check in self.checks if check["status"] == FAIL]

    @property
    def warnings(self):
        return [check for check in self.checks if check["status"] == WARN]

    @property
    def passed(self) -> bool:
        return not self.failures

    def line(self, check: dict) -> str:
        if check["status"] == OK:
            return "check %s: ok" % check["label"]
        return "check %s: %s %s" % (check["label"], check["status"],
                                    check.get("message") or "")

    def lines(self) -> list:
        return [self.line(check) for check in self.checks]

    def verdict(self) -> str:
        if self.passed:
            return "PASS"
        names = "、".join(check["label"] for check in self.failures[:5])
        if len(self.failures) > 5:
            names += " 等 %d 项" % len(self.failures)
        return "FAIL: %s" % names

    def payload(self) -> dict:
        return {
            "stage": self.stage,
            "pass": self.passed,
            "checks": [dict(check) for check in self.checks],
            "warnings": [check["label"] for check in self.warnings],
            "failures": [check["label"] for check in self.failures],
        }


# --- gate receipts ----------------------------------------------------------


def receipt_path(run_dir: str, stage: str, doc_id: str | None = None) -> str:
    """Where a gate verdict is written back (report/status read it, never re-run)."""
    run_dir = os.path.abspath(run_dir)
    if stage in DOC_STAGES or stage in PHASE2_STAGES:
        if not doc_id:
            raise common.Failure(common.EXIT_USAGE,
                                 "%s 关卡需要 doc id 才能写回执" % stage)
        return os.path.join(common.doc_dir(run_dir, doc_id), "receipts",
                            "gate-%s.json" % stage)
    return os.path.join(run_dir, "receipts", "gate-%s.json" % stage)


def write_receipt(run_dir: str, stage: str, doc_id: str | None,
                  checker: Checker) -> str:
    """Record one verdict so ``report.py``/``status.py`` can summarise it."""
    payload = {
        "version": 1,
        "stage": stage,
        "doc": doc_id,
        "at": common.now_iso(),
        "pass": bool(checker.passed),
        "verdict": checker.verdict(),
        "checks": [dict(check) for check in checker.checks],
    }
    target = receipt_path(run_dir, stage, doc_id)
    common.atomic_write_json(target, payload)
    return target


def read_receipt(run_dir: str, stage: str, doc_id: str | None = None):
    """The latest recorded verdict, or ``None`` when no gate ran yet."""
    try:
        return common.read_json(receipt_path(run_dir, stage, doc_id))
    except (common.Failure, OSError):
        return None


# --- run / document context -------------------------------------------------


class GateRun:
    """``run.json`` plus lazy per-document contexts."""

    def __init__(self, run_dir: str):
        self.run_dir = os.path.abspath(run_dir)
        self.run = common.load_run(self.run_dir)
        self.work = os.path.abspath(self.run.get("work") or "")
        self.doc_entries = [entry for entry in (self.run.get("docs") or [])
                            if isinstance(entry, dict) and entry.get("id")]
        self._docs = {}

    def doc(self, doc_id: str) -> "DocContext":
        if doc_id not in self._docs:
            self._docs[doc_id] = DocContext(self, doc_id)
        return self._docs[doc_id]

    @property
    def doc_ids(self) -> list:
        return [entry["id"] for entry in self.doc_entries]

    def entry(self, doc_id: str) -> dict:
        for entry in self.doc_entries:
            if entry["id"] == doc_id:
                return entry
        raise common.Failure(common.EXIT_USAGE, "run.json.docs 里没有 %s" % doc_id)


class DocContext:
    """One document: paths resolved once, artifacts loaded lazily."""

    def __init__(self, gate: GateRun, doc_id: str):
        self.gate = gate
        self.id = doc_id
        self.entry = gate.entry(doc_id)
        self.doc = common.load_doc(gate.run_dir, doc_id)
        # 缺字段不是崩溃：e8–e12（以及阶段一）都要给人话 FAIL（D1 报的 KeyError）。
        missing = [key for key in ("audio", "text", "stem", "result_dir",
                                   "pack_dir", "manifest")
                   if not self.doc.get(key)]
        if missing:
            raise common.Failure(
                common.EXIT_FAIL,
                "doc.json 缺字段 %s（回 E1 重新 bootstrap 生成 doc.json）"
                % "、".join(missing),
                reason="doc_field_missing",
            )
        self.dir = common.doc_dir(gate.run_dir, doc_id)
        self.audio = os.path.abspath(self.doc["audio"])
        self.text = os.path.abspath(self.doc["text"])
        self.stem = self.doc["stem"]
        self.result_dir = common.resolve_work_path(gate.work, self.doc["result_dir"])
        self.pack_dir = common.resolve_work_path(gate.work, self.doc["pack_dir"])
        self.manifest = common.resolve_work_path(gate.work, self.doc["manifest"])
        # The $RUN copy is the authoritative frozen manifest; the 产出区 copy is
        # only the review-period copy and may be deleted by cleanup (E7).
        self.run_manifest = os.path.join(self.dir, "切分清单.md")
        self.area = os.path.dirname(os.path.abspath(self.pack_dir))
        self._cache = {}

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def json(self, name: str):
        return common.read_json(self.path(name))

    def cached(self, key: str, loader):
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    # common artifacts ---------------------------------------------------
    def blocks(self):
        return self.cached("blocks", lambda: self.json("blocks.json"))

    def dispatch(self):
        return self.cached("dispatch", lambda: self.json("dispatch.json"))

    def segments(self):
        return self.cached("segments", lambda: self.json("segments.json"))

    def cut_report(self):
        return self.cached("cut_report", lambda: self.json("cut-report.json"))

    def pack_report(self):
        return self.cached("pack", lambda: self.json("pack.json"))

    def baseline_manifest(self) -> tuple:
        """(path, text) of the 5-column manifest used by e4/e6/e7.

        0.0.8: E5 translate_apply overwrites $RUN/切分清单.md with the
        6-column version and freezes the E4 five columns in manifest-5col.md.
        Post-E5 replays of e4/e6/e7 keep auditing that frozen snapshot.

        A **5-column** $RUN manifest wins over the snapshot: E5 always leaves
        the live copy 6 columns, so a 5-column live copy means E4 was re-run
        after E5 (regroup) and the snapshot is stale.  This is what lets
        「改分组 → 回 E4」 pass e4 again and invalidate the old cut binding
        (e6) until E5 is redone.
        """
        run_text = None
        if os.path.isfile(self.run_manifest):
            run_text = common.read_text(self.run_manifest)
            if common.manifest_columns(run_text) == 5:
                return self.run_manifest, run_text
        snapshot = self.path("manifest-5col.md")
        if os.path.isfile(snapshot):
            return snapshot, common.read_text(snapshot)
        if run_text is not None:
            raise common.Failure(
                common.EXIT_FAIL,
                "缺 5 列基线清单（manifest-5col.md；$RUN/切分清单.md 已是 6 列）：%s"
                % snapshot)
        raise common.Failure(common.EXIT_FAIL,
                             "缺切分清单：%s（先跑 E4 assemble / E5 translate_apply）"
                             % self.run_manifest)

    def manifest_rows(self):
        def load():
            path, text = self.baseline_manifest()
            return common.parse_manifest_text(text, path)
        return self.cached("manifest", load)


# --- small helpers -----------------------------------------------------------


def _manifest_rows_tolerant(text: str) -> list:
    """Read the first five cells of every table row (A11 allows extra columns)."""
    rows = []
    for line in text.split("\n"):
        if not line.startswith("|"):
            continue
        cells = common.split_table_cells(line)
        if len(cells) < 5 or not ROW_NUMBER_RE.match(cells[0]):
            continue
        keep = cells[2]
        if keep not in ("是", "否"):
            raise ValueError("清单行 %s 的进卡组列不是 是/否" % cells[0])
        rows.append({
            "n": int(cells[0]),
            "type": cells[1],
            "keep": keep == "是",
            "position": cells[3].replace("\\|", "|"),
            "display": cells[4].replace("\\|", "|"),
        })
    return rows


def _quote_lines(text: str) -> list:
    return [line for line in text.split("\n") if line.startswith("> ")]


def _tail_from_keep(text: str):
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("%s" % MANIFEST_KEEP_HEAD):
            return "\n".join(lines[index:])
    return None


def manifest_extension_compatible(package_text: str, reference_text: str) -> tuple:
    """A11: is ``package_text`` the frozen manifest plus right-side additions?

    Returns ``(compatible, reason)``.  Only these may differ: quote lines after
    the first three, table columns after the fifth.  Everything the phase-two
    consumer relies on (title, first three quote lines, first five header
    cells, five separator groups, first five cells of every row, the whole
    tail from ``## 进卡组清单``) must be byte-identical.
    """
    package_lines = package_text.split("\n")
    reference_lines = reference_text.split("\n")
    if not package_lines or not reference_lines:
        return False, "清单为空"
    if package_lines[0] != reference_lines[0]:
        return False, "标题行不同"
    pkg_quotes = _quote_lines(package_text)[:3]
    ref_quotes = _quote_lines(reference_text)[:3]
    if len(ref_quotes) != 3:
        return False, "基准清单的引用块不足 3 行"
    if pkg_quotes != ref_quotes:
        return False, "引用块前 3 行不同"
    pkg_head = [line for line in package_lines if line.startswith("| 行 |")]
    ref_head = [line for line in reference_lines if line.startswith("| 行 |")]
    if not pkg_head or not ref_head:
        return False, "缺表头"
    pkg_cells = common.split_table_cells(pkg_head[0])
    ref_cells = common.split_table_cells(ref_head[0])
    if len(pkg_cells) < 5 or pkg_cells[:5] != ref_cells[:5]:
        return False, "表头前 5 格不同"
    pkg_sep = [line for line in package_lines if line.startswith("|---")]
    ref_sep = [line for line in reference_lines if line.startswith("|---")]
    if not pkg_sep or not ref_sep:
        return False, "缺分隔行"
    pkg_groups = common.split_table_cells(pkg_sep[0])
    ref_groups = common.split_table_cells(ref_sep[0])
    if len(pkg_groups) < 5 or pkg_groups[:5] != ref_groups[:5]:
        return False, "分隔行前 5 组不同"
    try:
        pkg_rows = _manifest_rows_tolerant(package_text)
        ref_rows = _manifest_rows_tolerant(reference_text)
    except ValueError as exc:
        return False, str(exc)
    if len(pkg_rows) != len(ref_rows):
        return False, "行数不同（%d vs %d）" % (len(pkg_rows), len(ref_rows))
    for left, right in zip(pkg_rows, ref_rows):
        if (left["n"], left["type"], left["keep"], left["position"], left["display"]) != \
                (right["n"], right["type"], right["keep"], right["position"],
                 right["display"]):
            return False, "第 %03d 行前 5 格不同" % right["n"]
    pkg_tail = _tail_from_keep(package_text)
    ref_tail = _tail_from_keep(reference_text)
    if pkg_tail is None or ref_tail is None:
        return False, "缺「进卡组清单」尾部"
    if pkg_tail != ref_tail:
        return False, "尾部区间/固定句不同"
    return True, ""


def wav_duration_sec(path: str) -> float:
    """Duration of a RIFF/WAVE file, including IEEE-float WAVs (format 3).

    ``wave`` in the standard library only understands integer PCM, but the
    cutter intentionally writes bit-exact float32 pieces, so the chunks are
    read directly here.
    """
    with open(path, "rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError("不是 RIFF/WAVE 文件")
        fmt = None
        fact = None
        data_size = None
        while True:
            chunk = handle.read(8)
            if len(chunk) < 8:
                break
            chunk_id = chunk[:4]
            size = struct.unpack("<I", chunk[4:])[0]
            if chunk_id == b"fmt ":
                payload = handle.read(size)
                if len(payload) < 16:
                    raise ValueError("fmt 块太短")
                fmt = struct.unpack("<HHIIHH", payload[:16])
                handle.seek(size & 1, 1)
            elif chunk_id == b"fact":
                payload = handle.read(size)
                if len(payload) >= 4:
                    fact = struct.unpack("<I", payload[:4])[0]
                handle.seek(size & 1, 1)
            elif chunk_id == b"data":
                data_size = size
                handle.seek(size + (size & 1), 1)
            else:
                handle.seek(size + (size & 1), 1)
    if fmt is None:
        raise ValueError("缺 fmt 块")
    channels, sample_rate, block_align = fmt[1], fmt[2], fmt[4]
    if sample_rate <= 0 or block_align <= 0:
        raise ValueError("fmt 参数非法")
    if fact is not None:
        frames = fact
    elif data_size is not None:
        frames = data_size // block_align
    else:
        raise ValueError("缺 fact/data 块")
    return frames / float(sample_rate)


def _sha(path: str):
    try:
        return common.sha256_file(path)
    except OSError:
        return None


def _package_drift(gate: GateRun, checker: Checker) -> None:
    """接口约定 §7: script-tree drift is a WARN, never a FAIL."""
    recorded = ((gate.run.get("package") or {}).get("scripts_sha256") or "")
    if not recorded:
        checker.warn("package", "run.json 没有 package.scripts_sha256")
        return
    current = common.scripts_tree_sha()
    if current != recorded:
        checker.warn("package", "drift（recorded=%s… current=%s…；不要跨版本混跑）"
                     % (recorded[:8], current[:8]))
    else:
        checker.ok("package")


def _load_blocks(gate: GateRun, doc: DocContext):
    return assemble.load_blocks(gate.run_dir, doc.doc, doc.id)


def _manifest_reference(doc: DocContext):
    """The frozen 5-column reference: manifest-5col.md after E5, else $RUN."""
    parsed = doc.manifest_rows()
    return parsed, parsed["text"]


def _check_atom_fields(atoms, checker: Checker, label: str) -> bool:
    if not isinstance(atoms, list) or not atoms:
        checker.fail(label, "原子列表为空")
        return False
    for index, atom in enumerate(atoms, start=1):
        if atom.get("id") != index:
            checker.fail(label, "原子 id 不是从 1 连续（第 %d 个是 %r）" % (index, atom.get("id")))
            return False
        if not isinstance(atom.get("text"), str) or not atom["text"]:
            checker.fail(label, "原子 %d 没有文本" % index)
            return False
        if atom.get("speaker") not in (None, "M", "W"):
            checker.fail(label, "原子 %d speaker 非法：%r" % (index, atom.get("speaker")))
            return False
    return True


#: contract v1 §2: Q16（单题）or Q16-18（连续范围）。
Q_RANGE_RE = re.compile(r"^Q(\d+)(?:-(\d+))?$")


def _is_q_range(value) -> bool:
    """True when value is a contract [Qx-y] range with x <= y."""
    if not isinstance(value, str):
        return False
    match = Q_RANGE_RE.match(value)
    if not match:
        return False
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    return start <= end


# --- E1 ---------------------------------------------------------------------


def check_e1(gate: GateRun, checker: Checker) -> None:
    checker.expect("run", isinstance(gate.run, dict) and gate.run.get("version") == 1,
                   "run.json 不是 version 1")
    checker.expect("skill_version", bool(str(gate.run.get("skill_version") or "").strip()),
                   "run.json 没有记录 skill_version")
    checker.expect("work", os.path.isdir(gate.work), "工作区不存在：%s" % gate.work)
    checker.expect("docs", bool(gate.doc_entries), "run.json.docs 为空")

    try:
        phase = str((gate.run.get("options") or {}).get("phase") or "auto")
        env = bootstrap.probe_env(gate.work, phase)
        problems = bootstrap.env_problems(env, phase)
    except Exception as exc:  # noqa: BLE001 - probe must never crash the gate
        checker.fail("env", "环境探测失败：%s" % exc)
    else:
        if problems["missing"]:
            checker.fail("env", "环境缺项：%s" % "、".join(problems["missing"]))
        else:
            checker.ok("env", "6 项必需环境齐全")

    # 0.0.5: E1 preflight lives in run.json.preflight (the old standalone
    # e1-preflight.json is gone).  A 0.0.4 run must FAIL with a readable hint.
    preflight = gate.run.get("preflight")
    if not isinstance(preflight, dict):
        checker.fail(
            "preflight",
            "run.json 缺 preflight 对象（0.0.4 及更早的 run 只有 e1-preflight.json；"
            "请重跑 bootstrap.py 生成 run.json.preflight）")
    else:
        checker.ok("preflight")
        checker.expect("preflight_sha",
                       preflight.get("input_sha") == gate.run.get("input_sha"),
                       "run.json.preflight.input_sha 与 run.json.input_sha 不一致")

    for doc_id in gate.doc_ids:
        label = "%s doc" % doc_id
        try:
            doc = gate.doc(doc_id)
        except common.Failure as exc:
            checker.fail(label, exc.message)
            continue
        checker.expect(label, os.path.isfile(doc.audio) and os.path.isfile(doc.text),
                       "音频或原文不存在")
        if not (os.path.isfile(doc.audio) and os.path.isfile(doc.text)):
            continue
        audio_sha = _sha(doc.audio)
        text_sha = _sha(doc.text)
        checker.expect("%s audio_sha" % doc_id, audio_sha == doc.doc.get("audio_sha256"),
                       "音频 sha 与 doc.json 不符")
        checker.expect("%s text_sha" % doc_id, text_sha == doc.doc.get("text_sha256"),
                       "原文 sha 与 doc.json 不符")
        checker.expect("%s text_nonempty" % doc_id,
                       os.path.getsize(doc.text) > 0, "原文为空")
        # run.json entry and doc.json must describe the same files
        entry_audio = os.path.abspath(os.path.join(gate.work, doc.entry.get("audio") or ""))
        entry_text = os.path.abspath(os.path.join(gate.work, doc.entry.get("text") or ""))
        checker.expect("%s paths" % doc_id,
                       entry_audio == doc.audio and entry_text == doc.text,
                       "run.json 与 doc.json 的音频/原文路径不一致")
        # A3: an existing pack must have been archived (original path gone).
        # AMENDMENTS-2 A2-7: `--phase 2`（只制卡）reuses the pack on purpose.
        existing = doc.doc.get("existing_pack")
        phase = (gate.run.get("options") or {}).get("phase")
        if not existing:
            checker.ok("%s pack_conflict" % doc_id)
        elif phase == "2" and doc.doc.get("reuse_pack"):
            checker.ok("%s pack_conflict" % doc_id, "reuse")
        else:
            archived = [item for item in (gate.run.get("archived") or [])
                        if isinstance(item, dict) and item.get("doc") == doc_id]
            archived_match = any(os.path.abspath(item.get("from") or "") ==
                                 os.path.abspath(existing) for item in archived)
            checker.expect("%s pack_conflict" % doc_id,
                           archived_match and not os.path.exists(existing),
                           "已有同名包未归档：%s" % existing)


# --- E2 ---------------------------------------------------------------------


def check_e2(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    try:
        blocks = _load_blocks(gate, doc)
    except common.Failure as exc:
        checker.fail("blocks", exc.message)
        return
    checker.ok("blocks")
    source = blocks.get("source") or ""
    checker.expect("source_path", os.path.abspath(source) == doc.text,
                   "blocks.json 的 source 与 doc.json 不一致")
    current_sha = _sha(doc.text)
    checker.expect("source_sha", blocks.get("source_sha256") == current_sha,
                   "blocks.json 未绑定当前原文 sha")
    atoms = blocks.get("atoms") or []
    checker.expect("atoms_total", blocks.get("atoms_total") == len(atoms),
                   "atoms_total 与实际原子数不符")
    _check_atom_fields(atoms, checker, "atoms")
    block_list = blocks.get("blocks") or []
    checker.expect("blocks_total", bool(block_list), "blocks 为空")
    if not block_list:
        return
    ids = [block.get("id") for block in block_list]
    checker.expect("block_ids", ids == ["b%02d" % index for index in range(1, len(ids) + 1)],
                   "块 id 不是 b01.. 连续")
    frame = {"title": 0, "ending": 0}
    collected = []
    all_atoms = [atom["id"] for atom in atoms]
    atoms_by_id = {atom["id"]: atom for atom in atoms}
    body_ids = []
    bad = []
    # contract v1 §2: only these four kinds carry numbered atoms.  title /
    # ending are layout blocks whose text is the single-space join of their
    # atoms; a present-but-empty one is a hard failure. 0.0.7 removed the old
    # test_intro kind entirely (old transcripts never reach here: E2 rejects
    # them at parse time, so the gate keeps no legacy compatibility layer).
    collecting_kinds = ("title", "directions", "body", "ending")
    for block in block_list:
        kind = block.get("kind")
        block_id = block.get("id")
        if kind not in common.BLOCK_KINDS:
            bad.append("块 %s 类型未知：%r" % (block_id, kind))
            continue
        if kind in frame:
            frame[kind] += 1
        block_atoms = block.get("atoms") or []
        if block_atoms:
            if kind in collecting_kinds:
                collected.extend(block_atoms)
                unknown = [atom_id for atom_id in block_atoms
                           if atom_id not in atoms_by_id]
                if unknown:
                    bad.append("块 %s（%s）引用了不存在的原子 %s"
                               % (block_id, kind,
                                  "、".join(str(item) for item in unknown)))
            else:
                bad.append("块 %s（%s）不该带原子" % (block_id, kind))
        if kind in ("title", "ending") and not block_atoms:
            bad.append("块 %s（%s）没有原子（空块）" % (block_id, kind))
        if kind == "body":
            body_ids.append(block_id)
            if not block_atoms:
                bad.append("正文块 %s 没有原子" % block_id)
            if not block.get("position"):
                bad.append("正文块 %s 缺 position" % block_id)
            if not _is_q_range(block.get("q_range")):
                bad.append("正文块 %s 的 q_range 缺失或格式不对（%r）"
                           % (block_id, block.get("q_range")))
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            bad.append("块 %s（%s）没有定型文本" % (block_id, kind))
            continue
        if kind == "marker" and text != (block.get("label") or "") + ".":
            bad.append("报幕块 %s 的文本不是「标签.」" % block_id)
        elif kind == "directions" and not (text == "Directions."
                                           or text.startswith("Directions: ")):
            # 0.0.6 (A4): 编号型「1、Directions.」只有指令没有正文时定型为
            # 「Directions.」；带正文的仍是「Directions: ...」。
            bad.append("Directions 块 %s 的文本既不是「Directions.」也不以"
                       "「Directions: 」开头" % block_id)
        elif kind in ("title", "ending"):
            # contract v1 §2: 开场/结尾的文本 = 原子单空格 join。
            expected = " ".join(atoms_by_id[atom_id]["text"]
                                for atom_id in block_atoms
                                if atom_id in atoms_by_id)
            if text != expected:
                bad.append("%s块 %s 的文本不是原子的单空格合并"
                           % ("开场" if kind == "title" else "结尾", block_id))
        elif kind == "questions_meta" and text.startswith("[META]"):
            bad.append("questions_meta 块 %s 的文本仍带着 [META] 标签（应剥掉）"
                       % block_id)
    checker.expect("block_props", not bad, "；".join(bad[:3]))
    # contract v1 §3.3: [META] is optional; when present it belongs to the
    # body block right before it and must carry the same q_range.  A body
    # without a meta line is complete on its own (0.0.7 dropped the old
    # "body must be followed by questions_meta" assumption).
    meta_bad = []
    for index, block in enumerate(block_list):
        if block.get("kind") != "questions_meta":
            continue
        previous = block_list[index - 1] if index else None
        if previous is None or previous.get("kind") != "body":
            meta_bad.append("questions_meta 块 %s 不紧跟正文块" % block.get("id"))
            continue
        if block.get("q_range") != previous.get("q_range"):
            meta_bad.append("[META] 块 %s 的 q_range（%r）与正文块 %s（%r）不一致"
                            % (block.get("id"), block.get("q_range"),
                               previous.get("id"), previous.get("q_range")))
    checker.expect("meta_q_range", not meta_bad, "；".join(meta_bad[:3]))
    # contract v1: the opening title and the ending are both 0/1.  Zero is
    # legal when the parser recorded missing_title / missing_ending; more than
    # one is still a structural error.
    warnings = [str(item) for item in blocks.get("warnings") or []]
    if frame["title"] > 1:
        checker.fail("titles", "开场标题数量超过 1（%d 个）" % frame["title"])
    elif frame["title"] == 0:
        checker.expect("titles", any("missing_title" in item for item in warnings),
                       "原文没有开场标题，warnings 里也没有 missing_title")
    else:
        checker.expect("title_first", block_list[0].get("kind") == "title",
                       "开场标题不在第一个块")
        checker.ok("titles")
    if frame["ending"] > 1:
        checker.fail("endings", "结尾数量超过 1（%d 个）" % frame["ending"])
    elif frame["ending"] == 0:
        checker.expect("endings", any("missing_ending" in item for item in warnings),
                       "没有结尾块，warnings 里也没有 missing_ending")
    else:
        checker.ok("endings")
        checker.expect("ending_last", block_list[-1].get("kind") == "ending",
                       "结尾不在最后")
    checker.expect("atoms_order", collected == all_atoms,
                   "块没有按原文顺序消费全部原子（%d vs %d）"
                   % (len(collected), len(all_atoms)))

    # 0.0.5: coverage is recomputed from atom spans + blocks[].line; the old
    # line_coverage map no longer exists.
    try:
        source_text = common.read_text(doc.text)
    except common.Failure as exc:
        checker.fail("source_coverage", exc.message)
    else:
        source_lines = source_text.split("\n")
        nonempty = {line for line, raw in enumerate(source_lines, start=1)
                    if raw.strip()}
        spans = blocks.get("source_spans")
        if isinstance(spans, list) and spans:
            atom_lines = {span.get("line") for span in spans
                          if isinstance(span, dict)
                          and isinstance(span.get("line"), int)}
        else:
            # old runs (pre-0.0.4) carry no source_spans; atoms[].line is the
            # same fact and keeps them readable.
            atom_lines = {atom.get("line") for atom in atoms
                          if isinstance(atom.get("line"), int)}
        block_lines = {block.get("line") for block in block_list
                       if isinstance(block.get("line"), int)}
        covered = atom_lines | block_lines
        checker.expect("source_coverage", covered == nonempty,
                       "原文非空行 %d 个，atom span/block.line 只覆盖 %d 个"
                       "（漏 %r，多 %r）"
                       % (len(nonempty), len(covered & nonempty),
                          sorted(nonempty - covered)[:8],
                          sorted(covered - nonempty)[:8]))
        bad_block_lines = [block.get("id") for block in block_list
                           if not isinstance(block.get("line"), int)
                           or block.get("line") not in nonempty]
        checker.expect("block_lines", not bad_block_lines,
                       "块的 line 不是非空源行：%r" % bad_block_lines[:5])

        # 0.0.4 (A): blocks.json.source_spans is a top-level list aligned with
        # atoms (id i+1, same line, 0-based right-open columns).  Optional: old
        # runs and the legacy profile do not carry the field and stay valid.
        if isinstance(spans, list) and spans:
            span_bad = []
            atom_lines = [atom.get("line") for atom in atoms]
            if len(spans) != len(atoms):
                span_bad.append("source_spans 有 %d 项，atoms 有 %d 个"
                                % (len(spans), len(atoms)))
            elif [span.get("id") if isinstance(span, dict) else None
                  for span in spans] != list(range(1, len(atoms) + 1)):
                span_bad.append("source_spans 的 id 不是 1..%d 连续" % len(atoms))
            for index, span in enumerate(spans):
                if not isinstance(span, dict):
                    span_bad.append("第 %d 个 span 不是对象" % (index + 1))
                    continue
                line = span.get("line")
                start = span.get("col_start")
                end = span.get("col_end")
                if not all(isinstance(value, int) for value in (line, start, end)):
                    span_bad.append("第 %d 个 span 字段不是整数" % (index + 1))
                    continue
                if index < len(atom_lines) and line != atom_lines[index]:
                    span_bad.append("第 %d 个 span 行号 %r ≠ 原子行号 %r"
                                    % (index + 1, line, atom_lines[index]))
                    continue
                if not 1 <= line <= len(source_lines):
                    span_bad.append("第 %d 个 span 行号越界：%r" % (index + 1, line))
                elif not 0 <= start < end <= len(source_lines[line - 1]) + 1:
                    span_bad.append("第 %d 个 span 列越界：%r-%r"
                                    % (index + 1, start, end))
            checker.expect("source_spans", not span_bad, "；".join(span_bad[:5]))

    # dispatch <-> body blocks
    try:
        dispatch = doc.dispatch()
    except common.Failure as exc:
        checker.fail("dispatch", exc.message)
        return
    tasks = dispatch.get("tasks") or []
    task_blocks = [task.get("block") for task in tasks]
    checker.expect("dispatch_body", task_blocks == body_ids,
                   "dispatch 任务与正文块不是一一对应（%r vs %r）" % (task_blocks, body_ids))
    by_id = {block["id"]: block for block in block_list}
    for task in tasks:
        block = by_id.get(task.get("block"))
        if block is None:
            continue
        checker.expect("dispatch_atoms:%s" % task.get("block"),
                       [atom.get("id") for atom in (task.get("atoms") or [])] == block["atoms"],
                       "任务原子与块原子不一致")
        expected = os.path.join(doc.dir, "blocks", (task.get("block") or "") + ".json")
        checker.expect("dispatch_receipt:%s" % task.get("block"),
                       os.path.abspath(task.get("receipt") or "") == os.path.abspath(expected),
                       "回执路径不是 docs/<id>/blocks/<block>.json")


# --- E3 ---------------------------------------------------------------------


def check_e3(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    try:
        blocks = _load_blocks(gate, doc)
        dispatch = doc.dispatch()
    except common.Failure as exc:
        checker.fail("artifacts", exc.message)
        return
    atoms_by_id = {atom["id"]: atom for atom in blocks["atoms"]}
    by_id = {block["id"]: block for block in blocks["blocks"]}
    body_ids = [block["id"] for block in blocks["blocks"] if block["kind"] == "body"]
    tasks = dispatch.get("tasks") or []
    if not tasks:
        checker.fail("dispatch", "dispatch.json 没有任务")
        return
    checker.ok("dispatch", "%d 块" % len(tasks))

    # $RUN/docs/<doc>/blocks/<block>.json is the authoritative receipt path
    # (接口约定 §8); dispatch.receipt is cross-checked only, so a copied or
    # moved $RUN still validates the files that are actually here.
    stale = []
    expected = {}
    for task in tasks:
        block_id = task.get("block") or ""
        path = os.path.join(doc.dir, "blocks", block_id + ".json")
        expected[task.get("task_id") or block_id] = path
        recorded = os.path.abspath(task.get("receipt") or "")
        if recorded != os.path.abspath(path):
            stale.append(task.get("task_id") or block_id)
    if stale:
        checker.warn("dispatch_paths",
                     "dispatch.receipt 与 $RUN 实际路径不一致（RUN 被复制/移动？）：%s；"
                     "以 docs/<id>/blocks 为准" % "、".join(stale[:5]))
    else:
        checker.ok("dispatch_paths")
    missing = [task_id for task_id, path in expected.items()
               if not os.path.isfile(path)]
    checker.expect("receipts", not missing, "缺回执：%s" % "、".join(missing))
    for task in tasks:
        block = by_id.get(task.get("block"))
        label = "receipt:%s" % task.get("block")
        if block is None:
            checker.fail(label, "块不存在于 blocks.json")
            continue
        path = expected[task.get("task_id") or task.get("block")]
        if not os.path.isfile(path):
            continue
        try:
            receipt = common.read_json(path)
            assemble.receipt_segments(receipt, block, doc.id, path, [])
        except common.Failure as exc:
            checker.fail(label, exc.message)
            continue
        if receipt.get("role") != "segmenter":
            checker.fail(label, "role 应为 segmenter")
            continue
        if not isinstance(receipt.get("agent"), str) or not receipt["agent"].strip():
            checker.fail(label, "回执缺 agent")
            continue
        if not isinstance(receipt.get("finished_at"), str) or not receipt["finished_at"].strip():
            checker.fail(label, "回执缺 finished_at")
            continue
        try:
            assemble.validate_plan(block, receipt.get("segments"), atoms_by_id,
                                   doc.id, "回执", [])
        except common.Failure as exc:
            checker.fail(label, exc.message)
            continue
        checker.ok(label)
    extra = []
    blocks_dir = os.path.join(doc.dir, "blocks")
    if os.path.isdir(blocks_dir):
        for name in sorted(os.listdir(blocks_dir)):
            if name.endswith(".json") and name[:-5] not in set(body_ids):
                extra.append(name)
    if extra:
        checker.warn("extra_receipts", "非正文块的回执被忽略：%s" % "、".join(extra))


# --- E4 ---------------------------------------------------------------------


def check_e4(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    try:
        blocks = _load_blocks(gate, doc)
    except common.Failure as exc:
        checker.fail("blocks", exc.message)
        return
    try:
        segments = doc.segments()
    except common.Failure as exc:
        checker.fail("segments", exc.message)
        return
    rows = segments.get("rows") or []
    if not rows:
        checker.fail("rows", "segments.json 没有行")
        return
    checker.expect("row_numbers", [row.get("n") for row in rows] ==
                   list(range(1, len(rows) + 1)), "行号不是 1..N 连续")
    copies = [dict(row) for row in rows]
    try:
        assemble.check_rows(copies, blocks, doc.id)
    except common.Failure as exc:
        checker.fail("rows", exc.message)
        return
    checker.ok("rows", "%d 行" % len(rows))
    body_blocks = [block for block in blocks["blocks"] if block["kind"] == "body"]
    body_by_id = {block["id"]: block for block in body_blocks}
    covered = []
    coverage_errors = []
    for row in rows:
        if row["type"] != "正文":
            continue
        block = body_by_id.get(row.get("block"))
        if block is None:
            coverage_errors.append("第 %s 行的块 %r 不是正文块" % (row["n"], row.get("block")))
            continue
        if row.get("position") != block.get("position"):
            coverage_errors.append("第 %s 行的位置与块 %s 不一致" % (row["n"], block["id"]))
        covered.extend(row.get("atoms") or [])
    expected_atoms = [atom_id for block in body_blocks for atom_id in block["atoms"]]
    if covered != expected_atoms:
        coverage_errors.append("正文行消费的原子与正文块不一致（%d vs %d）"
                               % (len(covered), len(expected_atoms)))
    checker.expect("atom_coverage", not coverage_errors, "；".join(coverage_errors[:3]))
    bad_words = [row["n"] for row in rows
                 if row.get("words") != len(str(row.get("text_cut") or "").split())]
    checker.expect("words", not bad_words, "行 words 与切分文本词数不符：%r" % bad_words[:5])
    current_sha = _sha(doc.text)
    checker.expect("source_sha", segments.get("source_sha256") == current_sha,
                   "segments.json 未绑定当前原文 sha")
    # flags must be exactly the recomputed long_atom list (A9)
    expected_flags = [{"n": row["n"], "kind": "long_atom", "words": row["words"]}
                      for row in rows
                      if row["type"] == "正文" and row["words"] > assemble.LONG_ATOM_WORDS]
    checker.expect("flags", segments.get("flags") == expected_flags,
                   "segments.flags 与正文行实际情况不符")
    # 0.0.10: merge_candidates must be exactly the multi-atom body rows.
    # Old (0.0.9) segments.json has no such key; treat it as empty and WARN
    # instead of failing so an old run can still be inspected/re-run.
    expected_candidates = [
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
    candidates = segments.get("merge_candidates")
    if not isinstance(candidates, list):
        checker.warn("merge_candidates",
                     "旧版 segments.json 没有 merge_candidates（0.0.9），按空候选处理")
    else:
        checker.expect("merge_candidates", candidates == expected_candidates,
                       "segments.merge_candidates 与正文多原子行不一致（重跑 assemble.py）")
    # cut text
    cut_path = doc.path("切分文本.txt")
    checker.expect("cut_text", os.path.isfile(cut_path), "缺 切分文本.txt")
    if os.path.isfile(cut_path):
        raw = open(cut_path, "rb").read().decode("utf-8")
        checker.expect("cut_text_nl", raw.endswith("\n") and not raw.endswith("\n\n"),
                       "切分文本末尾应恰好一个换行")
        lines = raw[:-1].split("\n") if raw.endswith("\n") else raw.split("\n")
        checker.expect("cut_text_rows", lines == [row["text_cut"] for row in rows],
                       "切分文本与 segments.json 逐行不一致")
        checker.expect("cut_text_empty", not any(not line.strip() for line in lines),
                       "切分文本有空行")
    # manifest
    try:
        parsed = doc.manifest_rows()
    except common.Failure as exc:
        checker.fail("manifest", exc.message)
        return
    checker.ok("manifest")
    checker.expect("manifest_stem", parsed["stem"] == doc.stem, "清单 stem 与 doc.json 不符")
    first_line = parsed["text"].split("\n")[0]
    checker.expect("manifest_title",
                   first_line == "# %s · 切分清单（含进卡组标记）" % doc.stem,
                   "清单标题行不是冻结模板")
    manifest_rows = parsed["rows"]
    checker.expect("manifest_rows", len(manifest_rows) == len(rows),
                   "清单 %d 行 vs segments %d 行" % (len(manifest_rows), len(rows)))
    mismatched = [row["n"] for row, expected in zip(manifest_rows, rows)
                  if (row.get("n"), row.get("type"), row.get("keep"),
                      row.get("position"), row.get("display")) !=
                  (expected["n"], expected["type"], expected["keep"],
                   expected["position"], expected["text_display"])]
    checker.expect("manifest_content", not mismatched,
                   "清单与 segments 行内容不一致：%r" % mismatched[:5])
    recorded = parsed["recorded"]
    keep_rows = [row for row in rows if row["keep"]]
    body_rows = [row for row in rows if row["type"] == "正文"]
    quiz_rows = [row for row in rows if row["type"] == "题目"]
    expected_skip_types = "/".join(
        name for name in ("报幕", "指令", "开场", "结尾")
        if any(row["type"] == name for row in rows))
    checker.expect("manifest_skip_types",
                   recorded.get("skip_types") == expected_skip_types,
                   "清单统计句的否类串与表格实际类型不符")
    checker.expect("manifest_counts",
                   recorded.get("keep") == len(keep_rows)
                   and recorded.get("body") == len(body_rows)
                   and recorded.get("questions") == len(quiz_rows)
                   and recorded.get("skip") == len(rows) - len(keep_rows)
                   and recorded.get("keep_declared") == len(keep_rows)
                   and recorded.get("skip_declared") == len(rows) - len(keep_rows),
                   "清单表头统计与表格行数不一致")
    checker.expect("manifest_keep_ranges",
                   parsed["keep_ranges"] == [row["n"] for row in keep_rows],
                   "进卡组区间与表格行不一致")
    checker.expect("manifest_skip_ranges",
                   parsed["skip_ranges"] == [row["n"] for row in rows if not row["keep"]],
                   "不进卡组区间与表格行不一致")
    checker.expect("manifest_tail", parsed["tail_ok"], "清单缺固定收尾句")
    # sha bindings (A11).  0.0.5: read the recorded fields, never a side file;
    # the baseline is manifest-5col.md after E5, else the 5-column run manifest.
    baseline_path, baseline_text = doc.baseline_manifest()
    current_manifest_sha = common.sha256_text(baseline_text)
    checker.expect("baseline_manifest",
                   os.path.isfile(baseline_path) and bool(baseline_text),
                   "缺 5 列基线清单（manifest-5col.md 或 $RUN/切分清单.md）")
    checker.expect("segments_manifest_sha",
                   segments.get("manifest_sha256") == current_manifest_sha,
                   "segments.manifest_sha256 与 5 列基线不符（先跑 assemble.py）")
    checker.expect("doc_manifest_sha",
                   doc.doc.get("manifest_sha256") == current_manifest_sha,
                   "doc.json.manifest_sha256 与 5 列基线不符（A11；先跑 assemble.py）")
    if os.path.isfile(doc.manifest):
        # E5 rewrites the 产出区 review copy with the 6-column manifest, so a
        # re-run of e4 after E5 must accept the extension (first 5 columns and
        # the tail are still byte-identical to the frozen baseline).
        out_sha = _sha(doc.manifest)
        if out_sha == current_manifest_sha:
            checker.ok("out_manifest")
        else:
            try:
                compatible, reason = manifest_extension_compatible(
                    common.read_text(doc.manifest), baseline_text)
            except common.Failure as exc:
                compatible, reason = False, exc.message
            checker.expect("out_manifest", compatible,
                           "产出区清单与 5 列基线不兼容：%s" % reason)
    # no split anywhere (A9)
    checker.expect("no_split", not any("split" in row for row in rows),
                   "segments.json 出现 split 字段")
    # reviewer receipt (cut must not start before the human has seen the rows)
    _check_e4_reviewer(doc, checker)


def _check_e4_reviewer(doc: DocContext, checker: Checker) -> None:
    """E4 的「复核」：切分前的过目回执归 E4 关卡（C3 报的覆盖缺口）。

    E4 的顺序是 assemble → 派复核员 → 有 overrides 再 assemble → gate e4，
    所以 e4 缺复核回执必须 FAIL，否则「切前必须过目」这条铁律会漏掉一块。

    * 回执 ``review-<n>.json``：version 1 / doc / role=reviewer /
      verdict ∈ {ok, fixed} / flags、overrides 是数组；
    * 0.0.10（SPEC §2.4）：最新回执还要有 ``merge_review``，逐条覆盖
      ``segments.json.merge_candidates``；``keep`` 必须能找到同原子集合的
      正文行且 merge 理由合法，``split`` 必须在该块的 overrides 里；
    * ``verdict=ok``：overrides 必须为空；
    * ``verdict=fixed``：``segments.json.overrides_applied`` 必须覆盖最新回执
      的块，且 ``segments.review_file`` 指向该回执（拿旧汇编冒充不行）。
      此时 ``merge_review`` 的 n 指向改判前的行，所以覆盖检查比对
      ``segments.json.review_base_merge_candidates``。
    * 旧版 ``segments.json``（0.0.9）没有 merge_candidates：按空候选处理，
      旧复核回执没有 merge_review 也能过（不强迫重派 E3/E4）。
    """
    reviews = []
    for name in sorted(os.listdir(doc.dir)):
        match = re.match(r"^review-(\d+)\.json$", name)
        if not match:
            continue
        path = doc.path(name)
        try:
            payload = common.read_json(path)
        except common.Failure as exc:
            checker.fail("review:%s" % name, exc.message)
            continue
        errors = []
        if not isinstance(payload, dict) or payload.get("version") != 1:
            errors.append("不是 version 1")
        if not isinstance(payload, dict) or payload.get("doc") != doc.id:
            errors.append("doc 不符")
        if not isinstance(payload, dict) or payload.get("role") != "reviewer":
            errors.append("role 不是 reviewer")
        if not isinstance(payload, dict) or payload.get("verdict") not in ("ok", "fixed"):
            errors.append("verdict 非法：%r" % (payload.get("verdict")
                                                if isinstance(payload, dict) else None))
        if not isinstance(payload, dict) or not isinstance(payload.get("flags"), list):
            errors.append("flags 不是数组")
        if not isinstance(payload, dict) or not isinstance(payload.get("overrides"), list):
            errors.append("overrides 不是数组")
        if errors:
            checker.fail("review:%s" % name, "；".join(errors))
        else:
            reviews.append((int(match.group(1)), path, payload))
    if not checker.expect(
            "reviewer", bool(reviews),
            "缺复核员回执 review-<n>.json；回 E4 派复核员（附录①），"
            "把回执写进 $RUN/docs/<id>/ 后再跑 gate e4"):
        return
    reviews.sort(key=lambda item: item[0])
    latest_number, latest_path, latest = reviews[-1]
    overrides = latest.get("overrides") or []
    override_blocks = [str(item.get("block")) for item in overrides
                       if isinstance(item, dict)]
    try:
        segments = doc.segments()
    except common.Failure as exc:
        checker.fail("review_applied", exc.message)
        checker.fail("merge_review", exc.message)
        return
    if latest.get("verdict") == "ok":
        checker.expect("review_applied", not overrides,
                       "verdict=ok 的复核回执不该带 overrides（要改判请写 fixed）")
    elif not override_blocks:
        checker.fail("review_applied",
                     "verdict=fixed 但 overrides 为空；要么标 ok，要么给出改判块")
    else:
        applied = segments.get("overrides_applied") or []
        checker.expect(
            "review_applied",
            os.path.abspath(segments.get("review_file") or "") == os.path.abspath(latest_path),
            "最新复核回执的 overrides 尚未重新汇编（review_file 不是 review-%d）；"
            "回 E4 用 assemble --overrides 重跑" % latest_number)
        checker.expect("review_blocks", set(override_blocks) <= set(applied),
                       "复核改判的块没有全部应用：%r vs %r" % (override_blocks, applied))
    _check_e4_merge_review(latest, latest_number, segments, checker)


def _check_e4_merge_review(latest: dict, latest_number: int, segments: dict,
                           checker: Checker) -> None:
    """SPEC §2.4: reviewer must cover every merge_candidate, one by one."""
    candidates = segments.get("merge_candidates")
    old_schema = not isinstance(candidates, list)
    if old_schema:
        candidates = []
    if latest.get("verdict") == "fixed":
        base = segments.get("review_base_merge_candidates")
        if not isinstance(base, list):
            # 0.0.9-style reassembly: fall back to the current candidates.
            base = candidates
    else:
        base = candidates
    base = [item for item in base if isinstance(item, dict)]
    by_n = {item.get("n"): item for item in base if isinstance(item.get("n"), int)}

    review = latest.get("merge_review")
    if not isinstance(review, list):
        if base:
            checker.fail(
                "merge_review",
                "最新复核回执 review-%d 缺 merge_review 数组：有 %d 个多原子行"
                "（merge_candidates）必须逐条给 keep/split + why" % (latest_number, len(base)))
        elif old_schema:
            checker.ok("merge_review", "旧版 segments.json 无 merge_candidates，视为空候选")
        else:
            checker.fail(
                "merge_review",
                "最新复核回执 review-%d 缺 merge_review 数组（无候选时应写 []）" % latest_number)
        return

    errors = []
    decisions = {}
    for index, item in enumerate(review):
        where = "merge_review[%d]" % index
        if not isinstance(item, dict):
            errors.append("%s 不是对象" % where)
            continue
        n = item.get("n")
        decision = item.get("decision")
        why = item.get("why")
        if not isinstance(n, int) or isinstance(n, bool):
            errors.append("%s 的 n 不是整数：%r" % (where, n))
            continue
        if n in decisions:
            errors.append("第 %d 行重复给了 merge_review" % n)
            continue
        if decision not in ("keep", "split"):
            errors.append("第 %d 行的 decision 非法：%r" % (n, decision))
            continue
        if not isinstance(why, str) or not why.strip():
            errors.append("第 %d 行缺 why" % n)
            continue
        if len(why) > 40:
            errors.append("第 %d 行的 why 超过 40 字（%d 字）" % (n, len(why)))
        decisions[n] = item
    covered = set(decisions)
    expected = set(by_n)
    missing = sorted(expected - covered)
    extra = sorted(covered - expected)
    if missing:
        errors.append("未覆盖 merge_candidates：%s" % "、".join(str(n) for n in missing[:8]))
    if extra:
        errors.append("merge_review 里有候选之外的 n：%s" % "、".join(str(n) for n in extra[:8]))
    if not checker.expect("merge_review", not errors, "；".join(errors[:5])):
        return

    override_blocks = {str(item.get("block")) for item in (latest.get("overrides") or [])
                       if isinstance(item, dict)}
    applied = set(segments.get("overrides_applied") or [])
    rows = [row for row in (segments.get("rows") or []) if isinstance(row, dict)]
    split_errors = []
    keep_errors = []
    for n in sorted(decisions):
        item = decisions[n]
        candidate = by_n[n]
        if item["decision"] == "split":
            block = str(candidate.get("block"))
            if not override_blocks:
                split_errors.append("第 %d 行判 split，但回执没有 overrides" % n)
            elif block not in override_blocks:
                split_errors.append("第 %d 行判 split，但块 %s 不在 overrides 里" % (n, block))
            elif block not in applied:
                split_errors.append("第 %d 行判 split，但块 %s 未应用（overrides_applied）"
                                    % (n, block))
            continue
        atoms = tuple(candidate.get("atoms") or [])
        row = next((item_row for item_row in rows
                    if item_row.get("type") == "正文"
                    and tuple(item_row.get("atoms") or []) == atoms), None)
        if row is None:
            keep_errors.append(
                "第 %d 行判 keep，但当前 segments 找不到同原子集合 %s 的正文行"
                % (n, list(atoms)))
            continue
        row_merge = row.get("merge")
        if row_merge in assemble.MERGE_KINDS:
            continue
        why = item.get("why") or ""
        if candidate.get("reason_missing") and any(kind in why for kind in assemble.MERGE_KINDS):
            continue
        keep_errors.append(
            "第 %d 行判 keep，但该行 merge 理由不合法（行 merge=%r，why=%r；"
            "旧回执需在 why 里写明五类之一（含 turn））" % (n, row_merge, why))
    checker.expect("merge_split", not split_errors, "；".join(split_errors[:3]))
    checker.expect("merge_keep", not keep_errors, "；".join(keep_errors[:3]))


# --- E5（译文 + 6 列清单 + 定稿） ---------------------------------------





def check_e5(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    """E5：6 列清单 + translations.json + 译文复核回执 + approve.json 定稿。

    先看用户定稿令牌（``_check_manifest_finalized``）：没有 approve.json 就
    不能在 E6 切分；再看译文与 6 列清单本身（原 0.0.7 E8 的检查原样搬来）。
    """
    _check_manifest_finalized(gate, doc, checker)
    five_path = doc.path("manifest-5col.md")
    six_path = doc.path("切分清单.md")
    if not checker.expect("manifest_5col", os.path.isfile(five_path),
                          "缺 manifest-5col.md（E5 首跑应写 5 列快照；先跑 translate_apply）"):
        return
    if not checker.expect("manifest_6col", os.path.isfile(six_path),
                          "缺 $RUN 内的 6 列 切分清单.md（E5 先跑 translate_apply）"):
        return
    five_text = common.read_text(five_path)
    six_text = common.read_text(six_path)
    if not checker.expect("manifest_6col_columns", common.manifest_columns(six_text) == 6,
                          "$RUN 内的清单不是 6 列"):
        return
    try:
        stripped = common.strip_manifest_translation_column(six_text)
    except common.Failure as exc:
        checker.fail("first_five_columns", exc.message)
        return
    checker.expect("first_five_columns", stripped == five_text,
                   "6 列清单的前 5 列与 manifest-5col.md 逐字节不一致（一列不改五列）")
    # E5 时包还不存在：产出区根副本必须与 $RUN 副本逐字节一致；E7 分包后
    # 包内同名清单也要一致（改译文时 translate_apply 会三处一起重写）。
    out_six = doc.manifest
    if os.path.isfile(out_six):
        checker.expect("out_manifest", _sha(out_six) == _sha(six_path),
                       "产出区 6 列清单与 $RUN 副本不一致：%s" % out_six)
    pack_manifest = doc.doc.get("pack_manifest")
    if pack_manifest:
        pack_path = common.resolve_work_path(gate.work, str(pack_manifest))
        if os.path.isfile(pack_path):
            if _sha(pack_path) == _sha(six_path):
                checker.ok("pack_manifest")
            else:
                # A11: a downstream-extended pack (extra column / extra quote
                # line) must not fail E5 in the E8 aggregate — the first six
                # columns and the tail are still the approved bytes.
                pack_text = common.read_text(pack_path)
                compatible, reason = manifest_extension_compatible(pack_text, six_text)
                if compatible:
                    checker.warn(
                        "pack_manifest",
                        "pack_manifest_extended（包内清单在 E5 6 列基础上做了下游扩展，"
                        "第 6 列与尾部未变，A11 兼容）")
                else:
                    checker.fail("pack_manifest",
                                 "包内清单与 $RUN 6 列清单不兼容：%s" % reason)
    try:
        parsed = common.parse_manifest_6col(six_path)
    except common.Failure as exc:
        checker.fail("manifest_rows", exc.message)
        return
    rows = parsed["rows"]
    keep_rows = [row for row in rows if row["keep"]]
    if not checker.expect("manifest_rows", bool(keep_rows), "清单里没有 keep 行"):
        return
    empty_keep = [common.fmt_row(row["n"]) for row in keep_rows if not row["zh"]]
    filled_skip = [common.fmt_row(row["n"]) for row in rows
                   if not row["keep"] and row["zh"]]
    checker.expect("keep_filled", not empty_keep,
                   "keep 行译文为空：%s" % "、".join(empty_keep[:10]))
    checker.expect("skip_blank", not filled_skip,
                   "否行不该有译文：%s" % "、".join(filled_skip[:10]))
    bad_counts = [common.fmt_row(row["n"]) for row in keep_rows
                  if len(row["zh"]) != common.sentence_count(row["display"])]
    checker.expect("sentence_counts", not bad_counts,
                   "译文行数 ≠ 英文句数：%s" % "、".join(bad_counts[:10]))
    translations = _load_artifact(checker, doc.path("translations.json"), "translations")
    if translations is None:
        return
    rows_map = translations.get("rows") or {}
    keep_numbers = {str(row["n"]) for row in keep_rows}
    checker.expect("translations_cover", set(rows_map) == keep_numbers,
                   "translations.json 没覆盖全部 keep 行（%d vs %d）"
                   % (len(rows_map), len(keep_numbers)))
    keep_by_no = {str(row["n"]): row for row in keep_rows}
    mismatched = [number for number, zh in rows_map.items()
                  if number in keep_by_no and list(zh) != list(keep_by_no[number]["zh"])]
    checker.expect("translations_match", not mismatched,
                   "translations.json 与清单译文不一致：%s" % "、".join(mismatched[:10]))
    checker.expect("translations_sha",
                   translations.get("manifest_sha256_v2") == _sha(six_path),
                   "translations.json 未绑定当前 6 列清单 sha")

    # 0.0.4: every review receipt counts, not just the latest one.  A fix is an
    # absolute overwrite of zh, so an earlier fix is only "applied" when the
    # current translations.json still carries exactly that text.
    reviews = _numbered_receipts(doc.dir, r"^translate-review-(\d+)\.json$")
    if not checker.expect("translate_review", bool(reviews),
                          "缺译文复核回执 translate-review-<n>.json"):
        return
    reviews_by_file = {}
    format_errors = []
    bad_fixes = []
    for _number, review_path in reviews:
        name = os.path.basename(review_path)
        review = _load_artifact(checker, review_path, "translate_review_format")
        if review is None:
            return
        errors = _review_errors(review, doc.id, "translation-reviewer")
        if errors:
            format_errors.append("%s: %s" % (name, "；".join(errors)))
            continue
        reviews_by_file[name] = (review_path, review)
        for fix in review.get("fixes") or []:
            if not isinstance(fix, dict):
                bad_fixes.append("%s: 非对象" % name)
                continue
            raw = fix.get("no")
            try:
                number_text = common.fmt_row(int(str(raw).strip()))
            except (TypeError, ValueError):
                bad_fixes.append("%s: 行号 %r" % (name, raw))
                continue
            if str(int(number_text)) not in keep_numbers:
                # keep_numbers holds un-padded str(int) keys (translations.json
                # uses them too); number_text is zero-padded, so normalise first.
                bad_fixes.append("%s: 行 %s 不是 keep" % (name, number_text))
            elif list(fix.get("zh") or []) != list(
                    rows_map.get(str(int(number_text))) or []):
                bad_fixes.append("%s: 行 %s 未应用" % (name, number_text))
    if format_errors:
        checker.fail("translate_review_format", "；".join(format_errors[:5]))
        return
    checker.ok("translate_review_format")
    checker.expect("translate_review_applied", not bad_fixes,
                   "复核 fixes 未应用：%s" % "、".join(bad_fixes[:10]))

    # 0.0.4: reviews with fixes must be recorded in translations.applied_reviews
    # (file basename + byte sha + fix count), so a later review cannot silently
    # drop an earlier correction.
    recorded = translations.get("applied_reviews")
    problems = []
    seen = {}
    if recorded is None:
        recorded = []
    if not isinstance(recorded, list):
        problems.append("applied_reviews 不是数组")
        recorded = []
    for entry in recorded:
        if not isinstance(entry, dict):
            problems.append("applied_reviews 条目不是对象")
            continue
        raw = str(entry.get("file") or "")
        name = os.path.basename(raw)
        if not name:
            problems.append("applied_reviews 条目缺 file")
            continue
        # Normal reviews live in the doc dir; an ad-hoc ``--review /tmp/x.json``
        # receipt is consumed and may be gone by now, so a non-numbered file that
        # is not in the doc dir is skipped instead of failed (0.0.4 hotfix).
        candidate = raw if os.path.isabs(raw) else doc.path(name)
        in_doc = os.path.abspath(candidate) == os.path.abspath(doc.path(name))
        if not os.path.isfile(candidate):
            if re.match(r"^translate-review-\d+\.json$", name):
                problems.append("%s 不存在" % name)
            continue
        if in_doc:
            seen[name] = entry
        if entry.get("sha256") != _sha(candidate):
            problems.append("%s sha 不符" % name)
    for name, (path, review) in reviews_by_file.items():
        count = len(review.get("fixes") or [])
        if count == 0:
            continue  # empty reviews are not required in applied_reviews
        entry = seen.get(name)
        if entry is None:
            problems.append("%s 未登记（重跑 translate_apply.py）" % name)
        elif entry.get("fixes") != count:
            problems.append("%s fixes 条数不符（应为 %d）" % (name, count))
    checker.expect("translate_reviews_recorded", not problems,
                   "；".join(problems[:10]))


# --- E6（切分） --------------------------------------------------------------


def _audit_review_ignore(doc: DocContext, report: dict, checker: Checker) -> None:
    """0.0.6 (C2): audit cut's review_ignore against the current segments.json.

    status == ok may still carry ignored review issues: the cutter is told
    which 1-based rows never enter the cards, marks issues that touch only
    those rows as ignored and still writes the full result.  The gate has to
    prove the ignored set is still the current non-card set and that no
    ignored issue touches a keep row; otherwise "ignore the report" would be
    a way to cut a broken document.  Reports from 0.0.5 have no field: WARN.
    """
    ignore = report.get("review_ignore")
    if ignore is None:
        checker.warn("review_ignore",
                     "旧报告没有 review_ignore（0.0.5 之前），按旧口径放行")
        return
    try:
        rows = doc.segments().get("rows") or []
    except (common.Failure, OSError) as exc:
        checker.fail("review_ignore",
                     "读不到 segments.json，无法审计 review_ignore：%s"
                     % getattr(exc, "message", exc))
        return
    try:
        expected = sorted(int(row["n"]) for row in rows if not row.get("keep"))
    except (KeyError, TypeError, ValueError):
        checker.fail("review_ignore", "segments.json 的 rows 结构坏了，算不出非卡片行")
        return
    if not isinstance(ignore, list):
        checker.fail("review_ignore", "report.review_ignore 不是列表：%r" % (ignore,))
        return
    try:
        actual = sorted(int(number) for number in ignore)
    except (TypeError, ValueError):
        checker.fail("review_ignore", "report.review_ignore 含非整数：%r" % (ignore,))
        return
    checker.expect("review_ignore", actual == expected,
                   "review_ignore 与当前非卡片行不一致（清单变过？）：报告 %r vs 当前 %r"
                   % (actual[:8], expected[:8]))
    expected_set = set(expected)
    ignored = [issue for issue in report.get("issues") or []
               if isinstance(issue, dict) and issue.get("ignored")]
    bad = []
    for issue in ignored:
        try:
            segments = set(int(number) for number in issue.get("segments") or [])
        except (TypeError, ValueError):
            bad.append(issue.get("segments"))
            continue
        if not segments <= expected_set:
            bad.append(sorted(segments - expected_set))
    checker.expect("ignored_issues", not bad,
                   "ignored issue 触及进卡组行：%r" % bad[:3])


#: 0.0.10 (SPEC §4.4): an ``edge`` cut must cover at least this much audio;
#: keep in sync with ``cut_stage.COVERAGE_MIN``.
E6_COVERAGE_MIN = 0.95


def _e6_alignment_coverage(report: dict):
    """Classify ``cut-report.json.alignment`` for gate e6 (SPEC §4.4).

    0.0.10 cutaudio 1.1.1 adds ``alignment.coverage``: the share of the audio
    timeline the alignment actually reached.  ``edge`` mode with a low
    coverage means the transcript did not match the audio (the 卷2
    mixed-script failure: 140 segments, 122 issues).  ``cut_stage.py`` already
    exits 3 on that shape, but someone can call ``gate e6`` directly on an old
    report; this is the belt-and-braces check that the cut is never used.

    Returns ``(kind, message)`` with ``kind`` in ``ok`` / ``warn`` / ``fail``;
    pure function so the boundaries are unit-testable without a whole run.
    A missing field is a WARN (old cutaudio reports have no ``alignment`` or
    ``coverage``), never a silent pass.
    """
    alignment = report.get("alignment")
    if not isinstance(alignment, dict):
        return ("warn", "旧报告没有 alignment（cutaudio < 1.1.1），按旧口径放行")
    mode = alignment.get("cut_mode")
    if mode == "wave":
        # wave has the ASR word timeline; a low matched is only a warning
        # (cut_stage records alignment_low_matched), never a block.
        return ("ok", "wave 模式有 ASR 时间轴，不按 coverage 拦")
    if mode != "edge":
        return ("warn", "旧报告没有 cut_mode（cutaudio < 1.1.1），按旧口径放行")
    coverage = alignment.get("coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        return ("warn", "旧报告没有 coverage（cutaudio < 1.1.1），按旧口径放行")
    coverage = float(coverage)
    if coverage < E6_COVERAGE_MIN:
        return ("fail",
                "edge 对齐只覆盖 %.1f%% 音频（coverage=%.3f）；不要用这份切分，"
                "先处理原文或升级 cutaudio" % (coverage * 100, coverage))
    return ("ok", "edge 对齐覆盖 %.1f%% 音频（coverage=%.3f）"
            % (coverage * 100, coverage))


def check_e6(gate: GateRun, doc: DocContext, checker: Checker,
             delivered: bool = False) -> None:
    """E6：cut-report.json 绑定了当前切分文本 + E4 冻结的 5 列清单。"""
    path = doc.path("cut-report.json")
    checker.expect("cut_report", os.path.isfile(path), "缺 cut-report.json（先跑 E6 cut）")
    if not os.path.isfile(path):
        return
    try:
        report = common.read_json(path)
    except common.Failure as exc:
        checker.fail("cut_report", exc.message)
        return
    status = report.get("status")
    if status == "needs_review":
        issues = report.get("issues") or []
        hint = doc.path("issues.md")
        where = hint if os.path.isfile(hint) else "（见 review ZIP 内 issues.md）"
        checker.fail("status", "needs_review（%d 个 issue；%s）——停下问用户，不硬闯"
                     % (len(issues), where))
        return
    zip_info = report.get("zip") or {}
    if status != "ok":
        errors = report.get("errors") or []
        detail = "；".join("%s: %s" % (item.get("kind"), item.get("message"))
                           for item in errors if isinstance(item, dict)) or "见 report.json"
        checker.fail("status", "status=%r（应为 ok）：%s" % (status, detail))
        return
    checker.ok("status")
    # 0.0.10 (SPEC §4.4): refuse an edge cut whose alignment only reached part
    # of the audio.  cut_stage's own post-cut guard exits 3 already; this
    # covers a run driven straight to `gate e6` on a hand-kept report.
    kind, message = _e6_alignment_coverage(report)
    if kind == "ok":
        checker.ok("alignment_coverage", message)
    elif kind == "fail":
        checker.fail("alignment_coverage", message)
    else:
        checker.warn("alignment_coverage", message)
    # 0.0.4: the report must be bound to the exact text/manifest it was cut from,
    # so a stale ZIP cannot satisfy E6 after the cut text changed. 0.0.8: the
    # manifest binding is the E4-frozen manifest-5col.md (E5 overwrites the live
    # $RUN / 产出区 copies with the 6-column version, so it must not be used).
    current_text = _sha(doc.path("切分文本.txt"))
    if current_text is None:
        checker.fail("text_sha", "缺 切分文本.txt（先跑 assemble）：%s"
                     % doc.path("切分文本.txt"))
    else:
        checker.expect("text_sha", report.get("text_sha256") == current_text,
                       "cut-report 绑定的切分文本 sha 与当前不符（切分文本变过？）；"
                       "重跑 cut_stage --stage cut --reset")
    manifest_sha = None
    manifest_source = None
    try:
        manifest_source, baseline_text = doc.baseline_manifest()
        manifest_sha = common.sha256_text(baseline_text)
    except common.Failure:
        manifest_sha = None
    if manifest_sha is None:
        manifest_sha = _sha(doc.manifest)
        manifest_source = doc.manifest
    if manifest_sha is None:
        if delivered:
            checker.warn("manifest_sha", "5 列基线清单已被清理（A8），跳过清单 sha 复核")
        else:
            checker.fail("manifest_sha",
                         "缺 5 列基线清单（manifest-5col.md / 产出区清单；先跑 E4 汇编、E5 定稿）：%s"
                         % doc.manifest)
    else:
        checker.expect("manifest_sha", report.get("manifest_sha256") == manifest_sha,
                       "cut-report 绑定的清单 sha 与当前不符（%s）；重跑 cut_stage --reset"
                       % manifest_source)
    zip_path = zip_info.get("path") or ""
    checker.expect("zip_verified", zip_info.get("verified") is True, "report.zip.verified 不是 true")
    checker.expect("zip_path", bool(zip_path), "report.zip.path 为空")
    if zip_path:
        area = os.path.abspath(doc.area)
        checker.expect("zip_sane", common.is_within(area, os.path.abspath(zip_path)),
                       "ZIP 不在产出区内：%s" % zip_path)
        if os.path.isfile(zip_path):
            checker.ok("zip_exists")
            checker.expect("zip_file", zipfile.is_zipfile(zip_path),
                           "ZIP 打不开：%s" % zip_path)
        elif delivered:
            # E8 runs after cleanup; the ZIP was consumed by pack (A8) and is
            # intentionally gone.  The cut report still proves it existed.
            checker.warn("zip_cleaned",
                         "ZIP 已被 cleanup 清理（包已交付，A8）")
        else:
            checker.fail("zip_exists", "ZIP 不存在：%s" % zip_path)
    issues = report.get("issues") or []
    # 0.0.6 (B2): an ignored review issue still lives in report.issues for the
    # audit, but only non-ignored ones block E6.
    blocking = [issue for issue in issues
                if not (isinstance(issue, dict) and issue.get("ignored"))]
    checker.expect("issues", not blocking,
                   "report.issues 有未豁免问题（%d 项）" % len(blocking))
    _audit_review_ignore(doc, report, checker)
    expected_rows = None
    try:
        expected_rows = doc.segments()["stats"]["total"]
    except (common.Failure, KeyError, TypeError):
        try:
            expected_rows = doc.manifest_rows()["total"]
        except (common.Failure, KeyError):
            expected_rows = None
    checker.expect("segments", report.get("segments") == expected_rows,
                   "report.segments=%r 与清单行数 %r 不符"
                   % (report.get("segments"), expected_rows))

    # F1: cut's first segment always starts at 0.0 s; a low ASR similarity means
    # the first line may have swallowed speech the transcript does not cover.
    detail = (report.get("segments_detail") or [None])[0]
    if isinstance(detail, dict):
        similarity = detail.get("asr_similarity")
        if isinstance(similarity, (int, float)) and similarity < 0.90:
            checker.warn("first_segment",
                         "asr_similarity=%.2f (第 1 段可能含未覆盖语音)" % similarity)
        start_sample = detail.get("start_sample")
        start_sec = detail.get("start_sec")
        if (isinstance(start_sample, (int, float)) and start_sample > 0) or \
                (isinstance(start_sec, (int, float)) and start_sec > 0):
            checker.warn("first_segment",
                         "start=%s (第 1 段起点不是 0)" %
                         (start_sec if isinstance(start_sec, (int, float)) else start_sample))


# --- E7（分包） --------------------------------------------------------------


def _inspect_pack_audio(checker: Checker, pack_dir: str, manifest_name: str,
                        keep_numbers, pack_report: dict | None,
                        work: str = "", ffmpeg: str | None = None) -> dict:
    """Inspect the audio segments of one delivered pack (pure, unit-testable).

    The expected extension comes from :func:`common.pack_audio_ext`: a new
    ``pack.json.format`` wins, an old pack (no format) is inferred from the
    files.  Old wav packs and new mp3 packs both pass; a pack that mixes wav
    and mp3 FAILs.  Durations: mp3 through ``common.probe_audio`` (ffmpeg
    stderr, no ffprobe), wav through :func:`wav_duration_sec`.

    Returns ``{"ext", "audio", "durations", "mixed"}`` for the caller.
    """
    names = sorted(os.listdir(pack_dir))
    audio = []
    unexpected = []
    for name in names:
        path = os.path.join(pack_dir, name)
        if not os.path.isfile(path) or name == manifest_name:
            continue
        if AUDIO_SEGMENT_RE.match(name):
            audio.append(name)
        else:
            unexpected.append(name)
    checker.expect("pack_clean", not unexpected,
                   "包内有多余文件（非片段/清单）：%r" % unexpected)
    extensions = sorted({AUDIO_SEGMENT_RE.match(name).group(2).lower()
                         for name in audio})
    expected_ext = common.pack_audio_ext(pack_dir, pack_report).lower()
    mixed = len(extensions) > 1
    checker.expect("pack_format", not mixed,
                   "包内片段扩展名混排：%r（同一包必须统一为一种）" % extensions)
    expected_names = ["%03d%s" % (int(number), expected_ext)
                      for number in keep_numbers]
    checker.expect("segment_count", len(audio) == len(keep_numbers),
                   "片段数 %d != 进卡组行数 %d"
                   % (len(audio), len(keep_numbers)))
    checker.expect("segment_names", sorted(audio) == sorted(expected_names),
                   "片段文件名与进卡组行号不是一一对应（期望 *%s）" % expected_ext)
    durations = {}
    probe_failed = False
    for name in sorted(audio):
        path = os.path.join(pack_dir, name)
        extension = "." + AUDIO_SEGMENT_RE.match(name).group(2).lower()
        try:
            if extension == ".mp3":
                if not ffmpeg:
                    try:
                        ffmpeg = common.find_ffmpeg(work)
                    except common.Failure as exc:
                        checker.fail("segment_probe",
                                     "mp3 时长需要 ffmpeg：%s" % exc.message)
                        probe_failed = True
                        break
                probed = common.probe_audio(path, ffmpeg)
                seconds = probed["duration_sec"]
                expected = common.SEGMENT_FORMAT
                if (probed.get("sample_rate") != expected["sample_rate"]
                        or probed.get("channels") != expected["channels"]
                        or probed.get("kbps") != expected["bitrate_kbps"]):
                    checker.fail(
                        "segment_format:%s" % name,
                        "mp3 规格 %s Hz / %s 声道 / %s kbps"
                        "（要 44100 Hz / 单声道 / 64 kbps）"
                        % (probed.get("sample_rate"), probed.get("channels"),
                           probed.get("kbps")))
            else:
                seconds = wav_duration_sec(path)
        except (OSError, ValueError) as exc:
            checker.fail("segment:%s" % name,
                         "读不了 %s 时长：%s" % (extension.lstrip("."), exc))
            continue
        durations[name] = seconds
        if seconds <= 0.2:
            checker.fail("segment_duration:%s" % name,
                         "时长 %.3fs 不大于 0.2s" % seconds)
    if not probe_failed and audio and len(durations) == len(audio):
        checker.ok("segment_duration", "全部 > 0.2s")
    return {"ext": expected_ext, "audio": audio, "durations": durations,
            "mixed": mixed}


def _check_pack_format_meta(checker: Checker, pack_dir: str,
                            pack_report) -> None:
    """An mp3 pack that declares ``pack.json.format`` must declare the spec.

    Old wav packs (format absent, or actual files wav) are out of scope.
    """
    if not isinstance(pack_report, dict):
        return
    fmt = pack_report.get("format")
    if not isinstance(fmt, dict):
        return
    try:
        names = os.listdir(pack_dir)
    except OSError:
        return
    audio = [name for name in names
             if AUDIO_SEGMENT_RE.match(name)
             and os.path.isfile(os.path.join(pack_dir, name))]
    if not audio or not all(name.lower().endswith(".mp3") for name in audio):
        return
    expected = common.SEGMENT_FORMAT
    mismatches = []
    for key in ("container", "sample_rate", "channels", "bitrate_kbps"):
        if fmt.get(key) != expected[key]:
            mismatches.append("%s=%r（要 %r）" % (key, fmt.get(key), expected[key]))
    checker.expect("pack_format_meta", not mismatches,
                   "pack.json.format 与 mp3 规格不符：%s" % "、".join(mismatches))


def check_e7(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    """E7：交付包 = 片段（mp3，44.1k/mono/64k；旧包 wav 兼容）+ 6 列清单。

    清单前 5 列仍是 E4 冻结基线，第 6 列只 WARN（A11）。
    """
    pack_dir = os.path.abspath(doc.pack_dir)
    checker.expect("pack_dir", os.path.isdir(pack_dir), "包目录不存在：%s" % pack_dir)
    if not os.path.isdir(pack_dir):
        return
    expected_name = os.path.basename(doc.pack_dir)
    checker.expect("pack_name", os.path.basename(pack_dir) == expected_name,
                   "包目录名不是 %s" % expected_name)
    checker.expect("pack_name_exact", expected_name == doc.stem + PACK_SUFFIX,
                   "包目录名不是 <stem>%s" % PACK_SUFFIX)
    entry_names = sorted(os.listdir(pack_dir))
    manifest_name = os.path.basename(doc.manifest)
    checker.expect("pack_manifest", manifest_name in entry_names, "包内缺清单 %s" % manifest_name)
    subdirs = [name for name in entry_names
               if os.path.isdir(os.path.join(pack_dir, name))]
    checker.expect("pack_nodirs", not subdirs, "包内有子目录：%r" % subdirs)

    # expected keep rows: frozen reference manifest (产出区 copy or re-render)
    try:
        parsed_reference, reference_text = _manifest_reference(doc)
    except common.Failure as exc:
        checker.fail("reference", exc.message)
        return
    reference_rows = parsed_reference["rows"] if parsed_reference else \
        [{"n": row["n"], "type": row["type"], "keep": row["keep"],
          "position": row["position"], "display": row["text_display"]}
         for row in doc.segments()["rows"]]
    keep_numbers = [row["n"] for row in reference_rows if row["keep"]]

    # pack.json first: its format tells _inspect_pack_audio which extension to
    # expect (old packs have no format and are inferred from the files).
    pack_report_path = doc.path("pack.json")
    pack_report = None
    checker.expect("pack_json", os.path.isfile(pack_report_path), "缺 pack.json")
    if os.path.isfile(pack_report_path):
        try:
            loaded = common.read_json(pack_report_path)
        except common.Failure as exc:
            checker.fail("pack_json", exc.message)
        else:
            if isinstance(loaded, dict):
                pack_report = loaded
            else:
                checker.fail("pack_json", "pack.json 不是 JSON 对象")
    audio_info = _inspect_pack_audio(checker, pack_dir, manifest_name,
                                     keep_numbers, pack_report, work=gate.work)
    audio_names = audio_info["audio"]
    _check_pack_format_meta(checker, pack_dir, pack_report)

    # manifest: package copy vs frozen reference, with A11 extension tolerance
    if manifest_name in entry_names:
        package_text = common.read_text(os.path.join(pack_dir, manifest_name))
        package_sha = common.sha256_text(package_text)
        # 0.0.5: the assembler records the sha in segments.json / doc.json;
        # there is no manifest-sha.txt to read any more.  The reference is the
        # frozen 5-column baseline (manifest-5col.md after E5, else $RUN).
        current_manifest_sha = common.sha256_text(reference_text)
        checker.expect("baseline_manifest", bool(reference_text),
                       "缺 5 列基线清单（manifest-5col.md 或 $RUN/切分清单.md）")
        checker.expect("segments_manifest_sha",
                       doc.segments().get("manifest_sha256") == current_manifest_sha,
                       "segments.manifest_sha256 与 5 列基线不符（先跑 assemble.py）")
        checker.expect("doc_manifest_sha",
                       doc.doc.get("manifest_sha256") == current_manifest_sha,
                       "doc.json.manifest_sha256 与 5 列基线不符（先跑 assemble.py）")
        if package_sha != current_manifest_sha:
            compatible, reason = manifest_extension_compatible(package_text,
                                                               reference_text)
            if compatible:
                checker.warn("pack_manifest",
                             "pack_manifest_extended（包内 6 列清单的前 5 列与 manifest-5col "
                             "一致，只多出译文列/说明行，A11 兼容）")
            else:
                checker.fail("pack_manifest", "包内清单与冻结基准不兼容：%s" % reason)
        else:
            checker.ok("pack_manifest")
        # 产出区 review copy, when it still exists, is either the frozen 5-column
        # E4 copy or the 6-column E5 copy built on top of it.
        if os.path.isfile(doc.manifest):
            out_sha = _sha(doc.manifest)
            if out_sha != current_manifest_sha:
                try:
                    out_compatible, out_reason = manifest_extension_compatible(
                        common.read_text(doc.manifest), reference_text)
                except common.Failure as exc:
                    out_compatible, out_reason = False, exc.message
                checker.expect("out_manifest", out_compatible,
                               "产出区清单与 5 列基线不兼容：%s" % out_reason)

    # pack.json / receipt
    if pack_report is not None:
        listed = pack_report.get("files") or []
        listed_names = [item.get("file") for item in listed if isinstance(item, dict)]
        checker.expect("pack_json_files", sorted(listed_names) == sorted(audio_names),
                       "pack.json 的文件清单与包内片段不一致")
        checker.expect("pack_json_count", pack_report.get("count") == len(audio_names),
                       "pack.json 的 count 与片段数不符")
        for item in listed:
            if not isinstance(item, dict) or not item.get("file"):
                continue
            path = os.path.join(pack_dir, item["file"])
            if not os.path.isfile(path):
                checker.fail("pack_json_file", "pack.json 列了不存在的文件 %s" % item["file"])
                break
            if item.get("sha256") and _sha(path) != item["sha256"]:
                checker.fail("pack_json_sha", "%s 的 sha 与 pack.json 不符" % item["file"])
                break
    checker.expect("pack_receipt", os.path.isfile(os.path.join(doc.dir, "receipts", "pack.json")),
                   "缺 receipts/pack.json")


# --- E8（阶段一收尾：E1 + E2–E7 + cleanup/report） -------------------------





def _pack_delivered(doc: DocContext) -> bool:
    return (os.path.isdir(doc.pack_dir)
            and os.path.isfile(doc.path("pack.json"))
            and bool(os.listdir(doc.pack_dir)))


def _stage_checks(gate: GateRun, doc: DocContext, stage: str) -> Checker:
    checker = Checker(stage)
    if stage == "e6":
        # After E8 cleanup the ZIP is gone on purpose: a delivered pack is the
        # evidence, so the cut check must downgrade that one line to WARN.
        check_e6(gate, doc, checker, delivered=_pack_delivered(doc))
        return checker
    {"e2": check_e2, "e3": check_e3, "e4": check_e4,
     "e5": check_e5, "e7": check_e7}[stage](gate, doc, checker)
    return checker


def _merge_checks(source: Checker, target: Checker) -> None:
    """Copy the lines of one sub-checker into an aggregate checker."""
    for check in source.checks:
        if check["status"] == OK:
            target.ok(check["label"])
        elif check["status"] == WARN:
            target.warn(check["label"], check["message"])
        else:
            target.fail(check["label"], check["message"])


def check_e8(gate: GateRun, checker: Checker) -> None:
    """E8 收尾：E1 + 逐文档 E2–E7 + cleanup/report/产出区 的最后复查。"""
    e1 = Checker("e1")
    check_e1(gate, e1)
    try:
        write_receipt(gate.run_dir, "e1", None, e1)
    except OSError:
        pass
    _merge_checks(e1, checker)
    for doc_id in gate.doc_ids:
        try:
            doc = gate.doc(doc_id)
        except common.Failure as exc:
            checker.fail("%s doc" % doc_id, exc.message)
            continue
        for stage in DOC_STAGES:
            try:
                sub = _stage_checks(gate, doc, stage)
            except Exception as exc:  # noqa: BLE001 - aggregate, never abort E8
                checker.fail("%s %s" % (doc_id, stage), "复算异常：%s" % exc)
                continue
            try:
                write_receipt(gate.run_dir, stage, doc_id, sub)
            except OSError:
                pass
            label = "%s %s" % (doc_id, stage)
            if not sub.passed:
                checker.fail(label, sub.failures[0]["message"])
                continue
            warnings = [item["message"] for item in sub.warnings if item.get("message")]
            if warnings:
                checker.warn(label, "；".join(warnings))
            else:
                checker.ok(label)
        checker.expect("%s cleanup_receipt" % doc_id,
                       os.path.isfile(os.path.join(doc.dir, "receipts", "cleanup.json")),
                       "缺 cleanup 回执（先跑 cleanup.py）")
        checker.expect("%s pack_dir" % doc_id, os.path.isdir(doc.pack_dir),
                       "交付包不存在：%s" % doc.pack_dir)

    report_path = os.path.join(gate.run_dir, "report.md")
    checker.expect("report", os.path.isfile(report_path), "缺 report.md（先跑 report.py）")
    if os.path.isfile(report_path):
        text = common.read_text(report_path)
        checker.expect("report_nonempty", bool(text.strip()), "report.md 是空的")
        for doc_id in gate.doc_ids:
            checker.expect("report_%s" % doc_id, doc_id in text, "report.md 没提到 %s" % doc_id)
        checker.expect("report_extension", "下游扩展" in text,
                       "report.md 缺「下游扩展」行（A11）")

    area = None
    if gate.doc_ids:
        try:
            area = gate.doc(gate.doc_ids[0]).area
        except common.Failure:
            area = None
    if area is None:
        area = os.path.join(gate.work, "产出区")
    if os.path.isdir(area):
        leftovers = []
        for name in sorted(os.listdir(area)):
            path = os.path.join(area, name)
            if os.path.isdir(path) and (name.endswith(PACK_SUFFIX)
                                        or (".prev-" in name and PACK_SUFFIX in name)):
                continue
            leftovers.append(name)
        checker.expect("output_area", not leftovers,
                       "产出区还有非包残留：%r" % leftovers[:10])
    else:
        checker.fail("output_area", "产出区不存在：%s" % area)


# --- phase1 (stage-one aggregate) ------------------------------------------


def check_phase1(gate: GateRun, checker: Checker) -> None:
    """E1, then per-document E2-E7 in order; the first FAIL stops the run.

    Every sub-verdict is written back as a gate receipt so ``report.py`` and
    ``status.py`` can summarise it without recomputing anything.
    """
    e1 = Checker("e1")
    check_e1(gate, e1)
    try:
        write_receipt(gate.run_dir, "e1", None, e1)
    except OSError:
        pass
    _merge_checks(e1, checker)
    if not e1.passed:
        return
    for doc_id in gate.doc_ids:
        try:
            doc = gate.doc(doc_id)
        except common.Failure as exc:
            checker.fail("%s doc" % doc_id, exc.message)
            return
        for stage in DOC_STAGES:
            try:
                sub = _stage_checks(gate, doc, stage)
            except Exception as exc:  # noqa: BLE001 - readable FAIL, never abort
                sub = Checker(stage)
                sub.fail("stage", "复算异常：%s" % exc)
            try:
                write_receipt(gate.run_dir, stage, doc_id, sub)
            except OSError:
                pass
            label = "%s %s" % (doc_id, stage)
            if not sub.passed:
                checker.fail(label, sub.failures[0]["message"])
                return
            warnings = [item["message"] for item in sub.warnings
                        if item.get("message")]
            if warnings:
                checker.warn(label, "；".join(warnings))
            else:
                checker.ok(label)


# --- entry ------------------------------------------------------------------


def stage_checks(gate: GateRun, doc: DocContext, stage: str) -> Checker:
    """Public per-document stage runner (used by report.py and gate e8)."""
    return _stage_checks(gate, doc, stage)


# --- E9–E12（阶段二，只读磁盘；E5 定稿后的只读检查） -------------------------

#: 卡片标题（JSON 里存的是带反斜杠的转义形态，见 第二期接口 §4.1）。
PHASE2_TITLE_RE = re.compile(
    r"^【Y\d{2}M\d{2}-\\\[[IVXLC]+\\\]_"
    r"(?:NR\d+-\d+|Cs\d+-\d+|Pa\d+-\d+|Q\d{2})】$")


def _norm_no(value) -> str:
    """Normalise a card/row number to the 3-digit display form (or "")."""
    try:
        return common.fmt_row(int(str(value).strip()))
    except (TypeError, ValueError):
        return ""


def _card_by_no(cards, number: str):
    for card in cards or []:
        if _norm_no(card.get("no")) == number:
            return card
    return None


def _numbered_receipts(doc_dir: str, pattern: str) -> list:
    """All ``<pattern>`` receipts by their ``<n>`` suffix: ``[(n, path), ...]``."""
    found = []
    for name in sorted(os.listdir(doc_dir)):
        match = re.match(pattern, name)
        if match:
            found.append((int(match.group(1)), os.path.join(doc_dir, name)))
    found.sort()
    return found


def _latest_numbered_receipt(doc_dir: str, pattern: str):
    """Latest ``<pattern>`` receipt by its ``<n>`` suffix: ``(n, path)`` or ``(None, None)``."""
    found = []
    for name in sorted(os.listdir(doc_dir)):
        match = re.match(pattern, name)
        if match:
            found.append((int(match.group(1)), os.path.join(doc_dir, name)))
    found.sort()
    return found[-1] if found else (None, None)


def _load_artifact(checker: Checker, path: str, label: str):
    if not os.path.isfile(path):
        checker.fail(label, "缺 %s" % path)
        return None
    try:
        return common.read_json(path)
    except common.Failure as exc:
        checker.fail(label, exc.message)
        return None


def _review_errors(review: dict, doc_id: str, role: str) -> list:
    errors = []
    if not isinstance(review, dict) or review.get("version") != 1:
        errors.append("不是 version 1")
        return errors
    if review.get("doc") != doc_id:
        errors.append("doc 不符")
    if review.get("role") != role:
        errors.append("role 不是 %s（是 %r）" % (role, review.get("role")))
    if not isinstance(review.get("fixes"), list):
        errors.append("fixes 不是数组")
    return errors


def _final_manifest_path(gate: GateRun, doc: DocContext):
    """(path, text) of the 6-column manifest the approval binds.

    0.0.8 E5 order: the ``$RUN`` copy first (translate_apply writes it there
    before any pack exists), then the pack copy as the fallback (after E8
    cleanup or a phase-two entry, where only the delivered pack counts).
    """
    run_six = doc.path("切分清单.md")
    if os.path.isfile(run_six):
        text = common.read_text(run_six)
        if common.manifest_columns(text) == 6:
            return run_six, text
    path = common.resolve_final_manifest_path(gate.work, doc.doc)
    return path, common.read_text(path)


def _check_manifest_finalized(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    """E5 定稿令牌：approve.json 必须绑定当前 6 列清单的 sha。"""
    try:
        manifest_path, manifest_text = _final_manifest_path(gate, doc)
    except common.Failure as exc:
        checker.fail("manifest_finalized", exc.message)
        return
    if common.manifest_columns(manifest_text) != 6:
        checker.fail("manifest_finalized",
                     "清单还不是 6 列：%s（先跑 E5 translate_apply）" % manifest_path)
        return
    approve_path = doc.path("approve.json")
    if not os.path.isfile(approve_path):
        checker.fail(
            "manifest_finalized",
            "缺 approve.json（E5 交付 6 列清单并获用户认可后跑 approve.py；没有定稿不能 E6 切分）")
        return
    try:
        approve = common.read_json(approve_path)
    except common.Failure as exc:
        checker.fail("manifest_finalized", exc.message)
        return
    if not isinstance(approve, dict):
        checker.fail("manifest_finalized", "approve.json 不是 JSON 对象：%s" % approve_path)
        return
    if approve.get("doc") != doc.id:
        checker.fail("manifest_finalized",
                     "approve.json 的 doc=%r 不是 %s；重新跑 approve.py 定稿"
                     % (approve.get("doc"), doc.id))
        return
    if approve.get("mode") != "user":
        checker.fail("manifest_finalized",
                     "approve.json.mode=%r（应为 user）；重新跑 approve.py 定稿"
                     % approve.get("mode"))
        return
    current_sha = common.sha256_text(manifest_text)
    if approve.get("manifest_sha256") != current_sha:
        checker.fail(
            "manifest_finalized",
            "批准绑定的 sha 与当前 6 列清单不符（清单定稿后被改过？）；"
            "重新跑 approve.py 定稿")
        return
    checker.ok("manifest_finalized")
    # 0.0.5: approve.json also records one sha per manifest row, so a future
    # change can be pointed at the exact row.  An older token without rows is
    # still valid (manifest_sha256 already covers every byte).
    rows = approve.get("rows")
    if rows is None:
        checker.ok("approve_rows", "旧 approve.json 无 rows，按 manifest sha 兼容")
        return
    expected_rows = common.manifest_row_hashes(manifest_text)
    problems = []
    if not isinstance(rows, list):
        problems.append("rows 不是数组")
    else:
        if len(rows) != len(expected_rows):
            problems.append("rows 有 %d 项，清单有 %d 行"
                            % (len(rows), len(expected_rows)))
        for got, want in zip(rows, expected_rows):
            if not isinstance(got, dict):
                problems.append("第 %d 行不是对象" % want["n"])
                break
            if got.get("n") != want["n"]:
                problems.append("第 %d 项的 n=%r（应为 %d）"
                                % (len(problems) + 1, got.get("n"), want["n"]))
                break
            if got.get("sha256") != want["sha256"]:
                problems.append("第 %d 行 sha256 与当前清单不符" % want["n"])
                break
    checker.expect("approve_rows", not problems,
                   "approve.json.rows 与当前 6 列清单不符：%s；"
                   "重新跑 approve.py 定稿" % "；".join(problems[:5]))


def check_e9(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    """cards.json 与 6 列清单 / translations.json / 校对回执逐项一致（§7）。"""
    _check_manifest_finalized(gate, doc, checker)
    six_path = doc.path("切分清单.md")
    if not checker.expect("manifest_6col", os.path.isfile(six_path),
                          "缺 $RUN 内的 6 列 切分清单.md（先跑 E5 的 translate_apply）"):
        return
    try:
        parsed = common.parse_manifest_6col(six_path)
    except common.Failure as exc:
        checker.fail("manifest_rows", exc.message)
        return
    keep_rows = [row for row in parsed["rows"] if row["keep"]]
    cards_doc = _load_artifact(checker, doc.path("cards.json"), "cards_json")
    if cards_doc is None:
        return
    cards = cards_doc.get("cards") or []
    if not checker.expect("cards_count", len(cards) == len(keep_rows),
                          "cards.json 有 %d 张，清单 keep %d 行"
                          % (len(cards), len(keep_rows))):
        return
    checker.expect("cards_order",
                   [card.get("order") for card in cards] == list(range(1, len(cards) + 1)),
                   "cards.json 的 order 不是 1..%d" % len(cards))
    titles = [card.get("title") for card in cards]
    checker.expect("cards_titles", len(set(titles)) == len(titles),
                   "cards.json 标题重复")
    bad_titles = [title for title in titles if not PHASE2_TITLE_RE.match(str(title))]
    checker.expect("cards_ids", not bad_titles,
                   "标题不符合 ID 方案：%s" % "、".join(map(str, bad_titles[:5])))
    # D: every card's audio must be a real file in the delivered pack (mp3 or
    # wav) and must be named after the card number + the pack's real extension.
    # A bare existence check would accept another card's file or an outside path.
    pack_dir = os.path.abspath(doc.pack_dir)
    if not os.path.isdir(pack_dir):
        checker.fail("card_audio_dir", "交付包不存在，无法核对卡片音频：%s" % pack_dir)
    else:
        try:
            pack_report = doc.pack_report()
        except common.Failure:
            pack_report = None
        extension = common.pack_audio_ext(pack_dir, pack_report).lower()
        bad_audio = []
        for card in cards:
            number = str(card.get("no"))
            name = str(card.get("audio") or "")
            expected = None
            try:
                expected = "%03d%s" % (int(number), extension)
            except (TypeError, ValueError):
                expected = None
            reason = None
            if not name:
                reason = "没有 audio 文件名"
            elif "/" in name or "\\" in name or ".." in name:
                reason = "audio 必须是包内文件名：%r" % name
            elif expected is None:
                reason = "卡号不是数字：%r" % number
            elif name != expected:
                reason = "audio=%r，期望包内文件 %r" % (name, expected)
            elif not os.path.isfile(os.path.join(pack_dir, name)):
                reason = "包内缺音频文件 %r" % name
            if reason:
                bad_audio.append(number)
                checker.fail("card_audio:%s" % number, reason)
        if not bad_audio:
            checker.ok("card_audio", "全部符合包内文件名并存在")
    translations = _load_artifact(checker, doc.path("translations.json"), "translations")
    if translations is None:
        return
    rows_map = translations.get("rows") or {}
    keep_by_no = {common.fmt_row(row["n"]): row for row in keep_rows}
    template_bad, sha_bad, zh_bad, en_bad = [], [], [], []
    for card in cards:
        number = str(card.get("no"))
        row = keep_by_no.get(number)
        if row is None:
            template_bad.append(number)
            continue
        translated = rows_map.get(str(int(number))) if number.isdigit() else None
        if list(card.get("zh") or []) != list(translated or []):
            zh_bad.append(number)
        if common.normalize_card_content("\n".join(card.get("en") or [])) != \
                common.normalize_card_content(common.strip_speaker_markers(row["display"])):
            en_bad.append(number)
        expected = common.render_card_content(card.get("title"), card.get("zh") or [],
                                              card.get("en") or [])
        if (card.get("content_template") or "") != expected:
            template_bad.append(number)
        if card.get("content_sha256") != common.sha256_text(card.get("content_template") or ""):
            sha_bad.append(number)
    checker.expect("cards_template", not template_bad,
                   "模板不是逐字冻结模板：%s" % "、".join(template_bad[:5]))
    checker.expect("cards_sha", not sha_bad,
                   "content_sha256 与模板不符：%s" % "、".join(sha_bad[:5]))
    # 0.0.4: the card-set identity written by cards_apply (old runs only had
    # per-card content_sha256; a missing aggregate field is tolerated).
    aggregate = cards_apply.cards_sha256(cards)
    recorded_aggregate = cards_doc.get("cards_sha256")
    if recorded_aggregate is None:
        checker.ok("cards_aggregate_sha",
                   "旧 run 无顶层 cards_sha256，按 cards[] 现算兼容")
    else:
        checker.expect("cards_aggregate_sha", recorded_aggregate == aggregate,
                       "cards.json 的卡片集 sha 与内容不符（cards_apply 之后被改过？）")
    checker.expect("cards_zh", not zh_bad,
                   "中文行与 translations.json 不一致：%s" % "、".join(zh_bad[:5]))
    checker.expect("cards_en", not en_bad,
                   "英文去前缀归一后与清单原文不一致：%s" % "、".join(en_bad[:5]))

    number, review_path = _latest_numbered_receipt(
        doc.dir, r"^cards-review-(\d+)\.json$")
    if not checker.expect("cards_review", review_path is not None,
                          "缺校对回执 cards-review-<n>.json"):
        return
    review = _load_artifact(checker, review_path, "cards_review_format")
    if review is None:
        return
    errors = _review_errors(review, doc.id, "proofreader")
    if errors:
        checker.fail("cards_review_format", "；".join(errors))
        return
    checker.ok("cards_review_format")
    cards_by_no = {str(card.get("no")): card for card in cards}
    bad_fixes = []
    for fix in review.get("fixes") or []:
        if not isinstance(fix, dict):
            bad_fixes.append("非对象")
            continue
        if "zh" in fix:
            bad_fixes.append("中文改动（只收 en_lines，回 E5）")
            continue
        raw = fix.get("no")
        try:
            number_text = common.fmt_row(int(str(raw).strip()))
        except (TypeError, ValueError):
            bad_fixes.append("行号 %r" % raw)
            continue
        card = cards_by_no.get(number_text)
        if card is None:
            bad_fixes.append("行 %s 不在 cards.json" % number_text)
            continue
        applied = [re.sub(r"^[MW]:\s+", "", line) for line in card.get("en") or []]
        if [str(line) for line in applied] != [str(line) for line in fix.get("en_lines") or []]:
            bad_fixes.append("行 %s 未应用" % number_text)
    checker.expect("cards_review_applied", not bad_fixes,
                   "校对 fixes 未应用：%s" % "、".join(bad_fixes[:10]))


# --- E10 / E11 / E12（阶段二，写入与同步） ----------------------------------


def check_e10(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    plan = _load_artifact(checker, doc.path("markji-plan.json"), "plan")
    if plan is None:
        return
    if not checker.expect("plan_version", plan.get("version") == 1, "plan 不是 version 1"):
        return
    checker.expect("plan_doc", plan.get("doc") == doc.id, "plan 的 doc 不符")
    deck = plan.get("deck") or {}
    checker.expect("plan_deck", bool(deck.get("id")) and bool(deck.get("name")),
                   "plan 缺牌组 id/name")
    # 0.1.0 D2: the plan records the gate mode (veteran deck -> sample auto-pass).
    plan_gate = plan.get("gate")
    if not isinstance(plan_gate, dict) or plan_gate.get("mode") not in ("veteran", "first"):
        checker.fail("plan_gate",
                     "markji-plan.json 缺 gate.mode（0.0.10 的旧计划）；重跑 --stage plan")
        return
    mode = markji_push.gate_mode(plan)
    deck_count = markji_push.deck_card_count(deck)
    marker = plan_gate.get("marker")
    if mode == "veteran":
        marker_ok = marker == "deck_card_count>0" and deck_count > 0
    elif marker == "first_sample_pending":
        # 0.1.0: the sample card is already written, so a non-zero count is
        # expected; the gate still stays first until a successful push.
        marker_ok = deck_count > 0
    else:
        marker_ok = marker == "empty_or_missing" and deck_count == 0
    checker.expect(
        "plan_gate",
        plan_gate.get("mode") == mode
        and plan_gate.get("sample_required") == (mode == "first")
        and marker_ok,
        "plan.gate 与 deck.card_count=%d / marker=%r 不一致（重跑 --stage plan）"
        % (deck_count, marker))
    if mode == "veteran":
        checker.ok("gate_veteran",
                   "老牌组（已有 %d 张卡），样卡闸门自动放行" % deck_count)
    else:
        checker.ok("gate_first",
                   "样卡已写、等待 App 确认（重跑 plan 仍按新牌组）"
                   if marker == "first_sample_pending"
                   else "新/空牌组：先写 1 张样卡，App 确认后再批量")
    chapter = plan.get("chapter") or {}
    checker.expect("plan_chapter_name", bool(chapter.get("name")), "plan 缺章节 name")
    checker.expect("plan_chapter_action", chapter.get("action") in ("use", "create"),
                   "plan 章节 action 非法：%r" % chapter.get("action"))
    checker.expect("plan_chapter_id",
                   (chapter.get("action") == "use") == bool(chapter.get("id")),
                   "章节 use 必须有 id，create 必须没有 id")
    cards = plan.get("cards") or []
    counts = plan.get("counts") or {}
    checker.expect("plan_cards", bool(cards), "plan 没有卡片")
    checker.expect("plan_counts",
                   counts.get("cards") == len(cards) and
                   counts.get("create", 0) + counts.get("skip_identical", 0) +
                   counts.get("diff_pending", 0) == len(cards),
                   "plan counts 四数之和与卡片数不符：%r" % counts)
    recomputed = {"cards": len(cards), "create": 0, "skip_identical": 0, "diff_pending": 0}
    bad_actions = []
    for card in cards:
        action = card.get("action")
        if action not in recomputed:
            bad_actions.append("%s:%r" % (card.get("no"), action))
        else:
            recomputed[action] += 1
        if action in ("skip_identical", "diff_pending") and not card.get("card_id"):
            bad_actions.append("%s 缺 card_id" % card.get("no"))
    checker.expect("plan_actions", not bad_actions,
                   "plan 动作不是三态：%s" % "、".join(bad_actions[:5]))
    for key in ("create", "skip_identical", "diff_pending"):
        checker.expect("plan_count_%s" % key, counts.get(key) == recomputed[key],
                       "plan counts.%s=%r 与实际 %d 不符"
                       % (key, counts.get(key), recomputed[key]))
    checker.expect("plan_sha", plan.get("plan_sha256") == common.markji_plan_sha(plan),
                   "plan_sha256 与计划内容不符（被手改过？）")
    # 0.0.4: bind the plan and the approval token to the card-set content.
    cards_doc = _load_artifact(checker, doc.path("cards.json"), "cards_json")
    if cards_doc is None:
        return
    local_cards = cards_doc.get("cards") or []
    cards_identity = cards_apply.cards_sha256(local_cards)
    checker.expect("plan_decision",
                   plan.get("decision_sha256") == markji_push.decision_sha256(plan),
                   "plan.decision_sha256 与当前计划不符（缺字段或计划被手改；重跑 --stage plan）")
    checker.expect("plan_cards_sha", plan.get("cards_sha256") == cards_identity,
                   "plan.cards_sha256 与当前 cards.json 不符（卡片改过；重跑 --stage plan）")
    aggregate = cards_doc.get("cards_sha256")
    if aggregate is not None:
        checker.expect("cards_aggregate_sha", aggregate == cards_identity,
                       "cards.json 的卡片集 sha 与内容不符（cards_apply 之后被改过？）")
    receipt_path = doc.path(os.path.join("receipts", "markji-plan.json"))
    checker.expect("plan_receipt", os.path.isfile(receipt_path), "缺 receipts/markji-plan.json")
    # 0.1.0 D2: a veteran deck auto-passes the sample gate; an existing token is
    # only the (optional) permission to overwrite diff_pending cards.
    if mode == "veteran":
        approve_path = doc.path("approve2.json")
        if not os.path.isfile(approve_path):
            checker.ok("approve_optional",
                       "老牌组没有 approve2（只有覆盖 diff_pending 才需要）")
            return
        approve = _load_artifact(checker, approve_path, "approve2")
        if approve is None:
            return
        checker.expect("approve_mode", approve.get("mode") == "user",
                       "approve2.mode 不是 user")
        checker.expect("approve_sha", approve.get("plan_sha256") == plan.get("plan_sha256"),
                       "approve2 绑定的 plan sha 不是当前 plan（计划变了要重新批准）")
        checker.expect("approve_decision",
                       approve.get("decision_sha256") == plan.get("decision_sha256"),
                       "approve2.decision_sha256 与当前 plan 不符（计划变了要重新批准）")
        checker.expect("approve_cards",
                       approve.get("cards_sha256") == plan.get("cards_sha256"),
                       "approve2.cards_sha256 与当前卡片集不符（卡片改过要重新 plan + 批准）")
        checker.expect("approve_sample_mode",
                       approve.get("sample_mode") in (None, "veteran"),
                       "approve2.sample_mode 不是 veteran（新牌组令牌？重新 plan + 批准）")
        if counts.get("diff_pending", 0) > 0:
            checker.expect("approve_updates_ok", "updates_ok" in approve,
                           "有 diff_pending 但 approve2 没有 updates_ok 字段（用户没看过差异）")
        else:
            checker.expect("approve_updates_scope", not approve.get("updates_ok"),
                           "计划里没有 diff_pending，approve2.updates_ok 不该为 true"
                           "（重新 plan 后再批准）")
        return

    # first: the sample card the user saw in the App + the post-App token.
    approve = _load_artifact(checker, doc.path("approve2.json"), "approve2")
    if approve is None:
        return
    checker.expect("approve_mode", approve.get("mode") == "user", "approve2.mode 不是 user")
    checker.expect("approve_sha", approve.get("plan_sha256") == plan.get("plan_sha256"),
                   "approve2 绑定的 plan sha 不是当前 plan（计划变了要重新批准）")
    checker.expect("approve_chapter", approve.get("chapter_ok") is True,
                   "approve2.chapter_ok 不是 true（用户还没确认章节）")
    checker.expect("approve_decision",
                   approve.get("decision_sha256") == plan.get("decision_sha256"),
                   "approve2.decision_sha256 与当前 plan 不符（计划变了要重新批准）")
    checker.expect("approve_cards",
                   approve.get("cards_sha256") == plan.get("cards_sha256"),
                   "approve2.cards_sha256 与当前卡片集不符（卡片改过要重新 plan + 批准）")
    checker.expect("approve_sample_mode",
                   approve.get("sample_mode") in (None, "first"),
                   "approve2.sample_mode 不是 first（老牌组令牌？重新 plan + 批准）")
    plan_numbers = {_norm_no(item.get("no")) for item in plan.get("cards") or []}
    approve_no = _norm_no(approve.get("sample_no"))
    checker.expect("approve_sample_no", bool(approve_no) and approve_no in plan_numbers,
                   "approve2.sample_no=%r 不在 plan.cards 里" % approve.get("sample_no"))
    if counts.get("diff_pending", 0) > 0:
        checker.expect("approve_updates_ok", "updates_ok" in approve,
                       "有 diff_pending 但 approve2 没有 updates_ok 字段（用户没看过差异）")
    else:
        checker.expect("approve_updates_scope", not approve.get("updates_ok"),
                       "计划里没有 diff_pending，approve2.updates_ok 不该为 true"
                       "（重新 plan 后再批准）")
    sample_path = doc.path(os.path.join("receipts", "markji-sample.json"))
    sample = _load_artifact(checker, sample_path, "sample_receipt")
    if sample is None:
        return
    checker.expect("sample_mode", sample.get("mode") == "first",
                   "样卡回执不是 first 模式（新牌组必须先写样卡；重跑 --stage sample）")
    checker.expect("sample_result", sample.get("result") == "ok",
                   "样卡回执不是 ok：%r" % sample.get("result"))
    sample_sha_ok = sample.get("plan_sha256") == plan.get("plan_sha256")
    pending_rebind_ok = False
    if not sample_sha_ok and marker == "first_sample_pending":
        sample_entry = _card_by_no(plan.get("cards") or [],
                                   _norm_no(sample.get("no")))
        pending_rebind_ok = (
            bool(sample_entry)
            and sample_entry.get("action") == "skip_identical"
            and str(sample_entry.get("card_id") or "")
            == str(sample.get("card_id") or ""))
    checker.expect("sample_sha", sample_sha_ok or pending_rebind_ok,
                   "样卡回执绑定的 plan sha 不是当前 plan（计划变了要重做样卡）")
    checker.expect("sample_card", bool(sample.get("card_id")),
                   "样卡回执缺 card_id")
    checker.expect("sample_readback", bool(sample.get("readback_sha256")),
                   "样卡回执缺读回 sha（必须真的读回来比对过）")
    checker.expect("sample_cards_sha",
                   sample.get("cards_sha256") == plan.get("cards_sha256"),
                   "样卡回执的 cards_sha256 与当前卡片集不符（重建样卡）")
    sample_no = _norm_no(sample.get("no"))
    sample_card = _card_by_no(local_cards, sample_no) if sample_no else None
    checker.expect("sample_content_sha",
                   bool(sample_card)
                   and sample.get("content_sha256") == sample_card.get("content_sha256"),
                   "样卡回执的 content_sha256 与 cards.json 不符（重建样卡）")
    checker.expect("sample_no", bool(sample_no) and sample_no == approve_no,
                   "样卡回执的 no 不是 approve2.sample_no")


def check_e11(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    markji = _load_artifact(checker, doc.path("markji.json"), "markji")
    if markji is None:
        return
    checker.expect("markji_version", markji.get("version") == 1, "markji.json 不是 version 1")
    checker.expect("markji_doc", markji.get("doc") == doc.id, "markji.json 的 doc 不符")
    counts = markji.get("counts") or {}
    checker.expect("markji_written", counts.get("written", 0) >= 0, "markji counts 缺 written")
    cards = markji.get("cards") or []
    checker.expect("markji_cards", bool(cards), "markji.json 没有卡片记录")
    missing_ids = [str(card.get("no")) for card in cards
                   if card.get("action") in ("create", "skip_identical")
                   and not card.get("card_id")]
    checker.expect("markji_card_ids", not missing_ids,
                   "已有卡的记录缺 card_id：%s" % "、".join(missing_ids[:5]))
    receipt_path = doc.path(os.path.join("receipts", "markji-push.json"))
    checker.expect("push_receipt", os.path.isfile(receipt_path), "缺 receipts/markji-push.json")
    push_receipt = None
    if os.path.isfile(receipt_path):
        push_receipt = _load_artifact(checker, receipt_path, "push_receipt_format")
    plan = _load_artifact(checker, doc.path("markji-plan.json"), "plan")
    cards_doc = _load_artifact(checker, doc.path("cards.json"), "cards_json")
    if plan is None or cards_doc is None:
        return
    mode = markji_push.gate_mode(plan)
    approve = {}
    if os.path.isfile(doc.path("approve2.json")):
        try:
            approve = common.read_json(doc.path("approve2.json"))
        except common.Failure as exc:
            checker.fail("approve2", exc.message)
            return
    cards_identity = cards_apply.cards_sha256(cards_doc.get("cards") or [])
    if push_receipt is not None:
        partial_flag = markji.get("partial") is True
        receipt_partial = push_receipt.get("partial") is True
        checker.expect(
            "push_partial_receipt",
            receipt_partial == partial_flag
            and (push_receipt.get("limit") if receipt_partial else None)
            == (markji.get("limit") if partial_flag else None),
            "push 回执的 partial/limit 与 markji.json 不一致")
        checker.expect("push_cards_sha",
                       push_receipt.get("cards_sha256") == cards_identity
                       and markji.get("cards_sha256") == cards_identity,
                       "push 回执/markji.json 的 cards_sha256 与当前 cards.json 不符")
        written_bad = [str(card.get("no")) for card in push_receipt.get("cards") or []
                       if isinstance(card, dict) and card.get("content_sha256")
                       and not card.get("card_id")]
        checker.expect("push_written_ids", not written_bad,
                       "本批已写卡缺 card_id：%s" % "、".join(written_bad[:5]))
    # 0.0.4 / 0.1.0: first mode must prove the sample the user confirmed; a
    # veteran deck auto-passes the sample gate (no receipt required).
    if mode == "veteran":
        checker.ok("gate_veteran", "老牌组：不要求样卡回执")
    else:
        # The push receipt is the stage evidence; markji.json is the fallback.
        sample_summary = (push_receipt or {}).get("sample")
        if not isinstance(sample_summary, dict) or not sample_summary:
            sample_summary = markji.get("sample")
        if not isinstance(sample_summary, dict):
            sample_summary = {}
        approve_no = _norm_no(approve.get("sample_no"))
        checker.expect("push_sample",
                       bool(approve_no)
                       and _norm_no((sample_summary or {}).get("no")) == approve_no,
                       "markji.json.sample.no 不是 approve2.sample_no")
    limit = markji.get("limit") if markji.get("partial") else None
    try:
        channel = common.MarkjiChannel()
        verify = markji_push.verify_payload(plan, cards_doc, channel, limit=limit,
                                            updates_ok=bool(approve.get("updates_ok")))
    except common.Failure as exc:
        checker.fail("cloud", exc.message)
        return
    diffs = verify.get("diffs") or []
    kinds = [diff.get("kind") for diff in diffs]
    card_diffs = [diff for diff in diffs
                  if diff.get("kind") in {"count", "missing", "extra", "duplicate",
                                          "chapter"}]
    if card_diffs:
        checker.fail("cards", "云端卡片集合不对：%s" % "；".join(
            "%s:%s" % (diff.get("kind"), diff.get("detail"))
            for diff in card_diffs)[:300])
    else:
        pending = verify.get("counts", {}).get("diff_pending", 0)
        if pending:
            checker.warn("cards", "diff_pending=%d" % pending)
        else:
            checker.ok("cards")
    checker.expect("order", verify.get("order_verified") is True,
                   "章节 card_ids 顺序与 cards.json order 不一致")
    checker.expect("audio", not (set(kinds) & {"audio", "structure"}),
                   "音频挂载不对：%s" % "；".join(
                       "%s:%s" % (diff.get("kind"), diff.get("detail"))
                       for diff in diffs if diff.get("kind") in {"audio", "structure"})[:300])
    content_diffs = [diff for diff in diffs if diff.get("kind") == "content"]
    checker.expect("content", not content_diffs,
                   "内容归一后不一致：%s" % "、".join(
                       str(diff.get("title") or diff.get("no")) for diff in content_diffs[:5]))
    if markji.get("partial") is True:
        # --limit is a partial write: it must not claim the order was verified.
        partial_warnings = [str(item) for item in markji.get("warnings") or []]
        partial_ok = (markji.get("order_verified") is False
                      and any("partial" in item for item in partial_warnings))
        checker.expect("markji_order_verified", partial_ok,
                       "partial（--limit）必须记录 order_verified=false，"
                       "并在 warnings 里写 partial")
        if partial_ok:
            checker.warn("order_verified_partial",
                         "partial：--limit 不重排，顺序待全量 push 再验证")
    else:
        checker.expect("markji_order_verified", markji.get("order_verified") is True,
                       "markji.json 没有记录 order_verified=true")


def check_e12(gate: GateRun, doc: DocContext, checker: Checker) -> None:
    path = doc.path(os.path.join("receipts", "markji-sync.json"))
    receipt = _load_artifact(checker, path, "sync_receipt")
    if receipt is None:
        return
    if not checker.expect("sync_version", receipt.get("version") == 1,
                          "markji-sync.json 不是 version 1"):
        return
    checker.expect("sync_doc", receipt.get("doc") == doc.id, "markji-sync.json 的 doc 不符")
    mode = receipt.get("mode")
    checker.expect("sync_mode", mode in ("check", "update"),
                   "markji-sync.json 的 mode 非法：%r" % mode)
    checker.expect("sync_diffs", isinstance(receipt.get("diffs"), list),
                   "markji-sync.json 的 diffs 不是数组")
    if mode == "update":
        checker.expect("sync_fixed", not (receipt.get("diffs") or []),
                       "--update 之后仍有 %d 条差异" % len(receipt.get("diffs") or []))
        checker.ok("sync_updated")
    else:
        checker.ok("sync_check")


def run_stage(stage: str, run_dir: str, doc_id: str | None = None) -> Checker:
    """Run one gate and return a Checker with every check line recorded."""
    checker = Checker(stage)
    if stage not in ALL_STAGES:
        checker.fail("stage", "未知关卡：%r" % stage)
        return checker
    try:
        gate = GateRun(run_dir)
    except common.Failure as exc:
        checker.fail("run", exc.message)
        return checker
    _package_drift(gate, checker)
    if stage == PHASE1_STAGE:
        check_phase1(gate, checker)
        return checker
    if stage == "e1":
        check_e1(gate, checker)
        return checker
    if stage == "e8":
        check_e8(gate, checker)
        return checker
    if not doc_id:
        checker.fail("doc", "%s 需要 --doc" % stage)
        return checker
    if doc_id not in gate.doc_ids:
        checker.fail("doc", "run.json.docs 里没有 %s（可选：%s）"
                     % (doc_id, "、".join(gate.doc_ids)))
        return checker
    try:
        doc = gate.doc(doc_id)
    except common.Failure as exc:
        checker.fail("doc", exc.message)
        return checker
    except Exception as exc:  # noqa: BLE001 - 关卡必须给出可读 FAIL，不能崩
        checker.fail("doc", "读 doc.json 出错：%s（回 E1 重建 doc.json）" % exc)
        return checker
    if stage == "e6":
        check_e6(gate, doc, checker, delivered=_pack_delivered(doc))
        return checker
    {"e2": check_e2, "e3": check_e3, "e4": check_e4,
     "e5": check_e5, "e7": check_e7, "e9": check_e9, "e10": check_e10,
     "e11": check_e11, "e12": check_e12}[stage](gate, doc, checker)
    return checker
