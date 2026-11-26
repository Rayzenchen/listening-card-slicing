---
name: listening-card-slicing
description: 把「一段听力音频 + 已分段、无时间戳的原文」做成墨墨牌组里的成套听力卡。两个入口：新材料从 E1 起（选择材料/牌组门 → 预检 → 汇编 → 译文与 6 列清单定稿★ → 切分 → 分包 → 收尾）；继续已交付包从阶段二快路径起（E9 起：做成卡 / 同步）。12 个确定性步骤（E1–E12）、2 道人工闸门（E5 清单定稿、E10 首个新牌组 App 样卡确认）+ E1 选择门；共 3 次用户回话（老牌组 E10 自动放行，不算回话）。不转写、不建牌组、不删卡。收到「切这个音频」「做成卡」「写进墨墨」「继续处理」时使用。
---

# 听力训练卡 · 音频 → 本地包 → 墨墨牌组

> 版本：0.1.0（GitHub 发行包）。产物契约以 `scripts/` 为准；开发测试与含真实转写文本的夹具未随发行包，本文随 0.1.0 同步。

**0.1.0 变化（三项流程改进；E1–E12 编号 / 产物契约 / 脚本接口不变）**

1. **E1 选择门（D3）**：正式开工前先跑 `preflight_choices.py --work "$W" --json`，只读列出材料（`docs` / `needs_user`）与云端牌组候选（`decks` / `deck_error`）；再用 `ask_user_question` 问两件事：**多选处理对象**（末项「全部处理」）+ **单选牌组**（3–5 个 `名称（已有 N 张卡）` + 「自己填」；`decks=[]` / `deck_error` 时只给自填或沿用）。`bootstrap.py --select <ids> --deck-name "<精确名>"` 把选择存进 `run.json`；E10 默认使用，不再重复问。云端不可用**不阻塞**材料选择与阶段一。
2. **E9 中文校对「表格 + 直改」（D1）**：校对员回执可含 `zh_fixes`（整张卡中文行数组，`len(zh) == 英文句数`）；`cards_apply.py --review` 校验并渲染 `$RUN/docs/<doc>/zh-fixes.md`（卡片 / 当前 / 建议 / 理由），`cards.json` **原样不动**。用户敲定后 `zh_fix.py apply` **直接改** `translations.json`、`$RUN` + 包内 + 产出区 6 列清单；再 `approve.py` 重新定稿 → `cards_apply.py --refresh-zh` 更新 `cards-base.json` / `cards.json` → `gate e9` → E10 plan。**不重跑 E3/E5/E6/E7/E8**；用户可全部接受 / 只接受某几张 / 自己改措辞。
3. **E10 样卡闸门分 first / veteran（D2）**：`plan` 记录目标牌组 `card_count` 与 `gate.mode`。**first**（空/新牌组）直接写 1 张样卡（不再先要 `approve2`，会话框**不展示卡片正文**）→ 请用户去 App 看 → `approve2.py --sample-no <NNN> --chapter-ok` → `gate e10` → `push`。**veteran**（牌组已有卡）自动跳过样卡与 `approve2`，直接 push；`diff_pending` 仍默认跳过，覆盖需用户明确 `approve2.py --veteran --updates-ok` + `push --allow-update`。`first_sample_pending` 时重跑 `plan` 仍按 first：只补 `approve2`，**不重写样卡**，成功 push 后才回 veteran。

**0.0.10 历史**：分组先分对话/独白（五类 merge + `merge_review` 复核）；重编分组保留译文记忆（整行 + 句级）；E6 串行锁 / `--timeout 3600` / 实时 `cut-cutter.log` / 混排与低覆盖护栏；cutaudio 1.1.1；扇出默认 `workflow` 每波 ≤8。细节见各角色卡与 `references/runtime/pipeline.md`。
**0.0.9 历史**：分组从「宁并勿拆」改为「能独立听懂就拆」；`材料区/` 支持子目录与软链接/同内容去重。

把一段听力音频和它**已分段、无时间戳**的原文，做成**本地一包片段 + 云端一套卡**。

- **本地包**：`产出区/<stem>_音频切分（进牌组）/` —— 只含「进卡组＝是」的 `<NNN>.wav` 与一份 6 列清单（末列是译文）。
- **云端**：墨墨某个牌组下一个章节里的 K 张卡，每张挂自己那段音频。
- **一句话**：确定性活走 `scripts/`，语义判断派 6 种短任务；E1 选择门、2 道人工闸门（E5 / E10 first）必须停。

## 边界（不在本 Skill 里）

- **不做纯转写 / 语音识别**：没有原文就不开工；识别只用来定位时间，不改写文本。
- **不建牌组、不能删**：牌组必须用户先在 App 建好（章节可以新建）；云端没有删除接口，只能 `create` / `update`，写前必须看样卡。
- **不改用户原文、不装环境、不动 `系统/`**。

## 2 个入口（先看磁盘，不看对话历史）

```bash
ls 材料区/                                            # 有没有新材料
ls -d 产出区/*_音频切分（进牌组）/ 2>/dev/null        # 有没有已交付包
```

| 入口 | 用户说 | 起点 | 终点 |
|---|---|---|---|
| **A · 新材料** | 「切这个音频」「做成卡」「一条龙」 | **E1 选择门**（`preflight_choices.py` → `ask_user_question` → `bootstrap.py --select … --deck-name …`） | 默认 **E8 收尾停**；说「做成卡」继续 E9–E12 |
| **B · 继续已交付包** | 「做成卡」「写进墨墨」「加译文」「同步一下」 | **阶段二快路径（E9 起）**（`bootstrap.py --phase 2`） | E9–E12；只同步走 E12 |

- 两边都有、用户没说清 → 一句话问「先切新的，还是给已有包制卡」；**入口 B 必须加 `--phase 2`**：已有包按复用处理（`reuse_pack=true`），不报同名冲突；阶段二不需要 `gate e1` PASS。拿不到 `doc.json` 就问用户要件路径，**不要手拼**。旧包「覆盖」= `--archive-existing`（改名 `<pack>.prev-<ts>`，不删内容）；「保留」= 本次不选该文档（阶段二就拿不到 `doc.json`）。

## 两个阶段 · 12 步

| 阶段 | 步 | 名称 | 终点 |
|---|---|---|---|
| 阶段一 | E1–E2 | 选择门 + 预检 + 解析 | `run.json`（含 `deck_name`）/ `blocks.json` / `dispatch.json` |
| 阶段一 | E3–E4 | 分组 + 汇编 | 5 列清单 + 复核回执 |
| 阶段一 | E5 | 译文 + 6 列清单 + 定稿 ★闸门 1 | `translations.json` / 6 列清单 / `approve.json` |
| 阶段一 | E6 | 切分 | `cut-report.json` + 切分结果 ZIP（中间物） |
| 阶段一 | E7 | 分包 | 包 + `gate e7` PASS |
| 阶段一 | E8 | 收尾 | 包 + `report.md` + `gate e8` PASS |
| 阶段二 | E9 | 卡片 | `cards.json` |
| 阶段二 | E10 | 云端计划 + 样卡（first ★闸门 2 / veteran 自动放行） | `markji-plan.json` + `approve2.json`（first）+ 样卡回执（veteran 为 `skipped`） |
| 阶段二 | E11 | 批量写入 + 回读 | 云端 K 张 + `markji.json` |
| 阶段二 | E12 | 同步按需 | `receipts/markji-sync.json` |

## 6 条铁律

