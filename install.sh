#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENT_DIR="$HOME/.local/share/ibus/component"
COMPONENT_XML="$COMPONENT_DIR/voice-custom.xml"
ENGINE_NAME="voice-custom"
# 仅注册/写配置：批量安装可延后桌面激活，避免同一流程重复重启。
NO_RESTART="${VOICE_IME_NO_RESTART:-0}"

# 环境预检：只提示不阻断（FAIL/WARN 明细由 check-environment.sh 输出）。
if [[ -x "$ROOT_DIR/scripts/check-environment.sh" ]]; then
  echo "== 环境预检 =="
  "$ROOT_DIR/scripts/check-environment.sh" || \
    echo "（预检发现问题，详见上方 FAIL/WARN；安装继续，但请先解决 FAIL 项再使用）"
  echo
fi

chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py" "$ROOT_DIR/run-engine.sh" "$ROOT_DIR/scripts/voice-toggle.sh" "$ROOT_DIR/scripts/clipboard-paste.sh" "$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" "$ROOT_DIR/scripts/install-gnome-clipboard-paste-hotkey.sh" "$ROOT_DIR/scripts/uninstall-gnome-clipboard-paste-hotkey.sh" 2>/dev/null || chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  # 自动挑选可用解释器（conda 遮蔽 PATH 时仍能找到带 PyGObject 的系统 Python）。
  BASE_PY="$("$ROOT_DIR/scripts/python-select.sh" || true)"
  # venv 缺失时自动创建：--system-site-packages 让 dnf 的 PyGObject 继续可见。
  # 创建失败（只读目录/权限）不阻断安装，回退用系统解释器（下方 gi 检查兜底）。
  if [[ -n "${BASE_PY:-}" ]] && "$BASE_PY" -m venv "$ROOT_DIR/.venv" --system-site-packages >/dev/null 2>&1; then
    echo "已创建项目 venv：$ROOT_DIR/.venv（基于 $BASE_PY）"
    PYTHON="$ROOT_DIR/.venv/bin/python"
  else
    PYTHON="${BASE_PY:-}"
  fi
fi

# engine.py --xml 依赖 PyGObject（gi）；缺失时给可读提示而不是裸 traceback。
if ! "$PYTHON" -c 'import gi' >/dev/null 2>&1; then
  cat >&2 <<'EOF_GI'
错误：当前 Python（见上方路径）缺少 PyGObject（gi），无法生成 IBus 组件。
Fedora:        sudo dnf install python3-gobject
Debian/Ubuntu: sudo apt install python3-gobject
（若用项目 venv，请按 README 以 --system-site-packages 创建）
EOF_GI
  exit 1
fi

mkdir -p "$COMPONENT_DIR"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" "$ROOT_DIR/src/ibus_voice_ime/engine.py" --xml > "$COMPONENT_XML"

echo "已写入 IBus 组件：$COMPONENT_XML"

# Fedora/GNOME 的 IBus 默认只扫描 /usr/share/ibus/component；
# 这里把用户组件目录加入 IBus 扫描路径，并写入 environment.d 供下次登录继续生效。
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
ENV_DIR="$HOME/.config/environment.d"
ENV_FILE="$ENV_DIR/ibus-voice-ime.conf"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# 配置初始化/迁移、environment.d 写入和 daemon 重启前清理会话失效路径。
# 不清空有效救援值，也不触碰既有 RIME 自愈或渠道文件/manager 清理逻辑。
PATH_ENV_KEYS=(VOICE_IME_QWEN_ASR_PYTHON VOICE_IME_QWEN_ASR_MODEL_PATH VOICE_IME_QWEN_ASR_MODELS_DIR \
  VOICE_IME_MIMO_ASR_PYTHON VOICE_IME_MIMO_ASR_MODELS_DIR \
  VOICE_IME_MIMO_ASR_MODEL_PATH VOICE_IME_MIMO_ASR_TOKENIZER_PATH)
