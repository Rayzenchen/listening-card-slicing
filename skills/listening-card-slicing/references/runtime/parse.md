# 运行时 · E2 解析（contract v1 / 字段 / 覆盖）

> 0.0.7：E2 **只认 contract v1**——没有 profile 探测、没有旧格式猜测、没有兼容分支。
> 原文语法、正反例、迁移见 [transcript-contract.md](transcript-contract.md)；命令与关卡见 [pipeline.md](pipeline.md) §2/§6；清单/包字节见 [format.md](format.md)。
> **原文是唯一文本真源**：解析只删不发音的排版标签、分组、补句末句号；不纠错、不改写、不润色。

## 1. 输入与失败口径

- 输入只有一种：contract v1 原文（`## [开场]` / `## Section X` / `### Directions` / `### 篇目 [Qx-y]` / `[META] Questions …` / `Question N. …` / `## [结尾]`）。
- 旧格式（`# Part II…`、`## 试音部分`、裸结尾句、裸/编号型 `Questions`、无 `[Qx-y]` 的篇目）**直接拒绝**：`parse_source.py` exit 1，错误行形如

```text
ERROR: 原文解析失败（第 12 行）：篇目标题 `News Report 1` 缺少 `[Qx-y]`：应写成 `### News Report 1 [Q1-2]`
```

- stdout 末行是 `JSON {"ok":false,"exit_code":1,"stage":"e2","error":"…"}`；stderr 用 `ERROR:` 前缀（不叫 `FAIL`）。
- 迁移提示会点名对应的新标签（`## [开场]` / `## [结尾]` / `[Qx-y]` / `[META]`）；**不静默兼容、不猜排版、不手改原文**。
- 开场 / 结尾允许整块缺失：只记 `WARN missing_title` / `WARN missing_ending`，E2 仍 PASS。

## 2. `blocks.json` 顶层字段

```json
{
  "version": 1,
  "doc": "<doc>",
  "source": "<原文绝对路径>",
  "source_sha256": "<原文 sha256>",
  "format": "contract-v1",
  "warnings": ["…（missing_title）…"],
  "atoms_total": 0,
  "title": {"text": "…"},
  "ending": {"text": ""},
  "blocks": [],
  "atoms": [],
  "source_spans": []
}
```

- 0.0.7 删掉了 `profile` 与 `normalization`（`markdown_escapes` / `escaped_chars`），新增 `format: "contract-v1"`：格式不合格是 **FAIL**，不是计数后继续。
- 顶层 `title` / `ending` 只是审计汇总（`{"text": 原子拼接}`，缺块时 `""`）；真正结构在 `blocks[]` 里 `kind=title` / `kind=ending` 的块，关卡不依赖汇总字段。
- `source` / `source_sha256` 必须绑定当前原文；sha 变了回 E1 拿新 run（旧 run 改名 `.prev-<ts>`），**不改旧 `blocks.json`**。
- `atoms_total == len(atoms)`；`warnings` 是**中文提示句**，只有「缺开场」「缺结尾」两种（句子里含 `missing_title` / `missing_ending`），是提示不是失败。

## 3. 块（blocks，7 类；8 类标签行）

| kind | 清单类型 | 带原子 | 文本规则 | 行号 |
|---|---|---|---|---|
| `title` | 开场 | 必带 | 原子按单空格拼接；0 或 1 个，重复 FAIL，空块 FAIL | `## [开场]` |
| `marker` | 报幕 | 不带 | Section：`Section X.`；篇目：`篇目.`（**不含 `[Qx-y]`**） | 标签行 |
| `directions` | 指令 | 必带 | `Directions: …`；原子首句是 `Directions.` 时去重，不拼成 `Directions: Directions.` | `### Directions` |
| `body` | 正文 | 必带 | 无独立 text；带 `q_range`（`Q1-2` / `Q16`） | 篇目标题行（与 `marker` 同行）+ 原子 span |
| `questions_meta` | 指令 | 不带 | 剥掉 `[META] ` 后原样；带 `q_range`，必须与紧邻的前一个正文块一致 | `[META] …` |
| `question` | 题目 | 不带 | `Question N. …` 原句；带 `q_range=QN`；题号严格连续 +1（可从 N 起） | `Question …` |
| `ending` | 结尾 | 必带 | 原子按单空格拼接；0 或 1 个，必须在最后，空块 FAIL | `## [结尾]` |

- 块 id 连续 `b01..bNN`，按原文顺序排列；0.0.7 删除 `test_intro`，不再有第二条「指令」入口。
- `title` / `ending` 各 0/1 个：0 时只记 WARN，**不伪造**标题行或结尾行。
- `## Section X` 可缺：默认节 = 首个显式节的前一节（B→A、C→B），全缺 = A；默认节只用于 `position` / 显示，**不伪造 `Section A` 报幕行**。
- `marker` 不带 `q_range`：篇目范围只存在 `body` / `questions_meta` 上；报幕文本不含 `[Qx-y]`。
- `title` / `ending` 块的形状：`{id, kind, label: null, section: null, position: "开场"|"录音结尾", atoms: [原子 id…], line: N, text}`；`text` = 原子单空格拼接。
- 报幕 / 指令 / 题目 / 开场 / 结尾是**确定性片段**，由解析器定型，不派人判断；`body` 一个块 = E3 的一个裁定任务。