1. **顺序 + 现场重算**：按 E1→E12 走，每个 E 的 `gate.py` PASS 才进下一步；关卡从磁盘/云端重算，不信「上次过了」、不信子 agent 自报、不信对话。
2. **原文是唯一文本真源**：不纠错、不改写、不润色；只允许删不发音的排版标记、按规则分组、补句末句号。清单是它的唯一投影；E5 只往后加「译文」列。
3. **用户决定不代答**：E1 选择材料/牌组、E5 清单定稿、E10 首个新牌组样卡确认；`approve.json` / `approve2.json` 只由脚本写，批准绑内容 sha。老牌组 E10 自动放行，不算回话。3 次固定回话见下表。
4. **判断给短任务，确定性给脚本**：回执文件是唯一事实；不手改产物、不手敲云端写入。改译文派译文复核员，改分组回 E4（E6 才切音频）；E9 中文问题写 `zh_fixes`，用 `zh_fix.py` 直改（不重跑上游）。
5. **材料只读、系统不动、包干净**：`材料区/` 不删不改；`系统/` 只调用；包里只有清单 + `<NNN>.wav`，没有索引、子目录、ZIP 副本；不手删包。
6. **证据落盘、汇报只报事实**：回执/清单/报告都在 `$RUN`；回复只报路径、行数、关卡结果；不打印 Key / token / 带签名 URL。

## 2 道人工闸门 · 3 次固定回话（0.1.0）

| # | 时机 | 用户要回 | 我们做什么 | 不做的后果 |
|---|---|---|---|---|
| 1 | **E1 选择门（开工前）** | 这次处理哪几对材料 / 卡片写进哪个牌组 | `preflight_choices.py` 只读列材料 + 云端候选 → `ask_user_question`（材料多选，末项「全部处理」；牌组单选，3–5 个候选 +「自己填」）→ `bootstrap.py --select … --deck-name …` | 没选不建 run；不能替用户挑牌组，也不能建牌组 |
| 2 | **E5 交付 6 列清单后（切分前）** | 分组/译文对吗 / 改译文 / 改分组 | 认可才 `approve.py`（定稿）；改译文走复核 `fixes`；改分组回 E4 重汇编 | 没有 `approve.json`，`cut_stage` 不切（E6 进不去） |
| 3 | **E10 首个新牌组（first）样卡写入后** | 去 App 看过样卡，可以批量吗 / 差异卡覆盖吗 | first：`sample` 先写 1 张（不展示正文、不先要批准）→ 等 App 确认 → `approve2.py --sample-no … --chapter-ok` → `gate e10` → `push` | 没在 App 确认就 push = 越线；云端没有删除接口 |

- **E10 不再有「写样卡前批准」的旧回话**：`approve2.json` 是 **App 看样卡之后的确认令牌**（first），或 **覆盖 `diff_pending` 的授权**（veteran）。
- **老牌组（veteran）**：E10 样卡闸门自动放行，不写样卡、不要 `approve2`，直接 push；`diff_pending` 默认不动，用户明确同意才覆盖。**不算一次回话**。
- **E1 只问选择门这两类**（材料 / 牌组）；同名包冲突、配对歧义在选完材料后按 `needs_user` 追问（覆盖 = `--archive-existing` / 保留 = 本次不选）。缺原文、环境缺项是**阻塞**，报告后停。
- 指令短不等于可以跳过这些停点；云端没有删除接口，写错只能整张覆盖。

## 运行约定

```bash
W="$(pwd)"; [ -f "${W}/系统/tools/cut" ] && [ -d "${W}/材料区" ] || echo "STOP: 先问工作区路径"
S="$HOME/.agents/skills/listening-card-slicing/scripts"
MK="$HOME/.agents/skills/markji-channel/scripts/markji_channel.py"
RUN="<E1 首行 RUN= 打出的值>"
```

- **每次 bash 调用都是新 shell**：`W` / `S` / `MK` / `RUN` 在每条命令里重新给；`RUN` 不凭记忆拼，重跑 `bootstrap` 会拿回同一个值（输入内容没变）。
- 参数以 `python3 "${S}/<脚本>" --help` 为准。**本文件与 `--help` 冲突时以 `--help` 为准**，并把冲突写进汇报。
- `gate.py` 判读：`rc=0` 且最后一行 `PASS` 才算过；`rc=1` 看 `FAIL:` 与上面的 `check … FAIL`；`rc=2` 是参数错。`e1` / `e8` / `phase1` 全局，其余带 `--doc`。路径只从 `run.json` / `doc.json` / `task.receipt` / `report.json` / `markji-plan.json` 取，**不自己拼**（尤其 ZIP：只取 `cut-report.json.zip.path`）。
- 多文档：E2–E4 逐文档；E5 译文任务默认 `workflow` 并行（每波 ≤8）；**E6 必须串行**（锁 `$RUN/.locks/e6-e7.lock`，并发第二个 cut/pack exit 3）；E7–E8 逐文档/收尾；**E9–E12 串行**（云端频控）。任何一步不知道从哪继续：`python3 "${S}/status.py" --run "${RUN}"` 看 `NEXT=`，再按顺序跑关卡。

**阶段一 · 选择门 + 预检 + 解析（E1–E2）**
- **做**：E1 先用选择门确认处理哪几对材料、写哪个牌组 → 收齐音频 + 原文 → 建/续 `$RUN` → 原文拆成块与原子，生成裁定任务单。 ｜ **产出**：`run.json`（含 `preflight` 对象与 `deck_name`）、`doc.json`、`blocks.json`、`dispatch.json`。
- **关卡**：`gate.py e1` → `gate.py e2`。 ｜ **失败**：缺原文/配对不明/同名包 → 按 E1 选择门与 `needs_user` 问；E2 报错（旧格式/不合契约）→ 贴 `ERROR:` 行 + 迁移提示，**不改原文**（旧 md 用新 prompt 重转）。 ｜ **谁做**：总调度 + 用户（E1 选择门）。

## E1 · 选择门、预检、建运行目录

**第 0 步 · 选择门（0.1.0 必做；正式开工前先问）**

```bash
python3 "${S}/preflight_choices.py" --work "${W}" --json
# 只读：不建 run、不写文件。末行 JSON 的 docs / needs_user / decks / deck_error 是事实
```

- `docs`：每个可配对材料一条（`id` / `stem` / `audio` / `text` / `existing_pack`），id 与 `bootstrap` 完全一致；`needs_user` 是配对歧义、缺文本、同名包等。
- `decks`：云端牌组（`id` / `name` / `card_count` / `chapter_count`）；Key 失效 / 网络错误 / 缺 markji-channel 时 `decks=[]`、`deck_error` 一句人话，**不阻塞材料选择与阶段一**。
- 有 `needs_user` 先解决或在提问里说明；缺原文、环境缺项是**阻塞**，报告后停。

用 `ask_user_question` 问两题（模板可照抄）：

```text
① 材料（multi_select=true）
   标题：这次处理哪些材料？
   选项：<doc1 stem>（音频+原文）
         <doc2 stem>（音频+原文）
         …
         全部处理（共 N 对）

② 牌组（单选）
   标题：卡片写进哪个牌组？
   选项：<名称>（已有 N 张卡）      ← 从 decks 里按材料特征挑 3–5 个
         …
         自己填（回复精确牌组名）
```

- 材料选「全部处理」= 所有 `docs`；只选部分就传对应 ids（**为审计建议总是显式传 `--select`**）。
- 牌组选「自己填」→ 追问一句「请回复牌组精确名称（要与 App 里完全一致）」；**不建牌组**、不猜。
- `decks=[]` 或 `deck_error` → 只给「自己填」+ 沿用旧 run 已有的牌组名（若有）；牌组不阻塞阶段一。
- 用户选定后：

