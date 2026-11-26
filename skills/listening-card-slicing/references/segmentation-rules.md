# 卡片重组规则 · 裁定员手册（阶段一）

> **维护 / 档案**：运行时裁定规则见 [roles/segmenter.md](roles/segmenter.md)；本文件保留完整推导、正反例与裁决出处，不再作为执行入口。

> 你是「裁定员」：一个只活几分钟的子 agent。你拿到**一块**原文的原子列表，把它分组成若干「片段」，
> 写一个 JSON 回执，然后结束。你不写文章、不改文字、不切音频、不派任务。
>
> **0.0.10 口径（本文件判据的总纲，2026-11-26 实测追加「先分块类型」）**：
> **先看块内 speaker**：同时出现 M/W → 对话块，默认「连续同 speaker 的原子 = 一个回合 = 一行」
> （`turn`）；否则 → 独白块，默认「一个原子 = 一行」。
> 允许并的五类是 `turn` / `pure_response` / `fragment` / `compact_qa` / `dialogue_beat`，
> 其中 `turn` / `dialogue_beat` 只在对话块合法，独白块只能用前三类，
> 每个多原子组必须在回执里写 `merge` 理由，并由 `assemble.py` 机检（词数 / 问号 / 说话人）。
> 0.0.9 的「指代悬空」白名单**已删除**：完整句即使以 `He/She/It/They/This/That/His/Her/Their`
> 开头，只要带新事实就单独成行。0.0.8 的「宁并勿拆 / 并 > 拆」作废。

> 全篇引用的卷1 / 卷2 句子**文本逐字一致**，可以拿去做对答案的锚点；但**分组结论**以 0.0.10 判据为准
> （2026-11-26 用户实测的 16 处改分组是判断依据），0.0.8 黄金清单（120 行）不再是标准答案。
> 本文件的 §4.4 / §4.5 / §5 / §10 / §13 已按 0.0.10 改写；更早的章节只保留推导背景。


---

## 0. 你在流水线上的位置

```text
用户原文 → parse_source.py 切块切原子 → dispatch.json（一块一个任务）
        → 【你】把原子分组成片段 → 回执 blocks/<block>.json
        → assemble.py 编号、拼文本、出清单 → 复核员通读 → 切音频
```

**白话对齐术语**

| 术语 | 一句白话 |
|---|---|
| 原子 | 用户原文里的**一个编号行**（`1、…` 或 `M: 3、…`）去掉编号和说话人标记后剩下的**那一句**。它是本流程不可再分的最小单位。 |
| 片段 | 若干**连续**原子拼成的一行 = 将来切出来的**一段音频** = 牌组里的**一张卡**。 |
| 块（block） | 原文里一个篇目（`### News Report 1` 下的 11 个原子）或一段说明。**你只处理「正文」块。** |
| 正文 | 听力材料本身（新闻 / 对话 / 篇章），要进牌组、要做成卡的那部分。 |
| 报幕 / 指令 / 题目 / 开场 / 结尾 | 朗读出来的流程语音与题干；它们也有音频，但不是"材料"。 |
| 单听可懂测试（旧称防懵测试） | 本手册的核心判据：**把这一行单独放给一个中等水平学习者听，他能不能听懂。** 听不懂 = 完整句被并进了别的卡；听不过来 = 话题跳步 / 超容。0.0.10 起默认一个原子一行。 |
| 熔断 | 一行太长时强制断开（像电路过载跳闸）。0.0.10 的上限仍是 44 词。 |

---

## 1. 输入 / 输出契约

### 1.1 你收到什么

`dispatch.json` 里属于你的那个 task：

| 字段 | 含义 |
|---|---|
| `task_id` | 例如 `d01-b03` |
| `block` | 块 id，例如 `b03` |
| `kind` | 只可能是 `body`（你只接这类） |
| `atoms` | **有序**数组，每项 `{id, speaker, text}`；`speaker ∈ {"M","W",null}`，`null` = 独白 |
| `position` | 这块的位置，例如 `Section A / News Report 1 (Q1-2)` |
| `receipt` | 你要写的文件绝对路径（例如 `…/blocks/b03.json`） |
| `brief` | 指向本文件 |

**原子已经是"一句话"级别**，来自用户原文的分行；不要把它们再拆成词或半句（§1.4 是唯一规则）。
原子的 `text` **不含**编号和 `M:`/`W:` 前缀，但**含**原句标点。**一个字都不许改。**

### 1.2 你要交什么

一个 JSON 文件，写到 `task.receipt` 指的路径（用原子写：先写 `.tmp-<pid>` 再 `os.replace`，别让半个文件被读到）：

```json
{
  "version": 1,
  "doc": "d01",
  "block": "b03",
  "role": "segmenter",
  "agent": "segmenter-b03",
  "finished_at": "2026-11-23T15:10:00+08:00",
  "segments": [
    {"atoms": [6]},
    {"atoms": [7], "merge": null},
    {"atoms": [8]},
    {"atoms": [9, 10], "merge": "compact_qa"},
    {"atoms": [11, 12], "merge": "dialogue_beat"},
    {"atoms": [13]}
  ],
  "notes": "默认一句一行；9-10 紧凑问答、11-12 对话短反应串，各并一行"
}
```

**上面只是格式示例，id 是假设的**；0.0.10 的分组理由按 §4 现场判，不要背分组。真实 id 一律以 dispatch 为准。
**多原子组必须写 `merge`**（五类之一）；单原子组省略或写 `null`。漏写不阻塞旧回执，但会进
`merge_candidates.reason_missing`，复核员要补理由；写错枚举或违反机检 → `assemble.py` exit 1。


`segments` 的三条铁律（e3 关卡逐条机检）：

