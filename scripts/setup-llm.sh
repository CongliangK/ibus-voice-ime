#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="$ROOT_DIR/vendor"
LLAMA_DIR="${VOICE_IME_LLAMA_CPP_DIR:-$VENDOR_DIR/llama.cpp}"
MODELS_DIR="${VOICE_IME_MODELS_DIR:-$VENDOR_DIR/models}"
MODEL_ALIAS="${VOICE_IME_LLM_MODEL:-qwen3.5-0.8b}"
# Older installs persisted the previous Ollama example as an environment value;
# when running this setup script, treat that stale default as unset.
if [[ "$MODEL_ALIAS" == "qwen2.5:7b-instruct" ]]; then
  MODEL_ALIAS="qwen3.5-0.8b"
fi
# The user-selected upstream model.  The official repository is Transformers
# format; llama.cpp needs a GGUF conversion/quantization of this model.
UPSTREAM_MODEL_ID="${VOICE_IME_MODELSCOPE_MODEL:-Qwen/Qwen3.5-0.8B}"
GGUF_MODEL_ID="${VOICE_IME_MODELSCOPE_GGUF_MODEL:-unsloth/Qwen3.5-0.8B-GGUF}"
GGUF_INCLUDE="${VOICE_IME_MODELSCOPE_GGUF_INCLUDE:-*Q4_K_M*.gguf}"
PORT="${VOICE_IME_LLAMA_PORT:-18080}"
HOST="${VOICE_IME_LLAMA_HOST:-127.0.0.1}"

mkdir -p "$VENDOR_DIR" "$MODELS_DIR"

PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

ensure_llama_server() {
  if [[ -n "${VOICE_IME_LLAMA_SERVER:-}" && -x "${VOICE_IME_LLAMA_SERVER}" ]]; then
    echo "使用已有 llama-server: ${VOICE_IME_LLAMA_SERVER}"
    return 0
  fi
  if [[ -x "$LLAMA_DIR/bin/llama-server" ]]; then
    echo "使用 vendor llama-server: $LLAMA_DIR/bin/llama-server"
    return 0
  fi
  if command -v llama-server >/dev/null 2>&1; then
    echo "使用系统 llama-server: $(command -v llama-server)"
    return 0
  fi

  if [[ "${VOICE_IME_BUILD_LLAMA_CPP:-1}" == "0" ]]; then
    cat >&2 <<EOF_ERR
未找到 llama-server。请安装 llama.cpp，或设置：
  export VOICE_IME_LLAMA_SERVER=/path/to/llama-server
EOF_ERR
    exit 1
  fi

  if [[ ! -d "$LLAMA_DIR/.git" ]]; then
    echo "克隆 llama.cpp 到 $LLAMA_DIR ..."
    git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
  else
    echo "更新 llama.cpp ..."
    git -C "$LLAMA_DIR" pull --ff-only || true
  fi

  echo "构建 llama-server ..."
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    ${VOICE_IME_LLAMA_CMAKE_ARGS:-}
  cmake --build "$LLAMA_DIR/build" --config Release -j"$(nproc)" --target llama-server
  mkdir -p "$LLAMA_DIR/bin"
  cp "$LLAMA_DIR/build/bin/llama-server" "$LLAMA_DIR/bin/llama-server"
}

find_model() {
  if [[ -n "${VOICE_IME_LLAMA_MODEL_PATH:-}" && -f "${VOICE_IME_LLAMA_MODEL_PATH}" ]]; then
    printf '%s\n' "${VOICE_IME_LLAMA_MODEL_PATH}"
    return 0
  fi
  local found
  found="$(find "$MODELS_DIR" -type f -name '*.gguf' 2>/dev/null | grep -E 'Q4_K_M|q4_k_m' | head -n 1 || true)"
  if [[ -z "$found" ]]; then
    found="$(find "$MODELS_DIR" -type f -name '*.gguf' 2>/dev/null | head -n 1 || true)"
  fi
  [[ -n "$found" ]] && printf '%s\n' "$found"
}

download_model() {
  local existing
  existing="$(find_model || true)"
  if [[ -n "$existing" ]]; then
    echo "已找到 GGUF 模型：$existing"
    return 0
  fi

  if [[ "${VOICE_IME_DOWNLOAD_LLM_MODEL:-1}" == "0" ]]; then
    cat >&2 <<EOF_ERR
未找到 GGUF 模型。llama.cpp 不能直接加载官方 Transformers 权重：$UPSTREAM_MODEL_ID
请下载/转换 GGUF 后设置：
  export VOICE_IME_LLAMA_MODEL_PATH=/path/to/qwen3.5-0.8b.gguf
EOF_ERR
    exit 1
  fi

  echo "安装/检查 ModelScope CLI ..."
  "$PYTHON" -m pip install -q -U modelscope
  local ms_cli
  ms_cli="$(dirname "$PYTHON")/modelscope"
  if [[ ! -x "$ms_cli" ]]; then
    ms_cli="$(command -v modelscope || true)"
  fi
  if [[ -z "$ms_cli" ]]; then
    echo "未找到 modelscope 命令。" >&2
    exit 1
  fi

  local target="$MODELS_DIR/qwen3.5-0.8b-gguf"
  mkdir -p "$target"
  echo "下载 Qwen3.5-0.8B 的 GGUF 量化模型 ..."
  echo "  上游模型：$UPSTREAM_MODEL_ID"
  echo "  GGUF 仓库：$GGUF_MODEL_ID"
  echo "  文件匹配：$GGUF_INCLUDE"
  if ! "$ms_cli" download \
      --model "$GGUF_MODEL_ID" \
      --include "$GGUF_INCLUDE" \
      --local_dir "$target"; then
    cat >&2 <<EOF_ERR
ModelScope GGUF 下载失败。
如果该仓库的文件名不同，请先查看模型文件列表，然后重试，例如：
  VOICE_IME_MODELSCOPE_GGUF_INCLUDE='*Q8_0*.gguf' ./scripts/setup-llm.sh
或手动下载 GGUF 并设置 VOICE_IME_LLAMA_MODEL_PATH。
EOF_ERR
    exit 1
  fi

  existing="$(find_model || true)"
  if [[ -z "$existing" ]]; then
    cat >&2 <<EOF_ERR
下载完成但没有找到 .gguf 文件。请检查 $target。
EOF_ERR
    exit 1
  fi
  echo "已下载 GGUF 模型：$existing"
}

