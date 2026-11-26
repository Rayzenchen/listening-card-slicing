# 材料区

把每次要处理的**音频 + 原文**放在这里（可放子目录，0.0.9 起递归扫描）：

- 音频：`.mp3` / `.m4a` / `.wav` / `.flac` 等
- 原文：同名 `.md` / `.txt`，推荐 `<音频主名>_听力原文.md`

原文必须是 **contract v1**，格式见
`~/.agents/skills/listening-card-slicing/references/runtime/transcript-contract.md`。

本仓库**不包含** AI Studio 转写工具、转写 prompt 或任何从音频提取的原文；
没有原文就停下，不要拿 ASR 结果顶替原文。