1. **恰好、按顺序**划分本块全部原子；每个原子出现且**只出现一次**；
2. 每组内的 `atoms` 必须是**连续升序**（不能 `[9,11]` 跳过 10）；
3. 不出现本块以外的原子 id（**不跨块合并**，§8）。

### 1.3 四条硬约束（违反 = 直接打回，不给理由）

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **只合并不改写**：你不产生任何新字符，只决定"哪几个原子挨在一起" | 用户的原文是唯一文本真源；切音频时程序要按这些字符去对齐发音，改了就对不上 |
| 2 | **不重复、不丢原子** | 丢一个 → 少一段音频、原文覆盖率不达标；重复 → 同一句话被切成两段，牌组里出现两张一样的卡 |
| 3 | **不跨块** | 块边界是篇目边界，跨过去就变成"两篇新闻拼一张卡" |
| 4 | **不判断类型** | 类型由原子的**来路**决定（§2），不由你的观感决定 |
| 5 | **不拆原子** | 原子是最小单位（§1.4，A9）。拆了回执会被 `assemble.py` 直接 exit 1 |

### 1.4 原子不可拆（v1 冻结，接口修订 A9）

**你的唯一动作是「把连续原子分到同一组」，不允许把单个原子拆成两段。**

- 回执里**不许出现 `split` 字段**（也不要发明 `parts` / `sub` 之类等价物）：`assemble.py` 见到就
  **直接 exit 1**（`check atom: split_not_supported`），不猜、不裁。
- 依据（实测，不是保守）：卷1 **154** 个原子、卷2 **153** 个原子，**最长 37 词、0 个 > 44 词**；
  0.0.8 的黄金清单 120 行对 154 个原子也是**零拆分**的连续分组。也就是说：真实材料里根本没有"必须拆原子"的场合。
- 万一你遇到一个确实过长的原子（> 44 词，或内部明显有两套完整主谓）：
  1. **照样把它完整放进某一组**（不许截断、不许复制）；
  2. 在 `notes` 里写一行 `long_atom: <原子 id> <词数> 理由一句话`；
  3. 复核员会把它转成 `flags[].kind="long_atom"`，由根 agent 报给用户。

**为什么不做"原子内拆分"**：拆点几乎不可能落在静音处（例：`He knew it tasted good, but butter made it better.`
中间没有停顿），强行切会把音频切进词里；而且一旦允许拆，就必须保证拆出来的碎片逐字拼回原句，
这等于在流水线里造文本——与"用户原文是唯一真源"直接冲突。要拆，正确做法是**用户改原文分段、另起一次 run**。

---

## 2. 先分清哪些不是你的活

六类片段里，**只有「正文」由你分组**。其余五类由原子的"来路"直接定型，`parse_source.py` 已经处理，
你既不需要、也不许去改。

| 来路（原文里的什么） | 类型 | 进卡组 | 位置列写法 | 谁定 |
|---|---|---|---|---|
| part 标题播报（`## College English Test, …`） | **开场** | 否 | `开场` | 程序 |
| Section 级标签（`## Section A`）、篇目标题（`### News Report 1` 等） | **报幕** | 否 | `Section A` / `Section A / News Report 1` | 程序 |
| `Directions:` 那一段（整块的原子拼一行） | **指令** | 否 | `Section A` | 程序 |
| `Questions 1 and 2 are based on …`（**每题组一行**） | **指令** | 否 | `Section A / Q1-2` | 程序 |
| 单个 `Question 1. …` 行 | **题目** | **是** | `Section A / Q1` | 程序 |
| 新闻 / 对话 / 篇章的编号原子 | **正文** | **是** | `Section A / News Report 1 (Q1-2)` | **你** |
| `That's the end of Listening Comprehension.` | **结尾** | 否 | `录音结尾` | 程序 |

**进卡组只由类型决定**：`keep = 类型 ∈ {正文, 题目}`。你不为"这句话重不重要"投票。
一句很短的 `Ah, a C.` 也进卡组（只要它是正文），一句很长的 `Directions` 也不进。

**为什么把类型和分组拆开**：类型是可机检的事实（它来自哪一行），分组是判断题。混在一起就会
"为了让这句话不进卡组，把它标成指令"——那是最容易发生的作弊，也是覆盖门要防的。

---

## 3. Phase A：原子级重组（打碎）

### 3.1 V-2.0 的 Phase A 在说什么

原话：把内容拆到"最小独立分句"级别，哪怕用 and / but / so 连起来的复合长句，只要两边都有完整主谓结构，
也强制拆成两句，**为每个分句打逻辑编号 `[1] [2] [3]…`**。

### 3.2 在本流程里，这一步已经做完了

用户原文本身就是**一句一行的编号稿**（`1、…`、`2、…`）。`parse_source.py` 把每个编号行变成**一个原子**，
原子自带全局连续 id。也就是说：**"打编号"这件事由用户的排版完成，你就是那个拿到 `[1] [2] [3]…` 的人。**

于是 Phase A 在本流程里落成两条操作：

1. **按原子读**：眼里的单位是原子，不是"一段话"。原子的边界就是你能合并的最小颗粒，也是 0.0.10 的**默认切点**。
2. **不做句内拆分**（v1 无例外，见 §1.4）。理由是三条硬的：
   - **用户的分行就是他的分段意图**，再拆等于替用户改稿；
   - 拆点常常不在静音处（`He knew it tasted good, but butter made it better.` 中间没有停顿），
     强行切会把音频切进词里；
   - 实测 154 个原子 **0 个 > 44 词**，说明原子粒度已经合适。

> 实例：原子 `He knew it tasted good, but butter made it better.`（卷1 第 1 篇新闻第 2 句）按 V-2.0 的
> 字面要求应当拆成两句，但它**没有拆**，而是**完整地**和相邻原子按 §4 判据组合。
> 这就是"Phase A 的意图（离散化）已由原文编号完成，而不是由你重做"。

