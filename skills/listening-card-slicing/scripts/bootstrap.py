#!/usr/bin/env python3
"""bootstrap.py -- E1: preflight the workspace, pair inputs, create/resume $RUN.

This stage answers three questions and writes down the answers so every later
stage can re-check them from disk:

1. **Can this machine run the pipeline?**  Six probes (workspace root, cut
   entry point, Python, whisper model directory, ffmpeg, split_cards).  A
   missing hard requirement is exit 2; a missing optional one (cleanup tool)
   only warns.
2. **Which audio belongs to which transcript?**  Pairing follows the frozen
   rule: the transcript name equals the audio stem or starts with it.  Zero
   candidates become ``needs_user`` entries and the script exits 3 so the
   root agent can ask the user once.  Several candidates are never guessed
   *unless* one of them carries a canonical 原文 name (0.0.6, DESIGN C3):
   ``<stem>_听力原文``, then ``<stem>_原文``, then the bare ``<stem>``.  The
   highest level that has candidates decides: exactly one wins (the other
   basenames go to ``ignored_texts`` for the audit only - never part of the
   run key, never a ``needs_user``); several at that level stay ambiguous.
   No canonical name at all also means ``ambiguous_pair`` + exit 3, while a
   lone ``<stem>_矫正.md`` is paired as any other single candidate.
   0.0.9 scans ``材料区/`` recursively (sub-directories are legal now) and
   folds duplicates before pairing: symlinks count once per realpath and
   identical basename+sha256 copies count once, keeping the shallowest path
   (so a root symlink plus its real file in a sub-directory stays one input
   with the same run key).  Folded candidates are audit-only under
   ``preflight.materials.ignored``; two *different* files sharing one audio
   stem are a real ambiguity and stay ``ambiguous_audio`` + exit 3.
3. **Which run directory is this?**  The run key is the *content* sha of the
   **selected** docs (0.0.4, ``common.content_run_key``): audio sha + text sha
   + workspace-relative paths, never mtime.  Re-running with the same bytes
   resumes the same ``$RUN`` (a touched file only warns ``input_touched``);
   changed bytes move the old run aside as ``<run>.prev-<ts>`` (never delete,
   never mix) or, when ``--run`` was given explicitly, stop with exit 3 and
   ``reason=run_mismatch`` in the report.  Only the selected docs can block the
   run: needs on unselected docs land in ``run.json.unselected_needs`` as WARN.

0.1.0 (D3): ``--deck-name`` records the deck the user picked at the E1 gate as
``run.json.options.deck_name`` (plus the flat ``run.json.deck_name`` for simple
readers) so E10 can default to it instead of asking again.  A deck name is not
part of the run identity: ``input_sha``, the selected docs and every ``--select``
semantic stay exactly as before.  On resume a new value updates the stored one
and no value keeps it; a fresh run starts without one.

Outputs: ``run.json`` (the E1 preflight object lives under
``run.json.preflight``; 0.0.5 stopped writing a second ``e1-preflight.json``)
and one ``doc.json`` per selected document.  Stdout always starts with
``RUN=<abs>`` and always ends with a ``JSON {...}`` line holding ``run``,
``docs``, ``env``, ``deck_name`` and ``needs_user``.

Exit codes: 0 ok / 1 failed to write / 2 usage or environment / 3 ask the user.

Note on paths: ``doc.json`` stores ``产出区/<stem>_…`` relative to the
workspace (接口约定 §4.2) for the default output root, and absolute paths
when a custom ``--out-root`` is used.  ``run.json`` stores inputs relative to
the workspace.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common  # noqa: E402

STAGE = "e1"

PACK_SUFFIX = "_音频切分（进牌组）"
MANIFEST_SUFFIX = "_切分清单.md"
RESULT_SUFFIX = "_切分结果"


# --- environment ------------------------------------------------------------


def probe_env(work: str, phase: str = "auto") -> dict:
    """Probe the environment facts of 接口约定 §1 (never guesses).

    0.0.4: ``phase`` does not change what is probed (a report is always
    useful), only what :func:`env_problems` treats as required.  ``phase=2``
    (the 只制卡 entry) only needs a writable workspace; cut/model/ffmpeg/
    split_cards are reported but never block.
    """
    phase = str(phase or "auto")
    writable = os.path.isdir(work) and os.access(work, os.W_OK)
    venv_python = os.path.join(work, "系统", ".venv", "bin", "python")
    workspace_ffmpeg = os.path.join(work, "系统", "bin", "ffmpeg")
    path_ffmpeg = shutil.which("ffmpeg")
    model_dir = os.path.join(work, "系统", "models", "whisper-large-v3-turbo")
    cut = os.path.join(work, "系统", "tools", "cut")
    split_cards = os.path.join(work, "系统", "tools", "split_cards.py")
    cleanup = os.path.join(work, "系统", "tools", "cleanup_outputs.py")
    say = shutil.which("say")

    python_path = venv_python if os.path.isfile(venv_python) else sys.executable
    env = {
        "work": {
            "path": work,
            "ok": writable,
            "required": True,
            "warning": None if writable else "工作区不可写：E1 需要能写 run/日志",
        },
        "cut": {
            "path": cut,
            "ok": os.path.isfile(cut),
            "executable": os.access(cut, os.X_OK),
            "required": True,
        },
        "python": {
            "path": python_path,
            "ok": True,
            "source": "venv" if python_path == venv_python else "sys.executable",
            "warning": None if python_path == venv_python else "工作区 venv 不存在，回落到当前 Python",
            "required": False,
        },
        "model": {
            "path": model_dir,
            "ok": os.path.isdir(model_dir),
            "required": True,
        },
        "ffmpeg": {
            "path": workspace_ffmpeg if os.path.isfile(workspace_ffmpeg) else (path_ffmpeg or workspace_ffmpeg),
            "ok": os.path.isfile(workspace_ffmpeg) or bool(path_ffmpeg),
            "source": "workspace" if os.path.isfile(workspace_ffmpeg) else ("PATH" if path_ffmpeg else None),
            "required": True,
        },
        "split_cards": {
            "path": split_cards,
            "ok": os.path.isfile(split_cards),
            "required": True,
        },
        "cleanup": {
            "path": cleanup,
            "ok": os.path.isfile(cleanup),
            "fallback": os.path.join(HERE, "cleanup.py"),
            "required": False,
            "warning": None if os.path.isfile(cleanup)
            else "cleanup_outputs.py 缺失，E7 改用本 skill 的 cleanup.py（兜底）",
        },
        "say": {
            "path": say,
            "ok": bool(say),
            "required": False,
            "warning": None if say
            else "say 不存在：D 队零依赖 E2E（合成音频）跑不了；不影响真实音频流程",
        },
    }
    if env["cut"]["ok"] and not env["cut"]["executable"]:
        env["cut"]["warning"] = "cut 没有可执行位，切分阶段会失败（chmod +x）"
    return env


def env_problems(env: dict, phase: str = "auto") -> dict:
    """Split probe results into hard failures and soft warnings.

    ``phase=2`` only requires ``work`` (see :func:`probe_env`); the normal
    phase still requires every item flagged ``required``.
    """
    phase = str(phase or "auto")
    if phase == "2":
        required = lambda name, item: name == "work"  # noqa: E731 - tiny local
    else:
        required = lambda name, item: bool(item.get("required"))  # noqa: E731
    missing = [name for name, item in env.items()
               if required(name, item) and not item.get("ok")]
    warnings = []
    for name, item in env.items():
        if item.get("warning"):
            warnings.append("%s: %s" % (name, item["warning"]))
    return {"missing": missing, "warnings": warnings}


# --- materials --------------------------------------------------------------


def _material_depth(area: str, path: str) -> int:
    """Directories below ``材料区/`` (0 = directly inside, 1 = one level down)."""
    return os.path.relpath(path, area).count(os.sep)


def _ignored_material(item: dict, kept: dict, kind: str) -> dict:
    """One audit entry for a candidate that was folded into *kept*."""
    if kind == "symlink":
        reason = "与 %s 指向同一目标（realpath 相同）" % kept["path"]
    else:
        reason = "basename+sha256 相同，保留路径较浅的一份"
    return {
        "path": os.path.abspath(item["path"]),
        "kept": os.path.abspath(kept["path"]),
        "kind": kind,
        "reason": reason,
    }


def scan_materials(work: str) -> dict:
    """Recursively collect the audio/text inputs below ``材料区/`` (0.0.9).

    Returns ``{"audio": [...], "text": [...], "ignored": [...]}``:

    * every depth is scanned (0.0.8 only looked at the direct children, so a
      ``材料区/【CET-4】26年06月_听力音频/`` sub-directory was invisible);
    * hidden files and directories (``.DS_Store`` included) are skipped;
    * symlinks are deduplicated by ``realpath`` - one target counts once and
      the shallowest path wins, so a root-level link plus its real file in a
      sub-directory keeps the link path (and therefore the same run key as
      before the sub-directory existed);
    * distinct paths whose basename *and* sha256 are identical are collapsed
      the same way; the dropped ones are returned in ``ignored`` for the
      E1 audit only - they never change pairing or the run key.

    The lists are absolute paths sorted shallowest-first, then by path.
    """
    area = os.path.join(work, "材料区")
    if not os.path.isdir(area):
        return {"audio": [], "text": [], "ignored": []}
    found = []
    for root, dirnames, filenames in os.walk(area):
        dirnames[:] = sorted(name for name in dirnames
                             if not name.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):  # .DS_Store and friends
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):  # dangling symlink / vanished file
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in common.AUDIO_EXTS:
                kind = "audio"
            elif ext in common.TEXT_EXTS:
                kind = "text"
            else:
                continue
            real = os.path.realpath(path)
            if not os.path.isfile(real):
                continue
            found.append({
                "path": os.path.abspath(path),
                "real": real,
                "name": name,
                "kind": kind,
                "size": os.path.getsize(path),
                "depth": _material_depth(area, path),
            })
    found.sort(key=lambda item: (item["depth"], item["path"]))

    ignored = []
    unique = []
    by_real = {}
    for item in found:  # sorted: the shallowest target wins
        kept = by_real.get(item["real"])
        if kept is None:
            by_real[item["real"]] = item
            unique.append(item)
        else:
            ignored.append(_ignored_material(item, kept, "symlink"))

    by_name_size = {}
    for item in unique:
        by_name_size.setdefault((item["name"], item["size"]), []).append(item)
    kept_items = []
    for group in by_name_size.values():
        if len(group) == 1:
            kept_items.append(group[0])
            continue
        by_sha = {}
        for item in group:  # only hash when basename+size collide
            item["sha256"] = common.sha256_file(item["path"])
            by_sha.setdefault(item["sha256"], []).append(item)
        for same in by_sha.values():
            kept_items.append(same[0])
            for item in same[1:]:
                ignored.append(_ignored_material(item, same[0], "duplicate"))

    kept_items.sort(key=lambda item: (item["depth"], item["path"]))
    audios = [item["path"] for item in kept_items if item["kind"] == "audio"]
    texts = [item["path"] for item in kept_items if item["kind"] == "text"]
    return {"audio": audios, "text": texts, "ignored": ignored}


def _matches(stem: str, text_path: str) -> bool:
    name = os.path.basename(text_path)
    base = os.path.splitext(name)[0]
    return base == stem or name.startswith(stem)


#: 0.0.6 (DESIGN C3): how the user names the 原文, most specific first.
CANONICAL_TEXT_SUFFIXES = ("_听力原文", "_原文", "")


def canonical_text(stem: str, candidates) -> str | None:
    """The single canonical transcript among several candidates, or None.

    The suffixes are tried one level at a time (``<stem>_听力原文``, then
    ``<stem>_原文``, then the bare ``<stem>``): a level with exactly one
    candidate wins, a level with several is ambiguous and returns None.
    That keeps ``x_听力原文.md`` ahead of ``x_原文.md`` while a same-level
    tie (e.g. ``x_听力原文.md`` + ``x_听力原文.txt``) is never guessed.
    """
    for suffix in CANONICAL_TEXT_SUFFIXES:
        name = stem + suffix
        found = [path for path in candidates
                 if os.path.splitext(os.path.basename(path))[0] == name]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None


def pair_documents(audios, texts, work: str | None = None) -> tuple:
    """Pair each audio with exactly one transcript; ambiguity never guessed.

    Returns ``(docs, needs)`` where ``docs`` are fully paired documents in
    audio-name order with stable ids (``d01``..) and ``needs`` are
    ``needs_user`` entries explaining every unpaired/ambiguous file.

    0.0.9: the recursive 材料区 scan can legitimately find two *different*
    files that share one audio stem (e.g. ``材料区/a/x.mp3`` and
    ``材料区/b/x.mp3`` with different bytes).  That is a real ambiguity - the
    audio itself is undecidable - so it becomes ``ambiguous_audio`` before
    any transcript matching instead of two silently duplicated documents.
    A transcript may still be the canonical three-level winner among several
    names; same-level ties stay ``ambiguous_pair`` (0.0.6 C3, unchanged).
    """
    docs = []
    needs = []
    by_stem = {}
    for audio in audios:
        by_stem.setdefault(os.path.splitext(os.path.basename(audio))[0],
                           []).append(audio)
    ambiguous_stems = {stem: paths for stem, paths in by_stem.items()
                       if len(paths) > 1}
    reported = set()
    for index, audio in enumerate(audios, start=1):
        doc_id = "d%02d" % index
        stem = os.path.splitext(os.path.basename(audio))[0]
        if stem in ambiguous_stems:
            paths = ambiguous_stems[stem]
            if stem not in reported:
                reported.add(stem)
                names = [common.rel_to(work, path) if work else path
                         for path in paths]
                needs.append(
                    {
                        "kind": "ambiguous_audio",
                        "id": doc_id,
                        "audio": audio,
                        "audios": [os.path.abspath(path) for path in paths],
                        "candidates": names,
                        "message": "同一个音频主名「%s」下有 %d 个不同内容的文件（%s），我不猜；"
                                   "请只留一个或改名" % (stem, len(paths), "、".join(names)),
                        "options": ["删掉/移走多余的同名音频", "改名让每个音频主名唯一"],
                    }
                )
            continue
        candidates = [text for text in texts if _matches(stem, text)]
        if not candidates:
            needs.append(
                {
                    "kind": "missing_text",
                    "id": doc_id,
                    "audio": audio,
                    "message": "音频《%s》没有找到唯一对应的原文（材料区里没有文件名以它开头的 .md/.txt）"
                               % os.path.basename(audio),
                    "options": ["把原文放进 材料区（文件名以音频名开头）", "从 --select 里排除这个音频"],
                }
            )
            continue
        if len(candidates) > 1:
            # 0.0.6 (DESIGN C3): a unique canonical 原文 wins; the rest is
            # audit-only.  No/multiple canonical names stay ambiguous_pair.
            chosen = canonical_text(stem, candidates)
            if chosen is not None:
                ignored = [path for path in candidates if path != chosen]
                docs.append({
                    "id": doc_id,
                    "audio": audio,
                    "text": chosen,
                    "stem": stem,
                    "ignored_texts": [os.path.basename(path)
                                      for path in ignored],
                })
                continue
            needs.append(
                {
                    "kind": "ambiguous_pair",
                    "id": doc_id,
                    "audio": audio,
                    "candidates": [os.path.basename(path) for path in candidates],
                    "message": "音频《%s》有 %d 个候选原文（%s），我不猜，请指定用哪一个"
                               % (os.path.basename(audio), len(candidates),
                                  "、".join(os.path.basename(path) for path in candidates)),
                    "options": ["指定一个原文文件（或删掉多余的）"],
                }
            )
            continue
        docs.append({"id": doc_id, "audio": audio, "text": candidates[0],
                     "stem": stem, "ignored_texts": []})

    # A transcript claimed by two audios is ambiguous for both of them.
    owners = {}
    for doc in docs:
        owners.setdefault(doc["text"], []).append(doc)
    duplicated = [paths for paths in owners.values() if len(paths) > 1]
    for group in duplicated:
        names = [os.path.basename(doc["audio"]) for doc in group]
        for doc in group:
            docs.remove(doc)
        for doc in group:
            needs.append(
                {
                    "kind": "duplicate_text",
                    "id": doc["id"],
                    "audio": doc["audio"],
                    "text": doc["text"],
                    "message": "同一个原文被多个音频匹配（%s），我不猜；请改名或分开处理"
                               % "、".join(names),
                    "options": ["重命名音频或原文，使每个音频只匹配一个原文"],
                }
            )
    return docs, needs


def materials_presence_needs(audios, texts) -> list:
    """``needs_user`` entries for an empty ``材料区/`` (no audio and/or text).

    0.1.0: shared with ``preflight_choices.py`` so the E1 choice gate and
    bootstrap describe the same empty-material state with the same kinds and
    wording instead of drifting apart.
    """
    needs = []
    if not audios:
        needs.append(
            {
                "kind": "no_audio",
                "message": "材料区里没有音频（支持 %s）" % "、".join(common.AUDIO_EXTS),
                "options": ["把音频放进 材料区/"],
            }
        )
    if not texts:
        needs.append(
            {
                "kind": "no_text",
                "message": "材料区里没有原文（支持 .md/.txt）",
                "options": ["把原文放进 材料区/，文件名以音频名开头"],
            }
        )
    return needs


# --- run directory ----------------------------------------------------------


def find_superseded_runs(root: str, work: str, stems) -> list:
    """Find older runs for *the same job* whose inputs have drifted.

    A run is the same job when its ``run.json.work`` matches and its selected
    stems are exactly the current set.  Input drift gives a new run key and
    therefore a new directory name, so the old directory cannot be found by
    name; this scan is how the contract's "drift renames the old run" rule is
    honoured.  Renaming is non-destructive and only ever touches directories
    that are unmistakably this skill's runs for this workspace and job.
    """
    wanted = set(stems)
    found = []
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir() or ".prev-" in entry.name:
            continue
        run_json = os.path.join(entry.path, "run.json")
        if not os.path.isfile(run_json):
            continue
        try:
            data = common.read_json(run_json)
        except common.Failure:
            continue
        if os.path.abspath(data.get("work") or "") != os.path.abspath(work):
            continue
        docs = [doc for doc in (data.get("docs") or []) if isinstance(doc, dict)]
        if {doc.get("stem") for doc in docs} != wanted:
            continue
        found.append(entry.path)
    return found


def _move_aside(path: str) -> str:
    """Rename ``path`` to ``<path>.prev-<ts>``; never overwrite, never delete.

    Used both for a stale ``$RUN`` and for an old cards pack (接口修订 A3);
    the timestamp is taken once so the ``-2`` fallback keeps the same stamp.
    """
    stamp = common.now_compact()
    target = "%s.prev-%s" % (path, stamp)
    suffix = 1
    while os.path.exists(target):
        suffix += 1
        target = "%s.prev-%s-%d" % (path, stamp, suffix)
    os.rename(path, target)
    return target


def archive_existing_packs(docs, selected, request: str, log) -> list:
    """接口修订 A3: move the selected docs' old packs aside, never delete.

    ``request`` is ``all`` or a comma separated id list.  Returns the
    ``archived[]`` entries for ``run.json``.  A requested id that is not
    selected, or has no existing pack, is a usage error (we do not guess what
    the caller meant).
    """
    selected_ids = [doc["id"] for doc in selected]
    if request == "all":
        targets = [doc for doc in selected if doc.get("existing_pack")]
    else:
        wanted = [part.strip() for part in request.split(",") if part.strip()]
        unknown = [doc_id for doc_id in wanted if doc_id not in selected_ids]
        if unknown:
            raise common.Failure(
                common.EXIT_USAGE,
                "--archive-existing 里这些 id 不在本次选中：%s（选中：%s）"
                % ("、".join(unknown), "、".join(selected_ids)),
            )
        without = [doc["id"] for doc in selected
                   if doc["id"] in wanted and not doc.get("existing_pack")]
        if without:
            raise common.Failure(
                common.EXIT_USAGE,
                "--archive-existing 里这些 id 没有可归档的旧包：%s" % "、".join(without),
            )
        targets = [doc for doc in selected
                   if doc["id"] in wanted and doc.get("existing_pack")]
    entries = []
    for doc in targets:
        old = doc["existing_pack"]
        if not os.path.isdir(old):
            raise common.Failure(
                common.EXIT_FAIL,
                "要归档的旧包已经不在了：%s（可能被别处移动过，请复查产出区）" % old,
            )
        new = _move_aside(old)
        entry = {"doc": doc["id"], "from": old, "to": new, "at": common.now_iso()}
        entries.append(entry)
        doc["existing_pack"] = None
        doc["archived_to"] = new
        if log:
            log("archived old pack %s -> %s" % (old, new))
    return entries


def _out_path(work: str, out_base: str, name: str, custom: bool) -> str:
    """Relative to the workspace for the default root, absolute for a custom root."""
    path = os.path.join(out_base, name)
    if custom:
        return path
    return common.rel_to(work, path)


def build_parser() -> argparse.ArgumentParser:
    parser = common.new_parser(
        "bootstrap.py",
        "E1：探测环境、扫描材料区并配对、建/续 $RUN，写 run.json（含 preflight）/ doc.json。",
    )
    parser.add_argument("--work", default=None, help="工作区根（默认从 cwd 向上找）")
    parser.add_argument("--run", default=None, help="显式指定 $RUN（不自动改名，冲突时 exit 3）")
    parser.add_argument("--select", default=None, help="只处理这些文档 id，例如 d01,d02")
    parser.add_argument("--deck-name", default=None, metavar="NAME",
                        help="E1 选定的墨墨牌组精确名称（存进 run.json.options.deck_name，"
                             "E10 默认使用；resume 时不给则保留旧值）")
    parser.add_argument("--out-root", default=None,
                        help="产出根目录（默认 <work>/产出区；相对路径按工作区解析）")
    parser.add_argument("--fresh", action="store_true",
                        help="不续跑：把同名旧 run 改名 .prev-<ts> 后新建")
    parser.add_argument("--archive-existing", default=None, metavar="d01,d02|all",
                        help="把选中文档的旧卡片包改名为 <pack>.prev-<ts> 并记进 run.json.archived[]")
    parser.add_argument("--phase", choices=("1", "2", "auto"), default="auto",
                        help="阶段：auto/1=原行为（已有同名包 → needs_user）；"
                             "2=只制卡入口（已有同名包直接复用，不报 needs_user）")
    parser.add_argument("--json", action="store_true", help="只输出 RUN= 首行与末行 JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    log = None
    result = {"ok": False, "exit_code": common.EXIT_FAIL, "stage": STAGE}
    if args.phase == "2" and args.archive_existing:
        print("ERROR: --phase 2（复用已有包）与 --archive-existing（归档后重切）互斥；只给一个。",
              file=sys.stderr)
        common.emit_json({"ok": False, "exit_code": common.EXIT_USAGE, "stage": STAGE,
                          "error": "--phase 2 与 --archive-existing 互斥"})
        return common.EXIT_USAGE

    try:
        # -- 0. pure usage checks before anything is created or written: a bad
        #       --deck-name must not leave a half-built $RUN behind (0.1.0 D3).
        requested_deck_name = _deck_name(args.deck_name)

        # -- 1. workspace (explicit --run wins, run.json remembers the work root)
        explicit_run = os.path.abspath(args.run) if args.run else None
        old_run = None
        want_phase = args.phase
        if explicit_run and os.path.isfile(os.path.join(explicit_run, "run.json")):
            old_run = common.load_run(explicit_run)
            if args.phase == "auto":
                # DESIGN §2.1: the phase comes from run.options.phase on resume.
                want_phase = (old_run.get("options") or {}).get("phase") or "auto"
            work = os.path.abspath(old_run.get("work") or "")
            if not os.path.isdir(work):
                raise common.Failure(
                    common.EXIT_USAGE,
                    "--run 的 run.json 指向的工作区不存在：%s" % work,
                )
            if args.work and os.path.abspath(args.work) != work:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "--work（%s）与 --run 记录的 work（%s）不一致；只给一个。"
                    % (os.path.abspath(args.work), work),
                )
        elif want_phase == "2" and args.work and os.path.isdir(args.work):
            # --phase 2 only needs a writable workspace: cut/model/ffmpeg/
            # split_cards are not required (DESIGN §2.1), so a workspace whose
            # 系统/ is not installed yet is still a legal 只制卡 input.
            work = os.path.abspath(args.work)
        else:
            work = common.resolve_work(args.work)

        # -- 2. environment (probe early; phase is finalised after the run
        #        identity is known, because a resumed run remembers its phase)
        env = probe_env(work, want_phase)
        problems = env_problems(env, args.phase)
        deferred_env = args.phase == "auto"
        if not deferred_env and problems["missing"]:
            raise common.Failure(
                common.EXIT_USAGE,
                "环境不完整，缺：%s。请先修好工作区环境（可跑 系统/tools/doctor.py 体检）。"
                % "、".join(problems["missing"]),
                env=env,
                env_warnings=problems["warnings"],
            )

        # -- 3. materials + pairing (0.0.9: recursive scan + dedupe)
        scanned = scan_materials(work)
        audios = scanned["audio"]
        texts = scanned["text"]
        ignored_materials = scanned["ignored"]
        docs, needs = pair_documents(audios, texts, work)
        materials = {
            "audio": [common.file_facts(path) for path in audios],
            "text": [common.file_facts(path) for path in texts],
            # Audit only: symlink / same-basename+sha256 candidates that were
            # folded into the ones above.  Never part of pairing or the run key.
            "ignored": ignored_materials,
        }
        paired_ids = {doc["id"] for doc in docs}
        needs.extend(materials_presence_needs(audios, texts))

        # -- 4. output root + existing packs
        custom_out = bool(args.out_root)
        if args.out_root:
            out_base = args.out_root
            if not os.path.isabs(out_base):
                out_base = os.path.join(work, out_base)
            out_base = os.path.abspath(out_base)
        else:
            out_base = os.path.join(work, "产出区")
        common.ensure_dir(out_base)

        existing_packs = []
        for entry in sorted(os.scandir(out_base), key=lambda item: item.name):
            if entry.is_dir() and entry.name.endswith(PACK_SUFFIX):
                existing_packs.append(entry.path)

        for doc in docs:
            doc["audio_sha256"] = common.sha256_file(doc["audio"])
            doc["text_sha256"] = common.sha256_file(doc["text"])
            doc["audio_size"] = os.path.getsize(doc["audio"])
            doc["text_size"] = os.path.getsize(doc["text"])
            pack = os.path.join(out_base, doc["stem"] + PACK_SUFFIX)
            doc["existing_pack"] = pack if os.path.isdir(pack) else None
            if doc["text_size"] == 0:
                needs.append(
                    {
                        "kind": "empty_text",
                        "id": doc["id"],
                        "audio": doc["audio"],
                        "text": doc["text"],
                        "message": "原文文件是空的：%s" % doc["text"],
                        "options": ["补上原文后重跑"],
                    }
                )
            if doc["audio_size"] == 0:
                needs.append(
                    {
                        "kind": "empty_audio",
                        "id": doc["id"],
                        "audio": doc["audio"],
                        "message": "音频文件是空的：%s" % doc["audio"],
                        "options": ["换一个音频文件"],
                    }
                )

        # -- 5. selection
        selected_ids = None
        if args.select:
            requested = [part.strip() for part in args.select.split(",") if part.strip()]
            unknown = [doc_id for doc_id in requested if doc_id not in paired_ids]
            if unknown:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "--select 里这些 id 不存在或没有配对成功：%s（可配对的：%s）"
                    % ("、".join(unknown), "、".join(sorted(paired_ids)) or "无"),
                )
            selected_ids = set(requested)
        selected = [doc for doc in docs if selected_ids is None or doc["id"] in selected_ids]
        if not selected:
            raise common.Failure(
                common.EXIT_ASK,
                "材料区里没有可处理的「音频+原文」成对文件；未创建 run，未改动任何文件。",
                work=work,
                env=env,
                docs=[],
                needs_user=needs,
                existing_packs=existing_packs,
            )

        # -- 6. $RUN identity: content sha of the *selected* docs only
        key = common.content_run_key(
            [(doc["audio"], doc["text"]) for doc in selected], work)
        input_facts = {
            "version": 1,
            "docs": [
                {
                    "doc": doc["id"],
                    "audio": common.rel_to(work, doc["audio"]),
                    "text": common.rel_to(work, doc["text"]),
                    "audio_sha256": doc["audio_sha256"],
                    "text_sha256": doc["text_sha256"],
                    "audio_size": doc["audio_size"],
                    "text_size": doc["text_size"],
                    # mtime is a hint only: never part of the key (DESIGN §2.1)
                    "audio_mtime_ns": os.stat(doc["audio"]).st_mtime_ns,
                    "text_mtime_ns": os.stat(doc["text"]).st_mtime_ns,
                    # 0.0.6 (DESIGN C3): audit only; the run key above uses
                    # the chosen pair, never this list.
                    "ignored_texts": list(doc.get("ignored_texts") or []),
                }
                for doc in selected
            ],
        }
        warnings = []
        if ignored_materials:
            warnings.append(
                "materials_ignored: 材料区有 %d 个重复/软链接候选只算一份"
                "（审计见 run.json.preflight.materials.ignored）" % len(ignored_materials))
        resume = False
        moved_aside = []
        if explicit_run:
            if old_run is not None:
                if old_run.get("input_sha") != key:
                    common.ensure_dir(explicit_run)
                    log = common.StageLog(explicit_run, STAGE)
                    log("input_sha mismatch: run=%r now=%r" % (old_run.get("input_sha"), key))
                    preflight = _preflight(
                        work=work, run_dir=explicit_run, env=env, docs=docs,
                        selected=selected, needs=needs, materials=materials,
                        existing_packs=existing_packs, reason="run_mismatch",
                        env_warnings=problems["warnings"], key=key,
                    )
                    # 0.0.5: no separate e1-preflight.json any more; the
                    # mismatch evidence travels in the JSON failure payload
                    # (the existing run.json belongs to another input and is
                    # never rewritten by a failed bootstrap).
                    print("RUN=%s" % explicit_run)
                    raise common.Failure(
                        common.EXIT_ASK,
                        "显式 --run 指向的 run 属于另一批输入（input_sha 不符）：%s\n"
                        "不自动改名；请换一个新目录，或不带 --run 让脚本自动建新 run。"
                        % explicit_run,
                        reason="run_mismatch",
                        run=explicit_run,
                        input_sha=key,
                        preflight=preflight,
                    )
                if args.fresh:
                    raise common.Failure(
                        common.EXIT_ASK,
                        "显式 --run 指向已有 run，同时又给了 --fresh；请换一个空目录，"
                        "或不带 --run 让脚本把旧 run 改名保留。",
                        reason="run_fresh_conflict",
                        run=explicit_run,
                    )
                resume = True
            else:
                if os.path.isdir(explicit_run) and os.listdir(explicit_run):
                    raise common.Failure(
                        common.EXIT_ASK,
                        "显式 --run 目录非空，但里面没有 run.json（不是本 skill 的 run）：%s\n"
                        "不往别人的目录里写；请换一个空目录。" % explicit_run,
                        reason="run_not_ours",
                        run=explicit_run,
                    )
                run_dir = explicit_run
        else:
            default_dir = common.default_run_dir(key)
            moved = []
            if os.path.isfile(os.path.join(default_dir, "run.json")):
                try:
                    existing = common.load_run(default_dir)
                except common.Failure:
                    existing = None
                if existing is not None and existing.get("input_sha") == key and not args.fresh:
                    resume = True
                    old_run = existing
                    run_dir = default_dir
                else:
                    moved.append(_move_aside(default_dir))
                    run_dir = default_dir
            elif os.path.isdir(default_dir) and os.listdir(default_dir):
                moved.append(_move_aside(default_dir))
                run_dir = default_dir
            else:
                run_dir = default_dir
            if not resume:
                # Input drift lands in a new keyed directory; move runs for the
                # same workspace+job aside so two runs never look current.
                for old_path in find_superseded_runs(
                        os.path.dirname(default_dir), work,
                        [doc["stem"] for doc in selected]):
                    if os.path.abspath(old_path) == os.path.abspath(run_dir):
                        continue
                    moved.append(_move_aside(old_path))
            moved_aside = moved
        if explicit_run:
            run_dir = explicit_run
        if resume and old_run is not None:
            # DESIGN §2.1: phase comes from run.options.phase on resume.
            if args.phase == "auto":
                want_phase = (old_run.get("options") or {}).get("phase") or want_phase
            # Touching a file without changing its bytes keeps the same run;
            # say so instead of silently reusing it (DESIGN §2.1).
            warnings.extend(_input_touch_warnings(old_run, input_facts))
        if deferred_env or want_phase == "2":
            problems = env_problems(env, want_phase)
            if problems["missing"]:
                raise common.Failure(
                    common.EXIT_USAGE,
                    "环境不完整，缺：%s。请先修好工作区环境（可跑 系统/tools/doctor.py 体检）。"
                    % "、".join(problems["missing"]),
                    env=env,
                    env_warnings=problems["warnings"],
                )

        common.ensure_dir(run_dir)
        common.ensure_dir(os.path.join(run_dir, "logs"))  # 接口修订 A6
        log = common.StageLog(run_dir, STAGE)
        for old_path in moved_aside:
            log("moved old run aside: %s" % old_path)
        for doc in selected:
            if doc.get("ignored_texts"):
                log("ignored extra text candidates: %s -> %s"
                    % (doc["id"], "、".join(doc["ignored_texts"])))
        for item in ignored_materials:
            log("ignored material %s: %s (kept %s)"
                % (item["kind"], item["path"], item["kept"]))

        # -- 7. old packs: archive on request, otherwise report a conflict
        archived = []
        if resume and old_run is not None and isinstance(old_run.get("archived"), list):
            archived.extend(old_run["archived"])
        if args.archive_existing:
            archived.extend(archive_existing_packs(docs, selected,
                                                   args.archive_existing, log))
        for doc in selected:
            if not doc.get("existing_pack"):
                continue
            if want_phase == "2":
                # AMENDMENTS-2 A2-7: the 「只制卡」 entry reuses the finished
                # pack on purpose; it is not a conflict.
                log("reuse existing pack for %s: %s" % (doc["id"], doc["existing_pack"]))
                continue
            needs.append(
                {
                    "kind": "existing_pack",
                    "id": doc["id"],
                    "audio": doc["audio"],
                    "existing_pack": doc["existing_pack"],
                    "message": "产出区已有同名包《%s》，默认不覆盖；请选择「覆盖（先归档再重切）」"
                               "或「保留（本次不选这个文档）」" % os.path.basename(doc["existing_pack"]),
                    "options": ["overwrite", "keep"],
                }
            )

        # -- 7b. only the *selected* docs may block the run (DESIGN §2.1)
        selected_scope = set(selected_ids or [doc["id"] for doc in selected])
        selected_needs = [item for item in needs
                          if not item.get("id") or item["id"] in selected_scope]
        unselected_needs = [item for item in needs
                            if item.get("id") and item["id"] not in selected_scope]
        for item in unselected_needs:
            warnings.append("unselected_needs: %s（%s）未选中，不阻断"
                            % (item.get("id"), item.get("kind")))
        exit_code = common.EXIT_ASK if selected_needs else common.EXIT_OK

        # -- 8. deck name picked at the E1 gate (0.1.0 D3): explicit wins, a
        #        resume keeps the old value, nothing is guessed.  It never
        #        enters the run identity (key/selected docs stay untouched).
        deck_name = requested_deck_name
        if deck_name is None and resume and old_run is not None:
            deck_name = _old_deck_name(old_run)

        # -- 9. write run.json (with the preflight object) + doc.json
        if resume and old_run is not None:
            created_at = old_run.get("created_at") or common.now_iso()
            package = old_run.get("package") or _new_package()
            version = old_run.get("skill_version") or common.skill_version()
        else:
            created_at = common.now_iso()
            package = _new_package()
            version = common.skill_version()

        run_json = {
            "version": 1,
            "skill_version": version,
            "work": work,
            "run": run_dir,
            "created_at": created_at,
            "input_sha": key,
            "input_facts": input_facts,
            "unselected_needs": unselected_needs,
            # 0.1.0 D3: flat copy for simple readers (markji_push.resolve_deck_name
            # prefers options.deck_name, then this one, then DEFAULT_DECK).
            "deck_name": deck_name,
            "options": _options(out_base, want_phase, deck_name),
            "docs": [
                {
                    "id": doc["id"],
                    "audio": common.rel_to(work, doc["audio"]),
                    "text": common.rel_to(work, doc["text"]),
                    "stem": doc["stem"],
                    "ignored_texts": list(doc.get("ignored_texts") or []),
                }
                for doc in selected
            ],
            "archived": archived,
            "package": package,
        }
        # 0.0.5: the preflight facts are part of run.json (run.json.preflight)
        # instead of a second small file; gate e1 reads that field.
        run_json["preflight"] = _preflight(
            work=work, run_dir=run_dir, env=env, docs=docs, selected=selected,
            needs=needs, materials=materials, existing_packs=existing_packs,
            reason=None, env_warnings=problems["warnings"], key=key,
            moved_aside=moved_aside, archived=archived, phase=want_phase,
            input_facts=input_facts, unselected_needs=unselected_needs,
            warnings=warnings,
        )
        common.atomic_write_json(os.path.join(run_dir, "run.json"), run_json)

        for doc in selected:
            # 0.0.9: per-doc audit of the duplicate/symlink candidates that
            # share this document's audio or transcript basename.
            doc_names = {os.path.basename(doc["audio"]),
                         os.path.basename(doc["text"])}
            doc_ignored = [
                common.rel_to(work, item["path"]) for item in ignored_materials
                if os.path.basename(item["path"]) in doc_names
                or os.path.basename(item["kept"]) in doc_names
            ]
            pack_dir_value = _out_path(work, out_base, doc["stem"] + PACK_SUFFIX, custom_out)
            manifest_value = _out_path(work, out_base, doc["stem"] + MANIFEST_SUFFIX, custom_out)
            pack_manifest_value = os.path.join(pack_dir_value, doc["stem"] + MANIFEST_SUFFIX)
            doc_json = {
                "version": 1,
                "id": doc["id"],
                "audio": os.path.abspath(doc["audio"]),
                "text": os.path.abspath(doc["text"]),
                "stem": doc["stem"],
                "audio_sha256": doc["audio_sha256"],
                "text_sha256": doc["text_sha256"],
                "lang": "auto",
                "result_dir": _out_path(work, out_base, doc["stem"] + RESULT_SUFFIX, custom_out),
                "pack_dir": pack_dir_value,
                "manifest": manifest_value,
                "existing_pack": doc["existing_pack"],
                "ignored_texts": list(doc.get("ignored_texts") or []),
                "ignored_materials": doc_ignored,
            }
            if want_phase == "2":
                doc_json["reuse_pack"] = bool(doc["existing_pack"])
                if doc_json["reuse_pack"]:
                    # E8 起直接读包内 6 列清单；manifest 也指向包内（不再指望产出区根副本）。
                    doc_json["pack_manifest"] = pack_manifest_value
                    doc_json["manifest"] = pack_manifest_value
            # DESIGN §2.1: merge, never clobber downstream keys such as
            # manifest_sha256 / pack_manifest / manifest_sha256_v2.
            doc_path = os.path.join(common.doc_dir(run_dir, doc["id"]), "doc.json")
            merged = {}
            if os.path.isfile(doc_path):
                try:
                    old_doc = common.read_json(doc_path)
                except common.Failure:
                    old_doc = None
                if isinstance(old_doc, dict):
                    merged.update(old_doc)
            merged.update(doc_json)
            common.atomic_write_json(doc_path, merged)
            log("doc %s: audio=%s text=%s" % (doc["id"], doc["audio"], doc["text"]))
        log("run.json written, input_sha=%s, docs=%s"
            % (key, ",".join(doc["id"] for doc in selected)))
        log.flush()

        needs_ids = {}
        for item in needs:
            if item.get("id"):
                needs_ids.setdefault(item["id"], []).append(item["kind"])
        doc_payload = [
            {
                "id": doc["id"],
                "audio": common.rel_to(work, doc["audio"]),
                "text": common.rel_to(work, doc["text"]),
                "stem": doc["stem"],
                "existing_pack": doc["existing_pack"],
                "archived_to": doc.get("archived_to"),
                "needs_user": needs_ids.get(doc["id"], []),
                "selected": selected_ids is None or doc["id"] in selected_ids,
                "ignored_texts": list(doc.get("ignored_texts") or []),
            }
            for doc in docs
        ]
        if want_phase == "2":
            for item in doc_payload:
                item["reuse_pack"] = bool(item["existing_pack"])
        result.update(
            {
                "ok": exit_code == common.EXIT_OK,
                "exit_code": exit_code,
                "run": run_dir,
                "work": work,
                "input_sha": key,
                "input_facts": input_facts,
                "resumed": resume,
                "deck_name": deck_name,
                "warnings": warnings,
                "unselected_needs": unselected_needs,
                "moved_aside": moved_aside,
                "docs": doc_payload,
                "materials_ignored": ignored_materials,
                "env": env,
                "env_warnings": problems["warnings"],
                "needs_user": selected_needs,
                "existing_packs": existing_packs,
                "archived": archived,
                "outputs": {
                    "run_json": os.path.join(run_dir, "run.json"),
                },
            }
        )

        if args.json:
            print("RUN=%s" % run_dir)
        else:
            print("RUN=%s" % run_dir)
            print("工作区：%s" % work)
            print("环境：%s" % " · ".join(
                "%s %s" % (name, "ok" if item.get("ok") else "缺")
                for name, item in env.items()
            ))
            if problems["warnings"]:
                for warning in problems["warnings"]:
                    print("WARN %s" % warning)
            print("材料：音频 %d · 文本 %d · 已配对 %d · 未配对/多义 %d"
                  % (len(audios), len(texts), len(docs), len(needs)))
            print("牌组：%s" % (deck_name or "（未指定；E10 需要用 --deck-name 补上）"))
            for doc in doc_payload:
                print("文档：%s %s（%s）%s%s%s"
                      % (doc["id"], doc["stem"],
                         "选中" if doc["selected"] else "未选中",
                         " 已有同名包" if doc["existing_pack"] else "",
                         " 已归档旧包 → %s" % os.path.basename(doc["archived_to"])
                         if doc.get("archived_to") else "",
                         " 待用户：" + "、".join(doc["needs_user"]) if doc["needs_user"] else ""))
                if doc["ignored_texts"]:
                    print("      忽略的额外原文候选（审计，不参与 run key）：%s"
                          % "、".join(doc["ignored_texts"]))
            if ignored_materials:
                print("已折叠 %d 个重复/软链接候选（审计，不参与配对与 run key）：%s"
                      % (len(ignored_materials),
                         "、".join(common.rel_to(work, item["path"])
                                  for item in ignored_materials)))
            for warning in warnings:
                print("WARN %s" % warning)
            for item in unselected_needs:
                print("WARN 未选中待办（不阻断）：%s" % item.get("message"))
            for item in selected_needs:
                print("待办：%s" % item["message"])
            print("已写：%s" % os.path.join(run_dir, "run.json"))
            print("已写：%s" % "、".join(
                os.path.join(common.doc_dir(run_dir, doc["id"]), "doc.json")
                for doc in selected
            ))
            print("结果：%s" % ("OK（可以进 E2）" if exit_code == common.EXIT_OK
                              else "需要用户决定（exit 3，先别继续）"))
        common.emit_json(result)
        return exit_code
    except common.Failure as failure:
        if log:
            log("FAIL " + failure.message)
            log.flush()
        return common.report_failure(STAGE, failure)
    except OSError as exc:
        return common.report_failure(
            STAGE,
            common.Failure(common.EXIT_FAIL, "文件读写失败：%s" % exc, reason="os_error"),
        )
    finally:
        if log:
            try:
                log.flush()
            except OSError:
                pass


def _new_package() -> dict:
    return {
        "scripts_sha256": common.scripts_tree_sha(),
        "locked_at": common.now_iso(),
    }


def _input_touch_warnings(old_run: dict, input_facts: dict) -> list:
    """WARN input_touched for files whose bytes are identical but mtime moved.

    DESIGN §2.1: mtime never changes the run key, so a touched file resumes
    the same run; the only place mtime still matters is this hint.
    """
    old_docs = {
        item.get("doc"): item
        for item in ((old_run.get("input_facts") or {}).get("docs") or [])
        if isinstance(item, dict) and item.get("doc")
    }
    warnings = []
    for current in input_facts.get("docs") or []:
        old = old_docs.get(current.get("doc"))
        if not old:
            continue
        touched = []
        for name in ("audio", "text"):
            old_mtime = old.get("%s_mtime_ns" % name)
            new_mtime = current.get("%s_mtime_ns" % name)
            if old_mtime is not None and new_mtime is not None and old_mtime != new_mtime:
                touched.append(name)
        if touched:
            warnings.append("input_touched: %s 的 %s mtime 变了但内容没变，沿用同一 run"
                            % (current.get("doc"), "/".join(touched)))
    return warnings


def _options(out_base: str, phase: str = "auto", deck_name: str | None = None) -> dict:
    """``run.json.options`` (phase/deck_name only when explicitly chosen).

    The old ``review`` option is gone; a stale ``run.json.options.review``
    left by an older run is simply ignored.  0.1.0 stores the E1 gate's deck
    choice here (and as a flat ``run.json.deck_name``); like ``phase`` it is
    only written when the value is known, so a run without a choice keeps a
    clean options object.
    """
    options = {"out_root": out_base}
    if phase and phase != "auto":
        options["phase"] = phase
    if deck_name:
        options["deck_name"] = deck_name
    return options


def _deck_name(value) -> str | None:
    """Normalise ``--deck-name``: None stays None, blanks are a usage error.

    The name must equal the deck's name in the App byte for byte, so only the
    surrounding whitespace is stripped; an empty value is a usage error rather
    than a silent "clear" (run.json values are updated, never removed).
    """
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        raise common.Failure(
            common.EXIT_USAGE,
            "--deck-name 不能是空字符串；要换牌组请给精确名称（run.json 里的旧值只更新、不清空）。",
        )
    return name


def _old_deck_name(run: dict) -> str | None:
    """The deck name already stored in a run, or None (0.1.0 D3).

    ``options.deck_name`` wins over the flat ``run.deck_name`` (the same
    precedence ``markji_push.resolve_deck_name`` uses), so a run written by
    either spelling resumes with the right value.
    """
    value = (run.get("options") or {}).get("deck_name") or run.get("deck_name")
    return str(value) if value else None


def _preflight(*, work, run_dir, env, docs, selected, needs, materials,
               existing_packs, reason, env_warnings, key, moved_aside=None,
               archived=None, phase="auto", input_facts=None,
               unselected_needs=None, warnings=None) -> dict:
    selected_ids = {doc["id"] for doc in selected}
    payload = {
        "version": 1,
        "generated_at": common.now_iso(),
        "work": work,
        "run": run_dir,
        "reason": reason,
        "input_sha": key,
        "env": env,
        "env_warnings": env_warnings,
        "materials": materials,
        "docs": [
            {
                "id": doc["id"],
                "audio": os.path.abspath(doc["audio"]),
                "text": os.path.abspath(doc["text"]),
                "stem": doc["stem"],
                "audio_sha256": doc["audio_sha256"],
                "text_sha256": doc["text_sha256"],
                "existing_pack": doc["existing_pack"],
                "archived_to": doc.get("archived_to"),
                "selected": doc["id"] in selected_ids,
            }
            for doc in docs
        ],
        "existing_packs": existing_packs,
        "needs_user": needs,
        "unselected_needs": unselected_needs or [],
        "warnings": warnings or [],
        "input_facts": input_facts,
        "moved_aside": moved_aside,
        "archived": archived or [],
    }
    if phase and phase != "auto":
        payload["phase"] = phase
    return payload


if __name__ == "__main__":
    sys.exit(main())