```bash
python3 "${S}/bootstrap.py" --work "${W}" --select d01,d02 --deck-name "<牌组精确名>" --json
python3 "${S}/gate.py" e1 --run "${RUN}"
```

- `--deck-name` 写 `run.json.options.deck_name` + 平铺 `run.json.deck_name`（resume 不给则保留旧值）；**E10 默认用它，不再重复问**。
- 入口 B 加 `--phase 2`；覆盖旧包加 `--archive-existing <doc>`。旧 `existing_pack` 冲突仍按 `needs_user` 追问（覆盖 / 保留=本次不选）。
- 首行 `RUN=<绝对路径>`；末行是带前缀的 `JSON {…}`（解析时剥掉 `JSON `），其中的 `docs` / `needs_user` / `env` / `deck_name` 是事实；预检对象在 `run.json.preflight`（不再单独写文件）。**材料区递归扫描**：`材料区/` 下任意层级的音频/文本都能发现（跳过隐藏文件与 `.DS_Store`）；同一条材料出现在多处时只算一份——符号链接按 `realpath` 去重、同名同内容副本按 basename+sha256 去重，保留较浅路径（所以根目录软链接 + 子目录真文件不会变成两份，run key 与只有那份软链接时相同），被折叠的候选只记进 `run.json.preflight.materials.ignored` 与 `materials_ignored` 审计，不参与配对。**一份音频只配一份原文**：多候选按三级优先 `<stem>_听力原文` > `<stem>_原文` > 裸 `<stem>`，同级多个才问用户；其余候选记 `ignored_texts`（审计，不进 run key）。同一音频主名（stem）下出现**不同内容**的多个音频 → `needs_user: ambiguous_audio`，不猜。**单文档也不省选择门**：至少确认牌组名；配对唯一且无同名包时只需一句「按 `<doc>`（`<stem>`）处理 + 牌组 `<名>`」。聊天里粘贴的原文**不是稳定输入**：请用户把原文放进 `材料区/`（同名 mp3 + md，可放子目录），**不要**替用户在工作区另造文本文件；只有音频、没有原文 → 停下要文本（转写走 SOP，产出必须是 contract v1，见 §E2）。
- 环境缺项（模型 / ffmpeg / venv）是**阻塞**：报告后停，不自行造；`needs_review` 是 E6 的事，不在 E1。

## E2 · 解析原文（contract v1 / 块 / 原子 / dispatch）

```bash
python3 "${S}/parse_source.py" --run "${RUN}" --doc <doc> --json
python3 "${S}/gate.py" e2 --run "${RUN}" --doc <doc>
```

- **只认 contract v1**（语法/正反例/迁移见 [runtime/transcript-contract.md](references/runtime/transcript-contract.md)）：`## [开场]` / `## Section X` / `### Directions` / `### 篇目 [Qx-y]` / `[META] Questions …` / `Question N. …` / `## [结尾]`。旧格式（`# Part II…`、`## 试音部分`、裸结尾句、裸/编号型 `Questions`、无 `[Qx-y]` 的篇目）**直接 FAIL**：把 `ERROR:` 行 + 迁移提示贴给用户，**不改原文**；旧 md 用新 prompt（自备的 contract v1 原文）重转。
- 块 = 结构单元；原子 = **一行里的唯一编号片段**（去掉行首 `N、` 与 `M:`/`W:`；一行两个编号 FAIL；`**M:**`、反斜杠转义、代码围栏、解释句都 FAIL）。`dispatch.json` 一块一条任务，`need_task_blocks` 就是 E3 要派的块数。
- **结构壳可缺**：`## [开场]` / `## [结尾]` 各 0/1（0 只记 `WARN missing_title` / `WARN missing_ending`，有则至少 1 个原子）；`## Section X` 可缺（默认节 = 首个显式节的前一节，全缺 = A；不伪造 `Section A` 行）。**篇目必须有 `[Qx-y]`**，`[META]` 可选但写了必须与篇目 `q_range` 一致，题号可从 N 起但必须连续 +1。
- 字段、`format=contract-v1`、覆盖与 `source_spans` 见 [runtime/parse.md](references/runtime/parse.md)。

**阶段一 · 分组 + 汇编（E3–E4）**
- **做**：派裁定员把每个「正文」块分组成卡片行；汇编确定性片段 + 回执，出 5 列清单；复核员整块改判。 ｜ **产出**：`blocks/<block>.json`、`review-<n>.json`、`segments.json`（`manifest_sha256`）、`切分文本.txt`、`切分清单.md`、`manifest-5col.md`。
- **关卡**：`gate.py e3` → `gate.py e4`。 ｜ **失败**：缺回执只补缺块；回执非法改名 `.bad-<ts>` 重派；不手改清单/切分文本。 ｜ **谁做**：裁定员 / 复核员（短任务）+ 总调度。

## E3 · 派裁定员分组（一块一个 agent；workflow 每波 ≤8）

- **0.0.10 判据**：先分块类型——块内 `speaker` 同时出现 M/W = **对话块**；无 speaker 或只有一种 = **独白块**。对话块默认「连续同 speaker 的原子 = 一个回合 = 一行」（`turn`，≤44 词；回合超过才按句界拆）；独白块默认「一个原子 = 一行」。允许并五类：`turn`（仅对话块，同 speaker 连续回合，≤44 词）、`pure_response`（被并入的段尾回应 ≤6 词且命中固定回应短语表，整行 ≤44 词）、`fragment`（段尾片段 ≤6 词且首词命中片段线索表或 ≤4 词，整行 ≤44 词）、`compact_qa`（问句在前 + ≤10 词回答，整行 ≤24 词）、`dialogue_beat`（仅对话块，≤6 原子、≤30 词、≥2 speaker）；回执必须写 `merge` 理由。独白块只能用 `pure_response` / `fragment` / `compact_qa`；**删除 0.0.9 的「指代悬空」白名单**：完整句即使以 `He/She/It/They/This` 开头也默认拆。判据以 `roles/segmenter.md` 为准，不查黄金清单。
- 用附录① 列出 `receipt` 不存在的正文任务；**默认 `workflow` 派发**（每波 ≤8，模板 `references/runtime/fanout.md#e3`）：派前预建 `docs/<doc>/blocks/`、预渲染任务单到 `docs/<doc>/prompts/`；任务单按 `references/subagent-briefs.md#segmenter` 渲染（=`roles/segmenter.md`）。
- 派完**不 sleep、不轮询**（`workflow` 调用本身是 await 的）；返回后先跑 `python3 "${S}/gate.py" e3 --run "${RUN}" --doc <doc>`，再只补缺的块。

**关卡**：每个 dispatch 任务都有回执，信封齐全（`role=segmenter`），原子恰好按序划分、不跨块、不拆原子；非正文块多余回执只记 WARN。

## E4 · 汇编 + 复核

```bash
python3 "${S}/assemble.py" --run "${RUN}" --doc <doc> --json
# 有复核改判时（整块替换；只收 review-<n>.json 或 {"version":1,"doc":"<doc>","overrides":[...]} 两种形态）
python3 "${S}/assemble.py" --run "${RUN}" --doc <doc> --overrides "${RUN}/docs/<doc>/review-<n>.json" --json
python3 "${S}/gate.py" e4 --run "${RUN}" --doc <doc>
```

