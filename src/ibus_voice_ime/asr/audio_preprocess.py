# -*- coding: utf-8 -*-
"""Lightweight audio preprocessing for the recorded WAV before ASR.

The recorder (``audio_session`` / ``voice._record_wav``) writes 16 kHz /
mono / signed 16-bit PCM WAVs straight from ``arecord`` with no treatment.
ASR backends then read that file verbatim, so quiet, loud, or noisy captures
all reach the model untouched and degrade recognition.

This module normalizes loudness, high-passes out low-frequency rumble, and
removes mains hum — all via the system ``sox`` binary, so no Python audio
dependencies are introduced.  ``preprocess_wav()`` rewrites the file in place
and returns its path; on any failure it silently falls back to the untouched
original so recognition is never blocked.

Denoise tiers (``VOICE_IME_DENOISE_TIER``, 2026-08-26 实测定案，结论来源：
电容麦底噪 99% 为 50Hz 工频哼声，Qwen3-ASR 对残余底噪鲁棒，无需重型模型):

* ``none``    — 旧行为原样保留（highpass + 可选 sox noisered + normalize）。
* ``notch``   — 纯 sox：highpass 80 + 50/100/150Hz 工频陷波 + normalize（零新
  依赖；出厂默认档是 rnnoise——见 run-engine.sh，本模块在无 wrapper 环境下的
  fallback 才是 notch。稳态 -39dB@50Hz，语音频段零损耗。注意：验收陷波深度必须
  用尾部 RMS——时域峰值会被 IIR 滤波器的起始瞬态支配而严重低估衰减）。
* ``rnnoise`` — ffmpeg：陷波 + RNNoise(bd)（arnndn 仅 ffmpeg 提供，模型在
  vendor/models/rnnoise/，由 scripts/fetch-rnnoise-model.sh 下载），再 sox
  normalize；缺 ffmpeg/模型/失败时自动降级为 notch 档（fail-safe 分级降级）。

Tunable via ``VOICE_IME_AUDIO_*`` environment variables (see ``run-engine.sh``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class _PreprocessSkipped(Exception):
    """Internal: raised to short-circuit while still returning the original."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [audio preprocess] {message}", flush=True)


# 陷波参数：50Hz 工频哼声及二/三次谐波（与视频产线 denoise_voice.py 同组态）
_TIER_NOTCHES = ((50.0, -30.0, 6.0), (100.0, -20.0, 6.0), (150.0, -15.0, 8.0))


def _denoise_tier() -> str:
    """none=旧行为 / notch=陷波 / rnnoise=陷波+RNNoise；未知值按 none（最保守）。"""
    tier = os.environ.get("VOICE_IME_DENOISE_TIER", "notch").strip().lower()
    if tier not in {"none", "notch", "rnnoise"}:
        _log(f"未知降噪档 {tier!r}，按 none（仅旧链）处理")
        return "none"
    return tier


def _rnnoise_model_path() -> str:
    env = os.environ.get("VOICE_IME_AUDIO_RNNOISE_MODEL")
    if env:
        return env
    root = Path(__file__).resolve().parents[3]
    return str(root / "vendor" / "models" / "rnnoise" / "bd.rnnn")


def _rnnoise_pass(in_path: str, tmpdir: str) -> str:
    """ffmpeg 一遍过：highpass 80 + 陷波 + RNNoise(bd)（arnndn 仅 ffmpeg 提供）。

    缺 ffmpeg/模型或执行失败时返回原路径——上层 sox 陷波链兜底（即降级为
    notch 档行为），识别永不阻塞。
    """
    if shutil.which("ffmpeg") is None:
        _log("ffmpeg 不在 PATH，rnnoise 档降级为 sox 陷波")
        return in_path
    model = _rnnoise_model_path()
    if not Path(model).exists():
        _log(f"rnnoise 模型缺失（{model}，可跑 scripts/fetch-rnnoise-model.sh），"
             "降级为 sox 陷波")
        return in_path
    af = ("highpass=f=80,anequalizer=" + "|".join(
            f"c0 f={f:g} w={w:g} g={g:g} t=1" for f, g, w in _TIER_NOTCHES)
          + f",arnndn=m={model}")
    out_path = str(Path(tmpdir) / "denoise.wav")
    try:
        completed = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", in_path, "-af", af,
             "-ar", "16000", "-c:a", "pcm_s16le", out_path],  # arnndn 跟随模型采样率(48k)，须显式回 16k
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode, ["ffmpeg"], stderr=completed.stderr)
        if not Path(out_path).exists() or Path(out_path).stat().st_size <= 44:
            _log("ffmpeg 无输出，降级为 sox 陷波")
            return in_path
        return out_path
    except Exception as exc:
        _log(f"ffmpeg rnnoise 失败，降级为 sox 陷波：{exc}")
        return in_path


