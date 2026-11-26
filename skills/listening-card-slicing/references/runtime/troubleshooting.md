# 运行时 · 冷故障页

> 先跑 `status.py --run "$RUN"` 看 `NEXT=`，再按 E 顺序跑 `gate.py`；**第一个 FAIL / 缺回执处就是断点**。
> 下面按「现象 → 先看 → 处理」排；日志在 `$RUN/logs/`，只看末尾 40 行，不整份贴进对话。

## E1–E2

| 现象 | 先看 | 处理 |
|---|---|---|
| E1 还没问材料/牌组 | `preflight_choices.py --work "$W" --json` 的 `docs` / `decks` | 先走选择门：`ask_user_question` 多选材料（末项「全部处理」）+ 单选牌组（3–5 候选 +「自己填」）；再 `bootstrap --select … --deck-name …` |
| `preflight_choices.py` 报 `deck_error`（401/无 Key/网络） | `decks` / `deck_error` | 云端不可用**不阻塞**材料：把人话转达，牌组名让用户直接回复；阶段一照跑；**不建牌组** |
| `preflight_choices.py` 列不出材料 / `needs_user` | `docs` / `needs_user` | 缺原文/配对歧义是阻塞：按 `needs_user.message` 问用户；同名包选覆盖/保留 |
| `bootstrap.py` exit 3 `needs_user` | `run.json.preflight` | 按 `needs_user` 处理：配对歧义、同名包（覆盖 = `--archive-existing` / 保留 = 本次不选） |
| `run.json.deck_name` 为空 | E1 是否传 `--deck-name` | 补 `bootstrap.py --work … --deck-name "<精确名>"`（resume 不给则保留旧值）；E10 默认用它 |
| 用户「自己填」的牌组名对不上 | `decks` 候选 / App 里的名字 | 要用户给 App 里**完全一致**的精确名；E10 plan 唯一匹配失败会 exit 3 列候选，不猜 |
| `bootstrap.py` exit 3 `run_mismatch` | 是否显式 `--run` | 去掉 `--run` 重跑（旧 run 自动 `.prev-<ts>`） |
| 已有包冲突，用户其实只制卡 | 有没有 `--phase 2` | 加 `--phase 2`；「保留」= 不选该文档 = 阶段二没有 `doc.json` |
| 环境缺项（模型/ffmpeg/venv） | `bootstrap` 的 `env` | 阻塞：报告后停，不自行装 |
| `parse_source.py` 报 sha 变了 | `doc.json.text_sha256` | 输入变了 → 回 E1 拿新 run；不改旧 `blocks.json` |
| `gate e2` FAIL（覆盖/原子/块） | `blocks.json` + 原文 | 解析器问题：贴「行号 + 原文」报告；**不改用户文本** |
| `ERROR: 原文解析失败（第 N 行）` | stderr + 原文 | 旧格式或不合 contract v1：按迁移提示改标签；旧 md 用新 prompt（contract v1 原文）重转，别手改凑格式 |
| `missing_title` / `missing_ending` 只是 WARN | `blocks.json.warnings` | 缺开场/结尾不阻断 E2；不伪造标题/结尾行，原文要不要补由用户决定，不代写 |

## E3–E4