- 正文块缺回执 → exit 3（列缺失 task id，**不产半成品**）→ 回 E3。
- `assemble.py` 在 `segments.json` 写 `merge_candidates`（正文多原子行的 `n/block/atoms/words/merge/reason_missing`），并在覆盖 6 列清单前把旧 6 列归档成 `manifest-6col.prev-<ts>.md`（供 E5 译文记忆）；旧回执没有 `merge` 时标 `reason_missing=true`，不阻塞汇编。
- 复核员一份文档一个（`#reviewer`），**用 `workflow` 派**（两卷并行），写 `review-<n>.json`（`verdict=ok|fixed`，`overrides` 是整块替换）；多轮写 `review-2.json`… 不覆盖旧回执。输入 `MERGE_CANDIDATES` 就是 `segments.json` 顶层的 `merge_candidates` 数组（不另发文件）；回执必须给 `merge_review` 逐条覆盖全部候选——`keep` 写明命中五类中的哪类（`turn` / `dialogue_beat` 仅对话块，独白块只前三类），`split` 必须给对应整块 `overrides`。
- **关卡**：行 1..N 连续；原子按序覆盖无丢重；类型/进卡组/位置合法；`words` 重算一致；切分文本与 rows 逐行一致；清单表头/统计/区间/固定句逐字正确；复核回执合法且 `overrides` 已应用。`gate e4` 机检 `merge_review` 覆盖、`keep` 能对上同原子集合的正文行、`split` 有 override；旧 run（无 `merge`）只记 `reason_missing`，不强制重派 E3。

**阶段一 · 译文 + 定稿 → 切分 → 分包 → 收尾（E5–E8）**
- **做**：先翻译 + 6 列清单，用户对照译文敲定分组后定稿；**定稿后才**切原声 → 只保留「进卡组＝是」的片段成包 → cleanup → phase1 → report。 ｜ **产出**：`translations.json`、6 列清单、`approve.json`、`cut-report.json`、切分结果 ZIP（中间物）、`产出区/<stem>_音频切分（进牌组）/`、`report.md`。
- **关卡**：`gate.py e5`（译文 + 6 列 + 定稿）→ `gate.py e6`（cut）→ `gate.py e7`（pack）→ `gate.py e8`（全局收尾）。 ｜ **失败**：`zh` 行数 ≠ 英文句数 → 只重派那个任务；E6 `needs_review` 停问用户；残留先 `cleanup.py` 再带 `--reset`；包目录非空 → 问用户归档还是保留。 ｜ **谁做**：翻译员 / 译文复核员（短任务）+ 总调度（后台切分）+ 用户（E5 定稿）。

## E5 · 译文 + 6 列清单 + 用户定分组/定稿 ★闸门 1

```bash
python3 "${S}/status.py" --run "${RUN}"          # 先看 NEXT；E5 是译文与定稿
python3 "${S}/translate_prepare.py" --run "${RUN}" --doc <doc> --chunk 8 --json
# ↑ 默认复用译文记忆（--reuse-memory）；要全部重译加 --no-reuse-memory
# 派翻译员：附录①，锚点 references/subagent-briefs.md#translator（只派 tasks 里 MISS 的）
python3 "${S}/translate_apply.py" --run "${RUN}" --doc <doc> --json
# 派译文复核员（#translation-reviewer）写 translate-review-<n>.json；fixes 可多轮
python3 "${S}/translate_apply.py" --run "${RUN}" --doc <doc> --review "${RUN}/docs/<doc>/translate-review-1.json" --json
# ↑ `--review` 可重复；默认自动发现 translate-review-<n>.json 并按 n 升序全部累计应用
```

- 只译「进卡组＝是」的行：一句英文 = 一句中文（`len(zh) == sentence_count`）；先直译；他/她不加括号、不换人名；对话只在换人处 `M: ` / `W: `；题目行 `问题N：`。已有包且清单已是 6 列、译文齐全 → `translate_prepare.py` 直接反解出 `translations.json`（`ALREADY_TRANSLATED=1`），跳过派翻译员，**仍要派译文复核员**。
- **0.0.10 译文记忆（默认开，整行 + 句级）**：`translate_prepare.py` 先从 `translations-memory.json`（没有就从最新 `manifest-6col.prev-*.md` / 当前 6 列清单自动 `snapshot`；整行 entry 可带句级 `sentences:[{en,speaker,zh}]`，句数≠中文行数时不写）预填——整行 `row["display"]` 与记忆 `en` **逐字相等**直接复用；整行没命中且是**单句新行**时，按句级 `sentence_index`（句子文本去掉 speaker）命中旧中文，并按新行 speaker 重写 `M:`/`W:` 前缀；**多句新行不拼接**。其余行才按 `--chunk` 切任务；`translate-tasks.json` 增加 `prefilled` 与 `counts`。全部可复用 → `tasks=0`，直接 `translate_apply.py`。实测 150 keep：整行 86 + 句级 56 = prefilled 142、pending 8（1 个任务）；apply 后 `reused=142` / `newly_translated=8`。重编后旧 `translate/` 回执与旧 `translate-review-<n>.json` 按行号已失效，默认**一起**归档为 `translate.prev-<ts>/`（`--keep-receipts` 保留）；等翻译回执时**不要**重跑 `translate_prepare.py`（会归档已收到的有效回执），续跑加 `--keep-receipts`；排障用 `python3 "${S}/translate_memory.py" snapshot --run "${RUN}" --doc <doc> --json`。
- `translate_apply.py` 一次写 `translations.json`（唯一真源）与两处 6 列清单（`$RUN` + `产出区`，同 sha）；前 5 列逐字节不变，校验不过 exit 1 且一个字节都不写。0.0.10 起先把 `translate-tasks.json.prefilled` 并入 `merged`（`origin=memory`）再收回执：prefilled ∪ 回执必须恰好覆盖全部 keep 行，同一行两边都有 → exit 1（`reused row also translated`）；`translations.json.stats` 增加 `reused` / `translated`（CLI 输出另有 `newly_translated`）；缺回执只看未 prefilled 的任务。`manifest-5col.md` 保持 E4 冻结基线，E6 切分只绑它。
- **交付**（附录②）：`present` 6 列清单 + 统计 + 前 15 行；请用户**对照译文看分组**（第 1 次固定回话）。这时**还没切音频**：改分组、改译文都不会产生重切。
- 用户认可后才：

```bash
python3 "${S}/approve.py" --run "${RUN}" --doc <doc> --json
python3 "${S}/gate.py" e5 --run "${RUN}" --doc <doc>
```

- `approve.py` 读**$RUN 内的 6 列清单**（不存在才回退包内 6 列），把整份清单 sha 与 `rows:[{n,sha256}]` 行哈希一起绑进 `approve.json`（`mode=user`）；不是 6 列 → exit 3；**不手写 `approve.json`**。`gate e5` 含定稿检查：approve 之前跑会报 `manifest_finalized` FAIL，属预期；**PASS 才能进 E6**。
- 改译文/措辞 → 译文复核员 `fixes` → `translate_apply.py --review` → 重新交付 → 重新 `approve.py` → `gate e5`。包已出来后再改译文**不用重跑 E6/E7**：`translate_apply` 会同步包内 6 列与 `pack.json` 的 sha → 重新 approve → `gate e5` → 复跑 E8 收尾 → E9。改分组/类型/进卡组/原文 → 回 E4 重汇编 + 复核 → 重译受影响行 → 重新 E5 定稿（**不切音频**；E6 才切）；旧 `approve.json` 自动失效。
- 阶段一定稿后清单再改一个字节，`gate e9` 的 `manifest_finalized` 会重算行哈希并拦。

