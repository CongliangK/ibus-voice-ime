#!/usr/bin/env bash
# 项目健康检查器（doctor）：像医生一样逐项体检，并能自动治疗。
#
# 用法：
#   ./scripts/doctor.sh         # 只检查 + 报告（不修复、不下载、不改任何文件）
#   ./scripts/doctor.sh fix     # 检查并自动修复：补词库/模型下载、修可执行位、
#                               # 重注册热键等；修复后自动复查
#
# 退出码：0 = 无 FAIL（或全部已修复）；1 = 仍有未解决问题；2 = 用法错误。
#
# 检查范围：环境依赖（复用 check-environment.sh）、IBus 注册、Rime 词库完整性
# （重点：新 clone 只带 git 跟踪的 schema，cn_dicts/编译产物在 .gitignore 里，
# 缺失会进入“方案在、词库无”的半残态——Rime 零候选，只剩记忆层出词）、
# 语音后端就绪度、桌面热键指向、脚本可执行位。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
MODE="check"
case "${1:-}" in
  "") MODE="check" ;;
  fix) MODE="fix" ;;
  -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
  *) echo "用法：$0 [fix]    （无参数 = 只检查；fix = 检查并修复）" >&2; exit 2 ;;
esac

FAILS=0
WARNINGS=0
FIXED=0
FAILED_ITEMS=()   # 待修复条目 id，fix 模式逐个治疗

ok()   { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '  [FAIL] %s\n' "$1"; FAILS=$((FAILS + 1)); }
fixed(){ printf '  [FIXED] %s\n' "$1"; FIXED=$((FIXED + 1)); }
header() { printf '\n== %s ==\n' "$1"; }

RIME_DATA="$ROOT_DIR/vendor/rime/share/rime-data"
RIME_BUILD="$ROOT_DIR/vendor/rime/build"
RIME_USER="${VOICE_IME_RIME_USER_DATA_DIR:-$HOME/.local/share/ibus-voice-ime/rime-user}"
ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
COMPONENT_XML="$HOME/.local/share/ibus/component/voice-custom.xml"

# rime-ice 资产三要素：方案文件（git 跟踪）、词库源（gitignore）、编译产物（gitignore）。
rime_ice_sources_present() {
  [[ -f "$RIME_DATA/rime_ice.schema.yaml" && -f "$RIME_DATA/cn_dicts/base.dict.yaml" ]]
}
rime_ice_build_present() {
  [[ -f "$RIME_BUILD/rime_ice.table.bin" ]] || [[ -f "$RIME_USER/build/rime_ice.table.bin" ]]
}

current_asr_backend() {
  # 渠道唯一事实源 = config.json 的 asr.backend（env > json > defaults）；
  # environment.d 的旧渠道行仅作兜底显示。
  local v
  v="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m ibus_voice_ime.config get asr.backend 2>/dev/null || true)"
  [[ -n "$v" ]] && { echo "$v"; return; }
  if [[ -r "$ENV_FILE" ]]; then
    v="$(grep -m1 '^VOICE_IME_ASR_BACKEND=' "$ENV_FILE" | cut -d= -f2- || true)"
    [[ -n "$v" ]] && { echo "$v"; return; }
  fi
  echo "qwen3-asr"
}

# ---------------------------------------------------------------- 环境依赖 --
check_env_dependencies() {
  header "环境依赖（check-environment.sh）"
  local out rc
  out="$(mktemp)"
  if [[ -x "$ROOT_DIR/scripts/check-environment.sh" ]]; then
    "$ROOT_DIR/scripts/check-environment.sh" >"$out" 2>&1 || true
    cat "$out"
    # 汇总行形如 “FAIL: 0  WARN: 2”
    if grep -qE 'FAIL: [1-9]' "$out"; then
      fail "环境依赖存在 FAIL 项（见上方输出，需人工处理）"
      FAILED_ITEMS+=(env-deps)
    fi
  else
    warn "check-environment.sh 不存在或不可执行"
  fi
  rm -f "$out"
}

