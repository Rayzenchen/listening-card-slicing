# 原文契约 contract v1（E2 唯一输入格式）

> 0.0.7 起，E2 只认这一种原文格式。旧格式（`# Part II…` / `## 试音部分` / 裸 `That's the end…` / 编号型 `Questions`）**一律拒绝**，报错给迁移提示，不静默兼容。
> 转写 prompt（把音频变成合规原文）在自备的 contract v1 原文；E2 产物字段见 [parse.md](parse.md)，清单/包字节见 [format.md](format.md)。

## 1. 一句话与一个理由

**一句话**：听音频的模型负责「分段、篇目、题号、哪些话真的念了」；E2 只做三件事——收货安检、把每句话变成原子、给原文封 sha。

**为什么**：0.0.6 的 E2 在猜 Markdown 排版（profile 检测、开场标题正则、结尾句正则、编号型 meta），同一句原文换个写法就解析成另一种结构。0.0.7 把结构写进标签：标签就是声明，不符合就 FAIL，不再猜。

## 2. 语法总表（8 类标签 / 块）

| 写出来的行 | block kind | 清单类型 | 带原子 | E2 生成的文本 |
|---|---|---|---|---|
| `## [开场]` + 若干 `1、…` | `title` | 开场 | 必带 | 原子单空格拼接 |
| `## Section X` | `marker` | 报幕 | 不带 | `Section X.` |
| `### Directions` + 若干 `1、…` | `directions` | 指令 | 必带 | `Directions: …` |
| `### News Report 1 [Q1-2]` 等 | `marker` | 报幕 | 不带 | `News Report 1.`（不含 `[Qx-y]`） |
| 篇目下的 `1、…` | `body` | 正文 | 必带 | 无独立 text，带 `q_range` |
| `[META] Questions …` | `questions_meta` | 指令 | 不带 | `Questions …`（剥掉 `[META] `） |
| `Question N. …` | `question` | 题目 | 不带 | 原句；带 `q_range=QN` |
| `## [结尾]` + 若干 `1、…` | `ending` | 结尾 | 必带 | 原子单空格拼接 |

- 篇目只有三种：`News Report N` / `Conversation N` / `Passage N`（N 从 1 起）。
- `## Section X` 是分组标签，也是报幕：建议按卷面结构写 A/B/C 的边界，哪怕音频没报（静音行只是非卡片行，E6 忽略）；整卷只有 A 时可省；全文没有也能解析（E2 推断默认节：首个显式节的前一节；全缺 = A）。
- `### Directions` 可以出现多次（每个 Section 一份）。
- `[开场]` / `[结尾]` 是**布局标题**，本身不发音；只有其中的 `1、…` 是语音行。没有声音就整块不写。

## 3. 硬规则（E2 逐条查）

### 3.1 排序与唯一
1. `## [开场]` 必须在任何 `## Section` / `###` 篇目 / `## [结尾]` 之前；最多 1 个，重复 FAIL。
2. `## [结尾]` 必须在最后一道 `Question` 或最后一篇正文之后，且是全文件最后一个块；最多 1 个，重复 FAIL。
3. `## [开场]` / `## [结尾]` 可以有内容，但不能是空块：有标签就必须至少有 1 个 `1、…`，否则 FAIL。
4. 开场 / 结尾可以**整块缺失**：只记 `WARN missing_title` / `WARN missing_ending`，`gate e2` 仍 PASS。

### 3.2 篇目与题号
5. `### <篇目>` 的 `[Qx-y]` **必填**；`x`、`y` 是正整数且 `x <= y`；单题写 `[Q16]`。缺 `[Qx-y]` 或格式不对 FAIL。
6. 篇目下的正文块至少 1 个原子；空篇目 FAIL。
7. `[META] Questions …` 可选：写了，`q_range` 必须与**紧邻的前一个正文块**一致；不写，篇目 `q_range` 独立成立（从 `Question` 题号取）。
8. `Question N. …` 必须与篇目 `q_range` 对齐且**严格连续 +1**（可从 N 起）；缺题、跳号、重复、越界都 FAIL。

### 3.3 行与原子
9. 每个 `1、…` 行**只能有一个编号原子**；一行两个编号 FAIL（0.0.6 的「同行多原子只 warning」已删除）。
10. 一句一行；每个块（开场 / Directions / 篇目 / 结尾）从 1 连续编号；原子文本 = 去掉行首 `N、` 与 `M:`/`W:` 后的原句，**一个字都不改**。
11. 对话写 `M: 1、…` / `W: 1、…`；编号在整段对话里连续，换人不重编；`speaker` 继承到下一个说话人为止。
12. `**M:**` / `**W:**`（加粗说话人）直接 FAIL，提示去掉星号。

