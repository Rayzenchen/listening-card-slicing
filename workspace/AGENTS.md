# 工作区入口（给 Agent）

这是**听力训练卡**工作区：一段听力音频 + 已分段、无时间戳的原文 → 本地切片包 + 墨墨牌组里的成套卡片。
**执行一律走 skill `listening-card-slicing`（0.1.0）**：两个阶段 / 12 步（E1–E12）、2 个入口、2 道人工闸门 + E1 选择门；共 3 次用户回话（E1 选择、E5 定稿、E10 首个新牌组样卡；老牌组 E10 自动放行）。阶段一 E1–E8、阶段二 E9–E12。

**0.1.0 改了三件事**：① **E1 选择门**——开工前用 `preflight_choices.py` 只读列材料 + 云端牌组候选，`ask_user_question` 多选材料（末项「全部处理」）+ 单选牌组（3–5 个候选 +「自己填」），`bootstrap.py --select … --deck-name …` 把选择存进 run，E10 默认使用；② **E9 中文校对表格 + 直改**——校对员写 `zh_fixes` 表格（卡号/当前/建议/理由），`cards_apply --review` 出 `zh-fixes.md`，用户敲定后 `zh_fix.py apply` 直改译文/6 列清单/cards-base/cards.json，再重新定稿 + `--refresh-zh` + `gate e9`，不重跑 E3/E5/E6/E7/E8；③ **E10 first/veteran**——空/新牌组先直接写 1 张样卡（不再先批准、不在会话展示正文），用户在 App 看过后 `approve2.py --sample-no … --chapter-ok`，再 `gate e10` → `push`；老牌组（已有卡）自动跳过样卡/approve2 直接 push，`diff_pending` 仍默认不动、覆盖要用户明确同意。

**0.0.8 的顺序变了**：E5 先翻译 + 6 列清单、用户对照译文定分组并定稿；E6 才切音频；E7 分包；E8 收尾。没定稿不切分。

**0.0.9 改了两件事**：① 分组默认从「宁并勿拆」翻成「能独立听懂就拆」（一行 = 一张卡，句界是默认切点；只有纯回应、指代悬空、紧凑问答三种情况才并）；② `材料区/` 支持子目录，`bootstrap.py` 递归扫描并按 realpath / 同名同内容去重（软链接与副本只算一份）。

**0.0.10 改了四件事**：① 分组先分对话/独白——对话块按「连续同 speaker 的回合」、独白块按「一个原子一行」，只允许 `pure_response` / `fragment` / `compact_qa` / `dialogue_beat` / `turn` 五类合并（后两类仅对话块），回执必须写 `merge` 理由，复核员 `merge_review` 逐条过目、`gate e4` 机检；② 重编分组保留译文记忆（整行 + 句级），只重译真正新增/变化的行；③ E6 必须串行（锁）、默认 `--timeout 3600`、cutter 输出实时写 `cut-cutter.log`，混排文本 + cutaudio 1.1.0 在切前拦、`edge` 覆盖率 <95% 在切后拦；④ cutaudio 升级到 1.1.1（英文主导的混排文本可走 ASR 锚定，低覆盖快速失败）。

本文件只说明工作区结构、触发语和维护边界。

## 先读什么

1. **先加载 skill**：`listening-card-slicing`（技能菜单里有就直接加载；没有就直读
   `~/.agents/skills/listening-card-slicing/SKILL.md`）。流程、关卡、命令全以它为准，不要凭记忆另写一套。
2. 再读本文件的「固定结构」，并用 `ls 材料区 产出区` 确认**当前实际**；本文不写死材料/包的清单。
3. 阶段二写云端依赖 skill `markji-channel`；它缺失或 Key 失效就停下，按它的说明处理，不要绕过。

## 两个入口

| 入口 | 用户说 | 起点 | 终点 |
|---|---|---|---|
| **A · 新材料** | 「切这个音频」「做成卡」「一条龙」 | E1 选择门（`preflight_choices.py` → 提问 → `bootstrap.py --select/--deck-name`） | 默认 E8 收尾停；说「做成卡」继续 E9–E12 |
| **B · 继续已交付包** | 「做成卡」「写进墨墨」「加译文」「同步一下」 | 阶段二快路径（E9 起，`bootstrap.py --phase 2`） | E9–E12；只同步走 E12 |

- 两边都有、用户没说清 → 一句话问「先切新的，还是给已有包制卡」。
- **入口 B 必须加 `--phase 2`**：已有包按复用处理，不报同名冲突；阶段二不需要 `gate e1` PASS。
- 旧包「覆盖」由 skill 的 `--archive-existing` 归档；「保留」= 本次不选该文档。不要手 `mv`、不要删包。

## 两个阶段 / 12 步