---

## 4. 核心判据：单听可懂测试（主判据）

### 4.0 先分块类型（0.0.10 实测追加，优先级最高）

判块类型只看 `blocks.json` 的 `speaker`，不看文本像不像对话：

| 块内 speaker | 块类型 | 默认单位 | 允许的 merge |
|---|---|---|---|
| 同时出现 **M 和 W** | **对话块** | 连续同 speaker 的原子 = 一个回合 = 一行（`turn`，≤44 词）；换 speaker 才断 | `turn` + `pure_response` / `fragment` / `compact_qa` / `dialogue_beat` |
| 无 speaker / 只有一种 speaker | **独白块** | 一个原子 = 一行 | 只有 `pure_response` / `fragment` / `compact_qa`；**禁止 `turn` / `dialogue_beat`** |

这条分流是 2026-11-26 用户实测的直接结论：16 条「该拆」全在独白块，
2 条「该并」全在对话块。先分块，再套 §4.4 的白名单，两者不混用。

### 4.1 怎么"跑"这个测试

对每一个候选行，做一次想象实验，只问一个问题：

> **把这一行单独放给一个中等水平学习者，没有任何上下文，他能不能听懂？**

- **能听懂** → 它是自足的，**自己一行**（0.0.10 默认）。
- **听不懂**（断头断尾、不知道在说谁 / 在答什么）→ 先看它是不是**段尾 ≤6 词的纯回应或片段**：
  是 → 按 §4.4 的 `pure_response` / `fragment` 并进它服务的那一行；不是 → **仍然单独一行**，
  并在 `notes` 记一句「疑似断头」（0.0.10 不接受「指代悬空所以并」这个理由）。

**「并进哪一行」的准确含义**：同一块内、紧挨着的前一组原子（也就是你上一条 `segments` 里那一组）。
不是「上一句原文」——如果上一句已经被并进更前面那组了，就跟着那组一起。

### 4.2 默认：一个原子一行（句界 = 默认切点）

0.0.10 的默认动作是**拆**：一个原子一行。理由：一行 = 一张卡；学习者点开一张卡，
听到的应该是一句能独立成立的话，而不是"半句话 + 上一句"。

**不要**因为下面这些理由去并：

- "它很短"（短不等于不能独立听懂；自足的短句照样单独一行）；
- "两句话说的是同一件事"（同一画面 ≠ 同一张卡）；
- "同一段里读起来更顺"（顺不顺是朗读体验，不是学习单位）。

### 4.3 必须拆：五种情形（不许并）

| # | 判据 | 说明 |
|---|---|---|
| 1 | **话题 / 论证步骤切换** | 先说 A 再说 B；论点→论据→反例→结论，每换一步就是新卡 |
| 2 | **时间 / 场景跳转** | 换了时间、地点、主体、画面；新闻里从一个事件切到另一个事件 |
| 3 | **≥3 个互不相干的信息点堆叠** | 人名 / 数字 / 地点 / 时间混排，合起来听不过来 |
| 4 | **合并后 > 44 词** | 硬上限（§6.2）；宁可拆，不许硬塞 |
| 5 | **对话里换 speaker 才断** | 对话块默认同一 speaker 的连续回合合成一行（`turn`，≤44 词）；换 speaker 才断，不要把同一说话人的连续 2+ 句逐句拆碎 |

⚠️ **关键词只是线索，不是判据。** 句首 `But / However / Then / So` 说明这里**可能**是断点，
但最终仍要问"这句离开上一句能不能听懂"：

| 句子 | 0.0.10 怎么判 | 为什么 |
|---|---|---|
| `But new research found that people believe someone who shows off their social status cares more about benefiting themselves than helping others.`（22 词） | **单独一行** | 有主谓、自带 `new research` 语境，离开上一句完全能听懂；而且这是新的论证步骤 |
| `But life wasn't easy for these animals.`（7 词） | **单独一行** | 0.0.9 曾以「`these animals` 指代悬空」并进上一句；0.0.10 删除该白名单。独白块禁止 turn/dialogue_beat，7 词又 > 段尾 6 词上限 → 拆 |
| `They probably had short lives, and the remains show evidence of severe injuries.`（13 词） | **单独一行** | 完整句、带新事实（寿命短、有伤）；`They` 开头不再是并卡理由 |

**一句话总结**：`But` / `He` / `They` / `This` 开头既不是「必须拆」，也不是「必须并」；0.0.10 只认
§4.4 的五类 merge（按块类型取用）与 44 词上限，其余一律默认拆。


### 4.4 允许并：五类白名单（白名单之外一律拆）

只有命中下面任一条，才允许并；**完整句、带新事实的句子、两个都能独立听懂的句子一律单独成行**。
多原子组必须在回执里写 `merge`（枚举逐字），`assemble.py` 会按表里的机检约束逐条验。
**先按 §4.0 判块类型再取用**：`turn` / `dialogue_beat` 只给对话块；独白块只能用 ① ② ③。

#### ⓪ `turn`：对话块里同一个说话人的连续回合（仅对话块，整行 ≤44 词）

对话块的默认单位就是**说话人回合**：连续同 speaker 的原子合成一行；换 speaker 才断。
不要把一个说话人的连续 2+ 句逐句拆成多张卡。

- 机检：块内 speaker 同时有 **M/W**；组内**所有原子 speaker 相同且 ∈ {M,W}**；整行 ≤44 词；
  **独白块禁止**（无 speaker / 单一 speaker 会被 `merge_invalid` 拒绝）。
