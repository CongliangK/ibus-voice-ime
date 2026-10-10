#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$ROOT_DIR/vendor/rime"

# Python sources live under src/ as the ibus_voice_ime package.
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
ENGINE="$ROOT_DIR/src/ibus_voice_ime/engine.py"

if [[ -d "$RUNTIME/lib" ]]; then
  export LD_LIBRARY_PATH="$RUNTIME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

LOG_DIR="$HOME/.local/share/ibus-voice-ime"
mkdir -p "$LOG_DIR"

# 日志保留策略：每次引擎启动把日志裁剪到最近 N 行（默认 1000，防止日志洪水；
# 运行中的追加侧裁剪见 log_trim 模块）。VOICE_IME_LOG_KEEP_LINES=0 可关闭。
trim_log() {
  local file="$1" keep lines tmp
  [[ -f "$file" ]] || return 0
  keep="${VOICE_IME_LOG_KEEP_LINES:-1000}"
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=1000
  [[ "$keep" -gt 0 ]] || return 0
  lines="$(wc -l < "$file")"
  [[ "$lines" -gt $((keep * 2)) ]] || return 0
  tmp="$(mktemp)"
  if tail -n "$keep" "$file" > "$tmp"; then
    mv "$tmp" "$file"
  else
    rm -f "$tmp"
  fi
}
trim_log "$LOG_DIR/engine.log"
trim_log "$LOG_DIR/error.log"

exec >> "$LOG_DIR/engine.log" 2>&1

# Source the persisted user environment (written by install.sh and the
# scripts/switch-*-asr.sh helpers) so the engine always reflects the user's
# chosen ASR/IME configuration, even when ibus-daemon was respawned with a
# stale session environment.  Lines are simple KEY=VALUE and safe to source.
# RIME 四键继承优先：调用方显式 export 的救援配置（如非 Fedora 机器改用系统
# librime：VOICE_IME_RIME_LIBRARY=/usr/lib64/librime.so.1）不被文件旧值覆盖。
_INHERITED_RIME_LIBRARY="${VOICE_IME_RIME_LIBRARY:-}"
_INHERITED_RIME_SHARED_DATA_DIR="${VOICE_IME_RIME_SHARED_DATA_DIR:-}"
_INHERITED_RIME_STAGING_DIR="${VOICE_IME_RIME_STAGING_DIR:-}"
_INHERITED_RIME_USER_DATA_DIR="${VOICE_IME_RIME_USER_DATA_DIR:-}"

# 陈旧会话环境自愈（2026-08-28 打字事故根因）：ibus-daemon 继承的是登录时
# 的会话环境；仓库被迁移/删除后，其中的绝对路径全部失效，而 environment.d
# 的新值要等下次登录才进入会话环境。继承的"救援值"只有指向真实存在的路径
# 时才有效——指向不存在路径的继承值视为陈旧并忽略，让 environment.d 与
# 本仓库默认值接管。
_forget_stale_inherit() {  # $1=_INHERITED_* 变量名：值非空且路径不存在时置空
  local -n var="$1"
  if [[ -n "$var" && ! -e "$var" ]]; then
    echo "[run-engine] 自愈：继承的 $1 指向不存在的路径（$var），已忽略" >&2
    var=""
  fi
  return 0
}
_forget_stale_inherit _INHERITED_RIME_LIBRARY
_forget_stale_inherit _INHERITED_RIME_SHARED_DATA_DIR
_forget_stale_inherit _INHERITED_RIME_STAGING_DIR
_forget_stale_inherit _INHERITED_RIME_USER_DATA_DIR