# ------------------------------------------------------------ 统一配置 --
check_unified_config() {
  header "统一配置（config.json）"
  local cfg_json="${VOICE_IME_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/ibus-voice-ime/config.json}"
  if [[ -f "$cfg_json" ]]; then
    ok "用户配置存在：$cfg_json"
    local vout
    if vout="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m ibus_voice_ime.config validate 2>&1)"; then
      ok "config validate 通过"
    else
      fail "用户 config.json 校验失败（修复后重跑 ./install.sh 或手动编辑）："
      while IFS= read -r line; do
        [[ -n "$line" ]] && printf '         %s\n' "$line"
      done <<< "$vout"
      FAILED_ITEMS+=(config-validate)
    fi
  else
    ok "未创建用户配置（全默认值生效；可运行 ./install.sh 生成骨架）"
  fi
  local backend
  backend="$(current_asr_backend)"
  ok "生效 asr.backend：$backend"
  # enabled 残留一致性：install.sh 迁移可能留下 asr.<x>.enabled=true 的旧
  # 渠道标志；voice.py 渠道链先命中先赢，残留会压过 asr.backend 的选择
  # （switch 脚本已改为写全互斥五叶，任一 switch 运行即可修复）。
  local mismatch
  mismatch="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" - <<'PY' 2>/dev/null
from ibus_voice_ime import config
from ibus_voice_ime.asr import (
    mimo_asr_runtime,
    mimo_cloud_asr,
    qwen_asr_runtime,
    siliconflow_asr,
    volc_bigmodel_asr,
)
# voice.py 的渠道判定顺序（先命中先赢）
chain = [
    ("qwen3-asr", qwen_asr_runtime),
    ("mimo-asr", mimo_asr_runtime),
    ("mimo-cloud-asr", mimo_cloud_asr),
    ("volc-bigmodel-asr", volc_bigmodel_asr),
    ("siliconflow-asr", siliconflow_asr),
]
winner = next((name for name, mod in chain if mod.selected()), None)
backend = str(config.get("asr.backend", default="")).strip().lower()
alias = {}
for names, canon in (
    (("qwen", "qwen3", "qwen3-asr", "qwen-asr"), "qwen3-asr"),
    (("mimo", "mimo-asr", "mimo-v2.5-asr", "mimo-v25-asr"), "mimo-asr"),
    (("mimo-cloud", "mimo-cloud-asr", "mimo-api", "mimo-api-asr", "mimo-tokenplan-asr"), "mimo-cloud-asr"),
    (("volc", "volc-asr", "volc-engine-asr", "volc-bigmodel", "volc-bigmodel-asr", "doubao-asr", "doubao-bigmodel-asr"), "volc-bigmodel-asr"),
    (("siliconflow", "siliconflow-asr", "sf-asr"), "siliconflow-asr"),
):
    for name in names:
        alias[name] = canon
expected = alias.get(backend)
if winner and expected and winner != expected:
    print(f"{winner}|{backend}")
PY
)"
  if [[ -n "$mismatch" ]]; then
    warn "enabled 标志使 voice.py 实际命中 ${mismatch%%|*}，与 asr.backend=${mismatch##*|} 不一致（迁移残留？运行任一 scripts/switch-*-asr.sh 写全互斥五叶修复）"
  else
    ok "渠道 enabled 标志与 asr.backend 一致"
  fi
  if [[ -r "$ENV_FILE" ]] && grep -qE '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD|VOICE_IME_VOLC_|VOICE_IME_SILICONFLOW)' "$ENV_FILE"; then
    warn "environment.d 仍含渠道配置行（会压过 config.json；重跑 ./install.sh 可迁移到 config.json）"
  fi
}

