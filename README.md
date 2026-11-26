# listening-card-slicing workbench

把「一段听力音频 + 已分段、无时间戳的原文」做成 **本地音频切片包 + 墨墨牌组卡片** 的完整工作台：

- `skills/listening-card-slicing`：主 skill（0.1.0，E1–E12，两个入口，E1 选择门 / E5 定稿 / E10 样卡）
- `skills/markji-channel`：墨墨云端读写通道
- `workspace/系统`：音频切分程序 `cutaudio 1.1.1`、体检/清理工具、离线模型下载脚本
- `workspace/材料区` / `workspace/产出区`：输入与交付（默认空）

> 目标平台：**macOS 14+ / Apple Silicon**。切分使用 MLX Whisper，Intel Mac / Windows 不能直接跑。
> 首次安装需要 Python 3.12、约 4 GB 磁盘、能访问 PyPI 与 Hugging Face。

## 一键安装

```bash
git clone https://github.com/Rayzenchen/listening-card-slicing.git
cd listening-card-slicing
./setup.sh
```

`setup.sh` 会：

1. 在 `workspace/系统/.venv` 建 Python 3.12 虚拟环境，安装 `requirements.lock.txt`（约 1 GB wheels）；
2. 链接 `imageio-ffmpeg` 的 ffmpeg 到 `workspace/系统/bin/ffmpeg`；
3. 下载 `mlx-community/whisper-large-v3-turbo` 到 `workspace/系统/models/`（约 1.5 GB，可重跑续传）；
4. 把两个 skill 安装到 `~/.agents/skills/`（已有同名 skill 会先备份，不静默覆盖）；
5. 跑 `workspace/系统/tools/doctor.py` 自检。

跳过 1.5 GB 模型下载（先装代码/依赖）：

```bash
SKIP_MODEL_DOWNLOAD=1 ./setup.sh
# 之后单独下载：
workspace/系统/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(repo_id="mlx-community/whisper-large-v3-turbo",
                  local_dir="workspace/系统/models/whisper-large-v3-turbo")
PY
```

已有虚拟环境/模型时可直接重跑 `./setup.sh`，脚本会复用。

## 准备材料

把音频和对应原文放进 `workspace/材料区/`（可放子目录）：

```text
workspace/材料区/
├── 25年06月-卷1.mp3
└── 25年06月-卷1_听力原文.md
```

原文必须是 **contract v1**：

```markdown
## [开场]
1、College English Test, Band 4.

## Section A
### Directions
1、In this section, you will hear three news reports.
### News Report 1 [Q1-2]
1、……
```

完整语法/正反例见
[`skills/listening-card-slicing/references/runtime/transcript-contract.md`](skills/listening-card-slicing/references/runtime/transcript-contract.md)。

**本仓库不包含** AI Studio 转写工具、转写 prompt、任何从音频提取的原文或相关测试夹具；
没有原文就停下，不要拿 ASR 结果顶替原文。

## 跑流程

在 `workspace/` 目录启动 DSH/agent，加载 `listening-card-slicing`，按触发语说：

- **「干活」**：E1–E12 完整流程
- **「切这个音频」**：跑到 E8（本地切片包）
- **「做成卡」**：继续 E9–E12（云端卡片）

E1 会先问两件事：处理哪些材料（可多选 / 全部处理）、写进哪个牌组（云端候选 + 自己填）。
E9 中文校对可直接出表格并改清单/卡片，不重跑上游；E10 对新牌组先写 1 张样卡，老牌组自动放行。

## 墨墨 Key

写云端前，在：

```text
~/.agents/skills/markji-channel/.env
```

填一行（安装时会创建空文件，权限 600）：

```text
MAIMEMO_TOKEN=你的Key
```

Key 从 墨墨 App →「我的」→ 更多设置 → 实验功能 → 开放 API 获取。
不要提交 `.env`；仓库已忽略它。

## 自检

```bash
workspace/系统/.venv/bin/python workspace/系统/tools/doctor.py --deep
```

看到 `DOCTOR: ok` 即可；`--deep` 会真正加载模型跑 1 秒合成音。

## 目录

```text
.
├── setup.sh
├── scripts/install-skills.sh
├── skills/
│   ├── listening-card-slicing/   # 0.1.0，含 scripts/references/roles
│   └── markji-channel/           # 云端通道 + .env.example
└── workspace/
    ├── AGENTS.md / CLAUDE.md / README.md
    ├── 材料区/                    # 空，放音频+原文
    ├── 产出区/                    # 空，交付包
    └── 系统/
        ├── src/cutaudio/          # 1.1.1
        ├── tools/                 # cut / doctor / setup / cleanup / split_cards
        ├── tests/                 # cutaudio 单元测试
        ├── requirements.txt
        └── requirements.lock.txt
```

## 已剔除 / 不提交

- AI Studio 转写 SOP、prompt、自动化脚本
- `材料区/` 的音频与 `_听力原文.md`
- `tests/fixtures` 中来自真实听力材料的转写/卡片夹具
- `系统/models` 的模型权重（setup 下载）
- `系统/.venv`、`产出区` 切片包、`*.wav` / `*.mp3`
- 任何 `.env` / API Key / 带签名 URL

开发测试未随发行包（夹具含真实转写文本）；运行时不需要测试目录。

## 故障排查

- **`OMP: Error #15`**：重跑 `./setup.sh`（会安装/复用一份共享 libomp 并补进 torch）；或 `brew install libomp` 后重跑。
- **Python 3.12 缺失**：`brew install python@3.12`；或用 `PY_BASE=/path/to/python3.12 ./setup.sh`。
- **模型下载慢/中断**：重跑 `./setup.sh`，`snapshot_download` 会续传；或手动按上面的 `snapshot_download` 命令。
- **`doctor` 报 ffmpeg 缺失**：重跑 setup；仍失败则
  `workspace/系统/.venv/bin/python -m pip install --force-reinstall --no-deps imageio-ffmpeg==0.6.0` 后重跑。
- **云端 401/403**：按 `markji-channel` 的提示换 Key；不要打印/提交 Key。

## 说明

本仓库为个人工作流打包，未附开源许可证；仓库默认私有。skill/工作区文档中的少量英文/中文例句只用于说明格式与规则，不是完整听力原文。
