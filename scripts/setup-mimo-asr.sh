#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv-mimo-asr"
MODELS_DIR="${VOICE_IME_MIMO_ASR_MODELS_DIR:-$ROOT_DIR/vendor/models/mimo-asr}"
SRC_DIR="${VOICE_IME_MIMO_ASR_SOURCE_DIR:-$ROOT_DIR/vendor/MiMo-V2.5-ASR}"
PYTHON_BIN="${VOICE_IME_MIMO_ASR_SETUP_PYTHON:-$(command -v python3)}"
ENABLE_NOW="${VOICE_IME_ENABLE_MIMO_ASR:-1}"
INSTALL_FLASH_ATTN="${VOICE_IME_MIMO_ASR_INSTALL_FLASH_ATTN:-1}"
MODEL_DEVICE_MAP="${VOICE_IME_MIMO_ASR_MODEL_DEVICE_MAP:-auto}"
MODEL_MAX_MEMORY="${VOICE_IME_MIMO_ASR_MODEL_MAX_MEMORY:-cuda:0=8GiB,cpu=64GiB}"
MODEL_OFFLOAD_FOLDER="${VOICE_IME_MIMO_ASR_MODEL_OFFLOAD_FOLDER:-$HOME/.local/share/ibus-voice-ime/mimo-offload}"
PYTORCH_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

mkdir -p "$MODELS_DIR" "$(dirname "$SRC_DIR")" "$MODEL_OFFLOAD_FOLDER"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "创建 MiMo-ASR 独立虚拟环境：$VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi

PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
"$PY" -m pip install --upgrade pip

cat <<'EOF_NOTE'
安装 MiMo-V2.5-ASR 依赖。官方建议 Python 3.12、CUDA >= 12.0。
该模型体积较大，并依赖独立源码仓库和 MiMo-Audio-Tokenizer。
如果当前系统 Python 太新导致 torch/torchaudio 安装失败，请用 Python 3.12：
  VOICE_IME_MIMO_ASR_SETUP_PYTHON=/path/to/python3.12 ./scripts/setup-mimo-asr.sh
EOF_NOTE

if [[ ! -d "$SRC_DIR/.git" ]]; then
  echo "克隆 XiaomiMiMo/MiMo-V2.5-ASR 源码：$SRC_DIR"
  git clone --depth 1 https://github.com/XiaomiMiMo/MiMo-V2.5-ASR.git "$SRC_DIR"
else
  echo "更新 MiMo-V2.5-ASR 源码：$SRC_DIR"
  git -C "$SRC_DIR" pull --ff-only || true
fi

# Patch upstream local source so RTX 4080-class 16GB GPUs can use GPU+CPU
# offload instead of forcing the entire model onto cuda:0.
PATCH_FILE="$SRC_DIR/src/mimo_audio/mimo_audio.py"
"$PY" - "$PATCH_FILE" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
text = p.read_text(encoding='utf-8')
if 'import os\nimport torch' not in text:
    text = text.replace('import random\nimport torch\n', 'import random\nimport os\nimport torch\n')
old = '''        start_loading_time = time.monotonic()
        self.model = MiMoAudioForCausalLM.from_pretrained(
            self.path,
            args=model_args,
            torch_dtype=torch.bfloat16,
            device_map={"": self.device},
        )
'''
new = '''        start_loading_time = time.monotonic()
        model_device_map = os.environ.get("VOICE_IME_MIMO_ASR_MODEL_DEVICE_MAP", "").strip()
        model_max_memory = os.environ.get("VOICE_IME_MIMO_ASR_MODEL_MAX_MEMORY", "").strip()
        model_offload_folder = os.environ.get("VOICE_IME_MIMO_ASR_MODEL_OFFLOAD_FOLDER", "").strip()
        from_pretrained_kwargs = {
            "args": model_args,
            "torch_dtype": torch.bfloat16,
            "device_map": {"": self.device},
        }
        if model_device_map:
            lower = model_device_map.lower()
            if lower == "auto":
                from_pretrained_kwargs["device_map"] = "auto"
            elif lower in {"cpu", "cuda", "cuda:0"}:
                from_pretrained_kwargs["device_map"] = {"": lower}
        if model_max_memory:
            max_memory = {}
            for item in model_max_memory.split(","):
                if not item.strip() or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key = key.strip()
                if key.startswith("cuda:"):
                    try:
                        max_memory[int(key.split(":", 1)[1])] = value.strip()
                    except Exception:
                        pass
                elif key:
                    max_memory[key] = value.strip()
            if max_memory:
                from_pretrained_kwargs["max_memory"] = max_memory
        if model_offload_folder:
            from_pretrained_kwargs["offload_folder"] = model_offload_folder
        self.model = MiMoAudioForCausalLM.from_pretrained(self.path, **from_pretrained_kwargs)
'''
if old in text:
    text = text.replace(old, new)
p.write_text(text, encoding='utf-8')
PY

"$PIP" install -r "$SRC_DIR/requirements.txt"
"$PIP" install -U modelscope "huggingface-hub>=0.26.0,<1.0"

if [[ "$INSTALL_FLASH_ATTN" != "0" ]]; then
  "$PIP" install 'https://github.com/Dao-AILab/flash-attention/releases/download/v2.7.4.post1/flash_attn-2.7.4.post1+cu12torch2.6cxx11abiFALSE-cp312-cp312-linux_x86_64.whl'
else
  cat <<'EOF_FLASH'
跳过 flash-attn 安装。注意：MiMo-Audio-Tokenizer 会导入 flash_attn；如未安装，验证/运行会失败。
EOF_FLASH
fi

MS_CLI="$VENV/bin/modelscope"
if [[ ! -x "$MS_CLI" ]]; then
  MS_CLI="$(command -v modelscope || true)"