def _run_sox(args: list[str]) -> None:
    """Run sox with the given argument list; raise CalledProcessError on failure."""
    completed = subprocess.run(
        ["sox", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            ["sox", *args],
            output=completed.stdout,
            stderr=completed.stderr,
        )


def _build_effect_chain(in_path: str, profile_path: str | None,
                        tier: str, ffmpeg_did_notch: bool) -> list[str]:
    """Build the sox argv tail (effects only) honoring the env switches."""
    effects: list[str] = []
    if tier == "none":
        # 旧行为原样保留：highpass + 可选 noisered + normalize。
        if _env_bool("VOICE_IME_AUDIO_HIGHPASS", True):
            freq = max(20, _env_int("VOICE_IME_AUDIO_HIGHPASS_FREQ", 100))
            effects += ["highpass", str(freq)]
        if _env_bool("VOICE_IME_AUDIO_DENOISE", False) and profile_path:
            amount = min(1.0, max(0.0, _env_float("VOICE_IME_AUDIO_DENOISE_AMOUNT", 0.3)))
            effects += ["noisered", profile_path, f"{amount:.3f}"]
    else:
        # notch/rnnoise 档：50Hz 哼声陷波（sox equalizer 稳态精确兑现 -30dB；
        # 时域峰值测量会被 IIR 起始瞬态误导，勿据此验收）。rnnoise 档的
        # ffmpeg 已做 highpass+陷波时 sox 不重复，只剩 normalize。
        if not ffmpeg_did_notch:
            if _env_bool("VOICE_IME_AUDIO_HIGHPASS", True):
                freq = max(20, _env_int("VOICE_IME_AUDIO_HIGHPASS_FREQ", 80))
                effects += ["highpass", str(freq)]
            if _env_bool("VOICE_IME_AUDIO_NOTCH", True):
                for f_hz, gain_db, width_hz in _TIER_NOTCHES:
                    effects += ["equalizer", f"{f_hz:g}", f"{width_hz:g}h", f"{gain_db:g}"]
    if _env_bool("VOICE_IME_AUDIO_NORMALIZE", True):
        headroom = max(0.0, _env_float("VOICE_IME_AUDIO_NORMALIZE_HEADROOM", 3.0))
        # gain -n normalizes to 0 dBFS peak; the trailing -headroom leaves
        # that much headroom so downstream encoders don't clip.
        effects += ["gain", "-n", f"-{headroom:.1f}"]
    return effects


def _make_noise_profile(in_path: str, tmpdir: str) -> str | None:
    """Sample the first N ms of the recording as a noise fingerprint.

    Returns the profile file path, or None if profiling failed (in which case
    spectral denoise is simply skipped rather than aborting the whole chain).
    The recording's opening usually contains ambient noise before the user
    starts speaking, which is a good enough fingerprint for light denoise.
    """
    if not _env_bool("VOICE_IME_AUDIO_DENOISE", False):
        return None
    profile_ms = max(50, _env_int("VOICE_IME_AUDIO_NOISE_PROFILE_MS", 400))
    profile_path = str(Path(tmpdir) / "noise.prof")
    # noiseprof reads its effect-input from sox's stdin when given "-": we pass
    # the trimmed head of the file via sox's `trim 0 <seconds>` effect instead.
    try:
        _run_sox([in_path, "-n", "trim", "0", f"{profile_ms / 1000.0:.3f}", "noiseprof", profile_path])
    except Exception as exc:
        _log(f"noiseprof failed, skipping denoise: {exc}")
        return None
    if not Path(profile_path).exists() or Path(profile_path).stat().st_size == 0:
        _log("noiseprof produced empty profile, skipping denoise")
        return None
    return profile_path


def preprocess_wav(wav_path: str) -> str:
    """Tiered denoise + normalize a recorded WAV (see module docstring).

    Rewrites ``wav_path`` in place (atomic rename of a processed temp file)
    and returns the same path.  Always returns a playable WAV: on any error or
    when sox is unavailable, the original file is left untouched and returned
    so ASR proceeds exactly as before.
    """
    if not _env_bool("VOICE_IME_AUDIO_PREPROCESS", True):
        return wav_path
    if not wav_path or not Path(wav_path).exists():
        return wav_path
    if shutil.which("sox") is None:
        _log("sox not found on PATH; skipping preprocessing")
        return wav_path

    tier = _denoise_tier()
    started = time.monotonic()
    # Work inside the input file's own directory so the final os.replace is on
    # the same filesystem (atomic) and the temp dir cleans up with it.
    workdir = Path(wav_path).resolve().parent
    try:
        with tempfile.TemporaryDirectory(prefix="audio-pre-", dir=str(workdir)) as tmp:
            src_path = wav_path
            ffmpeg_did_notch = False
            if tier == "rnnoise":
                processed = _rnnoise_pass(wav_path, tmp)
                ffmpeg_did_notch = processed != wav_path
                src_path = processed
            # 噪声指纹仅供旧链 noisered 使用；tier 档不做谱减，跳过采样省一次 sox。
            profile_path = _make_noise_profile(src_path, tmp) if tier == "none" else None
            effects = _build_effect_chain(src_path, profile_path, tier, ffmpeg_did_notch)
            if not effects:
                if src_path != wav_path:
                    # ffmpeg 已完成全部处理（normalize 被关闭的情形），直接采用。
                    os.replace(src_path, wav_path)
                    _log(f"done in {time.monotonic() - started:.3f}s (tier={tier}, ffmpeg only)")
                    return wav_path
                _log("all stages disabled; skipping")
                return wav_path
            out_path = str(Path(tmp) / "processed.wav")
            _run_sox([src_path, out_path, *effects])
            if not Path(out_path).exists() or Path(out_path).stat().st_size <= 44:
                _log("sox produced no output; keeping original")
                return wav_path
            os.replace(out_path, wav_path)
        elapsed = time.monotonic() - started
        _log(f"done in {elapsed:.3f}s (tier={tier}): {' '.join(effects)}")
        return wav_path
    except subprocess.CalledProcessError as exc:
        _log(f"sox failed (code {exc.returncode}); keeping original. stderr={exc.stderr.strip()}")
        return wav_path
    except Exception as exc:
        _log(f"preprocessing error; keeping original: {exc}")
        return wav_path
