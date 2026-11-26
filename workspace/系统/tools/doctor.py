#!/usr/bin/env python3
"""Self-check: can this workspace cut audio right now, and fully offline?

Run:  系统/tools/doctor.py            (human readable; re-runs itself under 系统/.venv)
      系统/tools/doctor.py --json     (machine readable, exit 0 = ready)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)

# Re-run under the environment venv, so a bare `系统/tools/doctor.py` does not
# silently use whatever python3 happens to be first on PATH.
_VENV_PY = os.path.join(WS, ".venv", "bin", "python")
if os.path.exists(_VENV_PY) and os.path.abspath(sys.executable) != os.path.abspath(_VENV_PY):
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

sys.path.insert(0, os.path.join(WS, "src"))

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str, fatal: bool = True) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail, "fatal": fatal})
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--deep", action="store_true", help="also load the model and align a 1 s beep")
    args = ap.parse_args()

    from cutaudio import runtime
    runtime.apply()

    # 1. venv / python
    check("python", sys.version_info[:2] == (3, 12), f"{sys.version.split()[0]} ({sys.executable})")

    # 2. pinned packages
    import importlib.metadata as md
    wanted = {"stable-ts": "2.19.1", "mlx-whisper": "0.4.3", "imageio-ffmpeg": "0.6.0"}
    for pkg, ver in wanted.items():
        try:
            got = md.version(pkg)
        except md.PackageNotFoundError:
            got = None
        check(f"pkg:{pkg}", got == ver, f"installed={got} pinned={ver}")

    # 3. ffmpeg
    ff = runtime.ffmpeg_path()
    if check("ffmpeg", bool(ff and os.path.exists(ff)), ff or "missing - run tools/setup.sh"):
        out = subprocess.run([ff, "-version"], capture_output=True, text=True)
        check("ffmpeg-runs", out.returncode == 0, (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else "no output")

    # 4. single shared OpenMP runtime
    omp = runtime.shared_openmp_path()
    check("openmp", bool(omp and os.path.exists(omp)), omp or "missing shared libomp (run tools/setup.sh)")

    # 5. model files
    from cutaudio import align as align_mod
    mdir = align_mod.model_dir()
    weights = os.path.join(mdir, "weights.safetensors")
    cfg = os.path.join(mdir, "config.json")
    ok_model = os.path.exists(weights) and os.path.exists(cfg)
    size = os.path.getsize(weights) / 1e6 if os.path.exists(weights) else 0
    check("model", ok_model and size > 1000, f"{mdir} ({size:.0f} MB)")

    # 6. offline: model dir and imports resolve without network
    check("offline-model", os.path.isdir(mdir) and os.path.dirname(mdir) == os.path.join(WS, "models"),
          "model is a local directory inside the workspace (no Hub download at run time)")

    # 7. imports + end-to-end smoke on a generated tone
    if all(c["ok"] for c in CHECKS if c["fatal"]):
        try:
            import numpy as np
            from cutaudio import audio as audio_mod, energy
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                sr = 16000
                t = np.arange(int(sr * 1.0)) / sr
                mono = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
                pcm = np.stack([mono, mono], axis=1)
                wav = os.path.join(td, "beep.wav")
                audio_mod.write_wav_16(wav, pcm, sr)
                back = audio_mod.read_wav(wav)
                dec = audio_mod.decode(wav)
                check("audio-io", back[0].shape[0] == sr and dec.frames == sr and dec.channels == 2,
                      f"wrote/read/decode roundtrip {dec.frames} samples, {dec.channels} ch, {dec.sample_rate} Hz")
            e, hop = energy.frame_energy(np.zeros(sr, dtype=np.float32), sr)
            check("energy", e.size == 100, f"{e.size} frames of {energy.FRAME_MS:.0f} ms")
        except Exception as exc:  # noqa: BLE001
            check("audio-io", False, f"{type(exc).__name__}: {exc}")

    if args.deep and all(c["ok"] for c in CHECKS if c["fatal"]):
        try:
            import numpy as np
            import tempfile
            model = align_mod.load_model(mdir)
            sr = 16000
            tt = np.arange(int(sr * 1.0)) / sr
            wave = (0.2 * np.sin(2 * np.pi * 300 * tt)).astype(np.float32)
            r = model.transcribe(wave, language="en", verbose=None)
            check("deep:model-run", r is not None, "model ran an inference on a synthetic tone")
        except Exception as exc:  # noqa: BLE001
            check("deep:model-run", False, f"{type(exc).__name__}: {exc}")

    failed = [c for c in CHECKS if not c["ok"] and c["fatal"]]
    status = "ok" if not failed else "broken"
    if args.json:
        print(json.dumps({"status": status, "checks": CHECKS}, ensure_ascii=False, indent=2))
    else:
        width = max(len(c["name"]) for c in CHECKS)
        for c in CHECKS:
            mark = "OK  " if c["ok"] else ("FAIL" if c["fatal"] else "WARN")
            print(f"[{mark}] {c['name']:<{width}}  {c['detail']}")
        print(f"\nDOCTOR: {status}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
