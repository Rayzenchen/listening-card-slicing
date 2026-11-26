# 工作手册 · 一页状态机 + 命令索引

> 本文件只做索引：状态机、命令从哪来、退出码。详细命令/产物见 [runtime/pipeline.md](runtime/pipeline.md)，
> 解析/清单/云端/排障见 [runtime/](runtime/)。参数以 `python3 scripts/<脚本> --help` 为准。
> 派发默认 `workflow`（每波 ≤8），见 [subagent-briefs.md#dispatch](subagent-briefs.md#dispatch) 与 [runtime/fanout.md](runtime/fanout.md)。

## 1. 变量

```bash
W="$(pwd)"; S="$HOME/.agents/skills/listening-card-slicing/scripts"
MK="$HOME/.agents/skills/markji-channel/scripts/markji_channel.py"
RUN="<bootstrap 首行 RUN=>"
```

每条 bash 都是新 shell，四条变量重新给；`RUN` 不凭记忆拼。入口 B（继续已交付包）用 `bootstrap --phase 2`。

## 2. 状态机

```text
E1 preflight_choices(只读)→★选择门(第 1 次回话：材料/牌组)→bootstrap(--select/--deck-name) ─e1→ E2 parse ─e2→
E3 派裁定员(workflow≤8) ─e3→ E4 assemble ─e4→
E5 translate_prepare→翻译→translate_apply→译文复核(+fixes→--review) ─e5→
   ★用户对照译文看分组（第 2 次回话）→ approve.py（定稿）→ gate e5 PASS
E6 cut(后台·串行锁) ─e6→ E7 pack ─e7→ E8 cleanup→phase1→report ─e8→
E9 cards_build→排版→cards_apply→校对(fixes/zh_fixes→--review)→（用户敲定 zh_fixes→zh_fix.py apply→approve.py→cards_apply --refresh-zh）─e9→
E10 whoami→plan(只读；gate=first/veteran)→
    first: sample(写 1 张)→★用户 App 看样卡（第 3 次回话）→approve2→ ─e10→
    veteran: 自动放行（免 sample/approve2）→ ─e10→
E11 push ─e11→ E12 sync --check/--update ─e12
```

## 3. 命令索引

| 步 | 命令（省略 `python3 "${S}/"`） | 一句话 |
|---|---|---|
| E1 | `preflight_choices.py --work "$W" --json` → `bootstrap.py --work "$W" --select d01,d02 --deck-name "<精确名>" --json` | 只读列材料 + 云端牌组候选 → 提问选材料/牌组 → 建/续 `$RUN`（`--phase 2` 复用已有包） |
| E2 | `parse_source.py --run "$RUN" --doc <doc> --json` | 原文 → `blocks.json` + `dispatch.json`（只认 contract v1；开场/结尾可缺；旧格式直接 FAIL） |
| E3 | （派裁定员）`gate.py e3 --run "$RUN" --doc <doc>` | 裁定员回执齐不齐；派发用 `workflow` 每波 ≤8（[fanout.md#e3](runtime/fanout.md#e3)） |
| E4 | `assemble.py --run "$RUN" --doc <doc> [--overrides F] --json` | 回执 + 确定性片段 → 清单/切分文本；0.0.10 输出 `merge_candidates`，复核员回执须有 `merge_review` 逐条覆盖（五类 merge；`turn` / `dialogue_beat` 仅对话块；细节见 [roles/reviewer.md](roles/reviewer.md) 的 merge_review 规则；gate e4 机检） |
| E5 | `translate_prepare.py --run "$RUN" --doc <doc> [--reuse-memory\|--no-reuse-memory] [--keep-receipts] --json` | 切翻译任务（已有 6 列会反解）；默认复用译文记忆（整行 + 句级）、只派新增/变化的多句行；`--no-reuse-memory` 关闭；重编后旧 `translate/` 默认归档 `translate.prev-<ts>/` |
| E5 | `translate_memory.py snapshot --run "$RUN" --doc <doc> [--from PATH] --json` | 译文记忆排障入口（整行 + 句级 `sentences`；`translate_prepare` 默认自动做同样的事） |
| E5 | `translate_apply.py --run "$RUN" --doc <doc> [--review F] --json` | 合入译文 + 复核 fixes，出 6 列清单（$RUN + 产出区） |
| E5 | `approve.py --run "$RUN" --doc <doc> --json` | 用户定稿：绑当前 6 列清单 sha；随后 `gate e5` 才 PASS |
| E6 | `cut_stage.py --run "$RUN" --doc <doc> --stage cut --timeout 3600 --json` | 后台切原声（先过 E5；cutaudio ≥ 1.1.0，混排原文需 ≥ 1.1.1；`--review-ignore` 只卡进卡组行；约 15 分钟；进度看 `docs/<doc>/cut-cutter.log`）。**必须串行**：锁 `$RUN/.locks/e6-e7.lock`，同时再跑一个 cut/pack → exit 3；`--timeout` 默认 3600 |
| E7 | `cut_stage.py --run "$RUN" --doc <doc> --stage pack --json` | 分卡成包；同文本 `--stage all` 会 resume |
| E8 | `cleanup.py --run "$RUN"` → `gate.py phase1` → `report.py` → `gate.py e8` | 顺序别反 |
| E9 | `cards_build.py --run "$RUN" --doc <doc> --chunk 6 --json` | 派生卡片 ID/顺序/断行任务 |
| E9 | `cards_apply.py --run "$RUN" --doc <doc> [--review F] [--refresh-zh] [--preview N\|--full-preview] --json` | 合入断行 + 校对 en_lines，渲染卡片；`--review` 另出 `zh-fixes.md`；`--refresh-zh` 从 6 列清单刷新中文（不重跑 layouter）；预览默认前 1–3 张样卡 |
| E9 | `zh_fix.py table/apply --run "$RUN" --doc <doc> --review F [--accept all\|N,N\|--fixes F] --json` | 中文表格 / 用户敲定后直改 `translations.json` + 6 列清单；不写 `approve.json` |
| E10 | `markji_push.py --run "$RUN" --doc <doc> --stage plan --json` | 只读预检 + 三态计划；写 `deck.card_count` + `gate.mode` |
| E10 | `markji_push.py --run "$RUN" --doc <doc> --stage sample [--no NNN] --json` | first：写 1 张样卡并读回（不要求 approve2）；veteran：只写 skipped 回执，不写云端 |
| E10 | `approve2.py --run "$RUN" --doc <doc> [--sample-no NNN --chapter-ok\|--veteran] [--updates-ok] --json` | first：App 看样卡后的确认令牌；veteran：仅覆盖 `diff_pending` 的授权 |
| E11 | `markji_push.py --run "$RUN" --doc <doc> --stage push --interval 0.6 --json` | 批量 create/skip + 重排 + verify |
| E12 | `markji_sync.py --run "$RUN" --doc <doc> --check\|--update --json` | 只报告 / 只更新差异卡 |
| 全程 | `gate.py <e1..e12\|phase1> --run "$RUN" [--doc <doc>]` | 从磁盘/云端重算；末行 PASS/FAIL |
| 全程 | `status.py --run "$RUN"` | 只读 `NEXT=`；排障第一步；切分中会多打 `CUT_RUNNING doc=… stage=… elapsed=… log=…` 与 `CUT_LAST_LOG=…` |

## 4. 退出码与输出

- `0` 成功 / `1` 检查或执行失败 / `2` 用法或环境 / `3` 需要用户决定。`gate.py` 只有 0/1/2。
- `--json` 时脚本最后一行是 `JSON <单行 JSON>`；`gate.py` 最后一行永远是 `PASS` / `FAIL: <摘要>`。
- 判读只认 `rc=0` + 最后一行 `PASS`；日志在 `$RUN/logs/`，只看末尾 40 行。

## 5. 断点

- `status.py` → `NEXT=`；或从 `gate e1` 起依次跑，第一个 FAIL / 缺回执处即断点。`status.py` 打印 `CUT_RUNNING` 时说明 E6/E7 正在跑（串行锁被占），等它结束再继续，不要重开。
- 输入内容没变重跑 `bootstrap` 会拿回同一个 `$RUN`；内容变 → 新 run，旧 run `.prev-<ts>`，从 E2 重跑。改过 `keep` 而切分文本没变 → 旧 `review_ignore` 与当前 segments 不一致，`gate e6` 要求 `--reset` 重切（审计，不是 bug）。
- 脚本树变了会打 `WARN drift`；不跨版本混跑。交付确认前不要 `--purge-run`。
- 详细断点表见 [runtime/pipeline.md](runtime/pipeline.md) §5；故障表见 [runtime/troubleshooting.md](runtime/troubleshooting.md)。

