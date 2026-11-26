# 运行时 · 云端（E10–E12）

> 传输层是 `markji-channel`（`markji_channel.py`）；本 Skill 只通过 `markji_push.py` / `markji_sync.py` 调用它。
> **三条不可逆事实**：没有建牌组接口；没有删除接口；`update` 是整张覆盖。所以只 create / update，写前必须看样卡。
> 不打印 Key / token / 带签名 URL；云端 `files[].url` 一律剔除，只留 `id` / `mime` / `size`。

## 1. 依赖与 Key

```bash
python3 "${MK}" whoami         # E10 第一步：确认 Key 有效
python3 "${MK}" key --set "<新Key>"   # 仅当用户明确给新 Key；写入 600 权限并立即联网验证
```

- 缺脚本 / `whoami` 失败 / 401 / 403 → **停下**，把 markji-channel 的人话指引原样转达；不自建客户端、不绕过。
- 牌组必须用户先在 App 建好；0.1.0 起 E1 选择门把用户选的**精确名称**存进 `run.json.options.deck_name`（+ 平铺 `run.json.deck_name`），E10 默认使用、不再重复问。没有存过值时回退默认 `【CET-4】声音→意思`（`→` U+2192，`【】` 全角）；`--deck-name` 显式覆盖。

## 2. 定位（E10 `--stage plan`，只读）

| 对象 | 规则 | 缺失/不唯一 |
|---|---|---|
| 牌组 | `decks --json` 按**精确名称**唯一匹配 | exit 3 列候选，问用户；**不建牌组** |
| 章节 | `chapters --json` 按**精确名称**唯一匹配 | 0 个 → plan 标 `create`（push 时 `mkchapter`）；≥2 → exit 3 |
| 云端卡 | 章节 `card_ids` 是展示顺序；`cards --json` 的 `cards[]` 是创建顺序 | 标题从 `content` 首行推导 |
| 样卡闸门 | `deck.card_count > 0` → `veteran`（已有卡，自动跳过样卡）；`0`/缺失 → `first`（空/新牌组，先写 1 张样卡） | 写进 `plan.gate`；`first_sample_pending` 例外见 §4 |

- 云端卡片**没有 `title` 字段**：从 `content` 首行 `[T#!!fff895#<title>]` 推导，与本地标题**逐字**比；首行不是这个格式 → 停下问，不猜。
- 同一章节出现重复标题 → 停下列给用户，不自动挑一张。
- `card_ids` 里缺的 id 用单张 `card` 补齐；`cards[]` 的返回序**不能当 order**。

## 3. 三态（plan 的核心安全阀）

对每张本地卡按标题查云端，动作只有三种：

| 动作 | 条件 | 云端效果 | 需要批准 |
|---|---|---|---|
| `create` | 云端没有这个标题 | 新建 | first：样卡 + App 后 `approve2`；veteran：直接 push（免样卡/`approve2`） |
| `skip_identical` | 有同名且归一化后一致 | 不动 | 不需要 |
| `diff_pending` | 有同名但不一致 | **默认不动**，列差异 | 只有 `--allow-update` + `approve2.updates_ok=true` 才 `update`（veteran 用 `--veteran --updates-ok`） |

**归一化口径**（plan 与 verify 共用）：`\r\n`/`\r`→`\n`；去行首 `- `/`-- `；去 `M: `/`W: `；连续空白压成一个半角空格；首尾 strip。

- 归一后相等 → `skip_identical`；不等 → `diff_pending`。**不要**用 `create/update/unchanged` 当动作名。
- 另记 `byte_equal`、`whitespace_only`、`structure_warn`（行数、空行、`---` 数、尾随换行、Audio 数）：只提醒，不改变三态。
- **默认不覆盖已有卡**：第 1 张常是用户手改版；无删除、风险不对称；`update` 是维护动作，不是默认动作。

## 4. `markji-plan.json` 与决策 sha

plan 至少含：