| 现象 | 先看 | 处理 |
|---|---|---|
| 子 agent 说 done 但回执不在 | `blocks/`、`dispatch.json` | 视为未完成，只重派缺块 |
| `gate e3` FAIL（schema/越块/改字） | `blocks/<block>.json` | 回执改名 `.bad-<ts>` 重派该块；不手修回执 |
| `assemble.py` exit 3 | stdout 缺失 task id | 回 E3 只补缺块，**不产半成品** |
| `assemble.py` exit 1（覆盖门/回执非法） | 日志、回执 | 坏回执改名 `.bad-<ts>` 重派；不手改 `segments.json` |
| `assemble.py` exit 1 `check merge: …`（`reason=merge_invalid`） | 该块回执的 `merge` 枚举 / 词数 / 问号 / speaker | 按消息改回执：多原子组只能写 `pure_response` / `fragment` / `compact_qa` / `dialogue_beat` / `turn`；回应/片段原子 ≤6 词，`compact_qa` 问句在前、整行 ≤24 词，`turn`/`dialogue_beat` 只用于对话块。改完重跑 `assemble.py`，不手改 `segments.json` |
| `assemble.py` 警告 `merge_unreviewed` | `segments.json.merge_candidates` 里 `reason_missing=true` 的行 | 旧回执没写 `merge` 理由（不阻塞汇编）；复核员必须在 `merge_review` 里逐条给 `keep`/`split` + `why`，否则 `gate e4` 拦 |
| `gate e4` FAIL `merge_review` | 最新 `review-<n>.json`、`segments.json.merge_candidates` | 复核回执缺 `merge_review`、漏候选、`why` 为空或没写明五类中的哪一类：让复核员补齐；判 `split` 的必须同时给 `overrides` 并重跑 `assemble.py --overrides` |
| 回执含 `split` / `parts` / `sub` | 该回执 | v1 不拆原子：删字段重派；要拆只能用户改原文另起 run |
| `gate e4` FAIL（复核回执） | `review-<n>.json` | 缺回执派复核员；`fixed` 未应用 → 带 `--overrides` 重跑 `assemble.py` |
| `gate e4` FAIL（行数/统计/区间） | `segments.json` vs 清单 | 回 E3/E4；不手改清单或切分文本 |

## E5

| 现象 | 先看 | 处理 |
|---|---|---|
| `translate_prepare` exit 3 | 清单是否已是 6 列 | 6 列但缺 `translations.json`：问清来历再决定重加列/复用 |
| `translate_apply` exit 1（行数/覆盖/格式） | `translate/*.json` | 重派对应任务；一句英文一句中文，不合并/不拆分 |
| `translate_apply` exit 1（`manifest_changed`） | `$RUN` 6 列清单 vs `manifest-5col.md` | 清单被手改：回 E5 重新合入；改完重新交付、重新定稿 |
| `translate_apply.py` exit 1 `reused_row_translated` | `translate-tasks.json.prefilled` 与 `translate/*.json` | 同一行既被译文记忆复用、又被回执翻了一遍；重编分组后让 `translate_prepare.py` 自动归档旧 `translate/`（不要加 `--keep-receipts`），旧 `translate-review-<n>.json` 也会随旧 `translate/` 一起归档；或手工移走旧回执再 `translate_apply.py` |
| 复核 fixes 没生效 | `translate-review-<n>.json`、`applied_reviews` | `--review` 可重复且默认累计；重跑 `translate_apply.py`，不手改译文 |
| `approve.py` exit 3 | 清单是不是 6 列 | 先出 6 列清单再定稿；不手写 `approve.json` |
| `gate e5` FAIL `manifest_finalized` | `approve.json` 在不在、绑的 sha 与 `$RUN` 6 列清单是否一致 | 先给用户看 6 列；认可后 `approve.py`，再 `gate e5`。approve 之前跑必 FAIL，属预期 |
| 用户要改分组/类型/进卡组 | 当前 6 列清单 | 回 E4 重汇编 + 复核 → 重译受影响行 → 重新 E5 定稿；**不切音频**（E6 才切） |

## E6–E8