## 4. 原子（atoms）

- 一个原子 = **一行里的唯一编号片段**：去掉行首 `N、` 与 `M:`/`W:`，其余字符原样保留；`speaker` = `M` / `W` / `null`。
- 原子 id 从 1 连续到 `atoms_total`；**同一个块内** `speaker` 继承到下一个说话人出现为止（`M` 说三句不必回写三次 `M:`）；换块清空。
- **一行只允许一个编号**：同行两个编号 FAIL（0.0.6 的「同行多原子 + WARN inline_atoms」已删除）。
- 不反转义：出现反斜杠转义（`\.`、`\$`、`\#`）直接 FAIL；E2 不写 `markdown_escapes` 计数。
- v1 **不拆原子**：回执里出现 `split` / `parts` / `sub` → `assemble.py` exit 1；原子过长只记 `long_atom` 提示，不拆。

## 5. 覆盖（`source_spans` + `block.line`）

- `blocks.json` 不写逐行覆盖映射：覆盖 = `source_spans`（每个原子占的行）∪ `blocks[].line`（每个块自己占的标签行）复算。
- `source_spans` 与 `atoms` 等长：第 i 项 `id == i`，`line` 是 1 基行号，`col_start/col_end` 是**该行 strip 后**的 0 基右开列号（当前实现整行一个原子：`0..len(strip)`）。
- 覆盖是 `gate e2` 硬检查：每个非空源行都必须被原子 span 行或块自己的 `line` 覆盖；漏行 / 块行非法 FAIL（解释句、`以下是…` 这类行就是在这里被拦下）。

## 6. `dispatch.json`

```json
{"version": 1, "doc": "<doc>", "tasks": [{
  "task_id": "<doc>-b05", "block": "b05", "role": "segmenter", "kind": "body",
  "atoms": [{"id": 8, "speaker": null, "text": "…"}],
  "position": "Section A / News Report 1 (Q1-2)",
  "receipt": "<RUN>/docs/<doc>/blocks/b05.json",
  "brief": "references/subagent-briefs.md#segmenter"
}]}
```

- 一块一条任务；`atoms` 必须与 `blocks.json` 该块原子**逐 id 一致**；`receipt` 固定 `docs/<doc>/blocks/<block>.json`。
- `position` 的节名来自显式 `## Section X` 或推断的默认节；篇目的 `(Qa-b)` 只来自 `body.q_range`。
- `brief` 是锚点，不是任务正文；根 agent 按锚点取 [roles/segmenter.md](../roles/segmenter.md) 渲染任务单。

## 7. `gate e2` 检查什么

| 检查 | 判据 | 失败提示 |
|---|---|---|
| 契约标签 | 每个非空行属于 8 类之一，且写法合法 | 列出「行号 + 原文 + 迁移提示」 |
| `title_first` | `## [开场]` 只能最前、最多 1 个 | 开场放最前 / 删掉重复 |
| 结尾最后 | `## [结尾]` 只能最后、最多 1 个 | 结尾放最后 |
| 阻塞报错 | 旧格式特征命中即 FAIL，不静默兼容 | 用新 prompt 重转 |
| 非空块 | title / ending / directions / body 至少 1 个原子 | 删空标签或补内容 |
| `[Qx-y]` | 篇目必带，`x <= y` | 补 `[Qx-y]` |
| `q_range` / `meta_q_range` | `body` 必有；`questions_meta` 有则必须等于紧邻前一个 body | 补 / 改范围 / 放进对应篇目 |
| 题号连续 | `question` 从 N 起严格 +1，与 `q_range` 对齐 | 补题 / 改范围 |
| 原子守恒 | 原子 id 连续、文本非空、`speaker ∈ {null,M,W}` | 修原文 |
| 覆盖 | `source_spans` ∪ `block.line` 覆盖全部非空源行 | 删解释句 / 补标签 |
| 封条 | `source_path` / `source_sha256` 与当前原文一致 | 回 E1 新 run |
| dispatch | 任务与正文块一一对应、原子一致、回执路径固定 | 重跑解析 |

## 8. 失败怎么处理

| 现象 | 处理 |
|---|---|
| `原文解析失败（第 N 行）：…` | 把「行号 + 原文 + 提示」贴进汇报；**不手改原文、不改 JSON**；按提示用新 prompt 重转或改标签后重跑 E2 |
| 提示旧格式特征 | 旧 md 不能补丁式混用：用 contract v1 原文 重转（见 transcript-contract §6） |
| `WARN missing_title` / `WARN missing_ending` | 不阻断、不伪造；要不要补由用户决定，不代写 |
| `source_sha256` 与当前原文不符 | 输入变了 → 回 E1 拿新 run，从 E2 重跑 |
| 块集合变了（改了标签） | 旧回执改名 `.bad-<ts>` 再重派 E3；不回滚旧 run |

## 9. 不要做的事

- 不在解析阶段判断分组、类型或进卡组；那是 E3/E4 的事。
- 不把标签、编号、`M:`/`W:` 带进切分文本；切分文本字节规则见 [format.md](format.md) §1。
- 不为旧格式写兼容分支，也不为「看起来像标题」的行猜结构；不认识的写法就是 FAIL。
- 不改用户原文：重转由用户/转写流程做，解析器只收货。