persist_env() {
  local model_path="$1"
  local component_dir="$HOME/.local/share/ibus/component"
  local ibus_component_path="$component_dir:/usr/share/ibus/component"
  mkdir -p "$HOME/.config/environment.d"

  local env_file="$HOME/.config/environment.d/ibus-voice-ime.conf"
  # Preserve existing ASR/Rime values, but replace old LLM-related keys so
  # repeated runs do not accumulate stale Ollama/default entries.
  if [[ -f "$env_file" ]]; then
    local tmp
    tmp="$(mktemp)"
    grep -vE '^(VOICE_IME_LLM_|VOICE_IME_LLAMA_|VOICE_IME_MODELSCOPE_)' "$env_file" > "$tmp" || true
    mv "$tmp" "$env_file"
  fi
  cat >> "$env_file" <<EOF_ENV
VOICE_IME_LLM_POSTPROCESS=1
VOICE_IME_LLM_INTERNAL=1
VOICE_IME_LLM_TRUST_OUTPUT=1
VOICE_IME_LLM_RERANK=0
VOICE_IME_LLM_BASE_URL=http://$HOST:$PORT/v1
VOICE_IME_LLM_API_KEY=local
VOICE_IME_LLM_MODEL=$MODEL_ALIAS
VOICE_IME_LLAMA_MODEL_PATH=$model_path
VOICE_IME_LLAMA_PORT=$PORT
VOICE_IME_LLAMA_HOST=$HOST
VOICE_IME_LLAMA_CTX_SIZE=${VOICE_IME_LLAMA_CTX_SIZE:-2048}
VOICE_IME_LLAMA_THREADS=${VOICE_IME_LLAMA_THREADS:-$(nproc)}
VOICE_IME_LLAMA_REASONING=${VOICE_IME_LLAMA_REASONING:-off}
VOICE_IME_LLM_CANDIDATES=${VOICE_IME_LLM_CANDIDATES:-1}
VOICE_IME_LLM_TIMEOUT=${VOICE_IME_LLM_TIMEOUT:-4}
VOICE_IME_LLM_TEMPERATURE=${VOICE_IME_LLM_TEMPERATURE:-0.1}
VOICE_IME_LLM_FALLBACK_RAW=1
VOICE_IME_LLM_LOG=$HOME/.local/share/ibus-voice-ime/llm.jsonl
EOF_ENV

  systemctl --user set-environment \
    "IBUS_COMPONENT_PATH=$ibus_component_path" \
    VOICE_IME_LLM_POSTPROCESS=1 \
    VOICE_IME_LLM_INTERNAL=1 \
    VOICE_IME_LLM_TRUST_OUTPUT=1 \
    VOICE_IME_LLM_RERANK=0 \
    "VOICE_IME_LLM_BASE_URL=http://$HOST:$PORT/v1" \
    VOICE_IME_LLM_API_KEY=local \
    "VOICE_IME_LLM_MODEL=$MODEL_ALIAS" \
    "VOICE_IME_LLAMA_MODEL_PATH=$model_path" \
    "VOICE_IME_LLAMA_PORT=$PORT" \
    "VOICE_IME_LLAMA_HOST=$HOST" \
    "VOICE_IME_LLAMA_CTX_SIZE=${VOICE_IME_LLAMA_CTX_SIZE:-2048}" \
    "VOICE_IME_LLAMA_THREADS=${VOICE_IME_LLAMA_THREADS:-$(nproc)}" \
    "VOICE_IME_LLAMA_REASONING=${VOICE_IME_LLAMA_REASONING:-off}" \
    "VOICE_IME_LLM_CANDIDATES=${VOICE_IME_LLM_CANDIDATES:-1}" \
    "VOICE_IME_LLM_TIMEOUT=${VOICE_IME_LLM_TIMEOUT:-4}" \
    "VOICE_IME_LLM_TEMPERATURE=${VOICE_IME_LLM_TEMPERATURE:-0.1}" \
    VOICE_IME_LLM_FALLBACK_RAW=1 \
    "VOICE_IME_LLM_LOG=$HOME/.local/share/ibus-voice-ime/llm.jsonl" 2>/dev/null || true
}

ensure_llama_server
download_model
MODEL_PATH="$(find_model)"
persist_env "$MODEL_PATH"

cat <<EOF
LLM 内置推理配置完成。
  选择模型：$UPSTREAM_MODEL_ID
  llama.cpp 模型：$MODEL_PATH
  服务地址：http://$HOST:$PORT/v1
  API 模型名：$MODEL_ALIAS

启用方式：重启 IBus 后使用语音输入；首次 LLM 后处理会自动拉起 llama-server。
如需立即重启：ibus restart
EOF