### 3.4 禁止的 Markdown
13. 反斜杠转义（`\.`、`\$`、`\#` 等）直接 FAIL。E2 不做反转义，也不再写 `normalization.markdown_escapes`。
14. 代码围栏、链接、加粗、斜体、HTML 一律不允许。
15. 「以下是…」「Here is…」这类解释 / 前言 / 总结句不允许；非空行必须能被某个块或原子覆盖。

### 3.5 封条与覆盖
16. 原文非空行必须 100% 被 **原子 span** 或 **块自己的行** 覆盖；漏行、块行非法 FAIL。
17. `blocks.json` 记 `source_sha256`；sha 变了就是新 run（回 E1），不改旧 `blocks.json`。

## 4. 正例

### 4.1 有头有尾（完整卷）

```markdown
## [开场]
1、College English Test, Band 4.
2、Part Two, Listening Comprehension.

## Section A
### Directions
1、In this section, you will hear three news reports.
2、At the end of each news report, you will hear two or three questions.

### News Report 1 [Q1-2]
1、A terrified cat has survived a five-mile round trip under the engine cover of a car.
2、The black cat was found curled up under the engine cover of David King's car.
[META] Questions 1 and 2 are based on the news report you have just heard.
Question 1. Where was the black cat found?
Question 2. What do we learn about the cat at the end of the news report?

## Section B
### Conversation 1 [Q3-4]
M: 1、Can you please hand me that book over there?
W: 2、Which one?
M: 3、The one on the top shelf.
[META] Questions 3 and 4 are based on the conversation you have just heard.
Question 3. What is the man looking for?
Question 4. Where is the book?

## [结尾]
1、That's the end of Listening Comprehension.
```

### 4.2 无头无尾（音频从 Section 中间开始，也没有结束语）

```markdown
## Section C
### Passage 1 [Q16-18]
1、Dozens of British students arriving for their first day of school on Tuesday were sent home.
2、About 30 students were turned away from Taverham High School in Norfolk, England.
[META] Questions 16 to 18 are based on the passage you have just heard.
Question 16. Why were dozens of British students sent home on Tuesday?
Question 17. What did the school do about the shoes?
Question 18. What did the head teacher say?
```

没有 `## [开场]`、没有 `## [结尾]` 就整块不写；不要补、不要造。

### 4.3 篇名没报（音频没念 Passage 2，只念了 Section C 与正文）

```markdown
## Section C
### Passage 2 [Q19-21]
1、As annual inflation climbs over 10 percent, many households are on tight budgets.
2、Private rental prices in Britain rose 3.2 percent over the 12 months to July 2022.
[META] Questions 19 to 21 are based on the passage you have just heard.
Question 19. What does the passage say about household budgets?
Question 20. What does the passage say about rental prices?
Question 21. What has made things worse?
```

- 篇目标题是**结构标签**：音频没念篇名也要写，否则正文没有 `q_range`、E3 不知道该派给谁。
- 但**不要再编造**一句 `1、Passage 2.`——报幕行由标签生成，静音会被 E6 的非卡片行豁免接住（`## Section X` 同理）。

## 5. 反例（旧格式已停用 / 现行错误）

### 5.1 旧格式：已停用，E2 直接拒绝

> 下面这些是 0.0.6 及更早的原文写法，**0.0.7 起不再支持**。命中的旧 md 必须用新 prompt 重转（§6），不要手改后混用。

| 旧写法 | 为什么不再认 | 现在怎么写 |
|---|---|---|
| `# Part II Listening Comprehension` | 旧版一级标题；0.0.7 没有 `TITLE_RE` / profile 检测 | `## [开场]` + 把标题报幕写成 `1、College English Test, Band 4.` |
| `## 试音部分` | 旧版试音块标题；已删除 `test_intro` kind | 试音句直接进 `## [开场]` 的编号句（不发音就整块不写） |
| 裸 `That's the end of Listening Comprehension.`（不在任何标签下） | 旧版结尾句；0.0.7 只认 `## [结尾]` | `## [结尾]` 下一行 `1、That's the end of Listening Comprehension.` |
| `1、Questions 1 and 2 are based on …`（编号型 meta） | 旧版把 META 当原子；0.0.7 标签不发音、不编号 | `[META] Questions 1 and 2 are based on the news report you have just heard.` |
| 裸 `Questions 1 and 2 are based on …`（无 `[META] `） | 旧版按裸行猜 meta | 行首加 `[META] ` |
| `### News Report 1`（缺 `[Qx-y]`） | 0.0.7 的 `q_range` 只能来自标签 | `### News Report 1 [Q1-2]` |

