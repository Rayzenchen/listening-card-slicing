#!/bin/bash
# Workspace setup: isolated venv, pinned deps, shared OpenMP fix, ffmpeg link, model cache.
# Safe to re-run: every step checks before acting. No global/conda changes are made.
set -euo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WS"
PY="${WS}/.venv/bin/python"

echo "== workspace: $WS"

if [ ! -x "$PY" ]; then
  if [ -n "${PY_BASE:-}" ]; then
    :
  elif command -v python3.12 >/dev/null 2>&1; then
    PY_BASE="$(command -v python3.12)"
  elif command -v python3 >/dev/null 2>&1 && \
       [ "$(command -v python3)" ] && \
       "$(command -v python3)" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)' 2>/dev/null; then
    PY_BASE="$(command -v python3)"
  else
    echo "ERROR: 需要 Python 3.12（brew install python@3.12），或设置 PY_BASE=/path/to/python3.12" >&2
    exit 2
  fi
  echo "-- creating venv from $PY_BASE"
  "$PY_BASE" -m venv "$WS/.venv"
fi
"$PY" -m pip install -q --upgrade pip >/dev/null

if [ -f "$WS/requirements.lock.txt" ]; then
  echo "-- installing pinned requirements (first run downloads ~1 GB of wheels)"
  "$PY" -m pip install -q --no-input -r "$WS/requirements.lock.txt"
elif [ -f "$WS/requirements.txt" ]; then
  echo "-- installing requirements.txt"
  "$PY" -m pip install -q --no-input -r "$WS/requirements.txt"
fi

echo "-- linking ffmpeg into bin/"
mkdir -p "$WS/bin"
FFOK=0
if [ -x "$WS/bin/ffmpeg" ] && [ "$(file -b "$WS/bin/ffmpeg" | grep -c Mach-O)" = "1" ] && "$WS/bin/ffmpeg" -version >/dev/null 2>&1; then
  FFOK=1
fi
if [ "$FFOK" = "0" ]; then
  FF="$("$PY" -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe() or '')" 2>/dev/null || true)"
  if [ -n "${FF:-}" ] && [ -x "$FF" ]; then
    ln -sfn "$FF" "$WS/bin/ffmpeg"
    echo "   -> $FF"
  else
    echo "   !! ffmpeg binary missing; run: .venv/bin/python -m pip install --force-reinstall --no-deps imageio-ffmpeg==0.6.0"
    exit 1
  fi
fi

if [ ! -e /opt/homebrew/opt/libomp/lib/libomp.dylib ] && command -v brew >/dev/null 2>&1; then
  echo "-- installing libomp via Homebrew (needed to share one OpenMP runtime)"
  brew install libomp >/dev/null 2>&1 || true
fi

echo "-- reconciling OpenMP runtimes (torch + numba must share one libomp)"
OMP="$("$PY" - "$WS" <<'PYEOF'
import hashlib, os, shutil, sys
ws = sys.argv[1]
torch_omp = os.path.join(ws, ".venv/lib/python3.12/site-packages/torch/lib/libomp.dylib")
candidates = ["/opt/anaconda3/lib/libomp.dylib",
              "/opt/homebrew/opt/libomp/lib/libomp.dylib",
              "/usr/local/opt/libomp/lib/libomp.dylib"]
if not os.path.exists(torch_omp):
    print("SKIP no torch libomp"); raise SystemExit(0)
def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""): h.update(c)
    return h.hexdigest()
cur = digest(torch_omp)
for c in candidates:
    if os.path.exists(c) and digest(c) != cur:
        shutil.copyfile(c, torch_omp)
        os.chmod(torch_omp, 0o644)
        print(f"PATCHED replaced torch/lib/libomp.dylib with {c}")
        break
else:
    found = [c for c in candidates if os.path.exists(c)]
    if not found:
        print("ERROR: no shared libomp found. Install Homebrew, then: brew install libomp")
        raise SystemExit(1)
    print("OK already a single shared libomp")
PYEOF
)"
echo "   $OMP"

if [ "${SKIP_MODEL_DOWNLOAD:-0}" = "1" ]; then
  echo "-- SKIP_MODEL_DOWNLOAD=1: skipping 1.5 GB model download"
elif [ ! -f "$WS/models/whisper-large-v3-turbo/weights.safetensors" ]; then
  echo "-- model weights missing; fetching mlx-community/whisper-large-v3-turbo (needs network, once, ~1.5 GB)"
  "$PY" - <<'PYEOF'
import os, shutil
from huggingface_hub import snapshot_download
ws = os.getcwd()
dst = os.path.join(ws, "models", "whisper-large-v3-turbo")
path = snapshot_download(repo_id="mlx-community/whisper-large-v3-turbo",
                         local_dir=dst)
print("   ->", path)
PYEOF
else
  echo "-- model present: models/whisper-large-v3-turbo"
fi

if [ "${SKIP_MODEL_DOWNLOAD:-0}" = "1" ] && [ ! -f "$WS/models/whisper-large-v3-turbo/weights.safetensors" ]; then
  echo "-- model skipped: run 'workspace/系统/tools/doctor.py' after downloading the model"
  echo "== setup done (model pending)"
  exit 0
fi
echo "-- doctor"
if "$PY" "$WS/tools/doctor.py"; then
  echo "== setup done"
else
  echo "== setup FAILED self-check (see doctor output above)" >&2
  exit 1
fi