| 现象 | 先看 | 处理 |
|---|---|---|
| 不知道能不能切 | `status.py` 的 `NEXT=` | 先过 E5 定稿（`gate e5 PASS`）；不要用 `gate e6` 探路（首跑必 FAIL），直接 `cut_stage --stage cut` |
| `cut_stage --stage cut` exit 3（E5 未定稿） | `approve.json`、`gate e5` | 回 E5 交付 6 列清单、用户认可后 `approve.py`；没定稿不切 |
| `cut_stage` exit 3「另一个 cut/pack 正在跑…E6/E7 必须串行」 | `$RUN/.locks/e6-e7.lock` | 同一 `$RUN` 同时只允许一个 cut/pack；等它结束再跑，或用 `status.py` 看 `CUT_RUNNING` / `NEXT`；不要删别人正持有的锁 |
| `status.py` 报 `stale lock` | 锁文件里的 `pid` | pid 已不在（上个 cut/pack 崩了）；**status 不会自动删**，确认没有其它 cut/pack 后手动 `rm $RUN/.locks/e6-e7.lock` 再重跑 |
| 想看切分进度 | `docs/<doc>/cut-cutter.log`、`status.py` 的 `CUT_RUNNING`/`CUT_LAST_LOG` | 0.0.10 起 cutter 的 stdout+stderr 实时进 `cut-cutter.log`；`tail -f` 或重跑 `status.py --run "$RUN"`（`--json` 里有 `cut_running` / `cut_last_log`） |
| `cut_stage` 超时 exit 1 | `cut-cutter.log` 末尾、`$RUN/logs/e6-*` 末尾 200 行 | 默认 `--timeout 3600`（25 分钟音频实测约 15 分钟，给 2 倍余量）；先确认是真卡住还是机器慢，再后台重跑，不要用 60 s 前台硬等 |
| `cut_stage` 切前 exit 3「原文含中文行…」 | `cut --version`、`切分文本.txt` | cutaudio 1.1.0 只要有一行 CJK 就放弃 ASR 锚定并把长音频压时；先装 1.1.1（见 `系统/README`），或把非英文行改为纯英文；≥1.1.1 自动放行并在 receipt 记 `mixed_script_text=true` |
| `cut_stage` 切后 exit 3「对齐只覆盖 X% 音频…不要用这份切分」 | `cut-report.json.alignment`（`cut_mode=edge` + `coverage<0.95`） | 别用这份 ZIP：大概率是原文含中文/原文与音频不匹配；先处理该 doc 的原文或升级 cutaudio，再 `cleanup.py --doc` + `cut_stage --stage cut --reset`；`cut-report.json` 留着排查 |
| `WARN alignment_coverage_missing` | 旧 cutaudio 的报告没有 `coverage` | 兼容旧报告，只 WARN 不拦；升级到 1.1.1 后重切才有覆盖率可审 |
| `WARN alignment_low_matched` | `alignment.cut_mode=wave` 且 `matched<0.9` | wave 有 ASR 时间轴，通常仍可用，只 WARN；按 issue/听感决定要不要重切 |
| `cut_stage` 报告 `reason=alignment_incomplete` | `cut-report.json.alignment.coverage` | 就是上面那条低覆盖拒切（edge + 覆盖 <95%）：这份切分不能用；先处理该 doc 的原文或升级 cutaudio，再 `cleanup.py --doc <doc>` → `cut_stage --stage cut --reset` |
| receipt `warnings` 里有 `mixed_script_text=true` | `切分文本.txt` 是否含中文行、`cut --version` | 不阻断：cutaudio ≥1.1.1 按「英文主导」继续走 ASR 锚定；若切出来仍错位，先处理原文再切。1.1.0 会在切前 exit 3（见前面「原文含中文行」那条） |
| `cut_stage` exit 3 `reason=cut_lock_busy` | `$RUN/.locks/e6-e7.lock`、`status.py --json` 的 `cut_running` | 另一个 cut/pack 正在跑：等它结束再跑；E6/E7 必须串行，不要并行、不要删活锁 |
| `cut_stage` exit 2（结果已存在） | `doc.result_dir`、`cut-report.text_sha256` | 同文本会 resume；文本变了/缺绑定才 `cleanup.py --doc` → `--reset` |
| `cut_stage` exit 3 / `needs_review`（进卡组行仍有 issue） | `cut-report.json`、`issues.md` | 停下问用户：贴段号 + 原因，选项重切/改文本/放弃；非卡片行（开场/报幕/指令/结尾）的 `ignored` issue 不阻断，仍写全量结果 |
| `gate e6` FAIL（zip/segments/issues） | `cut-report.json` | 重切或问用户；不改切分文本凑过 |
| `gate e6` FAIL `alignment_coverage` | `cut-report.json.alignment`（`cut_mode=edge` + `coverage<0.95`） | 与 `alignment_incomplete` 同一问题：这份切分不能用；处理原文或升级 cutaudio 后 `--reset` 重切，别手改报告绕关卡 |
| 包已交付后 `gate e6` 报 `manifest_sha` 不一致 | `cut-report.json` | 只 **WARN**，不硬拦；E6 只绑 `manifest-5col.md` |
| 单独 `gate e6` 报 ZIP 不存在、但包已交付 | `cut-report.json.zip.path` | e8 记 `WARN zip_cleaned`；单独 e6 仍 FAIL，属预期 |
| `cut_stage --stage pack` exit 2 | `cut-report.json` | 先跑 E6；ZIP 只从 `zip.path` 取，不猜路径 |
| `gate e7` FAIL（包内容/清单 sha） | `pack.json`、包目录 | 重跑 `--stage pack`；查 ZIP、`manifest-5col` 与 6 列清单是否同一轮 |
| `gate e8` FAIL（残留/缺报告/缺回执） | `产出区`、`receipts/cleanup.json` | 按 `cleanup.py` → `gate phase1` → `report.py` → `gate e8` 重跑；不手删包 |
| `report.md` 关卡列空白 | 是否先跑 `gate phase1` | E8 顺序必须 cleanup → phase1 → report → e8 |

