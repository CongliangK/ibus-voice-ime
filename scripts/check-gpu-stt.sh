#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

# Same env knobs as the IBus runtime; only the model defaults to tiny so the check is fast;
# pass VOICE_IME_WHISPER_MODEL=small/medium/... to validate another model.
export VOICE_IME_WHISPER_MODEL="${VOICE_IME_WHISPER_MODEL:-tiny}"
export VOICE_IME_WHISPER_DEVICE="${VOICE_IME_WHISPER_DEVICE:-cuda}"
export VOICE_IME_WHISPER_DEVICE_INDEX="${VOICE_IME_WHISPER_DEVICE_INDEX:-0}"
export VOICE_IME_WHISPER_COMPUTE="${VOICE_IME_WHISPER_COMPUTE:-float16}"
export VOICE_IME_WHISPER_LANGUAGE="${VOICE_IME_WHISPER_LANGUAGE:-zh}"
export VOICE_IME_REQUIRE_GPU_STT="${VOICE_IME_REQUIRE_GPU_STT:-1}"

CUDA_PIP_LIB_PATH="$("$PYTHON" - <<'PY' 2>/dev/null || true
import os
paths = []
for modname in ("nvidia.cublas.lib", "nvidia.cudnn.lib", "nvidia.cuda_nvrtc.lib"):
    try:
        mod = __import__(modname, fromlist=["__path__", "__file__"])
        candidates = list(getattr(mod, "__path__", []) or [])
        file = getattr(mod, "__file__", None)
        if file:
            candidates.append(os.path.dirname(file))
        for path in candidates:
            if path and os.path.isdir(path) and path not in paths:
                paths.append(path)
    except Exception:
        pass
print(":".join(paths))
PY
)"
if [[ -n "$CUDA_PIP_LIB_PATH" ]]; then
  export LD_LIBRARY_PATH="$CUDA_PIP_LIB_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

"$PYTHON" - <<'PY'
import os
import tempfile
import time
import wave

import ctranslate2  # type: ignore
from faster_whisper import WhisperModel  # type: ignore

model_name = os.environ["VOICE_IME_WHISPER_MODEL"]
device = os.environ["VOICE_IME_WHISPER_DEVICE"]
device_index = int(os.environ["VOICE_IME_WHISPER_DEVICE_INDEX"])
compute_type = os.environ["VOICE_IME_WHISPER_COMPUTE"]
language = os.environ.get("VOICE_IME_WHISPER_LANGUAGE", "zh") or None

cuda_count = ctranslate2.get_cuda_device_count()
print(f"CTranslate2 CUDA device count: {cuda_count}")
if device != "cuda":
    raise SystemExit(f"FAIL: VOICE_IME_WHISPER_DEVICE={device!r}, expected 'cuda'")
if cuda_count <= device_index:
    raise SystemExit(f"FAIL: CUDA device index {device_index} is not available")

with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
    wav_path = f.name
try:
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\0\0" * 16000)

    start = time.time()
    whisper = WhisperModel(
        model_name,
        device="cuda",
        device_index=device_index,
        compute_type=compute_type,
    )
    segments, info = whisper.transcribe(wav_path, language=language, vad_filter=True)
    list(segments)
    elapsed = time.time() - start
    print(f"OK: faster-whisper is running on cuda:{device_index} / {compute_type}")
    print(f"Model: {model_name}; language={info.language}; elapsed={elapsed:.3f}s")
finally:
    try:
        os.unlink(wav_path)
    except OSError:
        pass
PY
