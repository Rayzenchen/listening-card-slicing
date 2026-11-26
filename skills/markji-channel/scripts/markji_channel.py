#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""墨墨记忆卡 · 写入通道

只做一件事：把 content 原样送进云端牌库，或把云端内容原样取回。
不做语法校验、不做模板、不做制卡规则 —— 卡片长什么样完全由你决定。

    python3 markji_channel.py <命令> [参数]

凭证（API Key）查找顺序：
    1. 环境变量 MAIMEMO_TOKEN
    2. 本 skill 目录下的 .env        <- 换 Key 就改这里
    3. 当前目录逐级向上找 .env       <- 兼容旧工作区
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HOST = "https://open.maimemo.com"
API = HOST + "/open/api/v1/markji"

# 实测：服务端只接受 3。传 1 会返回 card_invalid_grammar_version
GRAMMAR_VERSION = 3

# 频控 10 秒 20 次 / 60 秒 40 次，写请求之间留间隔
WRITE_INTERVAL = 0.6

# 服务端拒绝 application/octet-stream，必须给真实 MIME
MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".msk1": "markji/mask",
}

KEY_VARS = ("MAIMEMO_TOKEN", "MARKJI_TOKEN")

_SELF_DIR = os.path.dirname(os.path.realpath(__file__))
# 装在 skill 里时脚本位于 <skill>/scripts/，Key 文件放在 skill 根目录；
# 平铺存放时 Key 文件就在脚本旁边。
SKILL_DIR = os.path.dirname(_SELF_DIR) if os.path.basename(_SELF_DIR) == "scripts" else _SELF_DIR
KEY_FILE = os.path.join(SKILL_DIR, ".env")

# 换 Key 时给用户的指引：只留最短路径，不让人翻目录
_KEY_HELP = """
{reason}

换 Key 有两种方式，任选其一：

  【最快】把新 Key 直接发给我（agent），我替你写进去 —— 你不用碰任何文件。
  【手动】打开下面这个文件，替换 MAIMEMO_TOKEN= 那一行：
      {key_file}

新 Key 从哪来（墨墨官方，免费，不需要开发者资质）：
  · App：墨墨背单词 →「我的」→ 更多设置 → 实验功能 → 开放 API
  · 网页：{token_url}
""".strip()

TOKEN_URL = "https://open.maimemo.com/open/api/v1/tokens/openapi"

TOKEN_EXPIRED = _KEY_HELP.format(
    reason="你的 API Key 已失效（过期或被重置），墨墨服务器拒绝了这次请求。",
    key_file="{key_file}", token_url=TOKEN_URL)

TOKEN_MISSING = _KEY_HELP.format(
    reason="还没配置 API Key，无法访问墨墨账号。",
    key_file="{key_file}", token_url=TOKEN_URL)


class ApiError(Exception):
    def __init__(self, status, message, payload=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload or {}


def die(msg, code=1):
    print("✗ " + str(msg), file=sys.stderr)
    sys.exit(code)


def read_env_file(path):
    """从 .env 读 Key；文件不存在或读不到就返回 None"""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in KEY_VARS:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    except OSError:
        pass
    return None


def find_token():
    """返回 (key, 来源说明)。找不到返回 (None, None)"""
    t = (os.environ.get("MAIMEMO_TOKEN") or "").strip()
    if t:
        return t, "环境变量 MAIMEMO_TOKEN"

    if os.path.isfile(KEY_FILE):
        v = read_env_file(KEY_FILE)
        if v:
            return v, KEY_FILE

    seen = set()
    d = os.path.abspath(os.getcwd())
    while d not in seen:
        seen.add(d)
        env = os.path.join(d, ".env")
        if os.path.isfile(env) and os.path.abspath(env) != os.path.abspath(KEY_FILE):
            v = read_env_file(env)
            if v:
                return v, env
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None, None


_TOKEN = None
_TOKEN_SOURCE = None


def token():
    global _TOKEN, _TOKEN_SOURCE
    if _TOKEN is None:
        _TOKEN, _TOKEN_SOURCE = find_token()
        if not _TOKEN:
            die(TOKEN_MISSING.format(key_file=KEY_FILE))
    return _TOKEN


def save_token(value):
    """写入 skill 目录的 .env（600 权限），替换已有 Key"""
    lines = []
    if os.path.isfile(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as fh:
            for line in fh:
                if line.split("=", 1)[0].strip() in KEY_VARS:
                    continue
                lines.append(line.rstrip("\n"))
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("MAIMEMO_TOKEN=" + value)
    tmp = KEY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEY_FILE)


def call(method, path, body=None, query=None, timeout=60):
    if path.startswith("http"):
        url = path
    elif path.startswith("/open/"):
        url = HOST + path
    else:
        url = API + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        if e.code in (401, 403):
            raise ApiError(e.code, TOKEN_EXPIRED.format(key_file=KEY_FILE), payload)
        errs = payload.get("errors") or []
        msg = "; ".join(str(x.get("message") or x.get("code") or "") for x in errs) or raw[:200]
        raise ApiError(e.code, msg, payload)
    except urllib.error.URLError as e:
        raise ApiError(0, "网络错误: %s" % e.reason, {})


def data_of(payload):
    return payload.get("data", payload)


def emit(args, obj, text):
    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text)


