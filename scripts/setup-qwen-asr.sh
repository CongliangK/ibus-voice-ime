#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv-qwen-asr"
MODELS_DIR="${VOICE_IME_QWEN_ASR_MODELS_DIR:-$ROOT_DIR/vendor/models/qwen3-asr}"
PYTHON_BIN="${VOICE_IME_QWEN_ASR_SETUP_PYTHON:-$(command -v python3)}"
ENABLE_NOW="${VOICE_IME_ENABLE_QWEN_ASR:-1}"
FORCE=0
PROXY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --proxy) [[ $# -ge 2 ]] || { echo "--proxy 需要参数" >&2; exit 2; }; PROXY="$2"; shift 2 ;;
    --proxy=*) PROXY="${1#--proxy=}"; shift ;;
    -h|--help) echo "用法：$0 [--force] [--proxy http://127.0.0.1:7890]（--force 跳过 GPU 检查）"; exit 0 ;;
    *) echo "未知参数：$1（--force 跳过 GPU 检查；--proxy http://.. 代理下载）" >&2; exit 2 ;;
  esac
done
if [[ -n "$PROXY" ]]; then
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
fi

# GPU 门控：无可用 CUDA 时本脚本装出来的 6GB 模型完全用不上；默认跳过并给云端替代
# （doctor fix / 手动运行都会走到这里，绕过了 init.sh 的门控）。--force 强制安装。
GPU_STATE="" GPU_REASON="" GPU_ACTION=""
if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
fi
# 仅当探测明确判定 partial/none 才拦截；探测脚本本身缺失（非完整 clone）时
# 不阻断安装，交由后续 import/加载失败兜底。
if [[ ( "$GPU_STATE" == "partial" || "$GPU_STATE" == "none" ) && $FORCE -eq 0 && "${VOICE_IME_FORCE_QWEN_SETUP:-0}" != "1" ]]; then
  echo "跳过：本机 NVIDIA GPU 不可用（${GPU_REASON:-未检测到}），本地 Qwen3-ASR 装了也跑不起来。"
  [[ -n "$GPU_ACTION" ]] && printf '%s\n' "$GPU_ACTION"
  echo "确认要在无 GPU 机器上安装（仅调试用）：./scripts/setup-qwen-asr.sh --force"
  exit 0
fi

DEFAULT_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"
case "${DEFAULT_MODEL,,}" in
  0.6|0.6b|qwen3-asr-0.6b) DEFAULT_MODEL=0.6b ;;
  1.7|1.7b|qwen3-asr-1.7b) DEFAULT_MODEL=1.7b ;;
  *)
    echo "无效的 VOICE_IME_QWEN_ASR_MODEL：$DEFAULT_MODEL（支持 0.6b / 1.7b）" >&2
    exit 2
    ;;
esac
DEFAULT_MODEL_DIR="$MODELS_DIR/Qwen3-ASR-$( [[ "${DEFAULT_MODEL}" == "1.7b" ]] && echo 1.7B || echo 0.6B )"

mkdir -p "$MODELS_DIR"

# Debian/Ubuntu 缺 python3-venv 时，`python3 -m venv` 会留下只有 bin/python、没有
# pip 的半成品目录且本脚本的就绪守卫（只看 bin/python）会误判“已就绪”，重跑永远
# 卡死。预检 ensurepip + 守卫升级为 bin/python+bin/pip 双条件，半成品自愈重建。
if ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
  echo "错误：$PYTHON_BIN 缺少 ensurepip/venv 模块，无法创建虚拟环境。" >&2
  echo "  Fedora:      sudo dnf install python3-pip" >&2
  echo "  Debian/Ubuntu: sudo apt install python3-venv python3-pip" >&2
  exit 1
fi
if [[ -d "$VENV" && -x "$VENV/bin/python" && ! -x "$VENV/bin/pip" ]]; then
  echo "检测到半成品 venv（有 python 无 pip，多为缺 python3-venv 时创建失败残留），删除重建：$VENV"
  rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" || ! -x "$VENV/bin/pip" ]]; then
  echo "创建 Qwen3-ASR 独立虚拟环境：$VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi

PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
echo "Qwen3-ASR venv Python：$("$PY" -V 2>&1)"
"$PY" -m pip install --upgrade pip

cat <<'EOF_NOTE'
安装 qwen-asr 依赖。注意：官方建议 Python 3.12；如果当前系统 Python 太新导致 PyTorch/qwen-asr 安装失败，
需要改用 Python 3.12 创建 .venv-qwen-asr，或使用官方 Docker/vLLM 部署。
EOF_NOTE
if ! "$PIP" install -U qwen-asr modelscope; then
  echo "错误：qwen-asr 依赖安装失败。常见原因：" >&2
  echo "  1. Python 版本不兼容（上面打印的版本）→ 用其他解释器重试：" >&2
  echo "       VOICE_IME_QWEN_ASR_SETUP_PYTHON=/usr/bin/python3.12 $0" >&2
  echo "  2. 网络不通 → 加代理重试：$0 --proxy http://127.0.0.1:7890" >&2
  exit 1
fi

# Download both requested models from ModelScope.  They can also be used by
# qwen-asr through local paths, avoiding runtime network downloads.
MS_CLI="$VENV/bin/modelscope"
if [[ ! -x "$MS_CLI" ]]; then
  MS_CLI="$(command -v modelscope || true)"