fi
if [[ -z "$MS_CLI" ]]; then
  echo "未找到 modelscope 命令，无法下载 MiMo-ASR 模型。" >&2
  exit 1
fi

if [[ ! -f "$MODELS_DIR/MiMo-Audio-Tokenizer/model.safetensors" ]]; then
  echo "下载 XiaomiMiMo/MiMo-Audio-Tokenizer ..."
  "$MS_CLI" download --model XiaomiMiMo/MiMo-Audio-Tokenizer --local_dir "$MODELS_DIR/MiMo-Audio-Tokenizer"
else
  echo "已存在：$MODELS_DIR/MiMo-Audio-Tokenizer"
fi

if [[ ! -f "$MODELS_DIR/MiMo-V2.5-ASR/model.safetensors.index.json" ]]; then
  echo "下载 XiaomiMiMo/MiMo-V2.5-ASR ..."
  "$MS_CLI" download --model XiaomiMiMo/MiMo-V2.5-ASR --local_dir "$MODELS_DIR/MiMo-V2.5-ASR"
else
  echo "已存在：$MODELS_DIR/MiMo-V2.5-ASR"
fi

# Verify imports only. Do not load model here; loading can take a long time and a lot of VRAM/RAM.
VOICE_IME_MIMO_ASR_SOURCE="$SRC_DIR" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ['VOICE_IME_MIMO_ASR_SOURCE'])
from src.mimo_audio.mimo_audio import MimoAudio
print('mimo asr import ok:', MimoAudio.__name__)
PY

if [[ "$ENABLE_NOW" != "0" ]]; then
  COMPONENT_DIR="$HOME/.local/share/ibus/component"
  IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
  ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
  mkdir -p "$(dirname "$ENV_FILE")" "$MODEL_OFFLOAD_FOLDER"
  if [[ -f "$ENV_FILE" ]]; then
    TMP="$(mktemp)"
    grep -vE '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|PYTORCH_CUDA_ALLOC_CONF)' "$ENV_FILE" > "$TMP" || true
    mv "$TMP" "$ENV_FILE"
  fi
  cat >> "$ENV_FILE" <<EOF_ENV
VOICE_IME_ASR_BACKEND=mimo-asr
VOICE_IME_QWEN_ASR=0
VOICE_IME_MIMO_ASR=1
VOICE_IME_MIMO_ASR_MODEL_PATH=$MODELS_DIR/MiMo-V2.5-ASR
VOICE_IME_MIMO_ASR_TOKENIZER_PATH=$MODELS_DIR/MiMo-Audio-Tokenizer
VOICE_IME_MIMO_ASR_SOURCE=$SRC_DIR
VOICE_IME_MIMO_ASR_PYTHON=$VENV/bin/python
VOICE_IME_MIMO_ASR_HOST=127.0.0.1
VOICE_IME_MIMO_ASR_PORT=18082
VOICE_IME_MIMO_ASR_LANGUAGE=auto
VOICE_IME_MIMO_ASR_DEVICE=cuda
VOICE_IME_MIMO_ASR_START_TIMEOUT=300
VOICE_IME_MIMO_ASR_TIMEOUT=300
VOICE_IME_MIMO_ASR_MODEL_DEVICE_MAP=$MODEL_DEVICE_MAP
VOICE_IME_MIMO_ASR_MODEL_MAX_MEMORY=$MODEL_MAX_MEMORY
VOICE_IME_MIMO_ASR_MODEL_OFFLOAD_FOLDER=$MODEL_OFFLOAD_FOLDER
PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF
EOF_ENV
  systemctl --user set-environment \
    "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
    VOICE_IME_ASR_BACKEND=mimo-asr \
    VOICE_IME_QWEN_ASR=0 \
    VOICE_IME_MIMO_ASR=1 \
    "VOICE_IME_MIMO_ASR_MODEL_PATH=$MODELS_DIR/MiMo-V2.5-ASR" \
    "VOICE_IME_MIMO_ASR_TOKENIZER_PATH=$MODELS_DIR/MiMo-Audio-Tokenizer" \
    "VOICE_IME_MIMO_ASR_SOURCE=$SRC_DIR" \
    "VOICE_IME_MIMO_ASR_PYTHON=$VENV/bin/python" \
    VOICE_IME_MIMO_ASR_HOST=127.0.0.1 \
    VOICE_IME_MIMO_ASR_PORT=18082 \
    VOICE_IME_MIMO_ASR_LANGUAGE=auto \
    VOICE_IME_MIMO_ASR_DEVICE=cuda \
    VOICE_IME_MIMO_ASR_START_TIMEOUT=300 \
    VOICE_IME_MIMO_ASR_TIMEOUT=300 \
    "VOICE_IME_MIMO_ASR_MODEL_DEVICE_MAP=$MODEL_DEVICE_MAP" \
    "VOICE_IME_MIMO_ASR_MODEL_MAX_MEMORY=$MODEL_MAX_MEMORY" \
    "VOICE_IME_MIMO_ASR_MODEL_OFFLOAD_FOLDER=$MODEL_OFFLOAD_FOLDER" \
    "PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF" 2>/dev/null || true
fi

cat <<EOF
MiMo-ASR 配置完成。
  Model:     $MODELS_DIR/MiMo-V2.5-ASR
  Tokenizer: $MODELS_DIR/MiMo-Audio-Tokenizer
  Source:    $SRC_DIR
  Python:    $VENV/bin/python

启用/重启：
  ./scripts/switch-mimo-asr.sh

回退 faster-whisper：
  export VOICE_IME_ASR_BACKEND=faster-whisper
  export VOICE_IME_MIMO_ASR=0
EOF