- 例（b30 实测）：`[83,84,85]` 是 W 连续三句、31 词 → `turn` 一行；
  段尾再并入 `M: I agree.`（2 词）→ 整行 `[83,84,85,86]` 33 词，该行标 `pure_response`。
- 反例：`[83,84,85,86]` 混了 W 和 M，不能标 `turn`（组内 speaker 必须一致）；
  该行若只并 86，就用 `pure_response`。
- 反例：独白新闻块 `[9,10]` 两个完整句标 `turn` → exit 1（块内没有 M/W）。
- 回合 >44 词才按句界拆；拆出来的每一段保持原子边界。

#### ① `pure_response`：纯回应 / 附和（段尾原子 ≤6 词）

段尾是听者没有获得新信息的短回应（`Yeah / Yes / No / Okay / OK / Sure / Sure thing / Right / Oh /
All right / Thanks / Thank you / I agree / Exactly / Absolutely / Of course / What / Why / Really /
Not really / That's true / Sounds good / Me too / You too`），忽略大小写与句末标点。

- 机检：**被并入的段尾回应原子 ≤6 词**且命中固定短语表；整行 ≤44 词。
- 例：`I've known him since primary school.` + `Yeah.` → 一行（`pure_response`）。
- 例：`Oh, well, that's ridiculous, isn't it?` + `It was noticeable, sure, but it wasn't an ugly scar.` +
  `It never occurred to me that he should have it taken care of like that.` + `I agree.` → 一行 33 词，
  段尾 `I agree.` 2 词命中短语表 → `pure_response`（用户 2026-11-26 实测接受）。
- **反例**：`Yeah, it's more convenient.`（5 词）**不是**纯回应——它带来了新信息（更方便），
  不命中固定短语表；`pure_response` 机检会 exit 1，应按普通完整句单独成行。

#### ② `fragment`：不完整片段（段尾原子 ≤6 词）

段尾是离开相邻回合就不成立的片段：`Because his girlfriend wanted him to.` / `So what?` / `Not really.`。

- 机检：**被并入的段尾片段原子 ≤6 词**；首词命中片段线索表
  （Because/So/But/And/Or/If/When/While/Which/That/To/For/With/Without/Yes/No/Not/Sure/Okay/Right/Well）
  **或**该原子 ≤4 词；整行 ≤44 词。
- 例：`Then why did he have the operation?` + `Because his girlfriend wanted him to.`（6 词，首词 Because）
  → `fragment`（也可以标 `compact_qa`，见 ③）。
- **反例**：`They probably had short lives, and the remains show evidence of severe injuries.`（13 词）
  ——完整句、13 词 >6，独白块又禁止 turn/dialogue_beat → 五类都不合法，必须单独成行。

#### ③ `compact_qa`：紧凑问答（整行 ≤24 词）

问句 + **≤10 词**的直接定性回答（`Yes` / `No` / 好 / 不好 / 一个人名、数字、理由核心词）→ 可合成一行。

- 机检：组内至少 1 个原子含 `?`；**问句在前、回答在后**；整行 ≤24 词。
- 例：`Then why did he have the operation?` + `Because his girlfriend wanted him to.`（13 词）→ 一行。
- 独白里的自问自答同理：`How did talking evolve?` + `There are two main theories.`（9 词）→ 一行。
- **长解释剥离**：回答后面紧跟长篇解释时，**从解释首句断开**——解释能独立听懂就单独一行。
  "长篇" = 解释部分 ≥ 2 个原子或 ≥ 25 词。

#### ④ `dialogue_beat`：对话短反应串（整行 ≤30 词、≤6 原子）

对话里一串短回合合起来是一个反应节拍（不是「把一段对话压成一张卡」）。

- 机检：所在块必须有 M/W 两种说话人；组内 **≥2 个不同 speaker**；**≤6 原子**；**整行 ≤30 词**。
- 例（用户 2026-11-26 实测接受）：`W: Then why did he have the operation?` +
  `M: Because his girlfriend wanted him to.` + `W: What?` + `W: That's so superficial and selfish of her.`
  → 4 原子、21 词、2 speaker → `dialogue_beat`。
- **反例**：把 `M: Hey, Mary.` + `M: You seem…` + `M: What happened?` + `W: Rush hour…` 四个原子
  压成一张 `dialogue_beat`。0.0.10 下 M 的三句是**同一个说话人回合**（`turn` 一行，11 词），
  `W: Rush hour…` 是另一个回合，必须另起一行；`dialogue_beat` 只给「跨 speaker 的短反应串」。

### 4.5 多个信号打架时的优先级

```text
⓪ 先判块类型：对话块默认「同 speaker 连续回合 = turn 一行」；独白块默认「一个原子一行」
① 44 词是硬上限：任何 merge 都不能让一行 > 44 词（单个原子本身 > 44 除外，记 long_atom）
② 独白块只有 pure_response/fragment/compact_qa 能推翻默认拆；turn/dialogue_beat 非法
③ 对话块可用 turn + 四类；每类还有自己的上限（turn ≤44 / 段尾 ≤6 词 / 整行 ≤24 / ≤30 词与 ≤6 原子）
④ merge 命中但加上去超上限 → 上限优先：保持独立成行，notes 记 over_cap: <原子 id>
⑤ 拿不准 → 拆；不要为了「顺口」硬找一个 merge 理由
```

**为什么上限优先**：一张卡超过 44 词（或问答/反应串超过各自上限），学习者一遍听不下来，等于把两卡的内容压成一卡；
而一个完整句单独成行，至少还能靠「这是下一句」的语流提示。两害相权，宁可留一个 over_cap，也不造超容行。


---

## 5. 对话场景

前提：先过 §4 的单听可懂测试；下面几条是对话里的**具体化**，不是替代。

### 5.1 默认单位：连续同 speaker 的原子 = 一个回合 = 一行（`turn`）

