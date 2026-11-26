---
name: markji-channel
description: 打通本地与墨墨记忆卡（Markji）云端牌库的读写通道——把 content 原样写入云端卡片、取回云端卡片正文、上传图片音频、移动卡片、新建章节。当用户要「把卡片写进墨墨」「同步到墨墨记忆卡」「读回牌组内容」「上传卡片配图」，或提到 markji、墨墨记忆卡、记忆卡、牌组、章节、制卡写入时使用。只负责传输，不含任何制卡规则与语法校验；卡片内容长什么样由用户自己的规则决定。遇到 401/403 时按本文「Key 失效」一节引导用户换 Key。
---

# 墨墨记忆卡写入通道

**本 skill 只做一件事：把本地内容送到云端牌库，或把云端内容取回本地。**
不生成卡片、不校验语法、不套模板 —— `content` 写什么就存什么。

用户的制卡规则（语法、拆卡、模板、风格）由用户自己掌握。**不要替用户改写 content。**

---

## 铁律

1. **不修改 content。** 原样提交。不补答案线、不调格式、不修错别字、不重排段落。
   即使用户给的内容看起来"不像合法卡片语法"，也照原样写入 —— 那是用户的规则。
2. **不做语法校验。** 本 skill 没有 lint 功能，也不要临时写一个。
3. **写入前必须 `--dry-run`。** 让用户看到将要提交的请求体，确认后再真正写入。
4. **写入不可逆。** 官方 API **没有删除接口**，卡片/章节只能在 App 或网页端手动删。
   大批量前先写 1 张，让用户去 App 里确认渲染效果。
5. **不建牌组。** 官方 API 没有建牌组接口，牌组必须用户先在 App 建好。
   本 skill 只能建**章节**和在章节里建卡片。
6. **只动自建私有牌组**（`source: SELF`）。收藏牌组写不了。
7. **token 绝不回显。** 不打印完整 Key、不写进日志、不放进回复正文。

---

## 脚本位置

```
scripts/markji_channel.py      # 零依赖，仅用 Python 标准库
```

所有命令都通过它执行：

```bash
python3 <skill>/scripts/markji_channel.py <命令> [参数]
```

加 `--json` 输出结构化结果（agent 解析用）。**给用户看结果时不要加 `--json`**，
默认输出已经是人话。

---

## 标准流程

### 1. 先确认 Key 可用

```bash
python3 scripts/markji_channel.py whoami
```

成功会显示可访问的牌组数和 Key 来源。**如果报 401/403，立刻停下，跳到本文末「Key 失效」。**

### 2. 定位目标

```bash
python3 scripts/markji_channel.py decks                    # 拿 mkjd_ 开头的牌组 ID
python3 scripts/markji_channel.py chapters <deck>          # 拿 mkjch_ 开头的章节 ID
python3 scripts/markji_channel.py cards    <deck> <chap>   # 读回整章正文（含 content）
python3 scripts/markji_channel.py card     <deck> <card>   # 读单张
```

- 牌组名/章节名**不是**稳定 ID，按名字定位时要精确匹配、要求唯一结果；不唯一就停下来问用户。
- `decks` 默认只列前 100 个。总数超过 `--limit` 时用 `--offset` 翻页，不要断言"牌组不存在"。

### 3. 写入

```bash
# 单张：content 从文件读（- 表示 stdin）
python3 scripts/markji_channel.py create <deck> <chap> --file card.txt --dry-run
python3 scripts/markji_channel.py create <deck> <chap> --file card.txt

# 批量：文件内用独占一行的 === 分隔（--sep 可改）
python3 scripts/markji_channel.py create-many <deck> <chap> --file deck.txt --dry-run
python3 scripts/markji_channel.py create-many <deck> <chap> --file deck.txt

# 整体替换已有卡片（不是增量补丁）
python3 scripts/markji_channel.py update <deck> <card> --file card.txt
```

**批量写入前必须让用户确认**：目标牌组、章节、卡片张数。

### 4. 媒体

```bash
python3 scripts/markji_channel.py upload ./pic.png --deck <deck>
```

返回 `file.id`，用户把它写进自己的 content 里（本 skill 不代写语法）。
上传去重：同一文件重复上传返回同一个 ID。

### 5. 整理

