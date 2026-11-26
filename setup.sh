#!/bin/bash
# One-command setup for the listening-card-slicing workbench.
# Target: macOS 14+ on Apple Silicon (MLX Whisper), Python 3.12.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="$ROOT/workspace"
SYS="$WS/系统"

echo "== listening-card-slicing workbench setup"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "ERROR: 该流程使用 MLX Whisper，需要 macOS + Apple Silicon（arm64）。" >&2
  echo "       当前：$(uname -s) $(uname -m)" >&2
  exit 1
fi

if [ ! -d "$SYS" ]; then
  echo "ERROR: 找不到 $SYS" >&2; exit 1
fi

# 1) Python / venv / deps / ffmpeg / model / doctor
"$SYS/tools/setup.sh"

# 2) Install skills into ~/.agents/skills
"$ROOT/scripts/install-skills.sh"

echo
echo "== setup complete"
echo "   workspace : $WS"
echo "   skills    : ${DSH_SKILLS_DIR:-$HOME/.agents/skills}"
echo
echo "下一步："
echo "  1. 把音频 + 符合 contract v1 的原文放进 $WS/材料区/"
echo "  2. 在 $WS 目录启动 agent，说「干活」或「切这个音频」"
echo "  3. 写云端前，在 ~/.agents/skills/markji-channel/.env 填 MAIMEMO_TOKEN"
echo "  4. 体检：$SYS/.venv/bin/python $SYS/tools/doctor.py --deep"