| 阶段 | 步 | 名称 | 终点 |
|---|---|---|---|
| 阶段一 | E1–E2 | 选择门 + 预检 + 解析 | `run.json`（含 `deck_name`）/ `blocks.json` / `dispatch.json` |
| 阶段一 | E3–E4 | 分组 + 汇编 | 5 列清单 + 复核回执 |
| 阶段一 | E5 | 译文 + 6 列清单 + 定稿 ★闸门 1 | `translations.json` / 6 列清单 / `approve.json` |
| 阶段一 | E6 | 切分 | `cut-report.json` + 切分结果 ZIP |
| 阶段一 | E7 | 分包 | 包 + `gate e7` PASS |
| 阶段一 | E8 | 收尾 | 包 + `report.md` + `gate e8` PASS |
| 阶段二 | E9 | 卡片 | `cards.json` |
| 阶段二 | E10 | 云端计划 + 样卡（first ★闸门 2 / veteran 自动放行） | `approve2.json` + 样卡回执 |
| 阶段二 | E11 | 批量写入 + 回读 | 云端 K 张 + `markji.json` |
| 阶段二 | E12 | 同步按需 | `receipts/markji-sync.json` |

## 3 次用户回话 / 2 道闸门 + E1 选择门

| # | 时机 | 用户要回 | 我们做什么 |
|---|---|---|---|
| 1 | **E1 开工前** | 这次处理哪几对材料 / 卡片写进哪个牌组 | `preflight_choices.py` 只读列材料 + 云端候选；`ask_user_question` 多选材料（末项「全部处理」）+ 单选牌组（3–5 候选 +「自己填」）；`bootstrap.py --select … --deck-name …` |
| 2 | **E5 交付 6 列清单后（切分前）** | 定分组/译文 / 改译文 / 改分组 | 认可才 `approve.py`；改译文走复核；改分组回 E4（不切音频） |
| 3 | **E10 首个新牌组（first）样卡写入后** | 在 App 看过，可以批量吗 / 差异卡覆盖吗 | first：先 `sample` 写 1 张 → 用户 App 看 → `approve2.py --sample-no … --chapter-ok` → `gate e10` → `push`；veteran 自动放行，不算回话 |

指令短不等于可以跳过这些停点；云端**没有删除接口**，写错了只能整张覆盖，所以这几处必须停、不得代答。E10 不再有「写样卡前批准」这一步；`approve2.json` 是 App 看样卡后的确认令牌（first）或覆盖差异卡的授权（veteran）。

## 触发语

- **「干活」** → 从入口 A 跑完整 12 步（E1–E12）。
- **「切这个音频」** → 入口 A，跑到 E8 收尾停。
- **「做成卡」「写进墨墨」** → 入口 A（如需要切分）或入口 B，继续 E9–E12。
- **「只要切片」** → 入口 A，跑到 E8 收尾（含 6 列清单与定稿），不做卡。
- **「整理工作区」「改 skill」「维护环境」「看看这个文件」** → 只做维护/回答，不启动制卡流程。

## 固定结构

```text
workspace/（本仓库的工作区目录）
├── 材料区/                          ← 用户输入：音频 + 对应原文（内容每次以 ls 为准）
├── 产出区/                          ← 交付：<音频主名>_音频切分（进牌组）/（mp3 片段 + 6 列清单）
├── 系统/                            ← 环境：切分程序、模型、Python、ffmpeg（平时不要动）
├── AGENTS.md / CLAUDE.md / README.md ← 给 Agent / 给人 的说明
└── 运行目录 $RUN（/tmp，skill 建）  ← 中间产物、回执、日志、报告
```