## E9

| 现象 | 先看 | 处理 |
|---|---|---|
| `gate e9` FAIL `manifest_finalized` | `approve.json` 的整份 sha 与 `rows[{n,sha256}]` vs 当前 6 列清单 | 没定稿 / 定稿后清单又改：回 E5 重新交付 + `approve.py`；若是 `zh_fix.py apply` 改的，按下方顺序重新定稿 |
| `cards_build` exit 3 | `doc.json.stem` | 用 `--id-scheme` 给 chapter/prefix/volume；不改用户文件名 |
| `cards_apply` exit 1（文本被改/前缀非法） | `cards/<task>.json` | 重派该卡所在排版任务；不手改 `cards.json` |
| `cards_apply --review` 生成了 `zh-fixes.md` 但 `cards.json` 没变 | 预期 | 只报不改；把表格交用户逐条敲定，再 `zh_fix.py apply` |
| `zh_fix.py apply` exit 1 句数不符 / 非 keep 行 / 缺 translations | 校对回执 `zh_fixes`、6 列清单、`translations.json` | `zh_fixes.zh` 必须是整张卡、`len(zh)==英文句数`、只改 keep 行；缺 `translations.json` 先跑 E5；不要手改清单 |
| `zh_fix.py apply` exit 1 `manifest_changed` | `translations.json.manifest_sha256_v2` vs 当前 6 列清单 | 清单在 E5 后被改过：先回 E5 重出清单，不要手改；`zh_fix.py` 不写 `approve.json` |
| `cards_apply --refresh-zh` exit 3 缺 `cards-base.json` / 排版回执 | `cards-base.json`、`cards/` | 先 `cards_build.py` + 排版员回执；`--refresh-zh` 只刷新中文，不重跑 layouter |
| `cards_apply --refresh-zh` 报中文行数不符 / `zh_not_keep_row` | 6 列清单 / `cards.json` | 先 `zh_fix.py apply` 把清单改对；不要手改 `cards.json` / 清单 |
| `cards_apply` exit 1 `zh_fix_requires_e5`（`fixes[].zh`） | 校对回执的 `fixes` | 中文建议写独立的 `zh_fixes`；`fixes` 只收 `en_lines`。**不是回 E5 重跑** |
| 中文只接受一部分 | 用户的批准文件 | `zh_fix.py apply --fixes /tmp/zh-approved.json` 只含接受行；未接受行保留原译并告知用户（gate 不为此 FAIL） |
| 中文句数/说话人对不上 | `cards.json` 的 `zh` / `en_lines` | E9 按句末标点切句、说话人只作句首标记；先比 `sentence_count` 与译文行数，不按说话人段数拆行 |

## E10–E12

