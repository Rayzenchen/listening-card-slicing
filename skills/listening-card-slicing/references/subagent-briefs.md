# 子 agent 任务单 · 锚点薄索引

> 脚本里的 `brief` 字段引用本文件的锚点（`#segmenter` / `#translator` / `#layouter`）；**实际任务单正文在 `references/roles/`**。
> 用法：按锚点取角色卡，预渲染成任务单文件，由 `workflow` 派发（见 [派发方式](#dispatch) 与 [runtime/fanout.md](runtime/fanout.md)）；回执 schema 已内联在角色卡里。

## 共用红线（每份任务单都写）

1. 只写一个回执文件；不改原文/英文/中文；不跨任务；不派子 agent；不跑脚本；不写云端。
2. 回执是唯一事实；回复只一行 `done <回执绝对路径>` + ≤120 字备注。
3. 只读任务单给的输入；禁止读 `tests/`、fixtures 或任何成品清单/卡片来照抄。
4. 原子/句子/卡片/行范围以任务单 JSON 为准，不自己重新解析。

<a id="dispatch"></a>
## 派发方式（0.0.10）

> 模板（E3/E5/E9 + 复核员）见 [runtime/fanout.md](runtime/fanout.md)；这里只记规则。

### 选路

| 场合 | 用什么 | 理由 |
|---|---|---|
| 任务数 ≥ 8，或 ≥ 3 个短任务（E3 块 / E5 翻译任务 / E9 断行任务），且不需要续聊 | **`workflow`** | 一次调用内排队，脚本可控每波 ≤ 8；失败只重派失败项 |
| 任务数 < 8 **且** 需要 continuable（后面还要 `send_message` 追加指令） | `subagent` | 只有这条路能续聊 |
| 复核员 / 译文复核员 / 校对员 | **`workflow`**（2 份文档 = 2 个并行） | 实测 2 个裸 `subagent` 2/2 `failed before it finished`；改 `workflow` 约 5 分钟完成 |

### 波宽

- `WAVE=8`。本机 2026-11-26 实测 `active child limit = 10`（`workflow` 一次派 16 → **整 run 报错** `subagent limit reached (active child limit: 10)`，0 个回执）；**不要假设全局 AGENTS.md 的 128**（与本机实测不符）。
- 同一时段还有别的手动子 agent 在飞就降到 6；宁少不多。
- 超限会终止整个 run，已启动的子 agent 可能还在写回执：报错后先查回执目录，再补派，不整批重来。

### 派前

1. **预建回执目录**：`mkdir -p "$RUN/docs/<doc>/blocks" "$RUN/docs/<doc>/translate" "$RUN/docs/<doc>/cards"`。
2. **查回执不存在**再派；`HAVE` 的不重派（命令见 [runtime/fanout.md](runtime/fanout.md#prepare) §2.2）。
3. **预渲染任务单**到 `$RUN/docs/<doc>/prompts/<task>.md`（抬头 + 角色卡全文 + 原样 JSON）：`workflow` 脚本没有文件系统，prompt 只能指向文件（fanout §2.3）。

### 失败与重试

- `workflow` 的 `agent()` 失败返回 `null`（不是抛异常）：脚本收集 `failed`，**只重派失败项**（最多 2 轮重试），不整批重来。
- 子 agent 回了 `done` 但回执缺 / 非法：以磁盘为准，非法回执改名 `.bad-<ts>` 后只补派它（算一轮重试）。
- 同一任务**最多重试 2 次**（首次 + 最多 2 次补派）；第 2 次补派仍失败 → 写 `$RUN/docs/<doc>/.failed-<ts>.json`（字段见 fanout §8）并**报告根 agent**，不静默跳过。
- `stopReason: "cancelled"` 是**被中断**，不是子 agent 失败：先查回执目录 + 跑 gate，只补缺的；不要因为 cancelled 整批重派。
- 派完不 sleep、不轮询：`workflow` 是 await 的；返回后先跑 gate，再只补缺的。

<a id="segmenter"></a>
## #segmenter · 裁定员

- 角色卡：[roles/segmenter.md](roles/segmenter.md)（预渲染进任务单，见「派发方式」）。
- 替换：`DOC` / `BLOCK` / `TASK_ID` / `POSITION` / `RECEIPT` / `N` / `ATOMS`（原样 JSON）/ `AGENT_NAME`。
- 输出：`docs/<doc>/blocks/<block>.json`；完成回复 `done <path>`。
- **0.0.10 判据**：先分对话块（M/W 同时出现）/独白块；对话块默认「连续同 speaker 的回合 = 一行」（`turn`，≤44 词），独白块默认「一个原子一行」。允许并五类：`turn` / `pure_response` / `fragment` / `compact_qa` / `dialogue_beat`；`turn` / `dialogue_beat` **仅对话块**，独白块只前三类。细节以角色卡为准。
- 派前查 `receipt` 不存在；一块只派一次；≥ 3 块走 `workflow`（模板 [runtime/fanout.md](runtime/fanout.md#e3)）。

<a id="reviewer"></a>
## #reviewer · 复核员

- 角色卡：[roles/reviewer.md](roles/reviewer.md)。
- 替换：`DOC` / `N` / `SEGMENTS_JSON` / `MANIFEST` / `BLOCKS_JSON` / `RECEIPT`（`review-<n>.json`）/ `AGENT_NAME`。
- 一份文档一个；有 `overrides` 就重跑 `assemble.py --overrides` + `gate e4`；多轮写 `review-2.json`… 不覆盖旧回执。
- **用 `workflow` 派**（一份文档一个 agent，两卷并行；[fanout.md#reviewers](runtime/fanout.md#reviewers)），不要用两个裸 `subagent`。
- **0.0.10 已同步**：输入 `MERGE_CANDIDATES` 就是 `SEGMENTS_JSON` 顶层 `merge_candidates`（不要求单独文件）；回执必须给 `merge_review` 逐条覆盖全部候选——`keep` 写明命中五类中的哪类（`turn` / `dialogue_beat` 仅对话块，独白块只前三类），`split` 给对应整块 `overrides`；`gate e4` 机检覆盖。

<a id="translator"></a>
## #translator · 翻译员

- 角色卡：[roles/translator.md](roles/translator.md)。
- 替换：`DOC` / `TASK_ID` / `N` / `ROWS`（原样 JSON）/ `RECEIPT`（`translate/<task>.json`）/ `AGENT_NAME`。
- 只译 keep 行；`len(zh) == sentence_count`；缺回执只补派该任务。
- ≥ 3 个任务走 `workflow`（[fanout.md#e5](runtime/fanout.md#e5)）。**0.0.10 已同步**：`translate_prepare.py` 默认复用译文记忆，`prefilled` 的行不生成任务，只派 `MISS`；`--no-reuse-memory` 恢复全量重译，重编后旧 `translate/` 回执默认归档为 `translate.prev-<ts>/`。

<a id="translation-reviewer"></a>
## #translation-reviewer · 译文复核员

- 角色卡：[roles/translation-reviewer.md](roles/translation-reviewer.md)。
- 替换：`DOC` / `N` / `TRANSLATIONS_JSON` / `MANIFEST_6COL` / `BLOCKS_JSON` / `RECEIPT`（`translate-review-<n>.json`）/ `AGENT_NAME`。
- 整行 `fixes`；根 agent 跑 `translate_apply.py --review` 后 `gate e5`；有 fixes 的 review 会记进 `applied_reviews`。
- **用 `workflow` 派**（一份文档一个 agent，多卷并行）；不要用裸 `subagent`。

<a id="layouter"></a>
## #layouter · 排版员

- 角色卡：[roles/layouter.md](roles/layouter.md)。
- 替换：`DOC` / `TASK_ID` / `N` / `CARDS`（原样 JSON）/ `RECEIPT`（`cards/<task>.json`）/ `AGENT_NAME`。
- 只加 `- `/`-- ` 与换行；不改字；不写 `M:`/`W:`；缺回执只补派该任务。
- ≥ 3 个任务走 `workflow`（[fanout.md#e9](runtime/fanout.md#e9)）。

<a id="proofreader"></a>
## #proofreader · 校对员

- 角色卡：[roles/proofreader.md](roles/proofreader.md)。
- 替换：`DOC` / `N` / `CARDS_JSON`（全量）/ `TRANSLATIONS_JSON` / `MANIFEST_6COL` / `RECEIPT`（`cards-review-<n>.json`）/ `AGENT_NAME`；`cards-preview.md` 只是前 1–3 张样卡，校对以 `cards.json` 全量为准。
- `fixes` 只收 `en_lines`；中文问题写独立的 **`zh_fixes`**（整张卡中文行数组，`len(zh) == 英文句数`，不写 `current`，一卡最多一条）；有 `zh_fixes` 也可以 `verdict=ok`（只报不改）。
- 根 agent 跑 `cards_apply.py --review`（校验 `zh_fixes` 并渲染 `$RUN/docs/<doc>/zh-fixes.md` 表格）→ 用户逐条敲定 → `zh_fix.py apply` 直改 `translations.json` + 6 列清单 → `approve.py` 重新定稿 → `cards_apply.py --refresh-zh` → `gate e9`。**不重跑 E3/E5/E6/E7/E8**，不重跑 layouter。
- **用 `workflow` 派**（一份文档一个 agent）；不要用裸 `subagent`。

## 派发 / 收口

- **默认 `workflow`**：每波 ≤ 8（`WAVE=8`），`await parallel` 逐波等；模板见 [runtime/fanout.md](runtime/fanout.md#skeleton)。**不要**再「一条消息把全部任务并排发」。
- 名字唯一：`seg-<doc>-<block>` / `review-<doc>` / `tr-<doc>-<task>` / `trev-<doc>` / `lay-<doc>-<task>` / `proof-<doc>`（`workflow` 用 `label`，`subagent` 用 `name`）。
- 派前预建回执目录、查 receipt 不存在、预渲染任务单；派完不 sleep、不轮询，返回后先跑 gate。
- 回执不合法 → 改名 `.bad-<ts>` 只重派它；子 agent 回复不算事实，`gate.py` 算。
- 同一任务最多重试 2 次；第 2 次补派仍失败 → `.failed-<ts>.json` + 报告根 agent，不静默跳过。