ENV_CONF="$HOME/.config/environment.d/ibus-voice-ime.conf"
if [[ -r "$ENV_CONF" ]]; then
  # 行级解析替代 `set -a; source`：值含空格（HOME/仓库路径带空格）时 source 会把
  # 值当 shell 语法执行导致引擎启动失败；systemd environment.d 也不做 shell 展开。
  if [[ -x "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
    eval "$("$ROOT_DIR/scripts/env-file-load.sh" "$ENV_CONF")"
  else
    set -a
    # shellcheck disable=SC1090
    source "$ENV_CONF"
    set +a
  fi
fi
[[ -n "$_INHERITED_RIME_LIBRARY" ]] && export VOICE_IME_RIME_LIBRARY="$_INHERITED_RIME_LIBRARY"
[[ -n "$_INHERITED_RIME_SHARED_DATA_DIR" ]] && export VOICE_IME_RIME_SHARED_DATA_DIR="$_INHERITED_RIME_SHARED_DATA_DIR"
[[ -n "$_INHERITED_RIME_STAGING_DIR" ]] && export VOICE_IME_RIME_STAGING_DIR="$_INHERITED_RIME_STAGING_DIR"
[[ -n "$_INHERITED_RIME_USER_DATA_DIR" ]] && export VOICE_IME_RIME_USER_DATA_DIR="$_INHERITED_RIME_USER_DATA_DIR"

# QWEN 同类自愈：陈旧的继承路径（仓库迁移残留）不得压过 env 文件/默认值。
[[ -n "${VOICE_IME_QWEN_ASR_MODEL_PATH:-}" && ! -e "$VOICE_IME_QWEN_ASR_MODEL_PATH" ]] && {
  echo "[run-engine] 自愈：继承的 VOICE_IME_QWEN_ASR_MODEL_PATH 指向不存在的路径，已忽略" >&2
  unset VOICE_IME_QWEN_ASR_MODEL_PATH
}

# LLM_CONFIG 同理：llm.json 是可选连接文件（缺失 = LLM 润色关闭，非错误，
# 见 llm_cloud_config.load_report 的契约）。陈旧的继承路径（仓库迁移残留）
# 忽略之，让 Python 端 config_path() 的 XDG 默认值接管——否则一次迁移后
# LLM 润色会被静默禁用且无任何日志。
# 注意：本脚本不导出 VOICE_IME_LLM_CONFIG 默认值。消费者只有 engine 进程内
# 的 llm_cloud_config.config_path()（自带 XDG 兜底）；doctor.sh 与
# setup-llm-cloud.sh 均自行计算路径。此前无条件导出的默认路径会进入引擎
# /proc/environ，被 doctor 的陈旧路径检查误报为 FAIL。
[[ -n "${VOICE_IME_LLM_CONFIG:-}" && ! -e "$VOICE_IME_LLM_CONFIG" ]] && {
  echo "[run-engine] 自愈：继承的 VOICE_IME_LLM_CONFIG 指向不存在的路径，已忽略" >&2
  unset VOICE_IME_LLM_CONFIG
}

# ---------------------------------------------------------------------------
# 统一 JSON 配置时代（docs/configuration.md）：本脚本不再导出任何
# VOICE_IME_* 默认值——渠道/行为默认统一由 config/defaults.json 在进程内生效
# （用户覆盖走 ~/.config/ibus-voice-ime/config.json）。environment.d 与会话
# env 中的 VOICE_IME_* 仍作为最高优先级覆盖生效（上面已 source env 文件），
# 机器路径与用户残留值不会被丢弃。
#
# 保留导出的例外（已逐一核对消费者）：
#   - PYTHONPATH / LD_LIBRARY_PATH：Python 解释器与 librime/CTranslate2 加载需要；
#   - VOICE_IME_LLM_POSTPROCESS / VOICE_IME_LLM_INTERNAL：本地 llama.cpp 遗留路径
#     的显式压制（非 `${...:-默认}` 模式的默认导出，是策略性覆盖）；
#   - PYTORCH_CUDA_ALLOC_CONF：非 VOICE_IME_ 键，Qwen sidecar 显存回收需要。
# VOICE_IME_LLM_CONFIG 不再默认导出（见上方自愈段注释）：llm.json 缺失是
# 合法的"功能关闭"状态，导出一个可能不存在的默认路径只会制造误报。
# 引擎外的 shell 消费者（scripts/voice-toggle.sh / clipboard-paste.sh 的 IPC
# socket 与日志路径、scripts/doctor.sh 等）都自行计算路径或读用户会话 env，
# 不依赖本进程导出的 VOICE_IME_* 默认值。
# ---------------------------------------------------------------------------
export VOICE_IME_LLM_POSTPROCESS=0
export VOICE_IME_LLM_INTERNAL=0
# Use an expandable-segment CUDA allocator so that after the idle watchdog
# moves the model to CPU RAM, ``torch.cuda.empty_cache()`` can actually return
# the reserved VRAM to the system.  Without this, post-inference cached blocks
# stay reserved (~3 GB on the 1.7B model) and are never released.
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

# If CUDA runtime libraries are installed through NVIDIA pip wheels, make them
# visible to CTranslate2 before Python starts.  System CUDA installs continue to
# work through the regular loader paths.
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

# If a cloud ASR backend is selected but its API key is not present in the
# environment, load it just-in-time from Bitwarden Secrets Manager.  This keeps
# keys out of persistent environment.d files; only this engine process
# receives the injected secret(s).  MiMo cloud, Volcano bigmodel and
# SiliconFlow ASR are all supported, and a single bws run wrapper can inject
# several if several are somehow configured.
#
# 渠道判定与密钥名改为从统一配置读（env > config.json > defaults.json，
# `config get` CLI 已实现该优先级）：本脚本不再导出渠道默认值，否则 env 层
# 永远压过 config.json 的渠道选择。遗留 VOICE_IME_*_ASR 渠道 flag 仍作为 env
# 覆盖开关参与判定。python 早晚要起，这点开销可接受。
ASR_BACKEND_LC="$("$PYTHON" -m ibus_voice_ime.config get asr.backend 2>/dev/null || true)"
if [[ -z "$ASR_BACKEND_LC" ]]; then
  # 二级回退：config CLI 失败（如 defaults.json 损坏/包缺失）时，读
  # environment.d 里残留的渠道行；都没有才落 qwen3-asr。
  ASR_BACKEND_LC="$(grep -m1 '^VOICE_IME_ASR_BACKEND=' "$ENV_CONF" 2>/dev/null | cut -d= -f2- || true)"
fi
ASR_BACKEND_LC="${ASR_BACKEND_LC:-qwen3-asr}"
ASR_BACKEND_LC="${ASR_BACKEND_LC,,}"
MIMO_ACTIVE=0
VOLC_ACTIVE=0
SILICONFLOW_ACTIVE=0
if [[ -z "${VOICE_IME_MIMO_API_KEY:-${MIMO_API_KEY:-}}" ]] && \
   [[ "$ASR_BACKEND_LC" == "mimo-cloud" || "$ASR_BACKEND_LC" == "mimo-cloud-asr" || "$ASR_BACKEND_LC" == "mimo-api" || "$ASR_BACKEND_LC" == "mimo-api-asr" || "$ASR_BACKEND_LC" == "mimo-tokenplan-asr" || "${VOICE_IME_MIMO_CLOUD_ASR:-0}" == "1" ]]; then
  MIMO_ACTIVE=1
fi
if [[ -z "${VOICE_IME_VOLC_API_KEY:-${VOLC_BIGMODEL_API_KEY:-${VOLC_ASR_API_KEY:-}}}" ]] && \
   [[ "$ASR_BACKEND_LC" == "volc" || "$ASR_BACKEND_LC" == "volc-asr" || "$ASR_BACKEND_LC" == "volc-engine-asr" || "$ASR_BACKEND_LC" == "volc-bigmodel" || "$ASR_BACKEND_LC" == "volc-bigmodel-asr" || "$ASR_BACKEND_LC" == "doubao-asr" || "$ASR_BACKEND_LC" == "doubao-bigmodel-asr" || "${VOICE_IME_VOLC_BIGMODEL_ASR:-0}" == "1" ]]; then
  VOLC_ACTIVE=1
fi
if [[ -z "${VOICE_IME_SILICONFLOW_API_KEY:-${SILICONFLOW_API_KEY:-}}" ]] && \
   [[ "$ASR_BACKEND_LC" == "siliconflow" || "$ASR_BACKEND_LC" == "siliconflow-asr" || "$ASR_BACKEND_LC" == "sf-asr" || "${VOICE_IME_SILICONFLOW_ASR:-0}" == "1" ]]; then
  SILICONFLOW_ACTIVE=1
fi
if [[ "$MIMO_ACTIVE" == "1" || "$VOLC_ACTIVE" == "1" || "$SILICONFLOW_ACTIVE" == "1" ]]; then
  BWS_ENV_FILE="${VOICE_IME_BWS_ENV_FILE:-${PI_BWS_ENV_FILE:-$HOME/.config/pi-secrets/bws.env}}"
  if [[ -r "$BWS_ENV_FILE" ]] && command -v bws >/dev/null 2>&1; then
    if [[ -x "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
      eval "$("$ROOT_DIR/scripts/env-file-load.sh" "$BWS_ENV_FILE")"
    else
      set -a
      # shellcheck disable=SC1090
      source "$BWS_ENV_FILE"
      set +a
    fi
    BWS_PROJECT_ID="${VOICE_IME_BWS_PROJECT_ID:-${BWS_PI_PROJECT_ID:-}}"
    if [[ -n "${BWS_ACCESS_TOKEN:-}" && -n "$BWS_PROJECT_ID" ]]; then
      PY_CMD=("$PYTHON" "$ENGINE" "$@")
      printf -v PY_CMD_Q '%q ' "${PY_CMD[@]}"
      # Build the in-wrapper env injection lines for each active backend.
      # Secret NAMES come from the unified config (asr.*.api_key_secret，
      # env VOICE_IME_*_API_KEY_SECRET 仍可覆盖——`config get` 已实现该优先级)；
      # the raw key itself never appears in any persisted file.
      BWS_INJECT=""
      if [[ "$MIMO_ACTIVE" == "1" ]]; then
        MIMO_SECRET="$("$PYTHON" -m ibus_voice_ime.config get asr.mimo_cloud.api_key_secret 2>/dev/null || echo XIAOMI_TOKEN_PLAN_CN_API_KEY)"
        BWS_INJECT+="if [[ -z \"\${VOICE_IME_MIMO_API_KEY:-}\" && -n \"\${${MIMO_SECRET}:-}\" ]]; then export VOICE_IME_MIMO_API_KEY=\"\${${MIMO_SECRET}}\"; fi; "
      fi
      if [[ "$VOLC_ACTIVE" == "1" ]]; then
        VOLC_SECRET="$("$PYTHON" -m ibus_voice_ime.config get asr.volc.api_key_secret 2>/dev/null || echo VOLC_BIGMODEL_ASR_API_KEY)"
        BWS_INJECT+="if [[ -z \"\${VOICE_IME_VOLC_API_KEY:-}\" && -n \"\${${VOLC_SECRET}:-}\" ]]; then export VOICE_IME_VOLC_API_KEY=\"\${${VOLC_SECRET}}\"; fi; "
      fi
      if [[ "$SILICONFLOW_ACTIVE" == "1" ]]; then
        SILICONFLOW_SECRET="$("$PYTHON" -m ibus_voice_ime.config get asr.siliconflow.api_key_secret 2>/dev/null || echo SILICONFLOW_API_KEY)"
        BWS_INJECT+="if [[ -z \"\${VOICE_IME_SILICONFLOW_API_KEY:-}\" && -n \"\${${SILICONFLOW_SECRET}:-}\" ]]; then export VOICE_IME_SILICONFLOW_API_KEY=\"\${${SILICONFLOW_SECRET}}\"; fi; "
      fi
      exec bws run --project-id "$BWS_PROJECT_ID" --shell bash -- "
${BWS_INJECT}
exec $PY_CMD_Q
"
    fi
  fi
fi

exec "$PYTHON" "$ENGINE" "$@"