```json
{
  "version": 1, "doc": "<doc>", "stem": "<stem>",
  "deck": {"id": "…", "name": "…", "card_count": 12},
  "chapter": {"id": "…", "name": "…", "action": "use|create"},
  "gate": {"mode": "first|veteran", "sample_required": true,
           "marker": "empty_or_missing|deck_card_count>0|first_sample_pending"},
  "cards": [{"no": "<NNN>", "title": "…", "action": "create|skip_identical|diff_pending",
             "card_id": "…", "audio": "<NNN>.wav", "file_id": "…", "diff_summary": "…",
             "byte_equal": false}],
  "counts": {"cards": 0, "create": 0, "skip_identical": 0, "diff_pending": 0},
  "cards_sha256": "<卡片集 sha>",
  "decision_sha256": "<决策 sha>",
  "plan_sha256": "<整文件 sha>",
  "policy": {}, "warnings": []
}
```

- `cards_sha256` = `[[no, content_sha256], …]` 按 order 的 sha（`cards_apply.cards_sha256`）。
- `decision_sha256` 只覆盖 `doc/stem/deck/chapter/cards/counts` 六个键，**排除** `generated_at/policy/warnings/version/plan_sha256/decision_sha256/cards_sha256`。`gate` 不参与 decision sha：同一批卡片重跑 plan 不会因闸门模式变化而让旧批准意外失效。
- `plan_sha256` 是整文件 sha（排除自身），与 decision sha 相互独立。
- **plan 只读**：plan 阶段不写云端；`diff_pending` 非空时先在回复里列出，等用户决定。
- **闸门模式**：`gate.mode` 由 `deck.card_count` 决定——`>0` = `veteran`（跳过样卡），`0`/缺失 = `first`（先写 1 张样卡）。`first_sample_pending` 的例外：first 样卡已写、但还没有成功 push 时，重跑 plan 仍标 `first`，避免样卡本身把牌组变成 veteran；成功 push 后才恢复 veteran。

## 5. 批准（E10 `approve2.py`）

```bash
# first：用户在 App 看过样卡之后
python3 "${S}/approve2.py" --run "${RUN}" --doc <doc> --mode user \
  --sample-no <NNN> --chapter-ok [--updates-ok] --json
# veteran：只有覆盖 diff_pending 才需要（--sample-no/--chapter-ok 可省略）
python3 "${S}/approve2.py" --run "${RUN}" --doc <doc> --veteran [--updates-ok] --json
```

`approve2.json` 字段：`version/doc/mode=user/sample_mode=first|veteran/plan_sha256/decision_sha256/cards_sha256/sample_no/chapter_ok/updates_ok/approved_at/by=user`。

- **first**：`--sample-no` **必填**（3 位补零，且必须出现在 `plan.cards[].no` 里），`--chapter-ok` **必填**；样卡回执必须存在、`mode=first`、`result=ok`、同一 plan/cards、且回执 `no` 等于 `--sample-no`。`--veteran` 在 first 计划上会被拒（`gate_mismatch`）。
- **veteran**：不加 `--veteran` 也可以什么都不写（push 不需要令牌）；`--veteran --updates-ok` 只表示用户明确同意覆盖 `diff_pending` 的旧卡，`--sample-no`/`--chapter-ok` 可省略。
- `--updates-ok`：只在 `counts.diff_pending > 0` 且用户明确同意覆盖时加；`diff_pending == 0` 时传它会被拒（`updates_not_applicable`）。
- plan 重算过 → `decision_sha` / `plan_sha` 变 → **旧批准作废**，重新 plan 再批准。
- 不要手算 sha、不要手写 `approve2.json`；只调脚本。

## 6. 样卡（E10 `--stage sample`）