| 现象 | 先看 | 处理 |
|---|---|---|
| `markji_push --stage plan` exit 3 | 报错里的候选 | 牌组/章节不唯一或缺失：问用户确认精确名；**不建牌组** |
| plan 输出 `E10 闸门：老牌组` | `plan.gate.mode=veteran`（`deck.card_count>0`） | 正常：不写样卡、不需要 `approve2`，`gate e10` 走 `gate_veteran` 后直接 push；`diff_pending` 默认跳过 |
| plan 输出 `E10 闸门：新/空牌组` | `plan.gate.mode=first`（`card_count=0`） | first：先 `--stage sample` 写 1 张（不需要 approve2），用户 App 看过后再 `approve2.py --sample-no … --chapter-ok` |
| `sample` exit 3 缺 `approve2.json` | 顺序 | first 样卡**先写、后 approve2**；不要按旧顺序先写批准。veteran 直接跳过 sample |
| `first_sample_pending`（重跑 plan 仍 first） | `receipts/markji-sample.json` + 还没有成功 push | 样卡已写：只补 `approve2.py --sample-no … --chapter-ok`，**不要重写样卡**；成功 push 后才变 veteran |
| `approve2.py --veteran` 被拒 `gate_mismatch` | plan gate 是 first | 首个新牌组不能跳过样卡：按 first 流程 `sample` → App 看 → `approve2 --sample-no … --chapter-ok` |
| `--updates-ok` 被拒 `updates_not_applicable` | `counts.diff_pending` | 计划里 0 张差异：不需要授权；有差异时先让用户看差异清单再批 |
| `sample` exit 1 读回不一致 | `receipts/markji-sample.json` | 不 push；定位 content 还是 audio id；修本地后重新 plan → 重新样卡 |
| `sample_stale` / `sample_mismatch` | 回执 `plan_sha256` / `cards_sha256` / `no` | 与当前 plan/cards/approve2 对不上：重跑 plan；`first_sample_pending` 时只补 approve2，否则重做样卡 |
| `push` exit 3 老牌组缺 `approve2` | 预期 | 老牌组不需要令牌；只有覆盖 `diff_pending` 才 `approve2.py --veteran --updates-ok` + `push --allow-update` |
| 老牌组 `diff_pending` 被跳过 WARN | 默认保护 | 用户明确要覆盖才加两个参数；否则保留用户手改版 |
| `gate e10` 打 `gate_veteran` | 老牌组自动放行 | 正常，不是跳过风险检查：plan/deck/cards/decision 仍照查 |
| 数量不对 | push 回执 `skipped[]`、`markji.json` | 用 `--limit` 定位后重跑（create/skip 幂等） |
| 顺序不对 | 章节 `card_ids` vs `cards.json.order` | 重跑 `--stage push`（会重排，最多两轮）；`cards` 返回序不可信 |
| `--limit` 后 gate e11 报顺序 | 回执 `partial` / `order_verified` | 预期：`partial=true` 不重排、`order_verified=false`；全量 push 再验证 |
| `common_too_many_request` | 频控 | 等 10 秒重试（脚本内建 ≤6 次）；别缩间隔、别并发 |
| plan 有 `diff_pending` | 用户取舍 | 默认跳过；用户同意才 `--updates-ok` + `--allow-update`（veteran 用 `--veteran`） |
| `markji_sync --check` exit 1 | `receipts/markji-sync.json.diffs[]` | 有差异（不是崩）：给用户看，确认后 `--update` |
| `markji_sync --update` exit 3 `cards_changed` | `approve2.cards_sha256` vs 当前 `cards.json` | 本地改过卡（E9 `zh_fix.py` / 断行 / E5 重译）：重新 plan + approve2，再 update |
| `gate e12` 仍 FAIL | `diffs[]` | 用户不同意覆盖 → 回 E9/E5 对齐本地；否则让用户点头覆盖 |
| `markji_channel.py` 401/403 | Key 状态 | 停下转达人话指引；用户给新 Key 后 `key --set` |

## 通用

- **任何 gate 打 `WARN drift`**：脚本树变了；不要跨版本混跑，`--fresh` 重建或从 E3 往后并写明。
- **子 agent 回复不算事实**：只有回执文件 + `gate.py` 算。
- **不确定就停**：手改一处会让整条证据链对不上；宁可停下问用户或 root。