```bash
python3 scripts/markji_channel.py mkchapter <deck> "章节名"
python3 scripts/markji_channel.py move <deck> <chap> <card...> --to <目标章节ID>
```

---

## 命令总表

| 命令 | 作用 |
|---|---|
| `key` / `key --set <新Key>` | 查看 / 更换 API Key |
| `whoami` | 验证 Key，显示可访问牌组数 |
| `decks [--limit N] [--offset N]` | 列出牌组 |
| `chapters <deck>` | 列出章节 |
| `cards <deck> <chap>` | 一次取回整章卡片（含正文） |
| `card <deck> <card>` | 读单张卡片 |
| `create <deck> <chap> --file F` | 写入一张 |
| `create-many <deck> <chap> --file F` | 批量写入（`===` 分隔） |
| `update <deck> <card> --file F` | 整体替换一张 |
| `upload <path> [--deck ID]` | 上传图片/音频，返回 file.id |
| `move <deck> <chap> <card...> --to ID` | 移动卡片到别的章节 |
| `mkchapter <deck> "名字"` | 新建章节 |
| `raw <METHOD> <path> [--body JSON]` | 直接打任意端点（逃生舱） |

---

## Key 失效（401 / 403）

脚本遇到 401/403 会**自动打印换 Key 指引**，包含 `.env` 的完整路径。你要做的是：

1. 把脚本输出的那段指引**原样转达给用户**（已写成给非技术用户看的话，不要改写成术语）。
2. 告诉用户**最省事的一条路**：

   > 你把新的 API Key 直接发给我，我替你写进去，你不用碰任何文件。

3. 用户把 Key 发来后，执行：

   ```bash
   python3 scripts/markji_channel.py key --set "<用户给的新Key>"
   ```

   这条命令会**写入 → 设为 600 权限 → 立即联网验证**，一次完成。

**不要**让用户自己去翻目录找 `.env`，除非他明确说想手动改。
**不要**把完整 Key 打印到对话里 —— `key`（不带 `--set`）只显示打码后的值。

新 Key 的获取位置（这是官方唯一入口，免费、不需要开发者资质）：

- **App**：墨墨背单词 →「我的」→ 更多设置 → 实验功能 → 开放 API
- **网页**：<https://open.maimemo.com/open/api/v1/tokens/openapi>（有效期 7 天）

---

## 实测确认的接口行为

这些是在生产环境实测出来的，**和官方文档不一致的地方以实测为准**：

| 项 | 结论 |
|---|---|
| `grammar_version` | **只接受整数 3**。传 1 报 `card_invalid_grammar_version`；不传报必填错误。 |
| 建卡 body | **必须省略 `deck`/`chapter`**。照官方文档传会报 `not a valid ObjectId`。 |
| 建章节 body | **必须传 `deck`**（与建卡相反）。 |
| 移动卡片 body | **必须省略 `deck`/`chapter`**，只传 `card_ids` + `to_chapter_id`。 |
| 批量读正文 | `with_cards=true` **只在单章节端点生效**；牌组级不返回 `cards`。 |
| 上传文件 | `multipart/form-data`，字段名 `file`。**MIME 必须真实**，`application/octet-stream` 被拒。 |
| `order` | 0 基插入索引，不传则追加末尾。 |
| 删除 | **没有删除接口**。 |
| 建牌组 | **没有建牌组接口**。 |
| 可写范围 | 仅自建私有牌组（`source: SELF`）。 |

## 频控

- 10 秒 20 次 / 60 秒 40 次 / 记忆卡 5 小时 8000 次
- 批量写入默认每张间隔 0.6 秒（`--interval` 可调）

官方另有「每天最多创建 600 条内容」一条，原文举例是「例句、助记、释义」——
属背单词侧实体，**是否覆盖记忆卡卡片官方未说明**。超大批量前先小批试。

## 常见错误

| 报错 | 原因 |
|---|---|
| `common_unauthorized` (401) | Key 失效 → 见上节 |
| `not a valid ObjectId` (400) | body 里多传了 `deck`/`chapter` → 删掉 |
| `card_invalid_grammar_version` (400) | `grammar_version` 不是 3 |
| `mime 当前文件 mime 不支持` | 上传时 MIME 不是真实类型 |
| `Resource or Api not found` (404) | 该接口不存在（如试图删除） |