# ------------------------------------------------------------- IBus 注册 --
check_ibus_registration() {
  header "IBus 注册"
  if [[ ! -f "$COMPONENT_XML" ]]; then
    fail "组件 XML 缺失：$COMPONENT_XML（未运行 ./install.sh？）"
    FAILED_ITEMS+=(component-xml)
    return
  fi
  if grep -q "<exec>$ROOT_DIR/run-engine.sh" "$COMPONENT_XML"; then
    ok "组件 exec 指向本仓库"
  else
    fail "组件 exec 未指向本仓库（指向了旧副本？）"
    FAILED_ITEMS+=(component-xml)
  fi
  if [[ -f "$ENV_FILE" ]]; then
    if grep -q "VOICE_IME_RIME_LIBRARY=$ROOT_DIR/" "$ENV_FILE"; then
      ok "environment.d 路径指向本仓库"
    else
      fail "environment.d（$ENV_FILE）路径未指向本仓库"
      FAILED_ITEMS+=(env-file)
    fi
  else
    fail "environment.d 配置缺失：$ENV_FILE"
    FAILED_ITEMS+=(env-file)
  fi
  if pgrep -f "engine.py --ibus" >/dev/null 2>&1; then
    if pgrep -f "$ROOT_DIR/src/ibus_voice_ime/engine.py --ibus" >/dev/null 2>&1; then
      ok "引擎进程运行自本仓库"
    else
      warn "引擎在运行，但不是本仓库的实例（重启后生效：./scripts/ibus-restart.sh）"
    fi
    check_stale_engine_env
  else
    warn "引擎进程未运行（可能未启用输入法；重启 IBus 后复查）"
  fi
}