## E6 · 切分音频（后台长跑；E5 定稿后才跑；必须串行）

```bash
python3 "${S}/status.py" --run "${RUN}"          # E5 PASS 后 NEXT 是 E6
python3 "${S}/cleanup.py" --run "${RUN}" --doc <doc> --dry-run --json
# 后台跑（实测 25 分钟音频 cut≈14–15 分钟）：run_in_background=true；刚清理过才加 --reset
python3 "${S}/cut_stage.py" --run "${RUN}" --doc <doc> --stage cut --timeout 3600 --json > "${RUN}/logs/cut-<doc>.out" 2>&1
python3 "${S}/gate.py" e6 --run "${RUN}" --doc <doc>
```

- **前置是 E5 定稿**：`cut_stage.py` 会先查 `gate e5`；没有 `approve.json` 或 6 列清单没定稿 → exit 3，**不切**。
- **必须串行**：`cut_stage.py` 在 cut / pack 前抢 `$RUN/.locks/e6-e7.lock`（`flock LOCK_EX|LOCK_NB`）；第二个进程立即 exit 3（`另一个 cut/pack 正在跑…E6/E7 必须串行`）。`--stage all` 同一进程只拿一次锁；锁文件带 pid/doc/stage，`status.py` 会读。
- **实时日志**：cutter 的 stdout+stderr 行缓冲写 `docs/<doc>/cut-cutter.log`，`cut-report.json` / receipt 记 `cutter_log`；`status.py` 打 `CUT_RUNNING doc=… stage=… elapsed=… log=…` 与 `CUT_LAST_LOG=…`（`--json` 为 `cut_running` / `cut_last_log`）。
- **切前混排护栏**：`切分文本.txt` 含 CJK（中文行）时先探测 `cut --version`：< 1.1.1 → exit 3（提示升级 cutaudio 或改纯英文）；≥ 1.1.1 正常切，receipt `warnings` 记 `mixed_script_text=true`。
- **切后覆盖护栏**：`alignment.cut_mode == "edge"` 且 `alignment.coverage < 0.95` → exit 3（`对齐只覆盖 X% 音频…不要用这份切分`），保留 `cut-report.json` 排查；`wave` 模式 `matched < 0.9` 只 WARN。
- **收口只认 `cut-report.json`**：`status` / `zip.path` / `zip.verified` / `segments` / `issues` / `review_ignore` / `ignored_issues`；不从日志找「完成」。切前把 `keep=false` 的行号写进 `docs/<doc>/review-ignore.json` 交给 cutaudio（需 **≥ 1.1.0** 的 `--review-ignore`）：**只有触及 `keep=true` 行的 issue 才拦 `gate e6`**，开场/报幕/指令/结尾的静音只记 `ignored`，仍写全量结果；旧 cutaudio 没有该 flag 时回退旧行为并 warning，`gate e6` 审计 `review_ignore` 与 `ignored_issues`。`cut_stage` exit 3 / `needs_review`（进卡组行仍有 issue）→ 停下问用户，贴 `issues.md` 段号 + 原因。`gate e6` PASS 后**禁止**再 cleanup（会删 E7 要用的 ZIP），直接 E7。
- `cut-report.manifest_sha256` 绑 **`manifest-5col.md`**（E4 冻结基线），不绑 6 列；包已交付后 `gate e6` 的 `manifest_sha` 不一致**只 WARN**。
- 开头未覆盖语音：首行是「否」→ 记录继续；首行是「是」→ 请用户试听 `001.wav`，只有「接受」或「停下汇报」两种处置，不自行造文本。

## E7 · 分卡 + 成包 → 交付包

```bash
python3 "${S}/cut_stage.py" --run "${RUN}" --doc <doc> --stage pack --json
python3 "${S}/gate.py" e7 --run "${RUN}" --doc <doc>
```

- 还没切过可用 `--stage all` 一条命令跑切分 + 分卡（同样放后台、同样抢 E6/E7 串行锁；**先过 E5 定稿**）；**同一切分文本会 resume**（`cut-report.text_sha256` == 当前文本 sha 就跳过 cut 直接 pack）；文本变了 / 缺绑定才 exit 2 并提示 `--reset`。**只改 `keep` 而切分文本没变**时同样会 resume，旧 `review_ignore` 与当前 `segments.json` 不一致 → `gate e6` 要求 `--reset` 重切（审计，不是 bug）。`gate e6` PASS 后只跑 `--stage pack`。
- 包名/清单名由 `doc.json` 决定；ZIP 只从 `cut-report.json.zip.path` 取。**关卡**：目录名逐字、只有清单 + wav、wav 数 = 进卡组行数、文件名 = 行号、每个 wav > 0.2 s、清单 sha 三处绑定一致；包内 6 列清单的前 5 列 == `manifest-5col.md`，第 6 列扩展只 WARN。

## E8 · 汇报 + 清理（阶段一收尾）

```bash
python3 "${S}/cleanup.py" --run "${RUN}"          # ① 清 产出区 的切分文本/清单/切分结果* 残留；不删 approve.json / 包内 6 列 / pack.json
python3 "${S}/gate.py" phase1 --run "${RUN}"       # ② 全局复算 e1–e7（给 report 的关卡列）
python3 "${S}/report.py"  --run "${RUN}"          # ③ 汇总，写 $RUN/report.md
python3 "${S}/gate.py" e8 --run "${RUN}"           # ④ 全局关卡，不带 --doc
```

- **顺序必须 cleanup → phase1 → report → e8**；反过来 `report.md` 的关卡列会是空白。汇报：包路径、行数/进卡组数、`gate e8 PASS`；要发文件就 `present` 包内清单。**只报磁盘事实**。
- 阶段一（E1–E8）到此结束：包已带 6 列清单与 `approve.json`。默认「切」入口停在这里，说「切片已交付并定稿；要做成卡再说一声」；「只要切片/先不制卡」也停这里（阶段一不含卡片）。`$RUN` 先留着，全部交付、用户确认后再 `--purge-run`。

**阶段二 · 卡片（E9）**
- **做**：派生卡片 ID/顺序/断行任务 → 派排版员 → 合入渲染 → 派校对员（英文断行 `fixes` / 中文 `zh_fixes` 表格）→ 用户敲定中文后用 `zh_fix.py` 直改。 ｜ **产出**：`cards-base.json`、`card-tasks.json`、`cards/<task>.json`、`cards.json`、`cards-preview.md`（默认前 1–3 张样卡）、`cards-review-<n>.json`、`zh-fixes.md`。
- **关卡**：`gate.py e9`（`manifest_finalized` 重算 `rows` + `cards_aggregate_sha` + 逐卡校验）。 ｜ **失败**：断行改字 → 只重派那张卡的任务；ID/顺序不对 → 看 `stem`，必要时 `--id-scheme`；中文问题 → `zh_fixes` → `zh_fix.py apply`（不重跑 E3/E5/E6/E7/E8）。 ｜ **谁做**：排版员 / 校对员（短任务）+ 总调度 + 用户（敲定中文建议）。

## 阶段二 · E9 卡片（ID、断行、渲染、中文校对）

**前置：E5 定稿**：`approve.json` 存在、`mode=user`、sha == 当前 6 列清单（cleanup 前是 `$RUN` 清单，cleanup 后读包内清单）。没定稿或清单定稿后又改过 → `gate e9` 的 `manifest_finalized` 会拦，回 E5 重新交付并定稿；`cards_build.py` 开头也会查 `approve.json`，缺/过期 exit 3、**不写任务**。**阶段二从 E9 起**（E1–E8 属于阶段一）。