def read_content(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def split_cards(text, sep):
    """按独占一行的分隔符切分；分隔符本身由你决定（默认 ===）"""
    out, buf = [], []
    for line in text.split("\n"):
        if line.strip() == sep:
            chunk = "\n".join(buf).strip("\n")
            if chunk.strip():
                out.append(chunk)
            buf = []
        else:
            buf.append(line)
    chunk = "\n".join(buf).strip("\n")
    if chunk.strip():
        out.append(chunk)
    return out


# ---------------------------------------------------------------- 凭证

def cmd_key(a):
    """查看 / 更换 API Key"""
    if a.set:
        save_token(a.set.strip())
        print("✓ 新 Key 已写入：%s" % KEY_FILE)
        print("  文件权限已设为 600（仅本人可读）")
        _, d = call("GET", "/decks", query={"limit": 1})
        print("  验证通过，可访问 %s 个牌组" % data_of(d).get("total"))
        return

    cur, src = find_token()
    if not cur:
        print("状态：未配置\n")
        print(TOKEN_MISSING.format(key_file=KEY_FILE))
        return
    masked = cur[:6] + "…" + cur[-4:] if len(cur) > 12 else "…"
    print("状态：已配置")
    print("  来源：%s" % src)
    print("  Key ：%s" % masked)
    print("\n要更换，把新 Key 发给 agent，或手动编辑：\n  %s" % KEY_FILE)


# ---------------------------------------------------------------- 读

def cmd_whoami(a):
    _, d = call("GET", "/decks", query={"limit": 1})
    dd = data_of(d)
    decks = dd.get("decks") or []
    creator = decks[0].get("creator") if decks else None
    emit(a, {"total_decks": dd.get("total"), "creator": creator, "key_source": _TOKEN_SOURCE},
         "✓ 凭证有效\n  可访问牌组: %s\n  账号: %s\n  Key 来源: %s" % (
             dd.get("total"), creator or "—", _TOKEN_SOURCE or "—"))


def cmd_decks(a):
    _, d = call("GET", "/decks", query={"limit": a.limit, "offset": a.offset, "folder_id": a.folder})
    dd = data_of(d)
    decks = dd.get("decks") or []
    lines = ["共 %s 个牌组" % dd.get("total", "?")]
    for x in decks:
        lines.append("  %s\n    %s  卡片 %s  章节 %s" % (
            x["id"], x["name"], x.get("card_count"), x.get("chapter_count")))
    emit(a, {"total": dd.get("total"), "decks": decks}, "\n".join(lines))


def cmd_chapters(a):
    _, d = call("GET", "/decks/%s/chapters" % a.deck)
    chs = data_of(d).get("chapters") or []
    lines = []
    for c in chs:
        lines.append("  %s\n    %s  卡片 %s" % (c["id"], c["name"], len(c.get("card_ids") or [])))
    emit(a, {"chapters": chs}, "\n".join(lines) or "（无章节）")


def cmd_cards(a):
    # with_cards 只在「单章节」端点生效，可一次取回整章正文
    _, d = call("GET", "/decks/%s/chapters/%s" % (a.deck, a.chapter),
                query={"with_cards": "true"})
    dd = data_of(d)
    cards = dd.get("cards") or []
    lines = ["章节 %s  共 %d 张" % (dd.get("chapter", {}).get("name", "?"), len(cards))]
    for c in cards:
        lines.append("  %s  gv=%s rev=%s\n    %s" % (
            c["id"], c.get("grammar_version"), c.get("revision"),
            c.get("content", "").replace("\n", "\\n")[:100]))
    emit(a, {"chapter": dd.get("chapter"), "cards": cards}, "\n".join(lines))


def cmd_card(a):
    _, d = call("GET", "/decks/%s/cards/%s" % (a.deck, a.card))
    card = data_of(d).get("card", {})
    emit(a, card, "id=%s\nroot_id=%s\ngv=%s rev=%s\n--- content ---\n%s" % (
        card.get("id"), card.get("root_id"), card.get("grammar_version"),
        card.get("revision"), card.get("content", "")))


# ---------------------------------------------------------------- 写

def _create_body(content, order=None):
    body = {"card": {"content": content, "grammar_version": GRAMMAR_VERSION}}
    if order is not None:
        body["order"] = order
    return body


def cmd_create(a):
    content = read_content(a.file)
    path = "/decks/%s/chapters/%s/cards" % (a.deck, a.chapter)
    body = _create_body(content, a.order)
    if a.dry_run:
        print("POST " + API + path)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    _, d = call("POST", path, body)
    card = data_of(d)["card"]
    emit(a, card, "✓ 已写入\n  card=%s\n  root_id=%s  rev=%s  gv=%s" % (
        card["id"], card["root_id"], card["revision"], card["grammar_version"]))


def cmd_create_many(a):
    cards = split_cards(read_content(a.file), a.sep)
    if not cards:
        die("没有解析出任何卡片（分隔符: %r）" % a.sep)
    path = "/decks/%s/chapters/%s/cards" % (a.deck, a.chapter)
    if a.dry_run:
        print("将写入 %d 张 -> POST %s" % (len(cards), API + path))
        for i, c in enumerate(cards, 1):
            print("  #%d  %s" % (i, c.replace("\n", "\\n")[:80]))
        return
    ok, fail = [], []
    for i, c in enumerate(cards, 1):
        try:
            _, d = call("POST", path, _create_body(c))
            card = data_of(d)["card"]
            ok.append(card["id"])
            print("  [%d/%d] ✓ %s" % (i, len(cards), card["id"]))
        except ApiError as e:
            fail.append({"index": i, "error": e.message})
            print("  [%d/%d] ✗ %s" % (i, len(cards), e.message), file=sys.stderr)
            if e.status in (401, 403):
                break
        if i < len(cards):
            time.sleep(a.interval)
    emit(a, {"created": ok, "failed": fail},
         "\n完成：成功 %d / 失败 %d" % (len(ok), len(fail)))


def cmd_update(a):
    content = read_content(a.file)
    path = "/decks/%s/cards/%s" % (a.deck, a.card)
    body = _create_body(content)
    if a.dry_run:
        print("POST " + API + path)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    _, d = call("POST", path, body)
    card = data_of(d)["card"]
    emit(a, card, "✓ 已更新\n  card=%s  rev=%s" % (card["id"], card["revision"]))


def guess_mime(path):
    return MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def cmd_upload(a):
    path = os.path.abspath(a.path)
    if not os.path.isfile(path):
        die("文件不存在: " + path)
    mime = guess_mime(path)
    if mime == "application/octet-stream":
        die("无法从扩展名推断 MIME（%s）。服务端拒绝 octet-stream，"
            "请改用 png/jpg/mp3/m4a/wav/msk1 等已知类型。" % os.path.basename(path))
    boundary = "----markji" + uuid.uuid4().hex
    with open(path, "rb") as fh:
        blob = fh.read()
    parts = []
    if a.deck:
        parts.append(('--%s\r\nContent-Disposition: form-data; name="deck_id"\r\n\r\n%s\r\n'
                      % (boundary, a.deck)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                  'Content-Type: %s\r\n\r\n'
                  % (boundary, os.path.basename(path), mime)).encode("utf-8"))
    parts.append(blob)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    req = urllib.request.Request(API + "/files", data=b"".join(parts), method="POST")
    req.add_header("Authorization", "Bearer " + token())
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if e.code in (401, 403):
            die(TOKEN_EXPIRED.format(key_file=KEY_FILE))
        die("上传失败 HTTP %s: %s" % (e.code, raw[:300]))
    f = data_of(payload)["file"]
    emit(a, f, "✓ 已上传\n  file.id = %s\n  mime=%s  size=%sKB" % (
        f["id"], f.get("mime"), f.get("size")))


def cmd_move(a):
    path = "/decks/%s/chapters/%s/cards/move" % (a.deck, a.chapter)
    body = {"card_ids": a.cards, "to_chapter_id": a.to}
    if a.order is not None:
        body["order"] = a.order
    if a.dry_run:
        print("POST " + API + path)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    _, d = call("POST", path, body)
    dd = data_of(d)
    emit(a, dd, "✓ 已移动\n  原章节剩 %d 张，目标章节 %d 张" % (
        len(dd["old_chapter"].get("card_ids") or []),
        len(dd["new_chapter"].get("card_ids") or [])))


def cmd_mkchapter(a):
    # 注意：CreateChapter 的 body 需要 deck（与 CreateCard 相反）
    path = "/decks/%s/chapters" % a.deck
    body = {"deck": a.deck, "name": a.name}
    if a.order is not None:
        body["order"] = a.order
    if a.dry_run:
        print("POST " + API + path)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    _, d = call("POST", path, body)
    ch = data_of(d)["chapter"]
    emit(a, ch, "✓ 章节已创建\n  %s\n  %s" % (ch["id"], ch["name"]))


def cmd_raw(a):
    body = json.loads(a.body) if a.body else None
    query = {}
    for kv in (a.query or []):
        if "=" in kv:
            k, v = kv.split("=", 1)
            query[k] = v
    if not a.path.startswith("/"):
        die("path 必须以 / 开头，例如 /decks")
    status, payload = call(a.method.upper(), a.path, body, query or None)
    emit(a, payload, "HTTP %s\n%s" % (status, json.dumps(payload, ensure_ascii=False, indent=2)))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="以 JSON 输出（便于 agent 消费）")

    p = argparse.ArgumentParser(prog="markji_channel.py",
                                description="墨墨记忆卡写入通道（只管传输，不管制卡规则）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("key", parents=[common], help="查看 / 更换 API Key")
    s.add_argument("--set", default=None, metavar="NEW_KEY", help="写入新的 API Key")
    s.set_defaults(fn=cmd_key)

    sub.add_parser("whoami", parents=[common], help="验证凭证").set_defaults(fn=cmd_whoami)

    s = sub.add_parser("decks", parents=[common], help="列出牌组")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--folder", default=None)
    s.set_defaults(fn=cmd_decks)

    s = sub.add_parser("chapters", parents=[common], help="列出章节")
    s.add_argument("deck")
    s.set_defaults(fn=cmd_chapters)

    s = sub.add_parser("cards", parents=[common], help="一次取回整章卡片（含正文）")
    s.add_argument("deck")
    s.add_argument("chapter")
    s.set_defaults(fn=cmd_cards)

    s = sub.add_parser("card", parents=[common], help="读单张卡片")
    s.add_argument("deck")
    s.add_argument("card")
    s.set_defaults(fn=cmd_card)

    s = sub.add_parser("create", parents=[common], help="写入一张卡片（content 原样提交）")
    s.add_argument("deck")
    s.add_argument("chapter")
    s.add_argument("--file", required=True, help="content 文件；- 表示 stdin")
    s.add_argument("--order", type=int, default=None, help="插入位置，0 基；缺省追加到末尾")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_create)

    s = sub.add_parser("create-many", parents=[common], help="批量写入（文件内用分隔符切分）")
    s.add_argument("deck")
    s.add_argument("chapter")
    s.add_argument("--file", required=True)
    s.add_argument("--sep", default="===", help="卡片分隔符，独占一行（默认 ===）")
    s.add_argument("--interval", type=float, default=WRITE_INTERVAL, help="写请求间隔秒数")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_create_many)

    s = sub.add_parser("update", parents=[common], help="整体替换一张卡片")
    s.add_argument("deck")
    s.add_argument("card")
    s.add_argument("--file", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("upload", parents=[common], help="上传图片/音频，返回 file.id")
    s.add_argument("path")
    s.add_argument("--deck", default=None, help="可选：把文件归属到某个牌组")
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("move", parents=[common], help="把卡片移到另一个章节")
    s.add_argument("deck")
    s.add_argument("chapter")
    s.add_argument("cards", nargs="+")
    s.add_argument("--to", required=True, help="目标章节 ID")
    s.add_argument("--order", type=int, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_move)

    s = sub.add_parser("mkchapter", parents=[common], help="新建章节")
    s.add_argument("deck")
    s.add_argument("name")
    s.add_argument("--order", type=int, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_mkchapter)

    s = sub.add_parser("raw", parents=[common], help="直接打任意端点（逃生舱）")
    s.add_argument("method")
    s.add_argument("path")
    s.add_argument("--body", default=None, help="JSON 字符串")
    s.add_argument("--query", action="append", help="k=v，可重复")
    s.set_defaults(fn=cmd_raw)

    a = p.parse_args()
    try:
        a.fn(a)
    except ApiError as e:
        if e.status in (401, 403):
            die(e.message, 2)
        die("HTTP %s  %s" % (e.status, e.message), 2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