### 5.2 现行格式的典型错误

| 错误写法 | E2 怎么判 | 改成 |
|---|---|---|
| 一行两个编号：`1、… 2、…` | FAIL（每行只允许一个原子） | 拆成两行，各自编号 |
| `1、Question 1. …` | FAIL（题号行不能带原子编号） | `Question 1. …` |
| `**M:** 1、…` | FAIL（加粗说话人） | `M: 1、…` |
| `5\.00` / `\$5` | FAIL（反斜杠转义） | `5.00` / `$5` |
| 代码围栏包住整段 | FAIL（围栏不是内容） | 去掉围栏，只交 Markdown 正文 |
| `## [开场]` 出现在 Section 之后 | FAIL（顺序） | 开场放最前 |
| `## [结尾]` 不是最后一个块 | FAIL（顺序） | 结尾放最后 |
| `## [开场]` 下没有 `1、…` | FAIL（空块） | 有标签就至少 1 个原子，否则整块删掉 |
| 题号 1、3（跳号） | FAIL（题号连续 +1） | 补题或核对 `[Qx-y]` |
| `[META] Questions 1 and 2…` 放在 `[Q3-4]` 篇目下 | FAIL（meta 与 q_range 不一致） | 放进对应篇目 |
| 一行普通说明：「以下是转写」 | FAIL（非空行未被覆盖） | 删掉 |

## 6. 迁移：旧 md 怎么办

**结论：旧 md 不能继续用，必须用新 prompt 重转。** 0.0.7 不提供兼容层，也不做「先按旧格式试着解析」。

### 6.1 三种情况

| 手上有什么 | 要做什么 |
|---|---|
| 只有旧 md（`# Part…` / `## 试音部分` / 裸结尾 / 编号型 meta） | 用 contract v1 原文 重新转写音频，产出新 md；旧 md 留档但不再进 E1 |
| 旧 md + 音频，只想制卡 | 入口 B（`bootstrap.py --phase 2`）走阶段二快路径（E9 起），不需要原文，也不用重切 |
| 已交付的 0.0.6 包 | **不动**：包内 6 列清单、wav、`approve.json` 都还能用；0.0.7 只改「原文 → E2」这一段 |

### 6.2 手工迁移对照（转写工具不可用时的兜底）

1. 旧版一级标题 / 试音标题 → `## [开场]`，标题句逐句编号；
2. 每个 `### <篇目>` → 补 `[Qx-y]`（范围从该篇目的 `Questions X and Y` 或 `Question` 题号取）；
3. 裸 / 编号型 `Questions…` → 去编号、加 `[META] `；
4. 旧结尾句 → 移到最后的 `## [结尾]` 下，加 `1、`；
5. 删掉所有 `#`/`##` 旧标题、解释句、加粗与反斜杠转义；
6. 跑 §7 的自查，再跑 E2。

### 6.3 自查（旧格式特征）

```bash
md="材料区/<stem>_听力原文.md"
grep -nE '^# Part |试音部分|^[0-9]+、Questions|^### (News Report|Conversation|Passage) [0-9]+ *$|^\*\*[MW]:\*\*' "$md" || echo 'OK: 未命中旧格式特征'
```

命中任意一条 → 重转，不要手改后硬跑。

## 7. E2 怎么用这份契约

```bash
python3 "${S}/parse_source.py" --run "${RUN}" --doc <doc> --json
python3 "${S}/gate.py" e2 --run "${RUN}" --doc <doc>
```

- 解析失败：exit 1，stderr 一行 `ERROR: 原文解析失败（第 N 行）：…`，stdout 末行 `JSON {"ok":false,"exit_code":1,"stage":"e2","error":"…"}`；把「行号 + 原文 + 迁移提示」贴给用户，**不改原文**、不自己补标签。
- 解析成功：`blocks.json`（块/原子/覆盖/`source_sha256`）+ `dispatch.json`（正文块任务单）。字段见 [parse.md](parse.md)。
- 需要重跑时：原文内容一变就是新 run（旧 run 改名 `.prev-<ts>`），从 E2 重跑，旧回执作废。

## 8. 与下游的关系（一句话版）

- 只有 `body` 块进 E3 分组；`question` 一题一张卡；`marker` / `directions` / `questions_meta` / `title` / `ending` 都不进卡组。
- `[Qx-y]` 是 E4 位置列 `Qa-b` 的唯一来源；`[META]` 剥掉 `[META] ` 后原样进清单的指令行。
- 不改字：原子文本、题目原文都是原句投影；E5 只在右侧加译文列。