```bash
python3 "${S}/cards_build.py" --run "${RUN}" --doc <doc> --chunk 6 --json
# 派排版员：附录①，锚点 references/subagent-briefs.md#layouter
python3 "${S}/cards_apply.py" --run "${RUN}" --doc <doc> --json
# 派校对员（#proofreader）写 cards-review-<n>.json；fixes 只收 en_lines，
# 中文问题写独立的 zh_fixes（整张卡中文行数组）
python3 "${S}/cards_apply.py" --run "${RUN}" --doc <doc> --review "${RUN}/docs/<doc>/cards-review-1.json" --json
# ↑ 同时渲染 $RUN/docs/<doc>/zh-fixes.md（卡片 / 当前中文 / 建议中文 / 理由）；cards.json 原样不动
python3 "${S}/gate.py" e9 --run "${RUN}" --doc <doc>
```

- `cards-preview.md` 默认只写前 1–3 张样卡（`--preview N` 可调，最小 1；排障 `--full-preview` 写全量）；`cards.json` 才是全量机器真源，校对员读 `cards.json` + 6 列清单，不依赖截断预览。
- **断句口径（0.0.8 起）**：`cards_build.py` 先剥 `**M:**` / `**W:**`、按句末标点切句；说话人只作**句首标记**，同一说话人的连续几段不按说话人拆句。所以「按谁在说 4 段」不等于 4 句，不要据此回 E4 拆行；有疑问看 `sentence_count` 与译文行数是否一致。
- 章节名 `精听：<YY>年<MM>月-卷<N>`；卡片 ID `Y<YY>M<MM>` + `[<卷号罗马>]` + 家族（`NR<篇>-<序>` / `Cs<篇>-<序>` / `Pa<篇>-<序>` / `Q<两位题号>`），标题里的 `[` `]` 存储时带反斜杠转义。`stem` 派生不出 → exit 3，用 `--id-scheme` 给 `chapter/prefix/volume`。
- 断行：一句一行，句末标点必断；从句 / 并列分句行首 `- `，分句里的分句 `-- `（只一层）；下一行以 `- `/`-- ` 开头时，上一行行尾保留**一个**半角空格；**一个字都不许改**。
- 校对员的 `fixes` **只收 `en_lines`**；中文问题写 **`zh_fixes`**（整张卡中文行，`len(zh) == 英文句数`，不写 `current`）；`fixes[].zh` 仍被脚本拒绝（英文/中文通道分开）。

**中文校对直改（0.1.0 D1；不重跑 E3/E5/E6/E7/E8）**

1. 把 `$RUN/docs/<doc>/zh-fixes.md` 的表格发给用户（**必须含卡片编号 / 当前内容 / 建议内容 / 理由**，不能只说「有中文问题」）；用户可：全部接受 / 只接受某几张 / 自己改措辞。
2. 全部或部分接受：
   ```bash
   python3 "${S}/zh_fix.py" apply --run "${RUN}" --doc <doc> \
     --review "${RUN}/docs/<doc>/cards-review-1.json" --accept all --json
   # 只接受部分：--accept 005,006
   ```
   用户自己改措辞：把敲定后的最终中文写成批准文件
   `{"version":1,"doc":"<doc>","fixes":[{"no":"012","zh":["…"]}]}`，再：
   ```bash
   python3 "${S}/zh_fix.py" apply --run "${RUN}" --doc <doc> --fixes /tmp/zh-approved.json --json
   ```
   `apply` 只改 `translations.json` + `$RUN` / 包内 / 产出区 6 列清单；**不写 `approve.json`**，不碰音频 / `cut-report`，输出 `need_approve=true` / `need_refresh=true`。
3. 然后按顺序收口：
   ```bash
   python3 "${S}/approve.py" --run "${RUN}" --doc <doc> --json          # 用户已敲定，重新定稿
   python3 "${S}/cards_apply.py" --run "${RUN}" --doc <doc> --refresh-zh --json
   python3 "${S}/gate.py" e9 --run "${RUN}" --doc <doc>
   ```
   `--refresh-zh` 从当前 6 列清单（`$RUN` 优先，其次包内）刷新 `cards-base.json` / `cards.json`，复用现有排版回执，**不重跑 layouter**；缺 `cards-base.json` 或排版回执 → exit 3。
4. 只接受部分时，未接受的行保留原译并告知用户；gate 不因此 FAIL（用户知情即可）。`cards.json` 变过 → 旧 `markji-plan.json` / `approve2.json` 作废，E10 必须重新 plan。

**阶段二 · 云端计划 + 样卡（E10；first ★闸门 2 / veteran 自动放行）**
- **做**：`whoami` → 只读 plan（读 `deck.card_count` 定 first/veteran）→ first：直接写 1 张样卡 → 用户在 App 看 → `approve2` → 关卡；veteran：跳过样卡/`approve2`，直接关卡 → push。 ｜ **产出**：`markji-plan.json`、`approve2.json`（first；veteran 仅覆盖差异时需要）、`receipts/markji-plan.json`、`receipts/markji-sample.json`（veteran 为 `skipped`）。
- **关卡**：`gate.py e10`（`plan_decision` / `plan_cards_sha` / `plan_gate`；first 另有 `approve_*` / `sample_*`；veteran 走 `gate_veteran` 自动放行）。 ｜ **失败**：牌组不唯一/缺失 → 列候选问用户（不建牌组）；plan 重算 → 旧批准作废；样卡读回不一致 → exit 1，**不 push**。 ｜ **谁做**：总调度 + 用户（first 在 App 看样卡；veteran 决定是否覆盖 `diff_pending`）。

## E10 · 墨墨预检 + 样卡确认（★闸门 2：仅首个新牌组）

1. **通道自检**：`python3 "${MK}" whoami`。缺脚本 / 401 / 403 → 停下，把人话指引原样转达；换 Key 用 `python3 "${MK}" key --set "<新Key>"`（不打印 Key、不绕过）。
2. **只读计划**：

```bash
python3 "${S}/markji_push.py" --run "${RUN}" --doc <doc> --stage plan \
  --chapter-name "<章节名>" --json
# 牌组默认取 run.json.options.deck_name（E1 选择门写入）；要覆盖才加 --deck-name "<牌组精确名>"
```

- 牌组按**精确名称唯一匹配**（缺失/不唯一 exit 3 列候选，问用户；**不建牌组**）；章节存在 → `use` 并读 `card_ids`（展示顺序），不存在 → 将新建。逐张定三态：`create` / `skip_identical` / `diff_pending`（默认不动）。plan 写 `decision_sha256`、`cards_sha256`、`deck.card_count` 与 `gate`。
- **`gate.mode` 决定样卡闸门**：`veteran` = 目标牌组已有卡（`card_count > 0`），自动跳过样卡；`first` = 空/新牌组，先写 1 张样卡。plan 输出一行 `E10 闸门：…`。**这一步只读，不写云端。**
- **`first_sample_pending`**：上次 first 样卡已写、但还没成功 push 时，重跑 `plan` 会保持 `first`（不因样卡让牌组看起来像 veteran）。此时**只补 `approve2`**，不要重写样卡；成功 push 后才回 veteran。
3. **摊给用户看**（一条消息，别贴整章、**不贴新卡正文**）：牌组/章节、各类张数、`gate.mode`、`diff_pending` 逐张差异摘要（有才列）。按模式说明：
   - **first**：马上写 1 张样卡，请用户去 App 看；
   - **veteran**：样卡闸门自动放行，直接写新卡；Z 张不一致的卡默认不动，要覆盖请用户明确说。