对话块先看 `speaker`：**同一说话人的连续原子合成一个回合**（`merge:"turn"`，整行 ≤44 词）；
**换 speaker 才是断点**。不要把一个说话人的连续 2+ 句逐句拆成多张卡——这是 0.0.10 端到端冒烟
（卷1 正文被拆到 125 行）暴露的问题，也是用户 2026-11-26 的 2 条「并」的来源。

- 回合 ≤44 词 → `turn` 一行；>44 词 → 按句界拆（拆点仍在原子边界）。
- 回合之间可以用 `pure_response` / `fragment` 把段尾短回应并进来；
  `dialogue_beat` 只给「跨 speaker 的短反应串」，不是替代 turn。
- 例（b30 实测）：`[83,84,85]` W 连续三句 31 词 → `turn`；段尾 `M: I agree.` 2 词按
  `pure_response` 并入 → 整行 `[83,84,85,86]` 33 词，一行一张卡，该行标 `pure_response`。

反例（0.0.8 的旧做法，已作废）：把 `Hey, Mary.` + `You seem to be very much annoyed.` + `What happened?` +
`Rush hour in this city is killing me.` 四句并成一行（19 词）。0.0.10 下：`Hey, Mary.` / `You seem…` /
`What happened?` 都属于 M 的**同一个连续回合**（若中间没有 W 插话）→ `turn` 一行；
`Rush hour…` 换 W → 断点，单独一行（或按 dialogue_beat 规则另行判断，但它不是同一个 turn）。

### 5.2 紧凑问答 → 可并（`compact_qa`）

问 + 短答（≤10 词定性回答）可以锁一行，理由见 §4.4 ③：问句单听没有答案，答句单听没有问句，合起来才闭环。
机检：组内至少一个 `?`、问句在前回答在后、整行 ≤24 词。

### 5.3 短反应串 → 可并（`dialogue_beat`）

一串 ≤6 个短回合、整行 ≤30 词、组内 ≥2 speaker、合起来只是一个反应节拍，才允许 `dialogue_beat`。
注意它和 `turn` 的分工：同一 speaker 的连续回合用 `turn`；只有**跨 speaker 的反应串**才用 `dialogue_beat`。
不要拿它当「对话压缩器」。

例（用户 2026-11-26 实测接受）：
`W: Then why did he have the operation?` + `M: Because his girlfriend wanted him to.` +
`W: What?` + `W: That's so superficial and selfish of her.` → 4 原子、21 词 → 一行。

### 5.4 回答后的长解释 → 从解释首句断开

如果回答之后紧接着是长篇解释（≥ 2 个原子或 ≥ 25 词），把解释**整段推到下一行**；
解释本身如果还能拆成多个自足句，再按 §4.2 拆。

- 参照：`Hmm, could you send me his number, please?` + `Sure thing.` + `Just tell him exactly what you are looking for, and I'm sure he will find something good.`（17 词）
  —— 解释只有 1 个原子、17 词，不够"长篇"，可以留在同一行；如果后面还有第二个解释原子，就必须切开。

---

## 6. 独白场景

独白块（无 speaker / 只有一种 speaker 的新闻、篇章）没有换人提供天然停顿，最容易偷懒成「一人一大段一卡」。
**独白块禁止 `turn` / `dialogue_beat`**：这两类在 `assemble.py` 里会直接 `merge_invalid` exit 1。

### 6.1 打破独白惰性

**禁止**因为"同一个说话人一直在说"就把整段塞进一行。独白没有「说话人回合」这个单位
（`turn` 不适用），行边界由**句子能不能独立听懂**决定，默认一个原子一行。

### 6.2 容量：目标带 6–44 词，硬上限 44

| 指标 | 值 |
|---|---|
| 目标带 | **6–44 词**（大多数行落在这里） |
| 短句下限 | 自足的完整句可以短到 **4 词**；< 4 词只可能是纯回应（应已并入） |
| 硬上限 | **44 词**：合并后超过就不许并；单个原子本身 > 44 词不拆、记 `long_atom` |
| 历史基线 | 0.0.8 黄金清单 71 行正文：最短 8、中位 25、最长 44（旧「宁并勿拆」口径下的分布，仅作对照） |

**操作规则**

1. **默认一个原子一行**，先按 §4 判；独白块只有 `pure_response` / `fragment` / `compact_qa` 三类能并，
   `turn` / `dialogue_beat` 非法。
2. **合并后 > 44 词** → 找断点，不许交；找不到断点就保持拆（并记 `over_cap` 说明原因）。
3. **6–44 词之间** → 仍按 §4 判：跨话题/换主体要拆；同一论证步骤且段尾命中 merge 才可留。
4. **< 4 词** → 只可能是纯回应 / 片段，按 §4.4 ① / ② 并进相邻行（段尾 ≤6 词）。
5. **跨句粘连检查**：一行的首句或末句如果是 `This means…` / `The reason…` / `These findings…` / `That's why…`
   这种**靠上文才成立**的句子，先问「它离了上文还能听懂吗」：
   - **能听懂**（自带主语、自带结论）→ 单独成行（0.0.10 默认）；
   - **听不懂** → 也不能靠「指代悬空」直接并：0.0.10 已删除该白名单。独白块只有段尾恰好是
     ≤6 词的纯回应/片段，或整组命中 `compact_qa`，才允许并；否则拆，并在 `notes` 说明。
     卷1 例：`These findings suggest that people should change how they present themselves depending on their social goal.`
     （16 词）自己是完整结论 → 单独一行；
     `This means people are less willing to collaborate with them.`（11 词）与 `Modesty may be key when cooperation is essential.`（8 词）
     0.0.10 下**各成一行**：后者自带主语和条件从句，是自足格言式结论，不再因为"只有 8 词"被并走。


