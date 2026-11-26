# 运行时 · 流水线、命令与断点

> 运行时文档。命令参数以 `python3 scripts/<脚本> --help` 为准；本文件与 `--help` 冲突时以 `--help` 为准，并把冲突写进汇报。
> 一页状态机见 [workflow.md](../workflow.md)；排障见 [troubleshooting.md](troubleshooting.md)。

## 1. 变量与调用

```bash
W="$(pwd)"                                                    # 工作区根：有 系统/tools/cut 和 材料区/
S="$HOME/.agents/skills/listening-card-slicing/scripts"       # 本 Skill 的脚本目录
MK="$HOME/.agents/skills/markji-channel/scripts/markji_channel.py"   # 云端传输层（阶段二）
RUN="<E1 首行 RUN= 打出的值>"                                 # 所有中间产物只进这里
```

- **每次 bash 调用都是新 shell**：四条变量在每条命令里重新给；`RUN` 只能从 `bootstrap.py` 首行取，或从已有 `run.json` 读。
- 脚本只用 Python 标准库（3.9+）。阶段一用系统 `python3`；**不要**用工作区 `.venv` 跑本 Skill 的脚本（那是切分程序自己的环境）。
- 所有确定性动作只经 `scripts/` 封装：不直接调 `系统/tools/cut`、`split_cards.py`、`cleanup_outputs.py`，也不手敲 `markji_channel.py create/update`。
- `--json`：摘要压到一行；脚本最后一行是 `JSON <单行 JSON>` 或 `RUN=…`；`gate.py` 例外，最后一行永远是 `PASS` / `FAIL: <摘要>`。

## 2. 12 步 · 脚本 · 关卡 · 产物