4. **first：直接写 1 张样卡**（不再先要批准；`sample` 不要求 `approve2.json`，输出只报卡号/标题/卡 ID，不打印正文）：

```bash
python3 "${S}/markji_push.py" --run "${RUN}" --doc <doc> --stage sample --json
# 默认第一张 create 卡；要指定加 --no <NNN>。写后读回比对，不一致 exit 1，不往下写
```

5. **请用户在 App 看样卡**（附录③），停下等回话（第 3 次固定回话）。用户说「可以」后才写确认令牌并过关：

```bash
python3 "${S}/approve2.py" --run "${RUN}" --doc <doc> --mode user \
  --sample-no <NNN> --chapter-ok --json
python3 "${S}/gate.py" e10 --run "${RUN}" --doc <doc>
```

`approve2.json` 绑当前 `markji-plan.json` 的 `plan_sha256` / `decision_sha256` / `cards_sha256` 与 `sample_no`（`sample_no` 必须等于样卡回执的 `no`）；`--updates-ok` 只在 `diff_pending > 0` 且用户明确同意覆盖时加。plan 重算过 → 旧批准作废，重新 plan 再批准。
6. **veteran：跳过样卡与 `approve2`**：

```bash
# 可选：留一条“样卡已跳过”回执（不写云端）
python3 "${S}/markji_push.py" --run "${RUN}" --doc <doc> --stage sample --json
python3 "${S}/gate.py" e10 --run "${RUN}" --doc <doc>   # gate_veteran：老牌组，样卡闸门自动放行
```

`push` 不需要 `approve2.json` / 样卡回执。若用户明确要求覆盖 `diff_pending`：先写授权 `python3 "${S}/approve2.py" --run "${RUN}" --doc <doc> --veteran --updates-ok --json`，push 再加 `--allow-update`；没有授权 → 差异卡跳过并 WARN。老牌组 E10 自动放行，**不算一次固定回话**。
- 用户要改卡片 → 英文断行回 E9（重派排版员）；中文回 E9 中文直改（`zh_fix.py`）；分组/原文回 E4/E5。改完重算 plan、重新写样卡/批准。

**阶段二 · 批量写入 + 回读（E11）**
- **做**：后台 `push`（重读云端 → 逐张 upload → 只 create/skip → 按 order 重排 → verify）。first 需样卡回执 + App 后 `approve2`；veteran 两者都不需要。 ｜ **产出**：`markji.json`、`receipts/markji-push.json`（成功或失败都写；失败也保住已写 `card_id`）。
- **关卡**：`gate.py e11`（数量/标题唯一/音频/顺序/内容；first 查样卡/批准，veteran 查 `gate_veteran` 并跳过样卡检查；`diff_pending` 且 `updates_ok=false` 记 WARN）。 ｜ **失败**：数量不对看 `skipped[]`，用 `--limit` 定位；顺序不对重跑 `push`；**绝不手删云端卡**。 ｜ **谁做**：总调度（后台作业 + 收口）+ 用户（App 抽查）。

## E11 · 批量写入与回读核对

```bash
# 后台跑（约 3–6 分钟；频控下限约 2.4 分钟）；用户确认覆盖 diff_pending 时才加 --allow-update
python3 "${S}/markji_push.py" --run "${RUN}" --doc <doc> --stage push --interval 0.6 --json > "${RUN}/logs/push-<doc>.out" 2>&1
python3 "${S}/gate.py" e11 --run "${RUN}" --doc <doc>
```

- 一条命令干完：重读云端（防手改）→ 逐张 upload（同一文件幂等）→ 只做 `create` / `skip_identical`（`diff_pending` 跳过并记 `skipped[]`）→ 按 `order` 重排（`card_ids[i] != want[i]` 就 `move --order i`，最多两轮）→ `verify` 读回。
- **前置按模式**：first 必须已有样卡回执 + `approve2.json`（App 确认后）；veteran 不需要样卡/`approve2`，直接 push（`markji.json.sample` 记 `{"mode":"veteran","skipped":true}`）。
- **覆盖要显式**：只有 `approve2.updates_ok=true` **且**加 `--allow-update`，才对 plan 里 `action=diff_pending` 的卡整张 `update`（不新建、不删除）；veteran 的授权用 `approve2.py --veteran --updates-ok`。运行中新出现的 diff 跳过并 WARN。
- **频控**：10 秒 20 次 / 60 秒 40 次；遇 `common_too_many_request` 等 10 秒重试（最多 6 次）；不缩短间隔、不改并发。**`--limit N`**：只处理/核对前 N 张，回执记 `partial=true` 且 `order_verified=false`（不重排）；后续全量 push 再验证顺序。

**阶段二 · 同步按需（E12）**
- **做**：`--check` 只报差异 → 用户决定以哪边为准 → `--update` 只整张更新差异卡 → 关卡。 ｜ **产出**：`receipts/markji-sync.json`（check/update 都写 `cards_sha256`）。
- **关卡**：`gate.py e12`（`mode=update` 必须 `diffs == []`）。 ｜ **失败**：改卡片回 E9（中文 `zh_fixes` / 英文断行），改分组/原文回 E4/E5；`cards.json` 变了要重新 plan + `approve2`；顺序不对重跑 `markji_push.py --stage verify`；音频没挂上 → 回 E11。 ｜ **谁做**：总调度 + 用户（决定以哪边为准）。

## E12 · 后续同步（按需）

用户改译文、或想确认云端与本地是否一致时走这一步。**改卡片也走脚本**：中文校对建议走 E9 `zh_fixes` → `zh_fix.py apply` → `approve.py` → `cards_apply.py --refresh-zh`；重译走 E5 `translate_apply.py --review` → `approve.py` → `gate e5` → 复跑 E8 收尾 → `cards_apply.py`；英文断行走 E9 排版员 `fixes` → `cards_apply.py --review`。**不手改清单、不手改 `cards.json`。** `cards.json` 变过（`cards_sha256` 变了）→ 先重新 plan + `approve2`，再做 E12。

```bash
python3 "${S}/markji_sync.py" --run "${RUN}" --doc <doc> --check  --json   # 只报告差异
python3 "${S}/markji_sync.py" --run "${RUN}" --doc <doc> --update --json   # 只更新差异卡（需 updates_ok + 卡集 sha 一致）
python3 "${S}/gate.py" e12 --run "${RUN}" --doc <doc>
```

- `--check` 有差异 → exit 1（这是「有差异」，不是崩了）；`--update` 前先把差异清单给用户看，只对差异卡整张 `update`（不 create、不删除）；`approve2.cards_sha256` 与当前 `cards.json` 不一致 → exit 3（重新 plan + approve2）。
- 用户明确不覆盖的卡不要动，但 `diffs` 清不掉、`gate e12` 不会 PASS；这时要么用户点头覆盖，要么回 E9/E5 把本地对齐云端（以云端为准）。

## 断点续跑与清理

