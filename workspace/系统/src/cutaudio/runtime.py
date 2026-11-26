"""Workspace runtime bootstrap: single shared OpenMP runtime + FFmpeg on PATH.

Why this exists
---------------
stable-ts pulls in PyTorch (its wav2vec fallback imports torch at package import
time).  On Apple Silicon the venv then maps *two different* copies of
libomp.dylib into one process (torch/lib/libomp.dylib and numba resolving its own
@rpath copy).  mlx itself does not link OpenMP, but once the second copy is
dlopen()ed lazily libomp aborts with
  OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib already initialized
and the process dies with SIGABRT (exit 134).  KMP_DUPLICATE_LIB_OK is not enough
because MLX calls into OpenMP before libomp reads its environment.

Fix used here: exactly one clean LLVM OpenMP build is shared by torch and numba
(tools/setup.sh installs it into torch/lib/libomp.dylib), so libomp sees the same
image twice instead of two distinct ones.  This module is still defensive: it
preloads that shared runtime, sets the documented fallback env var, and puts the
workspace bin/ (which holds an ffmpeg symlink) at the front of PATH so stable-ts
can decode audio.
"""
from __future__ import annotations

import ctypes
import os
import sys

_APPLIED = False


def workspace_root() -> str:
    # the environment root (the folder holding src/, models/, bin/, .venv/):
    # this file is <env>/src/cutaudio/runtime.py -> up three levels
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def project_root() -> str:
    """The user-facing workspace root: the folder that holds 材料区/ and 产出区/.

    In the normal layout the environment lives in <workspace>/系统/, so the
    project root is one level above the environment root.  When the tool is
    unpacked on its own (no 材料区/产出区 next to it) the environment root
    doubles as the project root.
    """
    env_root = workspace_root()
    parent = os.path.dirname(env_root)
    for marker in ("材料区", "产出区"):
        if os.path.isdir(os.path.join(parent, marker)):
            return parent
    return env_root


def ffmpeg_path():
    """Absolute path of the FFmpeg binary the pipeline will use, or None."""
    cand = os.path.join(workspace_root(), "bin", "ffmpeg")
    if os.path.exists(cand) and os.access(cand, os.X_OK):
        return cand
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


def venv_dir() -> str:
    return os.path.dirname(os.path.dirname(sys.executable))


def shared_openmp_path():
    """The single libomp.dylib shared by torch/numba (installed by setup.sh)."""
    cands = [
        os.path.join(venv_dir(), "lib", "python3.12", "site-packages", "torch", "lib", "libomp.dylib"),
        "/opt/anaconda3/lib/libomp.dylib",
    ]
    for cand in cands:
        if os.path.exists(cand):
            return cand
    return None


def apply() -> None:
    """Make this interpreter able to run mlx + stable-ts in one process."""
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    lib = shared_openmp_path()
    if lib:
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            pass

    bin_dir = os.path.join(workspace_root(), "bin")
    parts = os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
    if os.path.isdir(bin_dir) and bin_dir not in parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")

    ffi = ffmpeg_path()
    if ffi:
        os.environ["IMAGEIO_FFMPEG_EXE"] = ffi
        if not os.path.exists(os.path.join(bin_dir, "ffmpeg")):
            try:
                os.makedirs(bin_dir, exist_ok=True)
                os.symlink(ffi, os.path.join(bin_dir, "ffmpeg"))
            except OSError:
                pass