fi
if [[ -z "$MS_CLI" ]]; then
  echo "未找到 modelscope 命令，无法下载 Qwen3-ASR 模型。" >&2
  exit 1
fi

if [[ ! -f "$MODELS_DIR/Qwen3-ASR-0.6B/model.safetensors" && ! -f "$MODELS_DIR/Qwen3-ASR-0.6B/model.safetensors.index.json" ]]; then
  echo "下载 Qwen/Qwen3-ASR-0.6B ..."
  "$MS_CLI" download --model Qwen/Qwen3-ASR-0.6B --local_dir "$MODELS_DIR/Qwen3-ASR-0.6B"
else
  echo "已存在：$MODELS_DIR/Qwen3-ASR-0.6B"
fi

if [[ ! -f "$MODELS_DIR/Qwen3-ASR-1.7B/model.safetensors" && ! -f "$MODELS_DIR/Qwen3-ASR-1.7B/model.safetensors.index.json" ]]; then
  echo "下载 Qwen/Qwen3-ASR-1.7B ..."
  "$MS_CLI" download --model Qwen/Qwen3-ASR-1.7B --local_dir "$MODELS_DIR/Qwen3-ASR-1.7B"
else
  echo "已存在：$MODELS_DIR/Qwen3-ASR-1.7B"
fi

# Verify the import.  Do not load model here; loading can take time and memory.
"$PY" - <<'PY'
import qwen_asr
from qwen_asr import Qwen3ASRModel
print('qwen_asr import ok:', getattr(qwen_asr, '__version__', 'unknown'))
PY

if [[ "$ENABLE_NOW" != "0" ]]; then
  COMPONENT_DIR="$HOME/.local/share/ibus/component"
  IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
  ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
  mkdir -p "$(dirname "$ENV_FILE")"
  # expandable_segments lets torch.cuda.empty_cache() actually return reserved
  # VRAM after the idle watchdog offloads the model to CPU RAM.
  PYTORCH_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
  # Remove stale Qwen ASR lines then append current config.
  if [[ -f "$ENV_FILE" ]]; then
    TMP="$(mktemp)"
    grep -vE '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD_ASR|VOICE_IME_VOLC_BIGMODEL_ASR|PYTORCH_CUDA_ALLOC_CONF)' "$ENV_FILE" > "$TMP" || true
    mv "$TMP" "$ENV_FILE"
  fi
  cat >> "$ENV_FILE" <<EOF_ENV
VOICE_IME_ASR_BACKEND=qwen3-asr
VOICE_IME_QWEN_ASR=1
VOICE_IME_MIMO_ASR=0
VOICE_IME_MIMO_CLOUD_ASR=0
VOICE_IME_VOLC_BIGMODEL_ASR=0
VOICE_IME_QWEN_ASR_MODEL=$DEFAULT_MODEL
VOICE_IME_QWEN_ASR_MODEL_PATH=$DEFAULT_MODEL_DIR
VOICE_IME_QWEN_ASR_PYTHON=$VENV/bin/python
VOICE_IME_QWEN_ASR_HOST=127.0.0.1
VOICE_IME_QWEN_ASR_PORT=18081
VOICE_IME_QWEN_ASR_LANGUAGE=Chinese
VOICE_IME_QWEN_ASR_DTYPE=bfloat16
VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0
VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=1024
VOICE_IME_QWEN_ASR_START_TIMEOUT=180
VOICE_IME_QWEN_ASR_TIMEOUT=180
PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF
EOF_ENV
  systemctl --user set-environment \
    "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
    VOICE_IME_ASR_BACKEND=qwen3-asr \
    VOICE_IME_QWEN_ASR=1 \
    VOICE_IME_MIMO_ASR=0 \
    VOICE_IME_MIMO_CLOUD_ASR=0 \
    VOICE_IME_VOLC_BIGMODEL_ASR=0 \
    "VOICE_IME_QWEN_ASR_MODEL=$DEFAULT_MODEL" \
    "VOICE_IME_QWEN_ASR_MODEL_PATH=$DEFAULT_MODEL_DIR" \
    "VOICE_IME_QWEN_ASR_PYTHON=$VENV/bin/python" \
    VOICE_IME_QWEN_ASR_HOST=127.0.0.1 \
    VOICE_IME_QWEN_ASR_PORT=18081 \
    VOICE_IME_QWEN_ASR_LANGUAGE=Chinese \
    VOICE_IME_QWEN_ASR_DTYPE=bfloat16 \
    VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0 \
    VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=1024 \
    VOICE_IME_QWEN_ASR_START_TIMEOUT=180 \
    VOICE_IME_QWEN_ASR_TIMEOUT=180 \
    "PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF" 2>/dev/null || true
fi

cat <<EOF
Qwen3-ASR 配置完成。
  0.6B: $MODELS_DIR/Qwen3-ASR-0.6B
  1.7B: $MODELS_DIR/Qwen3-ASR-1.7B
  Python: $VENV/bin/python

当前启用 $DEFAULT_MODEL（已写入 environment.d）。切换档位：
  ./scripts/switch-qwen-asr.sh 1.7b    # 或 0.6b

切到云端/其他后端：./scripts/switch-mimo-cloud-asr.sh cn / ./scripts/switch-volc-bigmodel-asr.sh
EOF