### 6.3 一卡一个"核心画面"或"一个论证步骤"

不是"一句话一卡"的机械版，而是：

- 叙事（新闻）：一个**事件片段**（谁做了什么 / 结果如何）；
- 说明（篇章）：一个**论证步骤**（论点 / 论据 / 反例 / 结论各一步）；
- 对话：一个**说话人回合**（见 §5.1；同 speaker 连续原子按 `turn` 合成一行）。

**注意**：这一条是"别把两个画面塞一张卡"的**下限**，不是"同画面就并"的许可；默认仍是 §4.2 一句一行。

---

## 7. 类型与进卡组（速查，不是你的活但要懂）

| 类型 | 判定依据（来路） | 进卡组 | 位置列 |
|---|---|---|---|
| 开场 | part 标题行（`## College English Test…`），补句末句号 | 否 | `开场` |
| 报幕 | Section / 篇目标题行，补句末句号（`Section A.`、`News Report 1.`） | 否 | `Section A` / `Section A / News Report 1` |
| 指令 | ① 一个 `Directions` 块的**全部原子拼一行**（文本以 `Directions: ` 起）；② 一条 `Questions X and Y are based on …` = 一行 | 否 | ① `Section A` ② `Section A / Q1-2` |
| 正文 | 你在处理的这些原子，分组后的每一行 | **是** | `Section A / News Report 1 (Q1-2)` |
| 题目 | 每个 `Question N. …` 单独一行 | **是** | `Section A / Q1` |
| 结尾 | `That's the end of Listening Comprehension.` | 否 | `录音结尾` |

**两条最容易搞混的（卷1 用户亲口裁决过）**

- `Questions 1 and 2 are based on the news report you have just heard.` = **指令**（否），单独一行；
- `Question 1. How did Tariq make corn taste better?` = **题目**（是），**每题单独一行**，绝不合并题干。

顺带记一个口径：位置列里的 Q 范围用**半角 `-`**（`Q1-2`），清单尾部区间用 **`–`（U+2013）**。两套符号不通用。

---

## 8. 边界铁律（八条，破一条就是废件）

1. **不跨块合并。** 块 = 一个篇目。`### Conversation 1` 的最后一句和 `### Conversation 2` 的第一句
   永远是两行，哪怕语义上"接着聊"。
2. **不把报幕并进正文或指令。** `News Report 1.` 是**单独一行**（0.0.8 黄金 004 行）。
   早期版本曾把它并到上一段末尾，用户明确否决（2026-11-20，"报幕部分单独拉出来"）。
3. **不把 `Question N.` 并进 `Questions X and Y…` 指令行。** 前者进卡组、后者不进；混一行会让
   「进卡组」这一列失去意义，也违反「每题单卡」。
4. **不改字。** 不纠错、不润色、不补主语、不把 `Um` 改成 `Uh`、不把 `centre` 改成 `center`、
   不把 `That's the end of listening comprehension.` 的大写 l 改掉。
5. **不重复、不丢原子。** 每个原子恰出现一次，顺序与本块输入一致。
6. **不造行。** 音频里可能有原文没记的语音（例如卷2 开头约 66 秒的中文念白），
   **不要**为它编文本、也不要给它留空行——切分文本只覆盖原文记录的内容。
7. **不二次分组。** 你只管自己这一块；发现邻块有问题，写进 `notes`，不要动别人的原子。
8. **不按"一人一段"切，也不按"一句一律一卡"硬套。** 判据只有"能不能独立听懂"：
   同一说话人连说 5 句可能就是 5 张卡；不同说话人的短问答可能合成 1 张卡（`compact_qa`）。

---

## 9. 判断流程（照这个顺序走，不要跳步）

1. **读一遍块内全部原子**（不写任何东西），在脑子里标出：谁在说、话题在哪拐弯、哪里有问答、
   哪里有纯回应/片段、哪里是短反应串。
2. **默认一个原子一行**：每个原子各自成组，先不管长短。
3. **逐组看段尾**：这一组如果要并，段尾是不是 ≤6 词的纯回应（`pure_response`）或片段（`fragment`）？
   是 → 标 merge；不是 → 保持拆。
4. **查问答/对话短串**：问句 + ≤10 词回答、整行 ≤24 词 → `compact_qa`；
   ≤6 个短回合、整行 ≤30 词、≥2 speaker → `dialogue_beat`。
5. **查必须拆的五条**（§4.3）：话题/论证切换、时间/场景跳转、≥3 信息点堆叠、超 44 词、对话实质回合。
   发现并过头 → 拆回去。
6. **查字数**（§6.2）：每行 6–44 为目标、最低 4（且必须自足）；< 4 回第 3 步；> 44 回第 5 步
   （单原子 > 44 记 `long_atom`）。
7. **查 §8 八条铁律**，逐条打勾。
8. **查连续性**：把所有 `segments[].atoms` 按序摊平，必须等于本块输入的 `atoms` 的 id 序列。
9. **写回执**（原子写）：单原子组省略 `merge`；多原子组写五类之一（先按 §4.0 判块类型）。`notes` 一句话说清
   "为什么这么分"（≤ 40 字）。
10. **回给调用方一行**：`done <回执绝对路径>` + 一句备注（≤ 120 字）。**不要贴回执内容。**

---

## 10. 反过拟合：只写判据，不写个案（5 正例 + 4 反例）

0.0.10 的判据是「先分块类型 + 五类白名单」：

1. **先判块类型**：对话块（speaker 同时有 M/W）默认同 speaker 连续回合 = 一行（`turn`）；
   独白块默认一个原子一行。