- **从哪继续**：重跑 `bootstrap` 拿回同一个 `$RUN`（输入内容没变）；按 `gate.py e1…e12` 顺序跑，第一个 FAIL/缺回执处就是断点。`status.py` 只读给 `NEXT=`（E10 会按 `gate.mode` 分别提示 sample / approve2 / push）。
- **E1 选择已存 run**：`run.json.options.deck_name` + `run.json.deck_name` 会保留；resume 不给 `--deck-name` 不覆盖。要换牌组：`bootstrap.py --work … --deck-name "<新名>"` 或 E10 `--deck-name` 显式覆盖，换完必须重新 `plan`（旧 plan/approve2 作废）。
- **输入漂移**：run key = 本次选中音频+原文的**内容 sha**；只 touch 不改变内容 → 同 run + `WARN input_touched`；内容变 → 新 run，旧 run 改名 `.prev-<ts>`，从 E2 重跑，旧回执作废。
- **重编分组**：用户改分组 → 回 E4 `assemble.py --overrides` → E5 `translate_prepare.py` 默认复用译文记忆（整行 + 单句记忆），只重译真正新增/变化的多句行（旧 `translate/` 回执自动归档 `translate.prev-<ts>/`）；不改音频、不重跑 E6。
- **只改中文（E9）**：`zh_fix.py apply` → `approve.py` 重新定稿 → `cards_apply.py --refresh-zh` → `gate e9` → E10 重新 plan；**不重跑 E3/E5/E6/E7/E8**，不重跑 layouter。
- **E10 样卡中断**：first 样卡已写、还没成功 push → `plan` 打 `first_sample_pending`，仍按 first：让用户在 App 看样卡 → `approve2.py --sample-no … --chapter-ok` → `gate e10` → `push`；**不要重写样卡**。成功 push 后才变 veteran。
- **E6 正在跑**：`status.py` 打 `CUT_RUNNING` 说明串行锁被占，等它结束再看 `NEXT=`；不要重开第二个 cut/pack（会 exit 3）。
- **清理时机**：只在「准备重切之前」`cleanup.py --doc <doc>` 与「E8 收尾」`cleanup.py`；`gate e6` PASS 后、E7 之前**禁止**清理（会删待分卡 ZIP）。`cleanup.py` 不删包、不删 `材料区/`、不碰 `系统/`、不删 `approve.json` / 包内 6 列清单 / `pack.json`；`--purge-run` 只在全部交付、用户确认后用（删了就不能复算）。
- 故障速查见 [runtime/troubleshooting.md](references/runtime/troubleshooting.md)；命令索引与 `$RUN` 结构见 [runtime/pipeline.md](references/runtime/pipeline.md)。

## 附录 · 3 个可照抄模板

### ① 派发指针（裁定员 / 复核员 / 翻译员 / 译文复核员 / 排版员 / 校对员通用）

```bash
# 列出还没回执的任务；F 换成 dispatch.json / translate-tasks.json / card-tasks.json
F="${RUN}/docs/<doc>/dispatch.json"
python3 -c 'import json,os,sys;[print("HAVE" if os.path.exists(t["receipt"]) else "MISS", t["task_id"], t["receipt"]) for t in json.load(open(sys.argv[1],encoding="utf-8"))["tasks"]]' "$F"
```

```text
派发默认走 workflow（不要一条消息里挨个调 subagent）：
  1) 预建回执目录：mkdir -p "$RUN/docs/<doc>/"{blocks,translate,cards}
  2) 只派 MISS；预渲染任务单到 $RUN/docs/<doc>/prompts/<task>.md（抬头 + 角色卡全文 + 原样 JSON）
  3) workflow 里 WAVE=8（每波 ≤8 个 agent()），await parallel 逐波等；agent() 失败返回 null → 只重派失败项（最多重试 2 次）
  4) workflow 脚本没有文件系统：agent 的 prompt 只指向任务单文件；gate 回到会话里跑
  5) 复核员 / 译文复核员 / 校对员也用 workflow（2 卷 2 个并行）；不用两个裸 subagent
  模板（E3 / E5 / E9 + 复核员）与失败落盘见 references/runtime/fanout.md
派完不 sleep、不轮询；workflow 返回后先跑对应关卡，再只补缺的。
任务单必须写明：只写自己那一个回执；不改原文/英文/中文；不跨任务；不派子 agent；回一行 `done <绝对路径>` + ≤120 字。
```

### ② E5 交付 6 列清单（present + 统计 + 摘录）

```bash
M="${RUN}/docs/<doc>/切分清单.md"
grep -m1 '^> \*\*进卡组＝是\*\*' "$M"
awk '/^\|---/{f=1;next} f&&/^\|/{print}' "$M" | head -15
```

```text
译文已合入，清单在附件（6 列：行 / 类型 / 进卡组 / 位置 / 原文 / 译文）。**音频还没切**——先请你对照译文把分组敲定。
【统计】总行数 <N>；进卡组 <K>（正文 <a> + 题目 <b>）；不进卡组 <N-K>。
【进卡组区间】<006–012、014–015、…>
你确认后我把清单定稿（绑 sha 进 approve.json），然后才切音频、分包；再往后才是制卡。
回我三种之一：①「分组和译文都没问题/定稿」→ 定稿并切分；②「第 X 行译文改成…」→ 出 fixes 重合并再给你看；③「第 X 行该拆开/该并到上一行/类型改…/这行进卡组」→ 回 E4 重汇编、重译受影响行、重新给你一版（**不切音频**；E6 才切）。
```

### ③ E10 样卡请示（first 专用；一条消息发完，等用户在 App 看）

```text
样卡已写入，请你在 App 里看一眼（这里不贴卡片正文）：
【牌组】<牌组名> → 【章节】<章节名>（<存在 / 将新建>）
【样卡】<标题>（音频 <NNN>.wav）；【不一致的卡】<Z> 张（默认不动，保持你手改的版本；要覆盖得你点头）
请确认三件事：1) 正反面音频能播放、是 <NNN> 那段；2) 中英文一句一行、语气自然；3) 英文断行顺眼。
回「可以」我就写确认令牌，按顺序写剩下 <K-1> 张（约 3–6 分钟），写完读回整章核对。要改现在说：墨墨没有删除接口，写进去的卡删不掉，改只能整张 update。
```

写法见 E10 第 4–5 步：`markji_push.py --stage sample`（**先写样卡，不先要批准**）→ 用户在 App 确认 → `approve2.py --sample-no <NNN> --chapter-ok` → `gate e10` → `push`。**veteran（老牌组）不发这条**：样卡闸门自动放行，直接 `gate e10` → `push`；`diff_pending` 默认不动，用户明确要覆盖才走 `approve2.py --veteran --updates-ok` + `--allow-update`。

## 配套文件

| 文件 | 什么时候看 |
|---|---|
| `references/runtime/pipeline.md` | 状态机、命令索引、`$RUN` 结构、断点续跑、PTC 包装 |
| `references/runtime/parse.md` | `format=contract-v1` / `blocks.json` / 字段 / 覆盖规则 |
| `references/runtime/transcript-contract.md` | 原文唯一格式：语法 / 正反例 / 旧格式迁移 |
| `references/runtime/format.md` | 切分文本 / 清单 / 包 / 卡片字节契约 |
| `references/runtime/cloud.md` | 牌组 / 章节 / 三态 / 批准 / 样卡 / 频控 / 回读 |
| `references/runtime/troubleshooting.md` | 冷故障页 |
| `references/runtime/fanout.md` | 派发模板：workflow 每波 ≤8（E3/E5/E9）、复核员并行、失败重试与 `.failed` 落盘 |
| `references/roles/` | 6 张角色卡（segmenter / reviewer / translator / translation-reviewer / layouter / proofreader） |
| `references/subagent-briefs.md` · `workflow.md` | 派发锚点薄索引（脚本里嵌的 `brief` 就是它）；一页命令索引 |
| `references/segmentation-rules.md` | 维护 / 档案：分组规则推导与正反例 |