- **first**：**不要求** `approve2.json`（App 确认在样卡之后）。`sample` 选一张 `create` 卡（默认第一张；`--no <NNN>` 指定），`upload` wav（同文件幂等）→ `create` → 读回 `content` / `files[]` → 比对；输出只报卡号/标题/卡 ID，**不打印卡片正文**。读回不一致 → exit 1，**不要 push**。
- **veteran**：**不写云端**，只写 `receipts/markji-sample.json = {"mode":"veteran","skipped":true,"result":"ok",...}`；`--no` 被忽略。`gate e10` 不要求这份回执，跑不跑都行。
- 回执 `receipts/markji-sample.json`（first）至少含：`mode=first` / `no` / `title` / `action` / `plan_sha256` / `decision_sha256` / `cards_sha256` / `content_sha256` / `readback_sha256` / `result` / `at`（另有 `card_id` / `file_id` / `chapter_id` / `chapter_created` / `byte_equal`）。
  - `content_sha256` = **cards.json 的期望内容** sha（不是云端读回）；`readback_sha256` = 云端读回正文的 sha；`cards_sha256` 必须等于当前卡片集，`result=ok` 才算样卡通过。
- 用户说「不行」→ 改本地 → 重新 plan → 重新样卡；**不要跳过样卡直接批量**（first）。
- **`first_sample_pending`**：first 样卡写好但还没 push 成功时，重跑 plan 保持 first；此时只补 `approve2`，**不要重写样卡**；成功 push 后才转 veteran。

**请用户在 App 里确认四件事**：正反面音频都能播且是同一段；中英文一句一行；语气自然、无代词括号/人名替换问题；标题里的 `[I]` 没被吃掉。

## 7. 批量写入（E11 `--stage push`）

```bash
python3 "${S}/markji_push.py" --run "${RUN}" --doc <doc> --stage push \
  --interval 0.6 --json > "${RUN}/logs/push-<doc>.out" 2>&1
python3 "${S}/gate.py" e11 --run "${RUN}" --doc <doc>
```

- 前置按 `plan.gate.mode`：**first** 必须有 `approve2.json` **且** first 样卡回执（缺 → exit 3）；**veteran** 两者都不需要（有 `approve2.json` 时只读 `updates_ok`）。
- 开始前**重读云端**（防用户手改/手删）：计划里的 `create` 若现在已存在 → 降级为 `skip_identical` / `diff_pending`；计划变了 → 重新 plan + 重新批准。
- 逐张：`upload` wav（幂等）→ 只做 `create` / `skip_identical`；`diff_pending` 跳过并记 `skipped[]`。
- **只有** `--allow-update` **且** `approve2.updates_ok=true`，才对 plan 里 `action=diff_pending` 的卡整张 `update`（不新建、不删除）；veteran 的令牌用 `approve2.py --veteran --updates-ok`。运行中新出现的 diff 跳过并 WARN。
- 写入后按 `order` 重排：`card_ids[i] != want[i]` 就对 `want[i]` 调 `move --order i`，最多两轮；仍不一致 → 停、记回执、问用户。
- 回执 `receipts/markji-push.json` **成功或失败都写**；失败也保住已写 `card_id`，记 `result=fail` + 已完成 `cards[]`。`markji.json.sample` 对 veteran 记 `{"mode":"veteran","skipped":true}`，对 first 记样卡摘要。

**频控**：10 秒 20 次 / 60 秒 40 次；`--interval` 默认 0.6 s；遇 `common_too_many_request` 等 **10 秒**重试（最多 6 次）；不缩短间隔、不改并发。

**`--limit N`**：只处理/核对前 N 张，回执记 `partial=true`、`order_verified=false`（不重排），并带一条含 `partial` 的 WARN；后续全量 push 再验证顺序。

## 8. 回读与核对（E11 `verify` / `gate e11`）

| # | 项 | 判据 |
|---|---|---|
| 1 | 数量 | 章节卡数 == K（`--limit` 时为 N） |
| 2 | 标题集合 | 本地 K 个标题 == 云端标题集合，无缺无多 |
| 3 | 标题唯一 | 云端无重复标题 |
| 4 | 顺序 | `card_ids` == `cards.json` 的 `order` 序列 |
| 5 | 内容 | 每张归一化后 == 本地模板（id 替换后）；或 plan 动作 `diff_pending` 且 `updates_ok=false` → WARN |
| 6 | 音频 | 每张 `files[]` 恰好 1 个；两处 `[Audio#…]` 同 id；替换后 `mkjf_` 计数 == 2 |
| 7 | 无重复 | 同一标题/同一音频不出现两次 |
| 8 | 手改保护 | 用户手改卡保持用户版本，除非用户明确要求改 |

