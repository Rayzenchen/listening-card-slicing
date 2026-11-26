"""Audio I/O: decode to float32 PCM, compute exact sample geometry, write WAV.

Everything downstream works in integer sample indices of the *decoded* source, so
splitting is lossless: the pieces are contiguous slices of one decode and their
sample counts always add back up to the source length.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass

import numpy as np

from . import runtime

SUPPORTED_EXT = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".aif", ".aiff", ".caf", ".ogg", ".opus"}


class AudioError(RuntimeError):
    pass


@dataclass
class SourceAudio:
    path: str
    sample_rate: int
    channels: int
    frames: int
    pcm: np.ndarray          # shape (frames, channels) float32 in [-1, 1]
    codec: str
    audio_line: str

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate

    @property
    def mono(self) -> np.ndarray:
        return self.pcm.mean(axis=1).astype(np.float32)


def ffmpeg() -> str:
    exe = runtime.ffmpeg_path()
    if not exe:
        raise AudioError("FFmpeg not found: run tools/setup.sh (creates bin/ffmpeg)")
    return exe


def probe(path: str) -> dict:
    """Probe a media file. imageio-ffmpeg ships no ffprobe, so use ffmpeg -i."""
    exe = ffmpeg()
    proc = subprocess.run(
        [exe, "-hide_banner", "-nostdin", "-i", path, "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    if not os.path.isfile(path):
        raise AudioError(f"audio file does not exist: {path}")
    info: dict = {}
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            info["duration"] = line.split(",")[0].split(":", 1)[1].strip()
        if " Audio: " in line:
            info["audio_line"] = line
    if "audio_line" not in info:
        raise AudioError(f"no audio stream found (corrupt or wrong file?): {path}\n"
                         f"{proc.stderr.strip()[-600:]}")
    return info


def decode(path: str) -> SourceAudio:
    if not os.path.isfile(path):
        raise AudioError(f"audio file does not exist: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXT:
        raise AudioError(f"unsupported audio extension {ext!r}; supported: {sorted(SUPPORTED_EXT)}")

    info = probe(path)
    exe = ffmpeg()
    proc = subprocess.run(
        [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", path,
         "-vn", "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise AudioError(f"ffmpeg failed to decode {path}: {proc.stderr.decode('utf-8', 'replace')[:800]}")

    raw = np.frombuffer(proc.stdout, dtype="<f4")
    if raw.size == 0:
        raise AudioError(f"decoded zero samples from {path}")

    sample_rate = int(_parse_rate(info["audio_line"]))
    channels = int(_parse_channels(info["audio_line"]))
    if raw.size % channels != 0:
        raw = raw[: raw.size - (raw.size % channels)]
    pcm = raw.reshape(-1, channels).astype(np.float32)
    if not np.isfinite(pcm).all():
        pcm = np.nan_to_num(pcm, nan=0.0, posinf=0.0, neginf=0.0)

    codec = _parse_codec(info["audio_line"])
    if "pcm" not in codec and float(np.max(np.abs(pcm))) <= 1e-6:
        raise AudioError(f"decoded audio is completely silent - input is probably corrupt: {path}")
    return SourceAudio(path=path, sample_rate=sample_rate, channels=channels,
                       frames=int(pcm.shape[0]), pcm=pcm, codec=codec,
                       audio_line=info["audio_line"])


def _parse_rate(audio_line: str) -> int:
    tokens = audio_line.replace(",", " ").split()
    for idx, token in enumerate(tokens):
        if token == "Hz" and idx > 0 and tokens[idx - 1].isdigit():
            return int(tokens[idx - 1])
        if token.endswith("Hz") and token[:-2].isdigit():
            return int(token[:-2])
    raise AudioError(f"sample rate not found in: {audio_line}")


def _parse_channels(audio_line: str) -> int:
    tokens = audio_line.replace(",", " ").split()
    for token in tokens:
        low = token.lower()
        if low.startswith("mono"):
            return 1
        if low.startswith("stereo"):
            return 2
        if low.startswith("5.1"):
            return 6
        if low.endswith(("channels", "channel")):
            num = low.replace("channels", "").replace("channel", "")
            if num.isdigit():
                return int(num)
    # fallback: "44100 Hz, 2 channels" / the token right before "Hz" is the rate,
    # the token right after it is the channel count
    for idx, token in enumerate(tokens):
        if token == "Hz" and idx + 1 < len(tokens):
            nxt = tokens[idx + 1]
            if nxt.endswith(("channels", "channel")):
                return int(nxt.replace("channels", "").replace("channel", ""))
            if nxt != "mono" and nxt != "stereo" and nxt.isdigit():
                return int(nxt)
    raise AudioError(f"channel count not found in: {audio_line}")


def _parse_codec(audio_line: str) -> str:
    after = audio_line.split(" Audio: ", 1)[-1]
    head = after.split(",")[0].strip()
    for word in head.split():
        if word and not word.startswith("("):
            return word
    return "unknown"


def write_wav_f32(path: str, pcm: np.ndarray, sample_rate: int) -> None:
    """Write IEEE float32 WAV so pieces are bit-exact slices of the decode."""
    frames = pcm.shape[0]
    channels = 1 if pcm.ndim == 1 else pcm.shape[1]
    data = np.ascontiguousarray(pcm.reshape(frames, channels), dtype="<f4")
    payload = data.tobytes()
    block_align = channels * 4
    byte_rate = sample_rate * block_align
    fmt = struct.pack("<HHIIHH", 3, channels, sample_rate, byte_rate, block_align, 32)
    fact = struct.pack("<I", frames)
    with open(path, "wb") as fh:
        fh.write(b"RIFF")
        fh.write(struct.pack("<I", 4 + (8 + len(fmt)) + (8 + len(fact)) + (8 + len(payload))))
        fh.write(b"WAVE")
        fh.write(b"fmt ")
        fh.write(struct.pack("<I", len(fmt)))
        fh.write(fmt)
        fh.write(b"fact")
        fh.write(struct.pack("<I", len(fact)))
        fh.write(fact)
        fh.write(b"data")
        fh.write(struct.pack("<I", len(payload)))
        fh.write(payload)


def write_wav_16(path: str, pcm: np.ndarray, sample_rate: int) -> None:
    """Write 16-bit PCM WAV (used by the test-material generator only)."""
    frames = pcm.shape[0]
    channels = 1 if pcm.ndim == 1 else pcm.shape[1]
    data = pcm.reshape(frames, channels)
    ints = np.clip(np.round(data.astype(np.float64) * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(ints.tobytes())


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read back a WAV we wrote and return (pcm[frames, channels], sample_rate)."""
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise AudioError(f"not a RIFF/WAVE file: {path}")
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        size = struct.unpack("<I", blob[pos + 4:pos + 8])[0]
        body = blob[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise AudioError(f"broken WAV (missing fmt/data chunk): {path}")
    tag, channels, rate, _br, _ba, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 3 and bits == 32:
        pcm = np.frombuffer(data, dtype="<f4").reshape(-1, channels)
    elif tag == 1 and bits == 16:
        pcm = np.frombuffer(data, dtype="<i2").reshape(-1, channels).astype(np.float32) / 32767.0
    elif tag == 0xFFFE and bits == 32:
        pcm = np.frombuffer(data, dtype="<f4").reshape(-1, channels)
    else:
        raise AudioError(f"unsupported WAV format in {path}: tag={tag} bits={bits}")
    return pcm.astype(np.float32), int(rate)


def resample_to(mono: np.ndarray, sr_in: int, sr_out: int = 16000) -> np.ndarray:
    """High-quality rational resample (polyphase FIR), mono float32.

    MLX Whisper only accepts 16 kHz waveforms, while cutting always happens on the
    source-rate signal.  A plain linear interpolation would smear the spectrum and
    hurt the model, so use scipy's polyphase filter (no extra dependency: scipy is
    already pulled in by stable-ts/whisper).
    """
    if sr_in == sr_out:
        return np.ascontiguousarray(mono, dtype=np.float32)
    from math import gcd

    from scipy.signal import resample_poly
    g = gcd(int(sr_in), int(sr_out))
    return resample_poly(mono.astype(np.float64), sr_out // g, sr_in // g).astype(np.float32)


def verify_resample_ratio(sr_in: int) -> float:
    """Sanity value used by self-test: expected length ratio after resampling."""
    return 16000.0 / float(sr_in)

def check_disk_space(out_dir: str, needed_bytes: int) -> None:
    probe_dir = out_dir
    while not os.path.exists(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    free = shutil.disk_usage(probe_dir).free
    if free < needed_bytes:
        raise AudioError(
            f"insufficient disk space: need about {needed_bytes / 1e6:.1f} MB, "
            f"only {free / 1e6:.1f} MB free at {probe_dir}")