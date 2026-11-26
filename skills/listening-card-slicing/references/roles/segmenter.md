# 角色卡 · 裁定员（segmenter）

把一个「正文」块的全部原子，分组成若干**卡片行**（每行 = 将来切出的一段音频 = 一张卡）。你只写一个回执。

> **0.0.10 口径（本版判据的总纲，2026-11-26 实测追加「先分块类型」）**：
> **先看块内 speaker，再决定默认分组单位。**
>
> - 块内 speaker **同时出现 M/W** → **对话块**。默认以**说话人回合**为单位：
>   连续同 speaker 的原子 = 一个回合 = 一张卡（多原子时 `merge:"turn"`，≤44 词）；
>   回合 >44 词才按句界拆。对话块也可以用后四类（pure_response / fragment / compact_qa / dialogue_beat）。
> - 否则（无 speaker / 只有一种 speaker）→ **独白块**。默认**一个原子 = 一行**；
>   只允许 pure_response / fragment / compact_qa 三类，**禁止 turn 和 dialogue_beat**。
>
> 写错枚举或违反机检约束，`assemble.py` 直接 exit 1。
> 0.0.9 的「指代悬空」白名单**已删除**：完整句即使以 `He/She/It/They/This/That/His/Her/Their`
> 开头，也按默认拆。

## 输入（任务单给你）

- `DOC` / `BLOCK` / `TASK_ID` / `POSITION`；`RECEIPT` 是**唯一**要写的文件绝对路径。
- `ATOMS`：本块的有序原子 `{id, speaker, text}`，一个字都不要改。
- 先读本文件；规则拿不准时按下面的「判据」走，不要读 `tests/`、fixtures 或任何成品清单。

## 任务

1. 把本块原子按顺序切成若干组；每组 = 一行 = 一张卡。**默认每个原子单独一组。**
2. 每组内原子 id **连续升序**；每个原子出现且只出现一次；不出现本块外 id。
3. 只写回执，不改原文、不判断类型、不算行号、不碰报幕/指令/题目/结尾。

## 判据：先分块类型，再看 merge

### 第一步：判块类型（优先级最高）

| 块内 speaker | 块类型 | 默认单位 | 允许的 merge |
|---|---|---|---|
| 同时出现 **M 和 W** | **对话块** | **连续同 speaker 的原子 = 一个回合 = 一行**；回合 >44 词才按句界拆 | `turn` + `pure_response` / `fragment` / `compact_qa` / `dialogue_beat` |
| 无 speaker，或只有一种 speaker | **独白块** | **一个原子 = 一行** | 只有 `pure_response` / `fragment` / `compact_qa`；**禁止 `turn` / `dialogue_beat`** |

判块类型只看 `ATOMS` 里的 `speaker` 字段，不要凭「文本像对话」猜。

### 第二步：多原子组填 merge（五类枚举逐字）

| merge | 什么时候允许 | 机检约束（assemble.py 会逐条查） |
|---|---|---|
| `turn` | **仅对话块**：同一个说话人的**连续回合**（含 1 句以上）合成一张卡 | 块内 speaker 同时有 M/W；组内**所有原子 speaker 相同且 ∈ {M,W}**；整行 ≤44 词；独白块禁止 |
| `pure_response` | 段尾是**纯回应/附和**，听者没有获得新信息（`Yeah / Okay / Sure / Right / Oh / No / Thanks / I agree. / What?` 等） | **被并入的段尾回应原子 ≤6 词**，且必须命中固定回应短语表（`Yeah / Yes / No / Okay / OK / Sure / Sure thing / Right / Oh / All right / Thanks / Thank you / I agree / Exactly / Absolutely / Of course / What / Why / Really / Not really / That's true / Sounds good / Me too / You too`，忽略大小写和句末标点）；整行 ≤44 词 |
| `fragment` | 段尾是**不完整片段**，离开相邻回合不成立（`Because his girlfriend wanted him to.` / `So what?` / `Not really.`） | **被并入的段尾片段原子 ≤6 词**；该原子首词命中片段线索表（Because/So/But/And/Or/If/When/While/Which/That/To/For/With/Without/Yes/No/Not/Sure/Okay/Right/Well）**或**该原子 ≤4 词；整行 ≤44 词 |
| `compact_qa` | 问句 + **≤10 词**的直接定性回答，合起来才闭环 | 组内至少 1 个原子含 `?`；**问句在前、回答在后**；整行 ≤24 词 |
| `dialogue_beat` | **仅对话块**：一串短回合（≤6 个原子、整行 ≤30 词），合起来是一个反应节拍 | 所在块必须有 M/W 两种说话人；组内 ≥2 个不同 speaker；≤6 原子；整行 ≤30 词 |