SANITIZE_UNSET="$("$ROOT_DIR/scripts/env-sanitize.sh" "${PATH_ENV_KEYS[@]}")"
if [[ -n "$SANITIZE_UNSET" ]]; then
  eval "$SANITIZE_UNSET"
  # 只 unset 本进程等于没修：environment.d 在登录时注入会话，而 systemd 用户
  # 管理器环境会被每个新终端与引擎服务立刻继承——旧安装留下的陈旧路径键会
  # 一直压过新安装（doctor 永久 FAIL）。两处都要清。
  STALE_SESSION_KEYS=()
  while IFS= read -r line; do
    [[ "$line" == unset\ * ]] && STALE_SESSION_KEYS+=("${line#unset }")
  done <<< "$SANITIZE_UNSET"
  if (( ${#STALE_SESSION_KEYS[@]} > 0 )); then
    systemctl --user unset-environment "${STALE_SESSION_KEYS[@]}" 2>/dev/null || true
  fi
fi

# ------------------------------------------------- 统一 JSON 配置 ----------
# a) 用户 config.json 不存在时生成骨架（已存在不动）。init 只读 HOME/写一个
#    文件，失败不应中断整个安装（set -e 下显式兜底）。
if ! "$PYTHON" -m ibus_voice_ime.config init; then
  echo "⚠ config init 失败（只读 HOME / 磁盘满？）——不影响安装；可稍后手动重跑同命令" >&2
fi

# b) 老用户迁移：environment.d 里的渠道类 VOICE_IME_ 行迁入 config.json，
#    env 文件只保留路径类行（迁移前后各打印一行摘要）。渠道唯一事实源
#    = config.json 的 asr.backend（默认 qwen3-asr，见 config/defaults.json）。
# 渠道类 env 键（与 scripts/switch-*-asr.sh 共用同一份正则）。
CHANNEL_ENV_RE='^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_QWEN_ASR_[A-Z0-9_]+|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_ASR_[A-Z0-9_]+|VOICE_IME_MIMO_CLOUD[A-Z0-9_]*|VOICE_IME_MIMO_API_KEY[A-Z_]*|VOICE_IME_MIMO_BASE_URL|VOICE_IME_VOLC_[A-Z0-9_]+|VOICE_IME_SILICONFLOW[A-Z0-9_]*|PYTORCH_CUDA_ALLOC_CONF)='
CHANNEL_LINES=""
MIGRATE_PATH_LINES=""
if [[ -f "$ENV_FILE" ]] && grep -qE "$CHANNEL_ENV_RE" "$ENV_FILE"; then
  CHANNEL_LINES="$(grep -E "$CHANNEL_ENV_RE" "$ENV_FILE" || true)"
  echo "迁移前：environment.d 含 $(printf '%s\n' "$CHANNEL_LINES" | grep -c .) 行渠道配置，将迁入 config.json"
  MIGRATE_TMP="$(mktemp)"
  MIGRATE_KEPT="$(mktemp)"
  printf '%s\n' "$CHANNEL_LINES" > "$MIGRATE_TMP"
  # stdout（=清理后的文件内容）重定向到文件，不污染安装控制台；
  # 逐行去向（已迁移/路径型保留/密钥类不迁/无对应键）由 stderr 报告。
  # 路径型渠道键（模型/解释器路径）迁移器拒迁：仅当目标真实存在时写回 env
  # 文件（保住 run-engine.sh 的 env 层陈旧路径自愈）；指向已消失路径的行必须
  # 剔除——它会在登录时进入会话环境、压过 config.json 与新安装，导致 doctor
  # 永远报旧克隆的 FAIL（使用者实测的「重装 N 次都修不好」死循环）。
  PATH_KEEP_RE='^(VOICE_IME_QWEN_ASR_MODEL_PATH|VOICE_IME_QWEN_ASR_PYTHON|VOICE_IME_MIMO_ASR_MODEL_PATH|VOICE_IME_MIMO_ASR_PYTHON)='
  if "$PYTHON" -m ibus_voice_ime.config migrate-env-file "$MIGRATE_TMP" > "$MIGRATE_KEPT"; then
    MIGRATE_PATH_LINES="$(grep -E "$PATH_KEEP_RE" "$MIGRATE_KEPT" || true)"
    DROPPED_COUNT="$(grep -vcE "$PATH_KEEP_RE" "$MIGRATE_KEPT" || true)"
    DROPPED_COUNT="${DROPPED_COUNT:-0}"
    CHANNEL_LINES=""   # 迁移成功：env 文件里的渠道行随后丢弃（路径型保留行除外）
    if [[ -n "$MIGRATE_PATH_LINES" ]]; then
      STALE_FILE_KEYS="$("$ROOT_DIR/scripts/env-sanitize.sh" --from-file "$MIGRATE_KEPT" \
        "${PATH_ENV_KEYS[@]}" 2>/dev/null | while IFS= read -r unset_line; do
          [[ "$unset_line" == unset\ * ]] && printf '%s\n' "${unset_line#unset }"
        done)"
      if [[ -n "$STALE_FILE_KEYS" ]]; then
        KEPT_PATH_LINES=""
        while IFS= read -r path_line; do
          [[ -n "$path_line" ]] || continue
          path_key="${path_line%%=*}"
          case $'\n'"$STALE_FILE_KEYS"$'\n' in
            *$'\n'"$path_key"$'\n'*) continue ;;
          esac
          KEPT_PATH_LINES+="$path_line"$'\n'
        done <<< "$MIGRATE_PATH_LINES"
        MIGRATE_PATH_LINES="${KEPT_PATH_LINES%$'\n'}"
        STALE_FILE_COUNT="$(printf '%s\n' "$STALE_FILE_KEYS" | grep -c . || true)"
        echo "已从 environment.d 移除 $STALE_FILE_COUNT 行指向已消失路径的路径型渠道行（旧克隆/旧 venv 残留），并从 systemd 用户环境同步清除"
        # 用户管理器环境同步清除：否则新开终端/引擎服务仍会继承旧路径。
        systemctl --user unset-environment $STALE_FILE_KEYS 2>/dev/null || true
      fi
    fi
    CURRENT_BACKEND="$("$PYTHON" -m ibus_voice_ime.config get asr.backend 2>/dev/null || echo qwen3-asr)"
    echo "迁移后：渠道配置已写入 config.json，当前后端：$CURRENT_BACKEND"
    if [[ "$DROPPED_COUNT" -gt 0 ]]; then
      echo "已从 environment.d 移除 $DROPPED_COUNT 行未迁移渠道行（密钥本体/无对应配置键——明细见上方「未迁移」报告）"
    fi
  else
    echo "⚠ 迁移失败：渠道行原样保留在 $ENV_FILE（不影响本次安装；可稍后重跑 ./install.sh）" >&2
  fi
  rm -f "$MIGRATE_TMP" "$MIGRATE_KEPT"
fi

# c) 重写 environment.d：只写路径块 + 用户既有非渠道行（如 VOICE_IME_WHISPER_*
#    诊断配置，不属于渠道类），不再写默认后端块（BACKEND_LINES 机制
#    退役——渠道状态在 config.json 里，重跑安装不会静默重置用户选择）。
mkdir -p "$ENV_DIR"
PATH_ENV_RE='^(IBUS_COMPONENT_PATH|VOICE_IME_RIME_LIBRARY|VOICE_IME_RIME_SHARED_DATA_DIR|VOICE_IME_RIME_STAGING_DIR|VOICE_IME_RIME_USER_DATA_DIR)='
KEEP_LINES=""
if [[ -f "$ENV_FILE" ]]; then
  # 保留非渠道、非路径块的既有行（注释/诊断键/用户自加行）。
  KEEP_LINES="$(grep -vE "$CHANNEL_ENV_RE|$PATH_ENV_RE" "$ENV_FILE" || true)"
fi
cat > "$ENV_FILE.new" <<EOF_ENV
IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE
VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1
VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data
VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build
VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user
EOF_ENV
if [[ -n "$KEEP_LINES" ]]; then
  printf '%s\n' "$KEEP_LINES" >> "$ENV_FILE.new"
fi
if [[ -n "$CHANNEL_LINES" ]]; then
  # 迁移失败：全部渠道行原样保留。
  printf '%s\n' "$CHANNEL_LINES" >> "$ENV_FILE.new"
elif [[ -n "$MIGRATE_PATH_LINES" ]]; then
  # 迁移成功：只写回路径型保留行（拒迁，维持 env 层自愈）。
  printf '%s\n' "$MIGRATE_PATH_LINES" >> "$ENV_FILE.new"
fi
mv "$ENV_FILE.new" "$ENV_FILE"
chmod 600 "$ENV_FILE" 2>/dev/null || true

# 会话级环境（当前登录立即可用；下次登录由 environment.d 接管）。
# 渠道/行为默认值不再注入会话环境——由引擎进程内 defaults.json 提供。
if ! systemctl --user set-environment \
  "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
  "VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1" \
  "VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data" \
  "VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build" \
  "VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user" 2>/dev/null; then
  echo "提示：systemctl --user 不可用（SSH 无用户会话/非 systemd？），配置将在下次登录时由 environment.d 生效。"
fi
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" ibus write-cache >/dev/null 2>&1 || true

# 开机自愈：systemd 拉起的 ibus-daemon（--cache auto）被系统级注册缓存短路，
# 用户组件永不被扫——drop-in 改为 --cache refresh 强制重扫（无 unit 时跳过）。
if [[ -x "$ROOT_DIR/scripts/ensure-ibus-cache-refresh.sh" ]]; then
  "$ROOT_DIR/scripts/ensure-ibus-cache-refresh.sh" || true
fi

# Add to GNOME input sources without removing existing Rime.
if [[ "$NO_RESTART" != 1 ]] && command -v gsettings >/dev/null 2>&1; then
  python3 - <<'PY'
import ast
import subprocess
entry = ('ibus', 'voice-custom')
try:
    raw = subprocess.check_output([
        'gsettings', 'get', 'org.gnome.desktop.input-sources', 'sources'
    ], text=True).strip()
    sources = ast.literal_eval(raw)
except Exception:
    sources = []
if entry not in sources:
    sources.append(entry)
    try:
        subprocess.run([
            'gsettings', 'set', 'org.gnome.desktop.input-sources', 'sources', repr(sources)
        ], check=True)
        print('已加入 GNOME 输入源：', sources)
    except Exception as exc:
        print(f'警告：无法写入 GNOME 输入源（{exc}）；请手动把 {entry} 加入输入源。')
else:
    print('GNOME 输入源已存在：', entry)
PY
fi

if [[ "$NO_RESTART" != 1 ]]; then
"$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" || true
# Ctrl+Alt+P uses a tiny external helper only to read the desktop clipboard and
# show the visible "正在粘贴" notification.  The helper passes prepared text back
# to the engine, which commits it through the same _commit_voice_result path as
# voice recognition results.
"$ROOT_DIR/scripts/install-gnome-clipboard-paste-hotkey.sh" || true
fi

# 词库就绪性提示（只警告、不下载——install.sh 定位是轻量的每次注册/激活；
# 真正的资产初始化由一次性脚本 ./init.sh 完成）。
if [[ ! -f "$ROOT_DIR/vendor/rime/share/rime-data/cn_dicts/base.dict.yaml" ]]; then
  echo "⚠ rime-ice 词库未部署：键盘将回退 luna_pinyin_simp（可打字但词库小）。" >&2
  echo "  一次性初始化（默认部署词库/语音模型/降噪）：./init.sh" >&2
fi

# Let ibus-daemon reload component list.  This briefly restarts input methods.
# 后端/渠道配置以 config.json 为单一事实源（引擎进程内读取）；这里只透传
# 路径类变量给重启后的 daemon。
if [[ "$NO_RESTART" != 1 ]] && { command -v ibus >/dev/null 2>&1 || command -v ibus-daemon >/dev/null 2>&1; }; then
  env \
    IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
    LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
    VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
    VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
    VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
    "$ROOT_DIR/scripts/ibus-restart.sh" || true
fi
if [[ "$NO_RESTART" == 1 ]]; then
  echo '仅注册/写配置：已按 VOICE_IME_NO_RESTART=1 跳过 IBus 重启与 GNOME 激活。'
fi

echo
if [[ -n "${VOICE_IME_INSTALL_FROM_INIT:-}" ]]; then
  HINT_LINE="（由 init.sh 流程触发，语音安装与验收由后续步骤自动完成。）"
else
  HINT_LINE="首次安装请运行 ./init.sh；语音完整验收：./scripts/setup-qwen-asr.sh --verify-only。"
fi
cat <<EOF
输入法注册完成（注册成功不代表本地语音已通过推理验收）。
$HINT_LINE
切换方式：GNOME 顶栏输入法菜单，或 Super+Space，选择“自定义语音输入法”。
键盘输入示例：nihao + Space -> 你好
语音/原文/粘贴热键与后端以生效 config.json 为准；一键入口 setup.sh 完成后会打印「开始使用」卡片。
EOF

# init.sh 流程内模型尚未下载属预期（下一步语音步骤会装），只在独立运行时提示。
if [[ ! -d "$ROOT_DIR/vendor/models/qwen3-asr" && -z "${VOICE_IME_INSTALL_FROM_INIT:-}" ]]; then
  cat <<'EOF'
⚠ 尚未下载本地 ASR 模型（vendor/models/qwen3-asr/ 不存在）。语音输入前请二选一：
  1. 本地识别（需 NVIDIA GPU）：./scripts/setup-qwen-asr.sh
  2. 云端识别（无需 GPU，需 API Key）：./scripts/switch-mimo-cloud-asr.sh cn
键盘输入（Rime 拼音）不受影响，可直接使用。
EOF
fi

GPU_STATE="" GPU_REASON="" GPU_ACTION=""
if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
fi
if [[ "$GPU_STATE" != "ok" ]]; then
  cat <<EOF
⚠ 本机 NVIDIA GPU 不可用（${GPU_REASON:-未检测到}）：默认语音后端（本地 Qwen3-ASR）将无法运行。
键盘输入（Rime 拼音）不受影响。语音三选一：
  1. 修复 GPU 环境后安装本地识别：$GPU_ACTION
  2. 云端识别（无需 GPU/大陆直连，注册 cloud.siliconflow.cn；SenseVoiceSmall 官方标注免费，Qwen3-ASR 按秒计费）：VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh
  3. 云端识别（需 API Key）：./scripts/switch-mimo-cloud-asr.sh cn
EOF
fi