- **材料区**：用户放材料的地方；**只读**——不删、不改、不覆盖。文件可能在会话间被换成新卷，先 `ls`。0.0.9 起**允许放子目录**（任意层级都会被递归扫描，软链接/同名同内容副本只算一份）。
- **产出区**：只放交付包；中间产物由 skill 清理；**不要手删包**。
- **系统/**：`src/cutaudio`（切分程序，1.1.1；1.1.1：混排文本可走 ASR 锚定；edge 低覆盖 fast-fail）、`tools/`（入口/体检/清理）、`models/whisper-large-v3-turbo`（1.5 G）、
  `.venv/`（Python 3.12）、`bin/ffmpeg`、`tests/`。
  环境只调用不改；维护另开任务。
- **当前状态不要写死**：材料区/产出区的内容会变，每次先 `ls 材料区 产出区` 看实际。

## 运行与续跑（0.1.0）

- **E1 选择已存 run**：`run.json.options.deck_name` + 平铺 `run.json.deck_name` 会保留；resume 不给 `--deck-name` 不覆盖。要换牌组用 `bootstrap.py --deck-name "<精确名>"` 或 E10 `--deck-name`，换完必须重新 `plan`（旧 `approve2` 作废）。云端不可用不影响阶段一，牌组名让用户直接回复。
- **身份**：run key = 本次选中音频 + 原文的**内容 sha**；只 `touch` 不改内容 → 同一个 `$RUN`；内容变 → 新 run（旧 run 改名 `.prev-<ts>`），从 E2 重跑。
- **断点**：`python3 <skill>/scripts/status.py --run "$RUN"` 看 `NEXT=`（E10 会按 first/veteran 提示 sample / approve2 / push）；关卡一律 `gate.py` 现场重算，不信历史对话、不信子 agent 自报。
- **原文只认 contract v1（0.0.7 起的格式，0.0.9 不变）**：`## [开场]` / `## Section X` / `### Directions` / `### 篇目 [Qx-y]` / `[META] Questions…` / `Question N. …` / `## [结尾]`；语法与正反例见 skill 的 `references/runtime/transcript-contract.md`。
- **旧原文必须重转（contract v1）**：旧格式（`# Part II…`、`## 试音部分`、裸结尾句、裸/编号型 `Questions`、无 `[Qx-y]` 的篇目）E2 直接拒绝，skill 不再兼容；按 contract v1 重做（本发行包不含转写 prompt）。
- **头尾仍可缺**：`## [开场]` / `## [结尾]` 各 0/1、`## Section X` 可缺（默认节推断）、结尾缺失只 WARN；但 `### 篇目` 必须带 `[Qx-y]`，题号必须连续 +1。
- **E6 只卡进卡组行（0.0.6）**：切分前把 `keep=false` 行写入 `review-ignore.json`，cutaudio ≥1.1.0 只豁免这些行的 review issue；开场/报幕/指令/结尾静音不再阻断，仍写全量 ZIP；`gate e6` 审计 `review_ignore`/`ignored_issues`。
- **一份音频一份原文**：多候选按 `_听力原文` > `_原文` > 裸名；其余记 `ignored_texts`，要换文本请改名/移走旧版。
- **清理**：E6 重切前用 `cleanup.py --run "$RUN" --doc <doc>`（只清该文档）；E8 顺序固定 `cleanup.py → gate.py phase1 → report.py → gate.py e8`；`gate e6` PASS 后、E7 前禁止清理。
- **E9 前置**：`cards_build.py` 开头查 `approve.json`（E5 定稿）；缺/过期 exit 3、不写任务。
- **E9 中文校对**：校对员 `zh_fixes` → `cards_apply --review` 出 `zh-fixes.md` → 用户敲定 → `zh_fix.py apply` 直改 `translations.json` + 6 列清单 → `approve.py` 重新定稿 → `cards_apply --refresh-zh` → `gate e9`；不重跑 E3/E5/E6/E7/E8，不重跑 layouter。`cards.json` 变了，E10 plan/approve2 要重做。
- **E10 模式**：`plan.gate.mode` = first（空/新牌组，先写 1 张样卡，App 确认后 `approve2`）或 veteran（已有卡，免样卡/`approve2` 直接 push）；`first_sample_pending` 时重跑 plan 仍 first，只补 `approve2`、不重写样卡；`diff_pending` 默认不动。
- **后台长任务**：E6 单份 25 分钟音频实测约 14–15 分钟；**必须串行**（`--timeout 3600`，不要开两个一起跑）；E11 批量写入约 3–6 分钟（频控下限约 2.4 分钟）。放后台，别用前台 60 s 超时硬等。
- **汇报**：只报路径、行数、关卡结果；不打印 Key/token/带签名 URL。

## 铁律（本工作区）

1. **只走 skill 的步骤**：不手敲 `系统/tools/cut`、`split_cards.py`、`cleanup_outputs.py`，也不手写
   `markji_channel.py create/update`——所有确定性动作经 skill 的 `scripts/` 封装，路径和记录都在 `$RUN`。
2. **原文是唯一文本真源**：不改字、不纠错、不润色；格式只认 contract v1（旧格式必须重转）；没有原文就停下，**不要拿 ASR 结果顶替原文**。
3. **E5 定稿前不改清单**：定稿令牌 `approve.json` 绑定 6 列清单的 sha；定稿后清单再改一个字节，`gate e9` 会拦；E9 的 `cards_build` 也要求 `approve.json` 现行有效。没定稿不得进 E6 切分。
4. **云端只 create / update**：不建牌组（牌组必须先由用户在 App 建好）、不删除、不重复建同名卡；
   已有卡默认不动，覆盖要用户明确同意。
5. **三次用户回话必须停**：E1 选择材料/牌组、E5 清单定稿、E10 首个新牌组（first）在 App 看样卡后确认；不得代答。老牌组 E10 自动放行，不算回话。
6. **根目录保持干净**：只保留上面这几样；临时文件写 `$RUN` 或 `/tmp`，不要往根目录加。
7. **结构变化时同步更新 AGENTS.md 与 README.md**。

## 没有原文时

本发行包**不包含** AI Studio 转写工具/文本，也不包含任何从音频提取的原文。请自备符合 contract v1 的原文；没有原文就停下，不要用 ASR 结果顶替原文。

## 维护边界

- 用户要求修改本工作区、修改 skill、调整环境或状态格式时：**只做维护**，不启动制卡流程。
- 制卡规则与流程维护在工作区外的 skill 里；本工作区不复制第二份，避免两边打架。
- 清理材料/成果时保留 `AGENTS.md`、`CLAUDE.md`、`README.md`、`产出区/`、`系统/`。