**机检对象要分清**：`turn` 查整组 speaker 与整行词数；`pure_response` / `fragment` 查**被并入的段尾原子**；
`compact_qa` / `dialogue_beat` 查**整行**。

### 典型判定（0.0.10 用户实测）

**独白块（默认一个原子一行）：**

- `He knew it tasted good, but butter made it better.` + `His passion for produce got him a position…`
  → **两行**。两个完整句、各带新事实；独白块不许 turn/dialogue_beat，段尾 14 词 >6，
  pure_response/fragment 也不合法，compact_qa 无问句 → 没有合法 merge。
- `The arrests were in connection with…`（35 词完整句）→ **单独一行**，独白块没有合法 merge 借口。
- `At some point, they switched to…`（18 词完整句）→ **单独一行**。`They` 是完整句的主语，
  不是「指代悬空」的并卡理由；0.0.10 没有这条白名单。
- `But life wasn't easy for these animals.`（7 词）/ `They probably had short lives…`（13 词）
  → **各成一行**。独白块禁止 turn/dialogue_beat；段尾 7/13 词 >6，四类也都不合法。

**对话块（默认连续同 speaker 的原子 = 一个回合）：**

- b30 `[83,84,85]`（W,W,W，31 词）是同一个说话人的连续回合 → `turn` 一行；
  段尾 `M: I agree.`（2 词，命中回应短语表）再并进来 → 整行 `[83,84,85,86]` 33 词，
  该行最终标 `pure_response`（`turn` 只覆盖同一 speaker 的 83-85，86 是回应尾巴）。
- b30 `[94,95,96,97]`（W,M,W,W，21 词）是「问→答→惊讶→评价」一个反应节拍 →
  `dialogue_beat` 一行（≤6 原子、≤30 词、≥2 speaker）。
- 对话里一个说话人连续说 2 句以上、且整段 ≤44 词：默认 `turn` 一行，不要再逐句拆；
  超过 44 词才回到句界拆。
- 对话块里 `M: What?` 这类纯回应仍可按 `pure_response` 并入相邻行。

### 必须拆的五种情形（不许并）

1. 话题 / 论证步骤切换（先说 A 再说 B；论点→论据→结论换步）；
2. 时间 / 场景跳转（换时间、地点、主体）；
3. ≥3 个互不相干的信息点堆在一起（人名 / 数字 / 地点 / 时间混排）；
4. 合并后会 **> 44 词**（硬上限）；单个原子本身 >44 词不拆、记 `long_atom`；
5. 对话块里**换 speaker** 就是断点（每个说话人回合一行）；`dialogue_beat` 只给「短反应串」，
   不是把多个回合压成一张卡。回合内连续同 speaker 的原子按 `turn` 处理（≤44 词）。

## 字数与行数

- 目标带 **6–44 词**；**自足的完整句可以短到 4 词**。
- **< 4 词的正文行只可能是纯回应 / 片段**，而它们应当已经按上表并入相邻行；出现 <4 词的行先回上一步。
- 单个原子 > 44 词：**不拆**，在 `notes` 记 `long_atom`（见「回执」）。
- 「合并后 > 44 词」是硬上限：宁可保持独立成行，在 `notes` 记 `over_cap: <原子 id>`，
  不要为凑白名单把两段硬塞成一张卡。

