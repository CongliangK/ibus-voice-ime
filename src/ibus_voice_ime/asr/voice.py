# -*- coding: utf-8 -*-
"""Voice recording + ASR + post-processing helpers for the IBus prototype.

The engine calls `record_and_transcribe()` in a worker thread.
ASR policy: default to the local Qwen3-ASR sidecar (1.7B).  Backend selection
follows VOICE_IME_ASR_BACKEND / VOICE_IME_QWEN_ASR and falls through to the
local MiMo sidecar, MiMo cloud ASR, a custom command, faster-whisper, or vosk
in that order.

After ASR, the transcript is normalized only by deterministic local rules.
LLM post-processing is intentionally disabled and must not run.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import wave
from array import array
from pathlib import Path

from ibus_voice_ime.asr import audio_preprocess, mimo_asr_runtime, mimo_cloud_asr, qwen_asr_runtime, volc_bigmodel_asr
from ibus_voice_ime.memory import voice_terms
from ibus_voice_ime.text import chinese_script, text_postprocess


class VoiceError(RuntimeError):
    pass


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
    print(f"[{ts}] {message}", flush=True)


def _pcm_rms_s16le(data: bytes) -> float:
    if not data:
        return 0.0
    samples = array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if not samples:
        return 0.0
    # arecord emits little-endian S16_LE.  Most Linux machines are little-endian;
    # byteswap only if needed.
    if samples.itemsize != 2:
        return 0.0
    total = sum(int(x) * int(x) for x in samples)
    return (total / len(samples)) ** 0.5


def _arecord_device_args() -> list[str]:
    """VOICE_IME_ARECORD_DEVICE → arecord -D 参数（空=系统默认源）。

    直采指定设备（如 plughw:M2,0）不受系统默认源漂移影响（默认源被蓝牙
    抢占等，见 scripts/bt-play-restore.py）；M2 是 48k 设备，必须用 plughw
    前缀（自动重采样到 16k），不能写 hw:M2,0。
    """
    device = os.environ.get("VOICE_IME_ARECORD_DEVICE", "").strip()
    return ["-D", device] if device else []


def _record_wav_vad(path: str, max_seconds: int) -> None:
    """Record until silence using simple RMS VAD over arecord raw PCM."""
    max_seconds = max(1, int(os.environ.get("VOICE_IME_MAX_RECORD_SECONDS", str(max_seconds))))
    min_seconds = max(0.0, _env_float("VOICE_IME_MIN_RECORD_SECONDS", 0.4))
    silence_stop_ms = max(100.0, _env_float("VOICE_IME_SILENCE_STOP_MS", 1000.0))
    no_speech_timeout = max(0.5, _env_float("VOICE_IME_NO_SPEECH_TIMEOUT", 2.5))
    rms_threshold = max(1.0, _env_float("VOICE_IME_VAD_RMS_THRESHOLD", 500.0))
    chunk_ms = 100
    chunk_bytes = int(16000 * 2 * chunk_ms / 1000)

    cmd = ["arecord", *_arecord_device_args(), "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    started = time.monotonic()
    seen_voice = False
    trailing_silence_ms = 0.0

    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            while True:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                wf.writeframes(data)
                elapsed = time.monotonic() - started
                rms = _pcm_rms_s16le(data)
                if rms >= rms_threshold:
                    seen_voice = True
                    trailing_silence_ms = 0.0
                else:
                    trailing_silence_ms += chunk_ms

                if elapsed >= max_seconds:
                    break
                if seen_voice and elapsed >= min_seconds and trailing_silence_ms >= silence_stop_ms:
                    break
                if not seen_voice and elapsed >= no_speech_timeout:
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)

    if not Path(path).exists() or Path(path).stat().st_size <= 44:
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        raise VoiceError("VAD 录音失败" + (f"：{err.strip()}" if err.strip() else ""))


def _record_wav(path: str, seconds: int) -> None:
    """Record mono 16kHz WAV using tools usually present on Fedora."""
    seconds = max(1, int(seconds))
    if _env_bool("VOICE_IME_VAD_AUTO_STOP", False):
        try:
            _record_wav_vad(path, seconds)
            return
        except FileNotFoundError:
            pass
        except Exception:
            if not _env_bool("VOICE_IME_VAD_FALLBACK_FIXED", True):
                raise
            # Fall back to fixed-duration recording below.

    # arecord works through pipewire-alsa on this machine.
    cmd = [
        "arecord",
        *_arecord_device_args(),
        "-q",
        "-f", "S16_LE",
        "-r", "16000",
        "-c", "1",
        "-d", str(seconds),
        path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return
    except FileNotFoundError:
        pass
    except subprocess.CalledProcessError as exc:
        last_error = exc.stderr.strip()
    else:
        last_error = ""

    # Fallback: pw-record + ffmpeg conversion.
    raw = path + ".raw.wav"
    cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", raw]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=2)
        if Path(raw).exists() and Path(raw).stat().st_size > 44:
            subprocess.run(["ffmpeg", "-y", "-i", raw, "-ar", "16000", "-ac", "1", path],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return
    except Exception as exc:  # pragma: no cover - diagnostic path
        last_error = f"{last_error}; {exc}"

    raise VoiceError("录音失败，请检查麦克风权限/默认输入设备。" + (f"\n{last_error}" if last_error else ""))


def _transcribe_with_command(wav_path: str) -> str | None:
    template = os.environ.get("VOICE_IME_ASR_CMD", "").strip()
    if not template:
        return None
    if "{wav}" in template:
        cmd = template.format(wav=shlex.quote(wav_path))
    else:
        cmd = template + " " + shlex.quote(wav_path)
    proc = subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.stdout.strip()


_WHISPER_MODEL_CACHE = None
_WHISPER_MODEL_KEY = None


def _load_wav_16k_mono_float32(wav_path: str):
    """Load our recorded WAV without PyAV.

    faster-whisper normally decodes file paths through PyAV.  In an IBus-launched
    process PyAV can fail while decoding localized FFmpeg error messages
    (UnicodeDecodeError).  Our recorder already writes 16 kHz / s16 / mono, so
    pass a numpy waveform directly and bypass PyAV entirely.
    """
    import numpy as np  # type: ignore

    with wave.open(wav_path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
            return wav_path
        data = wf.readframes(wf.getnframes())
    if not data:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _transcribe_with_faster_whisper(wav_path: str) -> str | None:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return None

    global _WHISPER_MODEL_CACHE, _WHISPER_MODEL_KEY
    model_name = os.environ.get("VOICE_IME_WHISPER_MODEL", "large-v3")
    device = os.environ.get("VOICE_IME_WHISPER_DEVICE", "cuda")
    compute_type = os.environ.get("VOICE_IME_WHISPER_COMPUTE", "float16")
    device_index = _env_int("VOICE_IME_WHISPER_DEVICE_INDEX", 0)
    require_gpu = _env_bool("VOICE_IME_REQUIRE_GPU_STT", False)
    if require_gpu and device.lower() != "cuda":
        raise VoiceError("已要求 GPU STT，但 VOICE_IME_WHISPER_DEVICE 不是 cuda。")
    language = os.environ.get("VOICE_IME_WHISPER_LANGUAGE", "zh").strip() or None
    key = (model_name, device, compute_type, device_index)
    if _WHISPER_MODEL_CACHE is None or _WHISPER_MODEL_KEY != key:
        _log(
            f"ASR loading faster-whisper model={model_name} "
            f"device={device}:{device_index} compute={compute_type}"
        )
        _WHISPER_MODEL_CACHE = WhisperModel(
            model_name,
            device=device,
            device_index=device_index,
            compute_type=compute_type,
        )
        _WHISPER_MODEL_KEY = key
        _log(
            f"ASR loaded faster-whisper model={model_name} "
            f"device={device}:{device_index} compute={compute_type}"
        )
    initial_prompt = _voice_dictionary_prompt()
    audio = _load_wav_16k_mono_float32(wav_path)
    started = time.monotonic()
    segments, _info = _WHISPER_MODEL_CACHE.transcribe(
        audio,
        language=language,
        vad_filter=True,
        initial_prompt=initial_prompt or None,
    )
    text = "".join(seg.text for seg in segments).strip()
    elapsed = time.monotonic() - started
    _log(
        f"ASR transcribed with faster-whisper model={model_name} "
        f"device={device}:{device_index} compute={compute_type} "
        f"language={language or 'auto'} elapsed={elapsed:.3f}s chars={len(text)}"
    )
    return text


def _transcribe_with_qwen_asr(wav_path: str) -> str | None:
    if not qwen_asr_runtime.selected():
        return None
    started = time.monotonic()
    text = qwen_asr_runtime.transcribe(wav_path)
    elapsed = time.monotonic() - started
    _log(
        f"ASR transcribed with Qwen3-ASR model={qwen_asr_runtime.model_id()} "
        f"elapsed={elapsed:.3f}s chars={len(text)}"
    )
    return text


def _transcribe_with_mimo_asr(wav_path: str) -> str | None:
    if not mimo_asr_runtime.selected():
        return None
    started = time.monotonic()
    text = mimo_asr_runtime.transcribe(wav_path)
    elapsed = time.monotonic() - started
    _log(
        f"ASR transcribed with MiMo-ASR model={mimo_asr_runtime.model_id()} "
        f"elapsed={elapsed:.3f}s chars={len(text)}"
    )
    return text


def _transcribe_with_mimo_cloud_asr(wav_path: str) -> str | None:
    if not mimo_cloud_asr.selected():
        return None
    started = time.monotonic()
    text = mimo_cloud_asr.transcribe(wav_path)
    elapsed = time.monotonic() - started
    _log(
        f"ASR transcribed with MiMo cloud ASR model={mimo_cloud_asr.model_id()} "
        f"elapsed={elapsed:.3f}s chars={len(text)}"
    )
    return text


def _transcribe_with_volc_bigmodel_asr(wav_path: str) -> str | None:
    if not volc_bigmodel_asr.selected():
        return None
    started = time.monotonic()
    text = volc_bigmodel_asr.transcribe(wav_path)
    elapsed = time.monotonic() - started
    _log(
        f"ASR transcribed with Volcano bigmodel ASR resource={volc_bigmodel_asr.resource_id()} "
        f"elapsed={elapsed:.3f}s chars={len(text)}"
    )
    return text


def _transcribe_with_vosk(wav_path: str) -> str | None:
    model_dir = os.environ.get("VOICE_IME_VOSK_MODEL", "").strip()
    if not model_dir:
        return None
    try:
        import json
        from vosk import Model, KaldiRecognizer  # type: ignore
    except Exception:
        return None

    wf = wave.open(wav_path, "rb")
    if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
        raise VoiceError("Vosk 需要 16kHz/16bit/mono wav")
    rec = KaldiRecognizer(Model(model_dir), 16000)
    pieces: list[str] = []
    while True:
        data = wf.readframes(4000)
        if len(data) == 0:
            break
        if rec.AcceptWaveform(data):
            pieces.append(json.loads(rec.Result()).get("text", ""))
    pieces.append(json.loads(rec.FinalResult()).get("text", ""))
    return "".join(pieces).replace(" ", "").strip()


def _voice_dictionary_prompt() -> str:
    punct = (
        "请更积极地输出自然中文标点：根据语义加入逗号、句号、问号、顿号、分号或冒号，"
        "避免整段无标点；除识别文字和标点外不要额外解释。"
    )
    explicit = os.environ.get("VOICE_IME_WHISPER_PROMPT", "").strip()
    if explicit:
        return explicit + "\n" + punct
    # Kept for the faster-whisper fallback.  The active Qwen3-ASR backend gets
    # the same merged context through qwen_asr_runtime -> sidecar.  MiMo's
    # public ASR API does not currently expose hotword/context biasing.
    context = voice_terms.build_asr_context(max_chars=2000)
    return context or punct


def transcribe(wav_path: str) -> str:
    # Normalize loudness + remove low-frequency rumble and (optionally) steady
    # noise before any backend reads the file.  Cheap (sox subprocess) and
    # fails safe: on any problem it returns the original path untouched, so
    # recognition proceeds exactly as before.  Toggle via VOICE_IME_AUDIO_*.
    wav_path = audio_preprocess.preprocess_wav(wav_path)
    # Backend selection follows the VOICE_IME_ASR_BACKEND / *_ASR flags.  Order
    # is intentional: local Qwen3-ASR first (project default), then local MiMo,
    # MiMo cloud, custom command, faster-whisper, vosk.  Each helper returns
    # None when its selector is off, so the first selected backend wins.
    for backend in (
        _transcribe_with_qwen_asr,
        _transcribe_with_mimo_asr,
        _transcribe_with_mimo_cloud_asr,
        _transcribe_with_volc_bigmodel_asr,
        _transcribe_with_command,
        _transcribe_with_faster_whisper,
        _transcribe_with_vosk,
    ):
        text = backend(wav_path)
        if text is not None:
            return text
    raise VoiceError(
        "没有可用的语音识别后端。请检查 VOICE_IME_ASR_BACKEND / VOICE_IME_QWEN_ASR，"
        "或安装 faster-whisper，或设置 VOICE_IME_ASR_CMD / VOICE_IME_VOSK_MODEL。"
    )


def postprocess(raw_text: str) -> str:
    mode = os.environ.get("VOICE_IME_VOICE_MODE", "dictation").strip().lower()
    # When the Volcano bigmodel backend is doing cloud-side semantic smoothing
    # (enable_ddc), suppress the local filler-removal pass so the same
    # disfluencies are not processed twice; the cloud's smoothing is preferred.
    saved_remove_fillers = os.environ.get("VOICE_IME_REMOVE_FILLERS")
    if volc_bigmodel_asr.cloud_smoothing_on():
        os.environ["VOICE_IME_REMOVE_FILLERS"] = "0"
    try:
        normalized = text_postprocess.normalize(raw_text, mode=mode)
    finally:
        if saved_remove_fillers is None:
            os.environ.pop("VOICE_IME_REMOVE_FILLERS", None)
        else:
            os.environ["VOICE_IME_REMOVE_FILLERS"] = saved_remove_fillers
    # Strict policy: never call llm_postprocess here and do not add punctuation
    # locally. Keep the final path deterministic: cleanup -> script normalization.
    return chinese_script.normalize(normalized)


def record_and_transcribe(seconds: int | None = None) -> str:
    seconds = int(seconds or os.environ.get("VOICE_IME_RECORD_SECONDS", "5"))
    with tempfile.TemporaryDirectory(prefix="ibus-voice-ime-") as tmp:
        wav_path = str(Path(tmp) / "record.wav")
        _record_wav(wav_path, seconds)
        raw = transcribe(wav_path)
        return postprocess(raw)