| 步 | 脚本 | 关卡 | 主要产物 |
|---|---|---|---|
| E1 | `preflight_choices.py`（只读列材料 + 云端牌组候选）→ `ask_user_question` → `bootstrap.py`（`--select` / `--deck-name` 存进 `run.json`；多候选原文按 `_听力原文` > `_原文` > 裸 stem 取唯一，其余记 `ignored_texts`） | `gate e1` | `run.json`（含 `preflight` / `deck_name`）、`docs/<doc>/doc.json` |
| E2 | `parse_source.py`（**只认 contract v1**；开场/结尾可缺，缺什么就不生成什么行；旧格式 exit 1 + 迁移提示） | `gate e2` | `blocks.json`、`dispatch.json` |
| E3 | （派裁定员）`workflow` 每波 ≤8，模板 [fanout.md#e3](fanout.md#e3) | `gate e3` | `blocks/<block>.json` |
| E4 | `assemble.py`（写 `merge_candidates`；覆盖前把旧 6 列归档为 `manifest-6col.prev-<ts>.md`） | `gate e4`（含 `merge_review` 覆盖机检；五类 merge，`turn`/`dialogue_beat` 仅对话块） | `segments.json`（含 `manifest_sha256` / `merge_candidates`）、`切分文本.txt`、`切分清单.md`、`review-<n>.json` |
| E5 | `translate_prepare.py`（默认复用译文记忆：整行 + 句级；`--no-reuse-memory` 关闭）→ 翻译任务 → `translate_apply.py` → `approve.py` | `gate e5`（含定稿检查；approve 之后才 PASS） | `translate-tasks.json`（含 `prefilled`）、`translations-memory.json`（整行 + 句级 `sentences`）、`translations.json`、6 列清单（`$RUN` + 产出区）、`approve.json`（含 `rows`） |
| E6 | `cut_stage.py --stage cut`（cutaudio ≥ 1.1.0，混排文本需 ≥ 1.1.1；`--review-ignore` 只豁免非卡片行，仍写全量结果；**必须串行**：抢 `$RUN/.locks/e6-e7.lock` 失败即 exit 3；`--timeout` 默认 3600；进度看 `docs/<doc>/cut-cutter.log`；切前混排护栏、切后 edge coverage<0.95 护栏） | `gate e6` | `review-ignore.json`、`cut-report.json`、`receipts/cut.json`、切分结果 ZIP（中间物） |
| E7 | `cut_stage.py --stage pack` | `gate e7` | `产出区/<stem>_音频切分（进牌组）/`、`pack.json`、`receipts/pack.json` |
| E8 | `cleanup.py` → `gate.py phase1` → `report.py` → `gate.py e8` | `gate e8`（phase1 也在 E8 内跑） | `report.md`、`receipts/cleanup.json` |
| E9 | `cards_build.py` → 短任务 → `cards_apply.py`（`--review` 可生成 `zh-fixes.md`；`--refresh-zh` 从 6 列清单刷新中文）→ 校对员 `zh_fixes` → `zh_fix.py table/apply` | `gate e9` | `cards-base.json`、`cards.json`、`cards-preview.md`（默认前 1–3 张样卡）、`cards-review-<n>.json`、`zh-fixes.md` |
| E10 | `markji_push.py --stage plan`（读 `deck.card_count` 定 `gate`）；first：`--stage sample` → 用户 App 看 → `approve2.py`；veteran：跳过样卡/`approve2`，覆盖差异才 `approve2.py --veteran --updates-ok` | `gate e10`（first 查 sample+approve2；veteran 走 `gate_veteran`） | `markji-plan.json`、`approve2.json`（first）、`receipts/markji-plan.json`、`receipts/markji-sample.json`（veteran 为 `skipped`） |
| E11 | `markji_push.py --stage push` / `verify`（first 需 sample+approve2；veteran 不需要） | `gate e11` | `markji.json`、`receipts/markji-push.json` |
| E12 | `markji_sync.py --check` / `--update` | `gate e12` | `receipts/markji-sync.json` |
| 全程 | `gate.py`、`status.py`（切分中打 `CUT_RUNNING` / `CUT_LAST_LOG`） | — | 判定与 `NEXT=`；不写产物 |

**退出码**：`0` 成功 / `1` 检查或执行失败 / `2` 用法或环境 / `3` 需要用户决定（停下问，不许硬闯）。`gate.py` 只有 0/1/2。

- **E2 只认一种原文**：contract v1（`## [开场]` / `## Section X` / `### Directions` / `### 篇目 [Qx-y]` / `[META] Questions …` / `Question N. …` / `## [结尾]`）。语法/正反例/迁移见 [transcript-contract.md](transcript-contract.md)；旧格式直接 FAIL，不再猜排版。

- **E1 选择门（0.1.0 D3）**：`preflight_choices.py --work "$W" --json` 只读列 `docs` / `needs_user` / `decks` / `deck_error`（不建 run、不写文件）；云端不可用时 `decks=[]` + `deck_error`，不阻塞材料。用 `ask_user_question` 多选材料（末项「全部处理」）+ 单选牌组（3–5 个候选 +「自己填」）；再 `bootstrap.py --select … --deck-name …`。`--deck-name` 写 `run.json.options.deck_name`（+ 平铺 `run.json.deck_name`），E10 默认使用，resume 不给则保留旧值。
- **E9 中文直改（0.1.0 D1）**：校对员 `zh_fixes`（整张卡中文）→ `cards_apply.py --review` 生成 `zh-fixes.md`（只报不改）→ 用户敲定 → `zh_fix.py apply` 直改 `translations.json` + 6 列清单（`$RUN` / 包内 / 产出区）→ `approve.py` 重新定稿 → `cards_apply.py --refresh-zh` 刷新 `cards-base.json` / `cards.json` → `gate e9`；**不重跑 E3/E5/E6/E7/E8**，不重跑 layouter。
- **E10 first/veteran（0.1.0 D2）**：`plan` 记 `deck.card_count` + `gate`。`first`（空/新牌组）先写 1 张样卡（不要求 `approve2`、不打印正文），用户在 App 看过后才 `approve2.py --sample-no … --chapter-ok`；`veteran`（已有卡）跳过样卡/`approve2`，直接 push。`first_sample_pending` 时重跑 plan 仍按 first，只补 `approve2`、不重写样卡，成功 push 后才回 veteran。`diff_pending` 两种模式都默认跳过，覆盖需用户明确 `--updates-ok` + `--allow-update`。
- **E6 前置是 E5 定稿**：`cut_stage.py --stage cut` 先查 `gate e5`；`approve.json` 缺失/过期就 exit 3，不切。
- **E5 重编/续跑归档（0.0.10 P1）**：重编（E4 重汇编后）或中断重跑且旧产物还在时，`translate_prepare.py` 把旧 `translate/` **和** 旧 `translate-review-<n>.json` 一起归档到 `translate.prev-<ts>/`，避免旧 review 按行号套到新行上；等翻译回执时**不要**重跑 `translate_prepare.py`（会归档已收到的有效回执），续跑加 `--keep-receipts`。
- **E8 四步顺序固定**：`cleanup.py` → `gate.py phase1` → `report.py` → `gate.py e8`；`report.md` 的关卡列依赖 `gate phase1` 的回执，缺它会空白。

## 3. 状态机

```text
阶段 1   E1 preflight_choices(只读)→★选择门(第 1 次回话：材料/牌组)→bootstrap(--select/--deck-name) ─e1→ E2 parse ─e2→
阶段 2   E3 派裁定员(workflow≤8) ─e3→ E4 assemble(+复核/overrides) ─e4→
阶段 3   E5 translate_prepare(默认复用译文记忆)→翻译→translate_apply→译文复核(+fixes→--review) ─e5→
          ★ 用户看 6 列清单、定分组/译文（第 2 次回话）→ approve.py → E6
阶段 4   E6 cut(后台·串行锁) ─e6→ E7 pack ─e7→ E8 cleanup→phase1→report ─e8→ 阶段一终
阶段 5   E9 cards_build→排版(workflow≤8)→cards_apply→校对(fixes/zh_fixes→--review)
          →（用户敲定 zh_fixes→zh_fix.py apply→approve.py→cards_apply --refresh-zh）─e9（manifest_finalized）→
阶段 6   E10 whoami→plan(只读；gate=first/veteran)→
          first: sample(写 1 张)→★用户 App 看样卡（第 3 次回话）→approve2→ ─e10→
          veteran: ★自动放行（不算回话）→ ─e10→
阶段 7   E11 push(create/skip+重排+verify) ─e11→
阶段 8   E12 sync --check / --update ─e12
```

- **PASS 才走**：每个箭头都是一次 `gate.py`；FAIL 就地处理，不跳步。
- **两道闸门 + 选择门**：E1 选择材料/牌组（`run.json.deck_name`，选了才建/续 run）；E5 定稿（`approve.json` 绑当前 6 列清单 sha，没定稿不切）；E10 仅 first 需要 App 样卡确认（`approve2.json` + 样卡回执）；veteran 自动放行。
- **入口**：新材料从 E1 选择门起；继续已交付包从阶段二快路径起（`bootstrap --phase 2`，E9 起），阶段二不需要 `gate e1` PASS；牌组名沿用 `run.options.deck_name`，要换用 `--deck-name` 显式覆盖。
- `phase1` 聚合：`gate.py phase1 --run "$RUN"` 一次跑 e1 + 每 doc e2–e7，首个 FAIL 停；排障用，不替代逐关产物。

## 4. `$RUN` 结构

```text
$RUN/
├── run.json                 # skill_version/work/run/input_sha/input_facts/options/deck_name/docs/package/archived[]；preflight 对象在 run.json.preflight
├── report.md                # E8 汇总
├── logs/                    # <stage>[-<doc>]-<ts>.log
├── .locks/e6-e7.lock        # E6/E7 串行锁（pid/doc/stage；status.py 只读）
└── docs/<doc>/
    ├── doc.json             # 绝对路径、sha、stem、result_dir/pack_dir/manifest
    ├── blocks.json          # 原文真源（format=contract-v1 / 块 / 原子 / 覆盖 / warnings）
    ├── dispatch.json        # 正文块 → 裁定任务
    ├── blocks/<block>.json  # 裁定员回执
    ├── review-<n>.json      # 复核员回执（多轮不覆盖）
    ├── segments.json        # 汇编结果（行=片段；manifest_sha256）
    ├── prompts/<task>.md    # 预渲染任务单（workflow 派发用：抬头 + 角色卡 + 原样 JSON）
    ├── 切分文本.txt / 切分清单.md / manifest-5col.md
    ├── review-ignore.json / cut-report.json / pack.json / cut-cutter.log
    ├── translate-tasks.json / translate/<task>.json / translate-review-<n>.json / translations.json
    ├── manifest-6col.prev-<ts>.md / translations-memory.json / translate.prev-<ts>/   # E4 归档 / E5 记忆（整行+句级）/ 旧回执归档
    ├── .failed-<ts>.json    # 派发两次仍失败的任务（有才写；报告根 agent）
    ├── approve.json         # E5 定稿令牌：manifest_sha256 + rows[{n,sha256}]
    ├── card-tasks.json / cards/<task>.json / cards-base.json / cards.json / cards-preview.md（样卡预览，默认 1–3 张）
    ├── cards-review-<n>.json / zh-fixes.md   # 校对回执；中文建议表格（--review 生成，只报不改）
    ├── markji-plan.json / approve2.json / markji.json
    └── receipts/{cut,pack,cleanup,markji-plan,markji-sample,markji-push,markji-sync}.json
```

- **中间产物只进 `$RUN`**；`产出区/` 只允许评审期清单、切分中间产物和最终包。
- `receipts/markji-sample.json` 两种形态：first = `mode=first`（真实样卡）；veteran = `{"mode":"veteran","skipped":true,"result":"ok"}`（不写云端，`gate e10` 不需要它）。
- **路径从 JSON 取**：`doc.json.manifest` / `pack_manifest` / `report.json.zip.path` / `task.receipt` / `markji-plan.json`；不自己拼文件名。

## 5. 断点续跑

```bash
python3 "${S}/status.py" --run "${RUN}"          # 只读：NEXT= 一步 / DONE / WARN
python3 "${S}/bootstrap.py" --work "${W}" --json  # 输入内容没变 → 同一个 RUN（幂等）
```

| 想做什么 | 命令 | 注意 |
|---|---|---|
| 找回 `$RUN` | 重跑 `bootstrap`（入口 B 加 `--phase 2`） | 首行 `RUN=`；`run.options.deck_name` 保留 |
| 改 E1 选择/换牌组 | `bootstrap.py --work … --select … --deck-name "<新名>"`（或 E10 `--deck-name` 显式覆盖） | 换牌组后必须重新 `plan`，旧 `approve2` 作废 |
| 看断在哪 | `gate.py e1` 起依次跑 | 第一个 FAIL / 缺回执处即断点；也可先看 `status.py`（E10 按 gate 模式给 NEXT） |
| 重跑解析 | `parse_source.py --run … --doc <doc>` | 输入变了先拦 sha；块集合变了，旧回执改名 `.bad-<ts>` 再重派 |
| 补派缺块 | 见 SKILL 附录①（默认 `workflow` 每波 ≤8） | 只派缺的，不动已有合格回执 |
| 重切 | `cleanup.py --doc <doc>` → `cut_stage.py --stage cut --reset` | 不手删；`gate e6` PASS 后禁止清理；改过 `keep` 也要重切（`review_ignore` 与 segments 不一致时 `gate e6` 会要求 `--reset`） |
| 只重分卡 | `cut_stage.py --stage pack` | 需 `cut-report.json` 且 `zip.verified=true` |
| 收尾 | `cleanup.py` → `gate.py phase1` → `report.py` → `gate.py e8` | 顺序别反；`report.md` 关卡列依赖 phase1 回执；全部交付后再 `--purge-run` |
| 只重译变化行 | `translate_prepare.py`（默认整行 + 句级记忆）→ 只派真正新增/变化的多句任务 | 整行/单句命中直接 `prefilled`；`--no-reuse-memory` 关闭；`translate_memory.py snapshot` 排障 |
| 改译文（重译） | 译文复核员 `fixes` → `translate_apply.py --review` → `approve.py` 重新定稿 → `gate e5` → 复跑 E8 收尾 → `cards_apply.py` → `gate e9` | 不手改清单；包内 6 列会同步，不用重切 |
| 只改中文（E9） | `zh_fix.py apply` → `approve.py` → `cards_apply.py --refresh-zh` → `gate e9` → E10 重新 plan | 不重跑 E3/E5/E6/E7/E8；不重跑 layouter；`cards.json` 变了要重新 plan/approve2 |
| 改断行 | 排版员补缺或校对员 `en_lines` → `cards_apply.py --review` → `gate e9` | 中文不走这条；中文用 E9 `zh_fix.py` |
| 重出计划 | `markji_push.py --stage plan` → first 再 `--stage sample` → 用户 App 看 → `approve2.py`；veteran 直接 `gate e10`/push | plan 重算，旧批准作废；`first_sample_pending` 只补 approve2、不重写样卡 |
| 同步云端 | `markji_sync.py --check` → 用户确认 → `--update` → `gate e12` | update 后 `diffs` 必须为空；`cards_sha` 变了先重新 plan + approve2 |

- `status.py` 打印 `CUT_RUNNING doc=<doc> stage=<cut|pack> elapsed=<s> log=<path>`（或 `CUT_LAST_LOG=…`）时，E6/E7 正在跑（锁被占），等它结束再继续；`--json` 的 `cut_running` / `cut_last_log` 同义。
- **输入漂移**：run key = 本次选中音频+原文的内容 sha；只 touch 不改变内容 → 同 run + `WARN input_touched`；内容变 → 新 run、旧 run `.prev-<ts>`，从 E2 重跑。
- **脚本版本变了**：`gate` 打 `check package: WARN drift`（不 FAIL）；不要跨版本混跑，`--fresh` 重建或从 E3 往后并写明。
- **交付确认前不要 `--purge-run`**：所有关卡都靠 `$RUN` 现场重算；删了不能复算。

## 6. 关卡判读

- `gate.py <e1..e12|phase1> --run "$RUN" [--doc <doc>] [--json]`。
- **全局**：`e1`、`e8`、`phase1` 不带 `--doc`；`e2`–`e7`、`e9`–`e12` 必带。
- **联网**：只有 `e10`–`e12` 需要联网，且只读云端；断网 FAIL 不是 bug。`e5`–`e9` 全本地。
- **e4**：汇编产物 + 复核回执（`review-<n>.json` 存在、信封合法；`verdict=ok` 不带 `overrides`；`fixed` 的改判已应用）；0.0.10 还机检 `merge_review` 覆盖全部 `merge_candidates`（`keep` 要对上同原子集合的正文行、`split` 要有 override；旧 run 无 `merge` 只记 `reason_missing`）。
- **e1**：预检 + 选中文档；0.1.0 的 `run.json.deck_name` 只作审计/E10 默认值，不要求非空（入口 B/旧 run 可缺）。
- **e5**：译文/复核/6 列检查 + `manifest_finalized`（`approve.json` 存在、`mode=user`、sha == 当前 `$RUN` 6 列清单；cleanup 后读包内）。**approve 之前跑必 FAIL（未定稿是预期）**；PASS 才允许 E6。
- **e6**：`cut-report.json` 的 `text_sha256` / `manifest_sha256` 必须对应当前文本与 `manifest-5col.md`；串行锁冲突、混排护栏、edge coverage<0.95 都在 `cut_stage.py` 阶段 exit 3（gate 不管锁；旧报告无 `coverage` 字段只 WARN）；单独跑 e6 时 ZIP 缺失 = FAIL；**包已交付后** `manifest_sha` 不一致只 WARN，ZIP 缺失记 `WARN zip_cleaned`。`status=needs_review` 仍 FAIL（进卡组行有 issue）；`status=ok` 后审计 `report.review_ignore` == 当前 `keep=false` 行、`ignored_issues` 只碰非卡片行，不一致 FAIL（清单改过 → 重切）；没有该字段的旧报告只 WARN。
- **e9 `manifest_finalized`**：`approve.json` 存在、`mode=user`、sha == 当前 6 列清单；同时重算 `rows[{n,sha256}]` 行哈希，任一格改过就 FAIL。中文直改（`zh_fix.py apply`）改了清单后必须 `approve.py` 重新定稿，再 `cards_apply.py --refresh-zh` 重渲染 `cards.json`。
- **e10 `gate`**：读 `plan.gate.mode`；`veteran` 跳过 approve2/sample 检查（`checker.ok("gate_veteran", …)`），若存在 approve2 只校验 `updates_ok` 作用域；`first` 保留 approve2 + sample 检查，且 `approve2.sample_no == sample.no`；`first_sample_pending` 允许样卡回执按 `skip_identical` 重绑当前 plan。
- **e11 `diff_pending`**：plan 动作是 `diff_pending` 且 `updates_ok=false` → `WARN`，不 FAIL（用户手改卡没被覆盖是预期）。first 必须有 first 样卡 + approve2；veteran 不要求 `push_sample`，`approve_no` 为空也不 FAIL。
- **e7/e8 扩展清单**：前 5 列、表头前 5 格、区间行、固定句逐字不变、只在右侧有第 6 列 → `WARN pack_manifest_extended`，不 FAIL。
- 判读**只认** `rc=0` + 最后一行 `PASS`；不要拿「日志里没有 FAIL」当通过。

## 7. PTC（`run_code`）会话包装

PTC 会话里只有 `run_code` 能直接调用，`bash` / `subagent` 在程序里用 `tools.*`：

```ts
const SK = "/Users/me/.agents/skills/listening-card-slicing";
const RUN = "<RUN>";
const r = await tools.bash({ description: "E4 assemble", timeoutMs: 300000,
  command: `python3 "${SK}/scripts/assemble.py" --run "${RUN}" --doc <doc> --json` });
console.log((r.stdout.text || "").trim().split("\n").slice(-8).join("\n"));
```

- 变量跨程序保留；用 IIFE 包住，避免重复声明。
- 派短任务：**优先在会话层调 `workflow`**（每波 ≤8，模板见 [fanout.md](fanout.md)）；`run_code` 程序里只能拿 `tools.subagent`，只在 <8 且需要续聊时补派，不要并排几十个。
- 长任务：PTC 没有后台作业，`tools.bash({ timeoutMs: 540000 })` 前台跑（25 分钟音频约 14–15 分钟）；`run_code` 本身默认 120 s，显式加大。
- 小查询（缺哪些回执）直接跑 SKILL 附录① 的一条命令，不在 JS 里手写遍历。

## 8. 两阶段边界

- **阶段一（E1–E8）**：原文 → 5 列清单 → 译文 + 6 列清单 + `approve.py` 定稿（E5）→ 切分（E6）→ 包（E7）→ 收尾（E8）。默认「切」入口到 E8 停。
- **阶段二（E9–E12）**：已定稿的 6 列清单 → 卡片 → 云端 → 读回 → 同步；`gate e9` 的 `manifest_finalized` 是硬门。
- 包/清单的字节契约见 [format.md](format.md)；云端三态与批准见 [cloud.md](cloud.md)。