check_stale_engine_env() {
  # 2026-08-28 打字事故根因：登录会话缓存了仓库迁移前删除的旧绝对路径，
  # ibus-daemon 原样传给引擎，librime 加载失败 → 打字瘫痪（语音不受影响）。
  # run-engine.sh 已有自愈，这里检查"活引擎进程"的环境里是否仍有指向
  # 不存在路径的 VOICE_IME_* 变量（自愈失效/旧引擎未重启时报警）。
  local pid dead=0
  for pid in $(pgrep -f "$ROOT_DIR/src/ibus_voice_ime/engine.py --ibus"); do
    [[ -r "/proc/$pid/environ" ]] || continue
    while IFS= read -r line; do
      case "$line" in
        VOICE_IME_*=/*ibus-voice-ime*)
          local var="${line%%=*}" val="${line#*=}"
          if [[ -n "$val" && ! -e "$val" ]]; then
            fail "引擎环境 $var 指向不存在的路径：$val"
            dead=$((dead + 1))
          fi
          ;;
      esac
    done < <(tr '\0' '\n' < "/proc/$pid/environ")
  done
  if [[ $dead -eq 0 ]]; then
    ok "引擎进程环境无失效路径（陈旧会话环境自愈正常）"
  else
    FAILED_ITEMS+=(stale-env)
  fi
  return 0
}

# ----------------------------------------------------------- Rime 词库态 --
check_rime_assets() {
  header "Rime 键盘链路（本次重点：半残态检测）"
  if [[ ! -f "$ROOT_DIR/vendor/rime/lib/librime.so.1" ]]; then
    fail "内置 librime 缺失（先运行 ./scripts/vendor-rime-runtime.sh）"
    FAILED_ITEMS+=(rime-runtime)
    return
  fi
  if [[ ! -f "$ROOT_DIR/vendor/rime/lib/rime-plugins/librime-lua.so" ]]; then
    warn "librime-lua 插件缺失（rime-ice 的 lua 扩展不可用；setup-rime-ice.sh 可自动补齐）"
  fi

  if rime_ice_sources_present; then
    ok "rime-ice 方案 + 词库源（cn_dicts）就绪"
  elif [[ -f "$RIME_DATA/rime_ice.schema.yaml" ]]; then
    fail "半残态：rime_ice.schema.yaml 在，但 cn_dicts 词库缺失（gitignore 资产未部署）"
    fail "  引擎将回退 luna_pinyin_simp 或出零候选。修复：./init.sh 或 ./scripts/setup-rime-ice.sh"
    FAILED_ITEMS+=(rime-ice-deploy)
  else
    warn "rime-ice 方案文件不存在（引擎将使用内置 luna 方案，可正常打字但词库较小）"
  fi

  if rime_ice_sources_present; then
    if rime_ice_build_present; then
      ok "rime-ice 编译产物（table.bin）就绪"
    else
      warn "词库源在但编译产物缺失（首次按键前会触发编译，约 1-3 分钟）"
      FAILED_ITEMS+=(rime-ice-build)
    fi
  fi

  # 功能冒烟：隔离的临时 user 目录 + 本仓库 staging 产物，验证真实出词。
  # 不碰真实 rime-user（避免与运行中引擎抢 userdb 锁）。
  if rime_ice_sources_present && rime_ice_build_present; then
    local tmp rc
    tmp="$(mktemp -d)"
    rc=0
    env \
      LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib" \
      VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
      VOICE_IME_RIME_SHARED_DATA_DIR="$RIME_DATA" \
      VOICE_IME_RIME_STAGING_DIR="$RIME_BUILD" \
      VOICE_IME_RIME_USER_DATA_DIR="$tmp" \
      VOICE_IME_RIME_SCHEMA=rime_ice \
      PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
      python3 - >/dev/null 2>&1 <<'PY' || rc=1
import os
import sys

from ibus_voice_ime.rime.rime_backend import RimeSession

session = RimeSession()
try:
    ok = bool(session.query_candidates("nihao", 3))
finally:
    session.close()
sys.stdout.write("OK\n" if ok else "ZERO\n")
sys.stdout.flush()
# os._exit 跳过解释器 teardown：librime/glog 终局化与 Python 收尾并发时
# 偶发段错误，会污染诊断结论。
os._exit(0 if ok else 1)
PY
    rm -rf "$tmp"
    if [[ $rc -eq 0 ]]; then
      ok "rime_ice 功能冒烟通过（'nihao' 可出候选）"
    else
      fail "rime_ice 功能冒烟失败（词库在但不出词——建议重编译：./scripts/setup-rime-ice.sh）"
      FAILED_ITEMS+=(rime-ice-deploy)
    fi
  fi
}

# ------------------------------------------------------------ 语音后端 --
check_asr_backend() {
  header "语音后端（当前：$(current_asr_backend)）"
  local backend
  backend="$(current_asr_backend)"
  case "$backend" in
    qwen3-asr)
      local have_venv=1 have_model=1
      if [[ -x "$ROOT_DIR/.venv-qwen-asr/bin/python" ]]; then
        ok "Qwen sidecar venv 就绪"
      else
        fail "Qwen sidecar venv 缺失（$ROOT_DIR/.venv-qwen-asr）"
        have_venv=0
      fi
      local model_dir
      # 模型路径优先读 config.json（asr.qwen3.model_path），environment.d 旧行兜底。
      model_dir="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m ibus_voice_ime.config get asr.qwen3.model_path 2>/dev/null || true)"
      if [[ -z "$model_dir" || ! -d "$model_dir" ]]; then
        model_dir="$(grep -m1 '^VOICE_IME_QWEN_ASR_MODEL_PATH=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
      fi
      [[ -n "$model_dir" ]] || model_dir="$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B"
      if [[ -d "$model_dir" ]]; then
        ok "Qwen3-ASR 模型就绪（$model_dir）"
      else
        fail "Qwen3-ASR 模型缺失（$model_dir）"
        have_model=0
      fi
      if [[ -x "$ROOT_DIR/.venv-qwen-asr/bin/python" ]]; then
        # venv 存在不等于能用：CPU-only torch 轮子 / venv 腐化都会让 sidecar 秒退，
        # 只查可执行位会在这里给出误导性的全绿。
        if "$ROOT_DIR/.venv-qwen-asr/bin/python" -c 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)' >/dev/null 2>&1; then
          ok "sidecar venv 内 torch.cuda 可用"
        else
          fail "sidecar venv 的 torch 用不了 CUDA（CPU-only 轮子 / venv 损坏 / 驱动异常）。修复：rm -rf .venv-qwen-asr && ./scripts/setup-qwen-asr.sh"
          have_venv=0
        fi
      fi
      GPU_STATE="" GPU_REASON="" GPU_ACTION="" GPU_BRIEF=""
      if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
        eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
      fi
      case "$GPU_STATE" in
        ok) ok "NVIDIA GPU 可用（${GPU_BRIEF:-型号未知}）" ;;
        partial)
          warn "检测到 NVIDIA 显卡但 CUDA 驱动不可用：${GPU_REASON:-未知}"
          [[ -n "$GPU_ACTION" ]] && printf '         %s\n' "$GPU_ACTION"
          ;;
        *) warn "无 NVIDIA GPU：本地 Qwen 后端无法运行（切换云端：scripts/switch-mimo-cloud-asr.sh / switch-volc-bigmodel-asr.sh）" ;;
      esac
      if [[ $have_venv -eq 0 || $have_model -eq 0 ]]; then
        FAILED_ITEMS+=(qwen-setup)
      fi
      ;;
    mimo-cloud*|volc*|siliconflow*|sf-asr*)
      ok "云端后端（密钥由 BWS 运行时注入，不做落盘检查）"
      ;;
    *)
      ok "后端 $backend：不做专项检查（自定义命令/vosk/faster-whisper 请自行确认依赖）"
      ;;
  esac

  if [[ ! -f "$ROOT_DIR/vendor/models/rnnoise/bd.rnnn" ]]; then
    warn "RNNoise 模型缺失（降噪自动降级 notch 档；补齐：./scripts/fetch-rnnoise-model.sh）"
    FAILED_ITEMS+=(rnnoise-model)
  else
    ok "RNNoise 降噪模型就绪"
  fi
}

# ------------------------------------------------------ LLM 云端后处理 --
check_llm_cloud_config() {
  header "LLM 云端后处理（OpenAI 兼容）"
  local cfg="${VOICE_IME_LLM_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/ibus-voice-ime/llm.json}"
  if [[ ! -f "$cfg" ]]; then
    ok "未配置（默认关闭，走规则清理）。启用：./scripts/setup-llm-cloud.sh"
    return
  fi
  local perms
  perms="$(stat -c %a "$cfg" 2>/dev/null || echo '?')"
  if [[ "$perms" != "600" ]]; then
    warn "配置文件权限为 $perms（建议 chmod 600，内含 API Key）：$cfg"
  fi
  # 只做本地 JSON 解析/字段校验，不联网、不调用模型
  local out
  if out="$(PYTHONPATH="$ROOT_DIR/src" python3 -c '
from ibus_voice_ime.text import llm_cloud_config
cfg, problems = llm_cloud_config.load_report()
if cfg is None:
    print("INVALID")
    for p in problems:
        print("  - " + p)
elif cfg.enabled:
    print(f"OK {cfg.model} @ {cfg.base_url}（timeout={cfg.timeout}s）")
else:
    print("OFF（enabled=false，LLM 润色关闭）")
' 2>&1)"; then
    case "$out" in
      OK*)  ok "${out#OK }" ;;
      OFF*) ok "已配置但 enabled=false（LLM 润色关闭）" ;;
      INVALID*)
        warn "配置文件存在但未通过校验（LLM 已自动停用，语音输入不受影响）："
        printf '%s\n' "${out#INVALID$'\n'}" | sed 's/^/         /'
        warn "修复：编辑 $cfg 或重跑 ./scripts/setup-llm-cloud.sh"
        ;;
      *) warn "配置校验输出异常：$out" ;;
    esac
  else
    warn "无法运行配置校验（python3/PYTHONPATH 异常）：$out"
  fi
}

# ------------------------------------------------------------ 桌面集成 --
check_desktop_integration() {
  header "桌面集成（GNOME）"
  command -v gsettings >/dev/null 2>&1 || { warn "无 gsettings（非 GNOME？跳过本节）"; return; }
  local sources
  sources="$(gsettings get org.gnome.desktop.input-sources sources 2>/dev/null || echo '[]')"
  if [[ "$sources" == *"voice-custom"* ]]; then
    ok "GNOME 输入源含 voice-custom"
  else
    fail "GNOME 输入源未注册 voice-custom"
    FAILED_ITEMS+=(hotkeys)
  fi
  local paths raw path cmd cmd_file dangling=0 ours=0
  raw="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null || echo '[]')"
  paths="$(printf '%s' "$raw" | tr -d "[]'," | tr ' ' '\n' | grep -v '^$')"
  for path in $paths; do
    cmd="$(dconf read "${path}command" 2>/dev/null || true)"
    cmd="${cmd//\'}"
    case "$cmd" in
      *ibus-voice-ime*)
        # 命令可带参数（如 "voice-toggle.sh raw"），存在性只检查第一个词。
        cmd_file="${cmd%% *}"
        if [[ -f "$cmd_file" ]]; then
          if [[ "$cmd_file" == "$ROOT_DIR/"* ]]; then
            ours=$((ours + 1))
          else
            warn "快捷键指向其他副本：$cmd"
            FAILED_ITEMS+=(hotkeys)
          fi
        else
          fail "快捷键命令是断链（文件不存在）：$cmd"
          dangling=$((dangling + 1))
          FAILED_ITEMS+=(hotkeys)
        fi
        ;;
    esac
  done
  [[ $ours -ge 2 ]] && ok "语音/粘贴热键已注册并指向本仓库（共 $ours 个）"
  [[ $dangling -gt 0 ]] && fail "共 $dangling 个快捷键断链"
  if [[ $ours -eq 0 && $dangling -eq 0 ]]; then
    warn "未检测到指向本仓库的语音/粘贴热键（未运行 ./install.sh？）"
    FAILED_ITEMS+=(hotkeys)
  fi
}

# ------------------------------------------------------------ 可执行位 --
check_exec_bits() {
  header "脚本可执行位"
  local missing=() f
  for f in "$ROOT_DIR"/*.sh "$ROOT_DIR"/scripts/*.sh; do
    [[ -f "$f" && ! -x "$f" ]] && missing+=("$f")
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    ok "全部 shell 脚本可执行"
  else
    fail "以下脚本缺可执行位："
    for f in "${missing[@]}"; do printf '         %s\n' "${f#"$ROOT_DIR"/}"; done
    FAILED_ITEMS+=(exec-bits)
  fi
  # 仓库层：git 索引里以 100644 跟踪的 .sh（新 clone 会丢失可执行位的根因）
  if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    local tracked644
    tracked644="$(git -C "$ROOT_DIR" ls-files -s | awk '$1 == "100644" && $4 ~ /\.sh$/ {print $4}')"
    if [[ -n "$tracked644" ]]; then
      warn "git 索引中以下 .sh 以 100644 跟踪（clone 后无执行位，建议提交 mode 修复）："
      printf '         %s\n' $tracked644
    fi
  fi
}

# ------------------------------------------------------------------ 修复 --
do_fix() {
  header "修复（doctor fix）"
  local item restarted=0
  for item in "${FAILED_ITEMS[@]}"; do
    case "$item" in
      rime-ice-deploy|rime-ice-build)
        echo "==> 部署/重编译 rime-ice 词库（下载约 50MB + 编译 1-3 分钟；一键全量初始化可用 ./init.sh）"
        if "$ROOT_DIR/scripts/setup-rime-ice.sh" ${DOCTOR_PROXY:+--proxy "$DOCTOR_PROXY"}; then
          fixed "rime-ice 词库部署完成"
          restarted=1
        else
          fail "setup-rime-ice.sh 失败（网络？可加代理重试：DOCTOR_PROXY=http://127.0.0.1:7890 $0 fix）"
        fi
        ;;
      rnnoise-model)
        echo "==> 下载 RNNoise 模型（小文件）"
        if "$ROOT_DIR/scripts/fetch-rnnoise-model.sh" ${DOCTOR_PROXY:+--proxy "$DOCTOR_PROXY"}; then
          fixed "RNNoise 模型已补齐"
        else
          fail "fetch-rnnoise-model.sh 失败（网络？）"
        fi
        ;;
      exec-bits)
        local f
        for f in "$ROOT_DIR"/*.sh "$ROOT_DIR"/scripts/*.sh; do
          [[ -f "$f" && ! -x "$f" ]] && chmod +x "$f"
        done
        fixed "已为全部 shell 脚本补可执行位"
        ;;
      component-xml|env-file|hotkeys)
        echo "==> 重跑 ./install.sh（重注册组件/环境/热键；默认自动补全 rime-ice）"
        if "$ROOT_DIR/install.sh" >/dev/null 2>&1; then
          fixed "install.sh 重跑成功，注册已刷新"
          restarted=1
        else
          fail "install.sh 重跑失败，请手动排查"
        fi
        ;;
      qwen-setup)
        echo "==> 安装本地 Qwen3-ASR 后端（下载模型 + venv，约 6GB，耗时较长；一键全量初始化可用 ./init.sh）"
        if "$ROOT_DIR/scripts/setup-qwen-asr.sh" ${DOCTOR_PROXY:+--proxy "$DOCTOR_PROXY"}; then
          fixed "Qwen3-ASR 后端安装完成"
        else
          fail "setup-qwen-asr.sh 失败（网络/磁盘/Python 版本？）"
        fi
        ;;
      stale-env)
        echo "==> 引擎环境含失效路径，重启引擎（run-engine.sh 会自愈陈旧继承值）"
        if "$ROOT_DIR/scripts/ibus-restart.sh" >/dev/null 2>&1; then
          fixed "引擎已重启，失效路径已自愈"
        else
          fail "ibus-restart.sh 失败，请手动重启或重新登录"
        fi
        ;;
      env-deps)
        echo "==> 环境依赖缺失需人工处理（见上方 check-environment 的 FAIL 项）"
        ;;
    esac
  done

  if [[ $restarted -eq 1 ]]; then
    echo "==> 已变更词库/注册，重启输入法使引擎加载新状态"
    "$ROOT_DIR/scripts/ibus-restart.sh" >/dev/null 2>&1 || \
      echo "  提示：ibus-restart 不可用，请重新登录或手动 ibus restart"
  fi
}

# ------------------------------------------------------------------- 主流程 --
check_env_dependencies
check_unified_config
check_ibus_registration
check_rime_assets
check_asr_backend
check_llm_cloud_config
check_desktop_integration
check_exec_bits

if [[ "$MODE" == "fix" && ${#FAILED_ITEMS[@]} -gt 0 ]]; then
  do_fix
  printf '\n== 修复后复查 ==\n'
  FAILS=0; WARNINGS=0; FAILED_ITEMS=()
  check_unified_config
  check_ibus_registration
  check_rime_assets
  check_asr_backend
  check_llm_cloud_config
  check_desktop_integration
  check_exec_bits
fi

printf '\n== 汇总 ==\n'
printf 'FAIL: %s  WARN: %s' "$FAILS" "$WARNINGS"
[[ "$MODE" == "fix" ]] && printf '  FIXED: %s' "$FIXED"
printf '\n'
if [[ $FAILS -gt 0 ]]; then
  echo "仍有问题未解决；按上方各 FAIL 行的提示处理，或提 issue 附本输出。"
  exit 1
fi
echo "检查通过（WARN 为可选/降级提示）。日志：~/.local/share/ibus-voice-ime/*.log"
exit 0