2. **只有五类 merge 能并**：`turn`（仅对话块，同 speaker、整行 ≤44 词）/
   `pure_response`（段尾 ≤6 词回应）/ `fragment`（段尾 ≤6 词片段）/
   `compact_qa`（问 + 答，整行 ≤24 词）/ `dialogue_beat`（仅对话块、≤6 原子、整行 ≤30 词、≥2 speaker）。

0.0.9 的「指代悬空」不是判据，也不是白名单；`But / However / Then / So / This / They…`
只是**找断点的线索**，不决定并还是拆。

**禁止**把个别句子的命运写成规则：不许写「`But` 开头一律拆」、不许写「`He/They` 开头一律并」、
不许写「寒暄一律并」、不许写「某篇某句原来说这么切」、不许拿 0.0.8 黄金清单当答案背诵。
下面的例子只用来校准判法，不是句子清单。

### ✅ 正例 1：自足的新论证步骤 → 单独一行

```text
... The first theory is that language started with people making different sounds ...
12. The other main theory, which is more recent, is that people started off by gesturing,
    pointing at things with their hands and imitating actions using their bodies.
```

**判法**：27 词，自带主语（`The other main theory`）和完整谓语；听到就知道在讲"第二种理论"，
不需要上一句也能懂；同时这是**论证步骤切换**（从第一种理论切到第二种）→ **单独一行**。

### ✅ 正例 2：对话块同 speaker 连续回合 → `turn` 一行

```text
W: 6. Oh, well, that's ridiculous, isn't it?
W: 7. It was noticeable, sure, but it wasn't an ugly scar.
W: 8. It never occurred to me that he should have it taken care of like that.
M: 9. I agree.
```

**判法**：`[83,84,85]` 是 W 连续三句、31 词 ≤44 → `turn` 一行（同一说话人的连续回合不拆）；
段尾 `M: I agree.` 2 词命中回应短语表，按 `pure_response` 并入 → 整行 `[83,84,85,86]` 33 词。
**注意**：`[83-86]` 混了 W 和 M，本身不能标 `turn`（turn 要求组内 speaker 一致）；
最终这一行标 `pure_response`，但卡片是一行、33 词，与用户 070 的要求一致。

### ✅ 正例 3：纯回应 / 片段 → 段尾短东西并进上一行


```text
M: 4. Oh, well, that's ridiculous, isn't it?
W: 5. It was noticeable, sure, but it wasn't an ugly scar.
W: 6. It never occurred to me that he should have it taken care of like that.
M: 7. I agree.
```

**判法**：段尾 `I agree.` 2 词、命中固定回应短语表，整行 33 词 ≤44 → `pure_response`，四句一行。
`Yeah, it's more convenient.`（5 词）带新信息、不在短语表 → **不能**标 `pure_response`，单独一行。
`Because his girlfriend wanted him to.`（6 词、首词 Because）→ 可标 `fragment`（也可标 `compact_qa`）。

### ✅ 正例 4：紧凑问答 → 锁一行（`compact_qa`）

```text
27. Then why did he have the operation?
28. Because his girlfriend wanted him to.
```

**判法**：问句 7 词、答句 6 词；答句是"没有问句的答案"，问句是"没有答案的问题"，
合起来 13 词才是完整认知闭环 → 一行，`compact_qa`（≤24 词、问句在前）。

### ✅ 正例 5：对话短反应串 → 一行（`dialogue_beat`）

```text
W: 24. Then why did he have the operation?
M: 25. Because his girlfriend wanted him to.
W: 26. What?
W: 27. That's so superficial and selfish of her.
```

**判法**：4 原子、21 词、W/M 两种 speaker，合起来是一个反应节拍（追问→解释→惊讶→评价）
→ 一行，`dialogue_beat`（≤6 原子、≤30 词、≥2 speaker）。用户 2026-11-26 实测接受。

### ❌ 反例 1：把完整句当「指代悬空」并走（0.0.9 用户实测纠正）

```text
In Egypt, researchers have found buildings from around 3,500 B.C. containing the remains of animals,
including elephants, which were not native to Egypt.
But life wasn't easy for these animals.
They probably had short lives, and the remains show evidence of severe injuries.
```

**错法**（0.0.9）：`these animals` / `They` 指代上文 → 三句并成一行（43 词）。
**为什么错**：第 2 句 7 词、第 3 句 13 词，都超过 `pure_response`/`fragment` 的段尾 6 词上限；
两句都是带新事实的完整句。0.0.10 删除指代悬空白名单 → **三句各成一行**。
（`But life wasn't easy for these animals.` 和 `They probably had short lives…` 用户明确要求拆。）

### ❌ 反例 2：把「两个完整句」标成任一类 merge

```text
He knew it tasted good, but butter made it better.
His passion for produce got him a position as South Dakota's official Corn Ambassador.
```

**错法**：两句都讲玉米/黄油，看起来"同一件事"，就并成一行并硬标一个 merge。
**为什么错**：这是**独白块**——段尾 14 词 >6（`pure_response`/`fragment` 不合法）；组内没有问句
（`compact_qa` 不合法）；独白块没有 M/W（`turn` / `dialogue_beat` 直接非法）→ 五类全部不合法，
必须各自成行。

### ❌ 反例 3：把 `But` 开头当铁律，整句并进上一行

```text
Studies have shown that people who appear to be wealthy tend to be considered more intelligent, disciplined, and competent than those who do not.
But new research found that people believe someone who shows off their social status cares more about benefiting themselves than helping others.
```

**错法**：看到 `But` 就认定"这是转折、必须跟上一句一起听"，把两句并成一行（24+22 = 46 词，还超了 44）。
**为什么错**：第 4 句自带主语 `new research`、自带结论，单听完全成立；段尾 22 词也不在任何 merge 的
段尾 ≤6 词范围内。→ **第 4 句单独一行**。