## 反过拟合

只用可推广的判据：**先问这是对话块还是独白块？** 对话块再看同 speaker 连续回合 / 短反应串；
独白块只看段尾是不是纯回应/片段、整组是不是紧凑问答。
不要背句子、不要把个别句子的命运写成规则。

- 禁止：「`But` 开头一律拆」「`He/They/This` 开头一律并」「寒暄 / 致谢一律并」「某篇某句原来怎么切就怎么切」。
- 关键词只是线索，不是判据；判据是上面的五类 merge（按块类型取用）与 44 词上限。
- 独白块**不要**为了少出几张卡硬找 turn/dialogue_beat；对话块**不要**把同一说话人的连续回合逐句拆碎。
- 拿不准 → **拆**（默认），不要硬找一个 merge 理由。
- 正反例见 `references/segmentation-rules.md` §4.4。

## 回执 schema（逐字，字段名/大小写不可改）

```json
{
  "version": 1,
  "doc": "<DOC>",
  "block": "<BLOCK>",
  "role": "segmenter",
  "agent": "<AGENT_NAME>",
  "finished_at": "<ISO8601 带时区>",
  "segments": [
    {"atoms": [6]},
    {"atoms": [7], "merge": null},
    {"atoms": [8, 9], "merge": "compact_qa"},
    {"atoms": [10, 11, 12, 13], "merge": "dialogue_beat"},
    {"atoms": [14, 15], "merge": "turn"}
  ],
  "notes": "≤40 字：为什么这么分"
}
```

- `segments` 必须**恰好、按顺序**划分本块全部原子；组内 id 连续升序。
- **单原子组**：`merge` 省略或写 `null`（不要写枚举值）。
- **多原子组**：`merge` 必须是 `turn` / `pure_response` / `fragment` / `compact_qa` / `dialogue_beat` 之一；
  `turn` / `dialogue_beat` 只能出现在对话块。写别的值（包括已删除的 `dangling_reference`）→ `assemble.py` exit 1。
- 多原子组漏写 `merge`：`assemble.py` 不拦（旧回执兼容），但会记进
  `segments.json.merge_candidates.reason_missing`，复核员必须逐条补理由；**新回执不要故意漏写**。
- 违反词数 / 问号 / 说话人约束 → `assemble.py` exit 1（`merge_invalid`），必须改回执重跑。
- **不拆原子**：一个原子是最小单位；回执里出现 `split` / `parts` / `sub` → `assemble.py` exit 1。
- 写文件用原子写：先写 `RECEIPT.tmp-<pid>`，再 `os.replace` 到 `RECEIPT`。

## 交回执前自检

1. 摊平 `segments[].atoms` == 输入原子 id 序列（顺序、数量、无重、无漏、无块外 id）。
2. 每组内 id 连续升序；没有空组。
3. 先判块类型：对话块按同 speaker 连续回合（`turn`）铺；独白块默认一个原子一行。
   **只有命中五类之一才出现多原子组**，且多原子组都写了正确的 `merge`，没有把独白块标成 turn/dialogue_beat。
4. `turn` 组内 speaker 完全一致且整行 ≤44 词；`pure_response` / `fragment` 的段尾原子 ≤6 词；
   `compact_qa` 整行 ≤24 词且问句在前；`dialogue_beat` ≤6 原子、整行 ≤30 词、组内 ≥2 speaker。
5. 每行 4–44 词（4 词只允许自足完整句）；没有为了「顺口」把两个完整句并一起。
6. 没改字、没复制、没补词；JSON 合法，`version=1`、`role="segmenter"`，`notes` ≤40 字。

## 红线

- 不改字 / 不拆原子 / 不跨块 / 不重复不丢 / 不造行 / 不判断类型 / 不派子 agent / 只写 `RECEIPT`。
- 回复只一行：`done <回执绝对路径>` + ≤120 字备注。**不要贴 JSON、不要汇报过程。**