- `gate e11` 还查 `markji.json` / push 回执的 `cards_sha256` 与当前卡片集一致；失败回执里的已写卡必须有 `card_id`。
- `gate e11` 按 `markji.sample_mode` / `plan.gate.mode` 分流：first 必须能对上 first 样卡 + `approve2`（`approve_no` 非空）；veteran 跳过 `push_sample` 检查，没有 `approve2` 时 `approve_no` 为空也不 FAIL（`gate_veteran`）。
- `partial=true` 时 `markji_order_verified` 走 WARN 分支，不 FAIL；不能把 `order_verified` 写成 true 假装验证过。

## 9. 后续同步（E12 `markji_sync.py`）

```bash
python3 "${S}/markji_sync.py" --run "${RUN}" --doc <doc> --check  --json
python3 "${S}/markji_sync.py" --run "${RUN}" --doc <doc> --update --json
python3 "${S}/gate.py" e12 --run "${RUN}" --doc <doc>
```

- 读云端（按 `card_ids` 顺序）与本地 `cards.json`；`cards.json` 不在时从「6 列清单 + `translations.json`」重建（断行信息丢了就只比中文/标题/音频，**断行差异不算差异**）。
- 比较：标题集合、数量、顺序、中文行、英文（去前缀压空白）、音频是否挂载。
- `--check`：只报告，写 `receipts/markji-sync.json`（`mode=check`、`diffs[]`）；**有差异 exit 1**（不是崩了）。
- `--update`：修订专用入口。先把 `diff_pending` 差异清单给用户看，确认后只对差异卡整张 `update`（不 create、不删除），完成后重读；`diffs` 必须为空才算完。
- `--update` 需要 `approve2.updates_ok == true`，且 `approve2.cards_sha256 == 当前 cards_sha256`；不等 → exit 3（`cards_changed`，重新 plan + approve2）。veteran 的授权用 `approve2.py --veteran --updates-ok`。
- 本地改过卡片（E9 `zh_fix.py` 中文直改 / 排版断行 / E5 重译）→ `cards_sha256` 变了：先重新 `plan`，再批准（first 重写样卡；veteran 只需 `--updates-ok` 授权），最后才 `--update`。
- 回执 check/update 都写 `cards_sha256`。

## 10. 退出码与失败速查

| 码 | 含义 | 典型动作 |
|---|---|---|
| 0 | 成功 / 无差异 | 过关卡 |
| 1 | 读回不一致 / 有差异 | 看回执 `diffs[]`，不硬写 |
| 2 | 依赖/Key/网络/用法 | 停下转达；换 Key 按 §1 |
| 3 | 要用户决定（牌组/章节不唯一、缺批准、plan 变） | 问用户 / 重新 plan + approve2 |

- `common_too_many_request` → 等 10 秒重试（≤6 次）；仍失败用 `--limit` 缩小，别并发。
- `gate_mismatch`（first 计划加了 `--veteran`）→ 不能跳过首个新牌组的样卡：按 first 流程先 `--stage sample`，用户 App 看过后再 `approve2 --sample-no … --chapter-ok`。
- `sample_mismatch` / `sample_stale`（样卡回执的 no/plan/cards 对不上）→ 重跑 `plan`；若 `gate.marker=first_sample_pending` 只补 `approve2`，否则重做样卡；不要写假回执。
- 404（试图删除）→ 没有删除接口；只能请用户在 App 手删。
- 任何云端写错：能 `update` 就 update 整张覆盖；不能改的（多建/重复）只能请用户手删，**不要调用删除接口**。