### ❌ 反例 4：把四个短回合并成一张卡（"短就该并"的旧习惯）

```text
M 1. Hey, Mary.
M 2. You seem to be very much annoyed.
M 3. What happened?
W 4. Rush hour in this city is killing me.
```

**错法**：四句都很短，就并成一行（0.0.8 黄金 039 行，19 词）。
**为什么错**：第 2 句是自足观察（7 词），第 4 句是**实质发言回合**（8 词，带来新信息"堵车要命"），
两句各自都能独立听懂；`dialogue_beat` 只给「短反应串」，不是把一段对话压成一张卡。
0.0.10 的正确结果：`Hey, Mary.`（无信息→可并入）+ `You seem…`（自足→一行）+
`What happened?`（追问→并入该行）= 一行 11 词；`Rush hour…` 单独一行。

---

## 11. 卷1 与卷2 的差异（0.0.7：转写成 contract v1 后消失）

0.0.7 起原文先统一转成 contract v1（`## [开场]` / `## Section X` / `### Directions` / `### 篇目 [Qx-y]` / `[META]` / `## [结尾]`），两卷交到你手上的 `blocks.json` 同形。下表只解释**历史原文**的排版差别（旧格式已停用），**不要据此判断**——你只看 E2 给的块。

| 项 | 卷1（旧原文） | 卷2（旧原文） | 你要做什么 |
|---|---|---|---|
| 开头 | 直接从旧一级标题开始 | 先有「试音部分」标题 + 6 个试音原子 | **不归你**：转写后都进 `## [开场]` 的编号句；旧格式已停用，E2 不再有 `试音` 位置 |
| Directions | 没有 `### Directions` 标题，5 个原子直接跟在 Section 后 | 有独立 `### Directions` 标题块 | **不归你**：转写后两卷都有 `### Directions`；块标题不另成行 |
| 篇目标题 | `### News Report 1` / `### Conversation 1` / `### Passage 1` | 同 | 不归你；0.0.7 起必须带 `[Qx-y]` |
| 编号原子 | 154 个 | 153 个（试音 6 + Directions 15 + 正文候选 132） | 你只看自己块里的 |
| 题干与结尾 | `Question N.` 25 条；结尾句在 `## [结尾]` 下 | 同 25 条；结尾句本来就在最后 | 不归你；**不要**改大小写或改字 |
| 标点习惯 | `marked A, B, C and D`（无牛津逗号）、Section A/B 用 `centre`、Section C 用 `center` | `marked A, B, C, and D`（有牛津逗号）、一律 `center` | 不归你；**不要**统一 |

**卷2 的一个实测事实（给流程用，不是给你改）**：卷2 音频开头约 66 秒是一段**中文念白**
（`2025年上半年,全国大学英语四级考试听力部分马上开始。` / `请戴上耳机。` / `录音开头有一段视听声音,约一分钟。` /
`听力考试将在试音结束后正式开始。` / `现在开始试音。`），之后还有 `试音结束。` 等过渡句才进标题。
**0.0.7 的 prompt 要求这些真实人声进 `## [开场]`**（没被记录的语音仍不要造行）；旧 md 里它们不在，所以旧 md 必须重转。你的任务是分组原文里的原子，**不要**给没记录的语音造行（§8 铁律 6）。

---

## 12. 拿不准时怎么办

- **拿不准就别自创**。把疑问写进 `notes`，仍然给出一个你认为最合理的分组（不要交空 `segments`）。
- 以下情况属于"结构异常"，必须写进 `notes` 并保持原样，不要修：
  - 块内 `speaker` 突变或整块都为 `null` 但文本明显是对话；
  - 原子链看起来缺了一句（上一句的答案找不到问句）；
  - 某个原子异常长（卷1/卷2 实测最长原子都是 **37 词、0 个 > 44 词**；若真出现 > 44 词的原子，**不许拆**，按 §1.4 记 `long_atom: <id> <词数>` 交回）。
- 你**不需要**：判断类型、算行号、起文件名、检查清单格式、跟复核员沟通。这些都不是你的活。
- 你的回复只允许一行：`done <回执绝对路径>` + ≤ 120 字备注。

---

## 13. 交回执前的 9 个自检（逐条打勾，缺一项重做）

1. 摊平 `segments[].atoms` == 本块输入的 id 序列（顺序、数量、无重、无漏），
   **且回执里没有任何 `split` / `parts` / `sub` 字段**（A9：出现即 exit 1）。
2. 没有出现本块以外的 id。
3. 每组内 id 连续升序。
4. 先按 §4.0 判块类型：对话块按同 speaker 连续回合（`turn`）；独白块默认一个原子一行。
   **只有五类 merge 才允许出现多原子组**，且多原子组都写了 `turn` / `pure_response` /
   `fragment` / `compact_qa` / `dialogue_beat` 之一（单原子组省略或 `null`）；独白块没有 turn/dialogue_beat。
5. `pure_response` / `fragment` 的段尾原子 ≤6 词（回应还要命中固定短语表）；
   `compact_qa` 整行 ≤24 词且问句在前；`dialogue_beat` ≤6 原子、整行 ≤30 词、组内 ≥2 speaker。
6. 每行 **4–44 词**（6 词以上为目标；4 词只允许自足完整句；题目不归你）；
   没有为了「顺口」把两个完整句并一起。
7. `But/He/They/This` 开头的原子：按块类型 + 五类 merge 与 44 词上限判，**不按开头词判**；
   并的写清命中哪一类，拆的写清为什么完整。
8. 没有改字、没有复制、没有自己补词（逐字比对输入原子）。
9. 回执是合法 JSON，字段齐全，`version=1`，`role="segmenter"`，`block`/`doc` 与任务一致。
