# 运行时 · 扇出与派发（workflow 优先）

> 0.0.10 起：语义任务（分组 / 翻译 / 断行 / 复核 / 校对）**默认走 `workflow`**，每波 ≤ 8 个 `agent()`。
> 规则速查见 [subagent-briefs.md](../subagent-briefs.md#dispatch)；命令见 [pipeline.md](pipeline.md)；角色卡在 [roles/](../roles/)。
> 本文件不复制角色卡正文，只给「怎么派、怎么收、失败怎么办」。

## 1. 每波为什么是 8

2026-11-26 本机实测（`session-7fb77e2f`，CET-4 25年06月 卷1/卷2）：

| 派法 | 实测结果 |
|---|---|
| `workflow` 一次 16 个 `agent()` | **整 run 报错**：`subagent limit reached (active child limit: 10)`；0 个回执 |
| 同一批改 9 + 7 两波 | 16/16 `done`，约 3 分钟 |
| 2 个复核员用裸 `subagent` | 2/2 `failed before it finished`（3–8 分钟才报，无回执） |
| 同一份复核改 `workflow` 2 个并行 | 2/2 `done`，约 5 分钟 |

- 本机 `active child limit = 10`（2026-11-26 实测）；全局 AGENTS.md 写的 128 与本次实测不符，**不要照抄那个数**。
- 超限的后果不是「某个任务变成 null」，而是**整个 workflow run 报错终止**；已启动的子 agent 可能还在跑、还会写回执。所以宁少不多：`WAVE=8`，同一时段还有别的手动子 agent 在飞就降到 6。
- `workflow` 调用本身是 await 的：脚本里每波 `await parallel` 等齐再下一波，整个 run 返回后直接看结果；**不要 sleep、不要轮询**。

<a id="prepare"></a>
## 2. 派前准备（三步都做）

```bash
RUN=<RUN>; DOC=<doc>
S="$HOME/.agents/skills/listening-card-slicing/scripts"
SK="$(dirname "$S")"
```

下面的 bash 片段都假设这四行已在同一条命令里重新给（每条 bash 都是新 shell）。

### 2.1 预建回执目录

```bash
mkdir -p "$RUN/docs/$DOC/blocks" "$RUN/docs/$DOC/translate" "$RUN/docs/$DOC/cards"
```

目录先建好，回执路径就是唯一事实；缺目录时子 agent 会多绕一步，失败也更难查。

### 2.2 列缺回执 + 生成 JS 任务数组

```bash
python3 - "$RUN" "$DOC" <<'PY'
import json, os, sys
run, doc = sys.argv[1:3]
base = os.path.join(run, "docs", doc)
for f in ("dispatch.json", "translate-tasks.json", "card-tasks.json"):
    p = os.path.join(base, f)
    if not os.path.exists(p):
        continue
    print("#", f)
    miss = []
    for t in json.load(open(p, encoding="utf-8"))["tasks"]:
        r = t["receipt"]
        if not os.path.isabs(r):
            r = os.path.join(base, r)
        name = t.get("task_id") or t["task"]
        print("HAVE" if os.path.exists(r) else "MISS", name)
        if not os.path.exists(r):
            miss.append(name)
    print("const TASKS = " + json.dumps(miss, ensure_ascii=False) + ";")
PY
```

- 只派 `MISS`；`HAVE` 的不重派（重派会覆盖已验收回执）。
- 用**对应步骤那个文件**下面的 `const TASKS = [...]`（E3 看 `# dispatch.json`，E5 看 `# translate-tasks.json`，E9 看 `# card-tasks.json`），整行贴进模板，避免手抄漏项。

### 2.3 预渲染任务单（每个任务一个 .md）

`workflow` 脚本**没有文件系统**，只能拼字符串；任务数据（ATOMS / ROWS / CARDS）必须提前落成任务单文件，prompt 只写「读这个文件」。这样也防止子 agent 拿错块。

```bash
# ROLE=segmenter | translator | layouter（对应 dispatch / translate-tasks / card-tasks）
ROLE=segmenter
python3 - "$RUN" "$DOC" "$SK" "$ROLE" <<'PY'
import json, os, sys
run, doc, sk, role = sys.argv[1:5]
base = os.path.join(run, "docs", doc)
src = {"segmenter": "dispatch.json", "translator": "translate-tasks.json",
       "layouter": "card-tasks.json"}[role]
book = json.load(open(os.path.join(base, src), encoding="utf-8"))
card = open(os.path.join(sk, "references/roles", role + ".md"), encoding="utf-8").read()
out = os.path.join(base, "prompts")
os.makedirs(out, exist_ok=True)
for t in book["tasks"]:
    task = t.get("task_id") or t["task"]
    receipt = t["receipt"]
    if not os.path.isabs(receipt):
        receipt = os.path.join(base, receipt)
    lines = ["# 任务单 · %s（role=%s）" % (task, role),
             "DOC: %s" % doc, "TASK_ID: %s" % task, "AGENT_NAME: %s" % task]
    if role == "segmenter":
        lines += ["BLOCK: %s" % t["block"], "POSITION: %s" % t["position"],
                  "ATOMS: %s" % json.dumps(t["atoms"], ensure_ascii=False)]
    elif role == "translator":
        lines += ["N: %d" % len(t["rows"]),
                  "ROWS: %s" % json.dumps(t["rows"], ensure_ascii=False)]
    else:
        lines += ["N: %d" % len(t["cards"]),
                  "CARDS: %s" % json.dumps(t["cards"], ensure_ascii=False)]
    lines += ["RECEIPT: %s" % receipt, "", "---", "（以下为角色卡全文）", "", card]
    open(os.path.join(out, task + ".md"), "w", encoding="utf-8").write("\n".join(lines))
print("prompts=%d dir=%s" % (len(book["tasks"]), out))
PY
```

- 幂等：重复跑只会覆盖任务单，不影响已写回执。
- 三段模板只差三处：`ROLE`、`TASKS` 来源、prompt 里的角色名；其余照抄。

<a id="skeleton"></a>
## 3. 通用骨架（三段共用）

- `const WAVE = 8;`，`chunks(tasks, WAVE)` 切波，每波 `await parallel(...)` 等齐再下一波。
- `agent()` 失败返回 `null`（不是异常）：收集进 `failed`；**只重派失败项**（把返回的 `failed` 填进下一次调用的 `TASKS`，最多 2 轮），不整批重来。
- `log()` 每波进度；脚本最后只 `return` 一个 JSON 摘要（不要把 prompt/回执正文带回来）。
- **不在脚本里跑 gate、不读回执**（没有文件系统）：关卡一律回到根 agent 侧跑。
- `agent()` 的 `label` 用任务名（同一批里唯一）；重派是第二次 workflow 调用，进度另起一批。

<a id="e3"></a>
## 4. E3 · 按块派裁定员

### 4.1 准备

```bash
# 2.1 建目录；2.2 取 dispatch.json 的 MISS → const TASKS；2.3 预渲染（ROLE=segmenter）
```

派前确认 `receipt` 不存在；一块只派一次。任务单按 [subagent-briefs.md#segmenter](../subagent-briefs.md#segmenter)（= `roles/segmenter.md`）。

0.0.10 判据（模板不变，prompt 只指向任务单）：先分对话块（M/W 都有）/独白块——对话块默认「连续同 speaker 回合 = 一行」（`turn`，≤44 词），独白块默认「一个原子一行」；允许并五类 `turn` / `pure_response` / `fragment` / `compact_qa` / `dialogue_beat`，`turn` / `dialogue_beat` 仅对话块、独白块只前三类。

### 4.2 workflow 脚本（照抄）

```js
// E3 · 裁定员：一块一个 agent，每波 8
const WAVE = 8;
const TASKS = ["d01-b05", "d01-b10"];          // 用 2.2 里 dispatch.json 的 const TASKS 整行替换
const DIR = "<RUN>/docs/<doc>/prompts/";       // 2.3 的 prompts 目录（绝对路径）
const PHASE = "E3 分组裁定";
phase(PHASE);
const chunks = (xs, n) => {
  const out = [];
  for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n));
  return out;
};
const prompt = (id) => [
  "你是「裁定员」子 agent（role=segmenter）。先用 read 工具读完整任务单：",
  DIR + id + ".md",
  "任务单里有角色卡全文、ATOMS 原样 JSON、RECEIPT 绝对路径；严格照它执行。",
  "只写 RECEIPT 那一个回执文件（原子写：先写 <RECEIPT>.tmp-<pid> 再 os.replace）；",
  "不改原文；不跑其它脚本；不派子 agent。完成后只回一行：done <回执绝对路径> + ≤120 字备注。",
].join("\n");
const ok = [];
const failed = [];
let waveNo = 0;
for (const wave of chunks(TASKS, WAVE)) {
  waveNo += 1;
  log("wave " + waveNo + ": " + wave.join(", "));
  const results = await parallel(wave.map((id) => () => agent(prompt(id), { label: id, phase: PHASE })));
  wave.forEach((id, i) => {
    if (results[i]) { ok.push(id); log("ok " + id); }
    else { failed.push(id); log("FAIL " + id); }
  });
  log("wave " + waveNo + " done: ok=" + ok.length + ", failed=" + failed.length);
}
return { step: "e3", total: TASKS.length, ok: ok.length, failed, done: ok };
```

### 4.3 收口

```bash
python3 "${S}/gate.py" e3 --run "${RUN}" --doc <doc>
```

- 缺回执 → 把 `failed` ∪ gate 列出的 MISS 再跑一遍**同一段脚本**（重试 1）；回执非法 → 改名 `.bad-<ts>` 后只补派它。
- 仍缺 → 只重跑剩下的（重试 2）；第 2 次补派仍缺 → 写 `.failed-<ts>.json`（§8）并报告根 agent，不静默跳过。
- 复核员是另一个任务（§7），不要和裁定员混在一波里省事。

<a id="e5"></a>
## 5. E5 · 按翻译任务派翻译员

### 5.1 准备

```bash
# 2.1 建 translate/ 目录；translate_prepare.py 之后用 2.2 取 MISS；2.3 预渲染（ROLE=translator）
```

- 只派 `translate-tasks.json` 里 `MISS` 的 task；`HAVE` 的只补缺。
- `translate_prepare.py` 默认复用译文记忆：未变行进 `prefilled`，`tasks` 里只剩新增/变化的行，**prefilled 的行没有任务、不用派**。`--no-reuse-memory` 恢复全量重译；重编后旧 `translate/` 回执默认归档为 `translate.prev-<ts>/`（`--keep-receipts` 保留）。
- 任务单按 [subagent-briefs.md#translator](../subagent-briefs.md#translator)（= `roles/translator.md`）；排障用 `translate_memory.py snapshot --run … --doc … --json`。

### 5.2 workflow 脚本（照抄）

```js
// E5 · 翻译员：一个翻译任务一个 agent，每波 8
const WAVE = 8;
const TASKS = ["d01-t01", "d01-t02"];          // 用 2.2 里 translate-tasks.json 的 const TASKS 整行替换
const DIR = "<RUN>/docs/<doc>/prompts/";
const PHASE = "E5 翻译";
phase(PHASE);
const chunks = (xs, n) => {
  const out = [];
  for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n));
  return out;
};
const prompt = (id) => [
  "你是「翻译员」子 agent（role=translator）。先用 read 工具读完整任务单：",
  DIR + id + ".md",
  "任务单里有角色卡全文、ROWS 原样 JSON、RECEIPT 绝对路径；严格照它执行。",
  "只译 ROWS 里的 keep 行（len(zh) == sentence_count）；只写 RECEIPT 那一个回执（原子写）；",
  "不改原文；不跑其它脚本；不派子 agent。完成后只回一行：done <回执绝对路径> + ≤120 字备注。",
].join("\n");
const ok = [];
const failed = [];
let waveNo = 0;
for (const wave of chunks(TASKS, WAVE)) {
  waveNo += 1;
  log("wave " + waveNo + ": " + wave.join(", "));
  const results = await parallel(wave.map((id) => () => agent(prompt(id), { label: id, phase: PHASE })));
  wave.forEach((id, i) => {
    if (results[i]) { ok.push(id); log("ok " + id); }
    else { failed.push(id); log("FAIL " + id); }
  });
  log("wave " + waveNo + " done: ok=" + ok.length + ", failed=" + failed.length);
}
return { step: "e5", total: TASKS.length, ok: ok.length, failed, done: ok };
```

### 5.3 收口

```bash
python3 "${S}/translate_apply.py" --run "${RUN}" --doc <doc> --json
python3 "${S}/gate.py" e5 --run "${RUN}" --doc <doc>
```

- `zh` 行数 ≠ 英文句数 / 缺回执 → **只重派那一个任务**（最多 2 轮重试），不整批重来。
- 全部行都 prefilled 时 `tasks=0`：不用派翻译员，直接 `translate_apply.py`；**仍要派译文复核员**。

<a id="e9"></a>
## 6. E9 · 按卡片任务派排版员

### 6.1 准备

```bash
# cards_build.py 之后：2.1 建 cards/ 目录；2.2 取 card-tasks.json 的 MISS；2.3 预渲染（ROLE=layouter）
```

- 一个任务 = `--chunk` 张卡（默认 6 张）；任务单按 [subagent-briefs.md#layouter](../subagent-briefs.md#layouter)（= `roles/layouter.md`）。
- 只派 `MISS`；断行改字 → 只重派那张卡所在的任务。

### 6.2 workflow 脚本（照抄）

```js
// E9 · 排版员：一个断行任务一个 agent，每波 8
const WAVE = 8;
const TASKS = ["d01-c01", "d01-c02"];          // 用 2.2 里 card-tasks.json 的 const TASKS 整行替换
const DIR = "<RUN>/docs/<doc>/prompts/";
const PHASE = "E9 断行";
phase(PHASE);
const chunks = (xs, n) => {
  const out = [];
  for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n));
  return out;
};
const prompt = (id) => [
  "你是「排版员」子 agent（role=layouter）。先用 read 工具读完整任务单：",
  DIR + id + ".md",
  "任务单里有角色卡全文、CARDS 原样 JSON、RECEIPT 绝对路径；严格照它执行。",
  "只加 `- `/`-- ` 与换行，一个字都不改；只写 RECEIPT 那一个回执（原子写）；",
  "不改原文；不跑其它脚本；不派子 agent。完成后只回一行：done <回执绝对路径> + ≤120 字备注。",
].join("\n");
const ok = [];
const failed = [];
let waveNo = 0;
for (const wave of chunks(TASKS, WAVE)) {
  waveNo += 1;
  log("wave " + waveNo + ": " + wave.join(", "));
  const results = await parallel(wave.map((id) => () => agent(prompt(id), { label: id, phase: PHASE })));
  wave.forEach((id, i) => {
    if (results[i]) { ok.push(id); log("ok " + id); }
    else { failed.push(id); log("FAIL " + id); }
  });
  log("wave " + waveNo + " done: ok=" + ok.length + ", failed=" + failed.length);
}
return { step: "e9", total: TASKS.length, ok: ok.length, failed, done: ok };
```

### 6.3 收口

```bash
python3 "${S}/cards_apply.py" --run "${RUN}" --doc <doc> --json
python3 "${S}/gate.py" e9 --run "${RUN}" --doc <doc>
```

- 断行改字 → 只重派那张卡的任务（最多 2 轮重试）；中文问题不重派排版员：校对员写 `zh_fixes` → `cards_apply.py --review` 出 `zh-fixes.md` → 用户敲定 → `zh_fix.py apply` → `approve.py` → `cards_apply.py --refresh-zh` → `gate e9`（不重跑 E3/E5/E6/E7/E8，不回 E5 重跑上游）。
- 校对员（`#proofreader`）是另一个任务（§7），用 workflow 派，不要用裸 `subagent`。

<a id="reviewers"></a>
## 7. 复核员 / 译文复核员 / 校对员：也用 workflow

复核类任务要通读全量、容易被中断；**2 份文档 = 2 个 `agent()` 并行，不要两个裸 `subagent`**（实测 2/2 失败）。只有 1 份文档也走 workflow，1 个 agent 即可。

| 步 | 角色卡 | 只读输入（由 prompt 给绝对路径） | 回执 |
|---|---|---|---|
| E4 | `roles/reviewer.md` | `segments.json`（含 `merge_candidates`）、切分清单、`blocks.json` | `review-<n>.json` |
| E5 | `roles/translation-reviewer.md` | `translations.json`、6 列清单、`blocks.json` | `translate-review-<n>.json` |
| E9 | `roles/proofreader.md` | `cards.json`（全量）、`translations.json`、6 列清单 | `cards-review-<n>.json`（`fixes` 只收 `en_lines`；中文写 `zh_fixes`） |

```js
// E4 · 复核员：一份文档一个 agent，两卷并行（E5/E9 只改角色卡、输入与回执名）
const DOCS = ["d01", "d02"];
const PHASE = "E4 复核";
const SK = "<SK>";                       // 技能根：~/.agents/skills/listening-card-slicing
const RUN = "<RUN>";
// RECEIPT 的 n = 该 doc 目录下下一个未使用的 review 编号（已有 review-1 就写 review-2；多轮复核不覆盖旧回执）
const REVIEW_N = { d01: 1, d02: 1 };
const prompt = (doc) => [
  "你是「复核员」子 agent（role=reviewer）。先 read 角色卡：",
  SK + "/references/roles/reviewer.md",
  "只读这三个输入，不给其它：",
  "SEGMENTS_JSON=" + RUN + "/docs/" + doc + "/segments.json",
  "MANIFEST=" + RUN + "/docs/" + doc + "/切分清单.md",
  "BLOCKS_JSON=" + RUN + "/docs/" + doc + "/blocks.json",
  "MERGE_CANDIDATES 在 SEGMENTS_JSON 顶层 merge_candidates（不要求单独文件）；逐条给 merge_review（0.0.10）",
  "RECEIPT=" + RUN + "/docs/" + doc + "/review-" + REVIEW_N[doc] + ".json",
  "严格按角色卡写 RECEIPT 一个回执（原子写）；不改任何输入；不跑脚本；不派子 agent。",
  "完成后只回一行：done <回执绝对路径> + ≤120 字备注。",
].join("\n");
phase(PHASE);
const results = await parallel(DOCS.map((doc) => () => agent(prompt(doc), { label: "review-" + doc, phase: PHASE })));
return DOCS.map((doc, i) => ({ doc, ok: !!results[i], out: (results[i] || "").slice(0, 400) }));
```

- 多轮复核：把模板里的 `REVIEW_N[doc]` 改成该 doc 目录下下一个未使用的编号（已有 `review-1.json` 就写 `review-2.json`），**不覆盖旧回执**。
- 复核完：E4 `verdict=fixed` → `assemble.py --overrides review-<n>.json`；E5 → `translate_apply.py --review`；E9 → `cards_apply.py --review`；然后跑对应 gate。
- 0.0.10 起 review 回执必须带 `merge_review`（逐条覆盖 `segments.json` 顶层 `merge_candidates`；`keep` 写明五类之一（`turn` / `dialogue_beat` 仅对话块），`split` 给整块 override），细节见 `roles/reviewer.md` 的 merge_review 规则；gate e4 机检覆盖。

## 8. 失败、重试与落盘

| 情形 | 算第几次 | 动作 |
|---|---|---|
| `agent()` 返回 `null` | 首次失败 | 只把 `failed` 重跑一遍（重试 1） |
| 子 agent 回了 `done` 但 gate 说缺 / 回执非法 | 首次失败（按磁盘） | 非法回执改名 `.bad-<ts>`；只补派它（重试 1） |
| 重试 1 仍 `null` / 仍缺 | 重试 1 失败 | 只重跑剩下的（重试 2） |
| 重试 2 仍 `null` / 仍缺 | 重试 2 失败 | 写 `.failed-<ts>.json` + 报告根 agent；**不静默跳过** |
| `workflow` 返回 `stopReason: "cancelled"` | **不算失败**（被中断） | 先查回执目录 + 跑 gate，只补缺的；不要整批重派 |

- 同一任务**最多重试 2 次**（首次 + 最多 2 次补派，共 3 次尝试）；第 2 次补派仍失败就停，交给根 agent 决定。
- `.failed-<ts>.json` 落在 `$RUN/docs/<doc>/.failed-<ts>.json`（根 agent 用 `date +%Y%m%d-%H%M%S` 生成 `<ts>`，把 workflow 返回的 `failed` 原样填进去），字段：

```json
{"version": 1, "doc": "<doc>", "step": "e3", "attempts": 3,
 "failed": [{"task": "d01-b05", "receipt": "<绝对路径>", "last": "agent() null / gate miss"}],
 "next": "报告根 agent；按 gate 列出的 MISS 人工处理"}
```

- **三个不要**：不要因为 `cancelled` 整批重派；不要在超限报错后立刻重派整批（先查回执，已启动的可能还在写）；不要把子 agent 的 `done` 当通过（只认回执 + gate）。
- 派完不要 sleep / 轮询：workflow 是 await 的，返回后直接跑 gate；gate 只补缺的。
