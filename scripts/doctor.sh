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
# 语音后端就绪度（只检查 config.json 生效的 asr.backend；qwen3-asr 在本机无
# 可用 NVIDIA GPU 时如实 FAIL 并给出换云端/仅键盘指引，不跑注定失败的 CUDA
# 运行时验收）、桌面热键指向、脚本可执行位。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$("$ROOT_DIR/scripts/python-select.sh" || command -v python3)"
MODE="check"
case "${1:-}" in
  "") MODE="check" ;;
  fix) MODE="fix" ;;
  -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
  *) echo "用法：$0 [fix]    （无参数 = 只检查；fix = 检查并修复）" >&2; exit 2 ;;
esac

FAILS=0
WARNINGS=0
WARN_TITLES=() WARN_IMPACTS=() WARN_GUIDANCE=()
FAIL_MSGS=()     # 汇总段复述每条 FAIL 的原因与修复方案，用户不必回翻上方明细
FIXED=0
FIX_FAILURES=0
FAILED_ITEMS=()   # 待修复条目 id，fix 模式逐个治疗

ok()   { printf '  [OK]   %s\n' "$1"; }
register_warn() {
  WARN_TITLES+=("${1//$'\n'/ }"); WARN_IMPACTS+=("${2//$'\n'/ }"); WARN_GUIDANCE+=("${3//$'\n'/ }")
  WARNINGS=${#WARN_TITLES[@]}
}
warn() {
  # 通用同标题去重：环境预检导入条目与本地检查标题全等（剥离来源前缀）
  # 时只保留一条，避免同一问题双报；不用子串匹配，不会误吞不同标题。
  local title="$1" existing
  for existing in "${WARN_TITLES[@]}"; do
    [[ "${existing#\[*\] }" == "$title" ]] && return 0
  done
  register_warn "$1" "$2" "$3"
  printf '  [WARN] %s —— %s；%s\n' "${WARN_TITLES[WARNINGS-1]}" "${WARN_IMPACTS[WARNINGS-1]}" "${WARN_GUIDANCE[WARNINGS-1]}"
}
fail() {
  FAIL_MSGS+=("${1//$'\n'/ }")
  FAILS=${#FAIL_MSGS[@]}
  printf '  [FAIL] %s\n' "$1"
}
# 信息行（不计数）：既有事实的说明，如验收 receipt 匹配时的降噪提示。
info() { printf '  [INFO]  %s\n' "$1"; }
# 模式感知跳过说明（不计数）：本机条件使某项检查失去意义时说明原因。
skip_note() { printf '  [SKIP] %s\n' "$1"; }
fixed(){ printf '  [FIXED] %s\n' "$1"; FIXED=$((FIXED + 1)); }
header() { printf '\n== %s ==\n' "$1"; }

RIME_DATA="$ROOT_DIR/vendor/rime/share/rime-data"
RIME_BUILD="$ROOT_DIR/vendor/rime/build"
RIME_USER="${VOICE_IME_RIME_USER_DATA_DIR:-$HOME/.local/share/ibus-voice-ime/rime-user}"
ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
# Observe the same persisted VOICE_IME overrides as the launcher. Parse safely,
# never source raw shell syntax, and never let unrelated keys change ROOT_DIR.
# RIME 四键镜像 run-engine 的继承救援语义：会话里已存在且路径有效的值
# 压过 environment.d——launcher 也不会用 env 文件的旧值覆盖有效救援值，
# doctor 若不遵循就会对引擎根本不会使用的路径误报 FAIL（红队实测）。
_doc_rescue() {
  local value="${!1:-}"
  [[ -n "$value" && -e "$value" ]] && printf '%s' "$value"
}
_DOC_RESCUED=()
for _doc_var in VOICE_IME_RIME_LIBRARY VOICE_IME_RIME_SHARED_DATA_DIR \
                VOICE_IME_RIME_STAGING_DIR VOICE_IME_RIME_USER_DATA_DIR; do
  _doc_val="$(_doc_rescue "$_doc_var")"
  if [[ -n "$_doc_val" ]]; then _DOC_RESCUED+=("$_doc_var=$_doc_val"); fi
done
if [[ -f "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
  eval "$(bash "$ROOT_DIR/scripts/env-file-load.sh" "$ENV_FILE" | grep -E '^export (VOICE_IME_[A-Z0-9_]+|PYTORCH_CUDA_ALLOC_CONF)=' || true)"
fi
for _doc_pair in "${_DOC_RESCUED[@]}"; do
  export "${_doc_pair%%=*}=${_doc_pair#*=}"
done
unset _doc_var _doc_val _doc_pair
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
  local out line detail title impact guidance in_summary=0
  out="$(mktemp)"
  if [[ -x "$ROOT_DIR/scripts/check-environment.sh" ]]; then
    "$ROOT_DIR/scripts/check-environment.sh" >"$out" 2>&1 || true
    cat "$out"
    # 只登记预检最终汇总中的 WARN/FAIL（原检查段也会输出，不能双计数）。
    # 明细约定：标题 —— 影响；指引；保留来源方便用户定位。
    local env_fail_msgs=()
    while IFS= read -r line; do
      if [[ "$line" == '== 汇总 ==' ]]; then in_summary=1; continue; fi
      if [[ $in_summary -eq 1 && "$line" == '  [FAIL] '* ]]; then
        env_fail_msgs+=("${line#'  [FAIL] '}")
        continue
      fi
      [[ $in_summary -eq 1 && "$line" == '  [WARN] '* ]] || continue
      detail="${line#'  [WARN] '}"
      title="${detail%% —— *}"; detail="${detail#* —— }"
      impact="${detail%%；*}"; guidance="${detail#*；}"
      register_warn "[环境预检] $title" "$impact" "$guidance"
    done < "$out"
    # 汇总行形如 “FAIL: 0  WARN: 2”
    if grep -qE 'FAIL: [1-9]' "$out"; then
      if [[ ${#env_fail_msgs[@]} -gt 0 ]]; then
        # 逐条透传 check-environment 汇总中的 FAIL 原因与修复方案。
        for line in "${env_fail_msgs[@]}"; do
          fail "[环境预检] $line"
        done
      else
        fail "环境依赖存在 FAIL 项（预检汇总未给出明细，请查看上方「环境依赖」段输出）"
      fi
      FAILED_ITEMS+=(env-deps)
    fi
  else
    warn "环境预检脚本缺失或不可执行" "没有完成环境依赖检查" "恢复 scripts/check-environment.sh 后运行 chmod +x scripts/check-environment.sh 并重新体检"
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
      fail "用户 config.json 校验失败：$(grep -m3 -E '^.+' <<<"$vout" | paste -sd'；' )。修复：修正后重跑 ./install.sh 或手动编辑 $cfg_json"
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
    warn "语音渠道 enabled 标志与配置不一致" "voice.py 实际命中 ${mismatch%%|*} 而非 ${mismatch##*|}" "运行所选渠道的 scripts/switch-*-asr.sh 写全互斥标志；参见 README 语音后端切换命令"
  else
    ok "渠道 enabled 标志与 asr.backend 一致"
  fi
  if [[ -r "$ENV_FILE" ]]; then
    CHANNEL_ENV_LINES="$(grep -E '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD|VOICE_IME_VOLC_|VOICE_IME_SILICONFLOW)' "$ENV_FILE" || true)"
    if [[ -n "$CHANNEL_ENV_LINES" ]]; then
      printf '  environment.d 渠道行（%s）：\n' "$ENV_FILE"
      while IFS= read -r channel_line; do
        printf '    %s\n' "$channel_line"
      done <<< "$CHANNEL_ENV_LINES"
      # 路径型行指向已消失的路径时，它就是「重装也修不好」的根源：这些键在
      # 会话环境里压过 config.json 与新安装（使用者实测的死循环）。switch 脚本
      # 与新 install.sh 都会清除，而旧 install.sh 会把它原样写回。
      STALE_CHANNEL_KEYS="$("$ROOT_DIR/scripts/env-sanitize.sh" --from-file "$ENV_FILE" \
        VOICE_IME_QWEN_ASR_PYTHON VOICE_IME_QWEN_ASR_MODEL_PATH VOICE_IME_QWEN_ASR_MODELS_DIR \
        VOICE_IME_MIMO_ASR_PYTHON VOICE_IME_MIMO_ASR_MODELS_DIR \
        VOICE_IME_MIMO_ASR_MODEL_PATH VOICE_IME_MIMO_ASR_TOKENIZER_PATH 2>/dev/null | while IFS= read -r unset_line; do
          [[ "$unset_line" == unset\ * ]] && printf '%s\n' "${unset_line#unset }"
        done)"
      if [[ -n "$STALE_CHANNEL_KEYS" ]]; then
        warn "environment.d 含指向已消失路径的路径型渠道行" \
          "这些键会进入会话环境并压过 config.json/新安装：$(printf '%s ' $STALE_CHANNEL_KEYS)（旧克隆/旧 venv 残留，重装不会覆盖它们）" \
          "运行 ./install.sh 自动清除（同时清 systemd 用户环境）；也可手动删除 $ENV_FILE 中对应行并执行 systemctl --user unset-environment $(printf '%s ' $STALE_CHANNEL_KEYS)；清完请重开终端或重新登录"
      else
        warn "environment.d 仍含渠道配置行" "旧环境行会压过 config.json 中的语音选择" "运行 ./install.sh 迁移旧配置后复查；路径仍有效的行属用户显式指定，会保留"
      fi
    fi
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
    if [[ -f "$ROOT_DIR/run-engine.sh" ]]; then
      ok "组件 exec 指向本仓库"
    else
      fail "组件 exec 指向不存在的路径：$ROOT_DIR/run-engine.sh"
      FAILED_ITEMS+=(component-xml)
    fi
  else
    fail "组件 exec 未指向本仓库（指向了旧副本？）"
    FAILED_ITEMS+=(component-xml)
  fi
  # daemon 视角：systemd 拉起的 ibus-daemon（--cache auto）会被系统级注册
  # 缓存 /var/cache/ibus/bus/registry 短路（读有效系统缓存即用，永不到
  # 用户缓存/IBUS_COMPONENT_PATH 扫描分支）；用户缓存被无组件路径的旧版
  # 重启覆写同样致病——两者症状一致：XML 与输入源都在，daemon 却不认识
  # voice-custom，重启电脑后输入法消失。引擎进程正在运行说明旧 daemon 已
  # 加载（进程未换），此时只降为 WARN。无法连接 daemon（CI/无桌面会话）
  # 时跳过本检测，不产生环境依赖的假 FAIL。
  if command -v ibus >/dev/null 2>&1; then
    _daemon_engines="$(ibus list-engine 2>/dev/null)" && [[ -n "$_daemon_engines" ]] || _daemon_engines=""
    if [[ -n "$_daemon_engines" ]]; then
      if grep -q '^  voice-custom ' <<<"$_daemon_engines"; then
        ok "IBus daemon 已加载 voice-custom 引擎"
      elif pgrep -f "$ROOT_DIR/src/ibus_voice_ime/engine.py --ibus" >/dev/null 2>&1; then
        warn "IBus daemon 未注册 voice-custom（系统级注册缓存短路或缓存被覆写）" "当前引擎进程仍存活可继续用；但下次重启/重新登录后输入法会消失" "运行 ./scripts/doctor.sh fix 或 ./install.sh 加固开机自愈并重建注册缓存"
      else
        fail "IBus daemon 未注册 voice-custom：组件 XML 在，但 daemon 注册缓存里没有（重启电脑后输入法消失即此因）"
        FAILED_ITEMS+=(ibus-registry)
      fi
    fi
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
      warn "引擎运行自其他仓库副本" "当前会话可能未使用本次安装的代码" "运行 ./scripts/ibus-restart.sh 后复查"
    fi
    check_stale_engine_env
  else
    if check_inactive_engine_paths; then
      warn "引擎进程未运行" "懒加载设计：切换输入法后首次按键才拉起，尚不能据此判定故障" "按 Super+Space 切到「自定义语音输入法」并输入 nihao+Space 试一次；成功可忽略，否则运行 ./scripts/ibus-restart.sh 后复查"
    fi
  fi
}

check_inactive_engine_paths() {
  # 无进程时没有 /proc 环境可查；读取与 launcher 相同的有效配置。
  # user/staging/log 目录允许首次运行创建，LLM 配置可选，不能误报 FAIL。
  local missing key path dead=0
  missing="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" - <<'PY_PATHS' 2>/dev/null
import os
from pathlib import Path
from ibus_voice_ime import config
if config.env_str("VOICE_IME_KEYBOARD_BACKEND", "rime") == "rime":
    # get() 会丢弃陈旧路径并自愈；体检须读取原始显式值，不能把坏配置隐藏掉。
    rime = config.load_user_config().get("rime", {})
    if not isinstance(rime, dict):
        rime = {}
    for leaf, env in (("library", "VOICE_IME_RIME_LIBRARY"),
                      ("shared_data_dir", "VOICE_IME_RIME_SHARED_DATA_DIR")):
        value = os.environ.get(env, rime.get(leaf))
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if not path.exists():
                print(f"rime.{leaf}\t{path}")
PY_PATHS
)"
  while IFS=$'\t' read -r key path; do
    [[ -n "$key" ]] || continue
    fail "引擎未运行且 config 的 $key 指向不存在的路径：$path（修正配置后运行 ./install.sh）"
    dead=$((dead + 1))
  done <<< "$missing"
  [[ $dead -eq 0 ]]
}

check_stale_engine_env() {
  # 2026-08-28 打字事故根因：登录会话缓存了仓库迁移前删除的旧绝对路径，
  # ibus-daemon 原样传给引擎，librime 加载失败 → 打字瘫痪（语音不受影响）。
  # run-engine.sh 已有自愈，这里检查"活引擎进程"的环境里是否仍有指向
  # 不存在路径的 VOICE_IME_* 变量（自愈失效/旧引擎未重启时报警）。
  #
  # 语义边界（防止假阳性 FAIL）：
  #   - VOICE_IME_LLM_CONFIG 是可选连接文件（setup-llm-cloud.sh 体系）：
  #     缺失 = LLM 润色功能关闭而非故障（llm_cloud_config.load_report 契约），
  #     其存在性/合法性由 check_llm_cloud_config 段负责，此处跳过——否则
  #     同一次检查里一个段对同一条件报 OK、另一个段报 FAIL，且 fix 模式
  #     的"重启引擎"永远修不好（run-engine 重启后导出同样的默认路径）；
  #   - VOICE_IME_LLM_LOG 是按需创建的日志文件：LLM 未启用/尚未写过日志时
  #     文件不存在是合法状态（2026-10 更新后假阳性事故：switch-*.sh 重启
  #     daemon 时导出的默认 llm.jsonl 路径被本检查误报 FAIL，且 fix 重启
  #     后 switch 脚本会再次注入同样路径，永远修不好）。真正陈旧的父目录
  #     由 run-engine.sh 的自愈段负责（父目录不存在时 unset）；
  #   - 只检查以 / 开头的绝对路径值（URL 等非路径值不参与存在性判断）；
  #   - 冒号分隔的多路径值逐段检查（空段跳过），不做整串 -e。
  local pid line var val part dead=0
  for pid in $(pgrep -f "$ROOT_DIR/src/ibus_voice_ime/engine.py --ibus"); do
    [[ -r "/proc/$pid/environ" ]] || continue
    while IFS= read -r line; do
      case "$line" in
        VOICE_IME_LLM_CONFIG=*)
          # 可选连接文件，见上方语义边界说明。
          ;;
        VOICE_IME_LLM_LOG=*)
          # 按需创建的日志文件，缺失合法，见上方语义边界说明。
          ;;
        VOICE_IME_*=/*ibus-voice-ime*)
          var="${line%%=*}" val="${line#*=}"
          # 模式锚定依赖通配回溯（值内含第二个 '=' 时 `*` 可跨 '=' 匹配），
          # 这里显式强制不变式：只有以 / 开头的值才参与存在性判断。
          [[ "$val" == /* ]] || continue
          [[ -n "$val" ]] || continue
          local parts=()
          IFS=':' read -r -a parts <<< "$val"
          [[ ${#parts[@]} -gt 0 ]] || continue
          for part in "${parts[@]}"; do
            [[ -n "$part" ]] || continue
            if [[ ! -e "$part" ]]; then
              fail "引擎环境 $var 指向不存在的路径：$part。修复：./scripts/doctor.sh fix（自动重启引擎，run-engine.sh 会自愈陈旧继承值）；或手动运行 ./scripts/ibus-restart.sh / 重新登录"
              dead=$((dead + 1))
            fi
          done
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
    warn "librime-lua 插件缺失" "rime-ice 的 Lua 扩展不可用" "运行 ./scripts/setup-rime-ice.sh 自动补齐；不用 Lua 扩展可忽略"
  fi

  if rime_ice_sources_present; then
    ok "rime-ice 方案 + 词库源（cn_dicts）就绪"
  elif [[ -f "$RIME_DATA/rime_ice.schema.yaml" ]]; then
    fail "半残态：rime_ice.schema.yaml 在，但 cn_dicts 词库缺失（gitignore 资产未部署）。影响：引擎将回退 luna_pinyin_simp 或出零候选。修复：./init.sh 或 ./scripts/setup-rime-ice.sh"
    FAILED_ITEMS+=(rime-ice-deploy)
  else
    warn "rime-ice 方案文件不存在" "键盘使用内置 luna，可正常打字但词库较小" "运行 ./scripts/setup-rime-ice.sh；接受 luna 可忽略"
  fi

  if rime_ice_sources_present; then
    if rime_ice_build_present; then
      ok "rime-ice 编译产物（table.bin）就绪"
    else
      warn "词库源在但编译产物缺失" "首次按键会触发词库编译，约等待 1-3 分钟" "可切换输入法后按键等待；或先运行 ./scripts/setup-rime-ice.sh 编译，接受首次等待可忽略"
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

# 验收 receipt 匹配（快速 stat 对比，不跑推理）：setup-qwen-asr.sh 的 smoke
# 验收/--verify-only 成功后写 $VENV/verify-receipt.json。解释器、模型路径
# （归一化比较——换目录同 stat 文件不得骗过 INFO，红队实测）、requirements
# sha256、模型文件清单（名称/大小/mtime_ns）均与当前实际一致时，向 stdout
# 打印最近验收时间，退出 0；receipt 缺失/损坏/任一项漂移则退出 1。
# stat 对比只是"配置未漂移"判据，不是内容完整性/防伪证明；强制复验走
# ./scripts/setup-qwen-asr.sh --verify-only。
verify_receipt_matches() {  # <receipt> <python> <model_dir> <requirements>
  [[ -f "$1" ]] || return 1
  "$PYTHON" - "$1" "$2" "$3" "$4" <<'PY' 2>/dev/null
import hashlib, json, os, sys
receipt_path, python, model_dir, reqs_path = sys.argv[1:5]
try:
    with open(receipt_path, encoding='utf-8') as stream:
        data = json.load(stream)
    def same_path(left, right):
        return left == right or os.path.realpath(left) == os.path.realpath(right)
    if data.get('stage') != 'smoke' or not same_path(str(data.get('python') or ''), python):
        raise SystemExit(1)
    if not same_path(str(data.get('model_path') or ''), model_dir):
        raise SystemExit(1)
    with open(reqs_path, 'rb') as stream:
        if data.get('reqs_sha256') != hashlib.sha256(stream.read()).hexdigest():
            raise SystemExit(1)
    recorded = data.get('model_files')
    if not isinstance(recorded, dict):
        raise SystemExit(1)
    actual = {name for name in os.listdir(model_dir)
              if os.path.isfile(os.path.join(model_dir, name))}
    if set(recorded) != actual:
        raise SystemExit(1)
    for name, entry in recorded.items():
        status = os.stat(os.path.join(model_dir, name))
        # 必须有 mtime_ns：旧 receipt（只有整秒 mtime）一律失效恢复 WARN——
        # 折算 mtime*1e9 会让整秒文件的旧 receipt 继续匹配，同秒篡改即可
        # 骗过 INFO（红队终轮实测）。
        if (not isinstance(entry, dict) or entry.get('size') != status.st_size
                or entry.get('mtime_ns') != status.st_mtime_ns):
            raise SystemExit(1)
    stamp = str(data.get('timestamp') or '')
    print(stamp[5:10] + ' ' + stamp[11:16] if len(stamp) >= 16 else stamp)
except SystemExit:
    raise
except Exception:
    sys.exit(1)
PY
}

# ------------------------------------------------------------ 语音后端 --
check_asr_backend() {
  header "语音后端（当前：$(current_asr_backend)）"
  local backend
  backend="$(current_asr_backend)"
  case "$backend" in
    qwen3-asr)
      local have_venv=1 have_model=1 qwen_python preflight_out
      # 模式感知门控：先探测 GPU 三态。backend=qwen3-asr 而本机无可用
      # NVIDIA GPU 属「环境与所选后端不匹配」——体检如实 FAIL（GPU 缺失不是
      # 可忽略的降级），但给出明确出路（换云端/仅键盘），且不再跑注定失败的
      # CUDA 运行时验收，避免用户被误导去修 venv/模型/重装 CUDA。
      GPU_STATE="" GPU_REASON="" GPU_ACTION="" GPU_BRIEF=""
      if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
        eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
      fi
      case "$GPU_STATE" in
        ok) ok "NVIDIA GPU 可用（${GPU_BRIEF:-型号未知}）" ;;
        partial)
          fail "本机 NVIDIA CUDA 驱动不可用，而当前语音后端 qwen3-asr 依赖本地 GPU"
          printf '         原因：%s\n' "${GPU_REASON:-未知}"
          [[ -n "$GPU_ACTION" ]] && printf '         %s\n' "$GPU_ACTION"
          printf '         出路：修复驱动后运行 ./scripts/setup-qwen-asr.sh 完整验收；或换云端（自备 API Key）VOICE_IME_SILICONFLOW_API_KEY=%s ./scripts/switch-siliconflow-asr.sh；仅用键盘可忽略下方语音资产 FAIL\n' "'sk-xxx'"
          FAILED_ITEMS+=(qwen-gpu-mismatch)
          ;;
        *)
          fail "本机无可用 NVIDIA GPU，而当前语音后端 qwen3-asr 依赖本地 GPU：换云端 ./scripts/switch-siliconflow-asr.sh（自备 API Key，VOICE_IME_SILICONFLOW_API_KEY='sk-xxx'）或仅键盘（键盘输入不受影响）"
          FAILED_ITEMS+=(qwen-gpu-mismatch)
          ;;
      esac
      qwen_python="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c 'from ibus_voice_ime.asr.qwen_asr_runtime import _python; print(_python())' 2>/dev/null || true)"
      [[ -n "$qwen_python" ]] || qwen_python="$ROOT_DIR/.venv-qwen-asr/bin/python"
      echo "  实际 sidecar 解释器：$qwen_python"
      if [[ -x "$qwen_python" ]] || command -v "$qwen_python" >/dev/null; then
        ok "Qwen sidecar 解释器存在（仍需运行时验收）"
      else
        fail "Qwen sidecar 解释器缺失：$qwen_python"
        # 生效路径优先级：环境变量 > 用户 config.json > 仓库默认。指向当前仓库
        # 之外且不存在时，多半是旧克隆写进 config.json 的绝对路径残留——删掉
        # 旧克隆目录并不会清掉它，在本仓库重装 venv 也修不好，需改配置本身。
        if [[ "$qwen_python" == /* && "$qwen_python" != "$ROOT_DIR"/* ]]; then
          printf '         该路径不在当前仓库（%s）内，且已不存在：这是旧克隆留下的显式覆盖，而不是本仓库缺文件。\n' "$ROOT_DIR"
          # 生效值优先级：环境变量 > config.json。路径型环境键由旧 install 写进
          # environment.d / systemd 用户环境后，删目录、重装、甚至 unset 单个终端
          # 都不会清掉它——必须指出是哪一层在覆盖，否则排查会一直在 config.json 打转。
          if [[ -n "${VOICE_IME_QWEN_ASR_PYTHON:-}" ]]; then
            printf '         来源：环境变量 VOICE_IME_QWEN_ASR_PYTHON（继承自 environment.d 或 systemd 用户环境，优先于 config.json）\n'
            printf '         排查：grep -rn "VOICE_IME_QWEN" ~/.config/environment.d/ ; systemctl --user show-environment | grep -i qwen\n'
            printf '         修复：运行 ./install.sh（自动清除指向已消失路径的行）后重开终端或重新登录；或手动删除 environment.d 中的对应行并执行 systemctl --user unset-environment VOICE_IME_QWEN_ASR_PYTHON\n'
          else
            printf '         来源：用户配置 ~/.config/ibus-voice-ime/config.json 的 asr.qwen3.python 键\n'
            printf '         修复：删除该键，或运行 ./scripts/switch-qwen-asr.sh 1.7b 重写为本仓库路径\n'
          fi
        fi
        have_venv=0
      fi
      local model_dir
      # Inspect the effective model, never hide a broken explicit path by
      # validating a different complete model in the default directory.
      model_dir="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c 'from ibus_voice_ime.asr.qwen_asr_runtime import model_id; print(model_id())' 2>/dev/null || true)"
      if [[ -d "$model_dir" ]]; then
        ok "Qwen3-ASR 模型就绪（$model_dir）"
      else
        fail "Qwen3-ASR 模型缺失（$model_dir）"
        if [[ "$model_dir" == /* && "$model_dir" != "$ROOT_DIR"/* && ! -d "$model_dir" ]]; then
          if [[ -n "${VOICE_IME_QWEN_ASR_MODEL_PATH:-}" ]]; then
            printf '         来源：环境变量 VOICE_IME_QWEN_ASR_MODEL_PATH（environment.d / systemd 用户环境残留，压过 config.json 与共享缓存）\n'
            printf '         修复：运行 ./install.sh 自动清除该行后重开终端/重新登录；模型本体仍可从共享缓存（~/.cache/ibus-voice-ime/models）重建链接，无需重新下载\n'
          else
            printf '         来源：config.json 的 asr.qwen3.model_path 键；删除该键或运行 ./scripts/switch-qwen-asr.sh 1.7b 后，模型会从共享缓存（~/.cache/ibus-voice-ime/models）重建链接，无需重新下载\n'
          fi
        fi
        have_model=0
      fi
      if [[ $have_venv -eq 1 ]]; then
        # 先跑轻量静态验收（解释器版本/Python.h 编译探测/C 编译器，秒级），
        # 把"旧版安装脚本用系统 Python 建的 venv 缺开发头文件"这类问题在
        # 加载 torch/GPU 之前就归类报出，doctor fix 可精准一键重建。
        local static_out
        if static_out="$("$PYTHON" "$ROOT_DIR/scripts/qwen-preflight.py" --python "$qwen_python" --stage static 2>&1)"; then
          printf '%s\n' "$static_out"
          if [[ "$GPU_STATE" == "ok" ]]; then
            if preflight_out="$("$PYTHON" "$ROOT_DIR/scripts/qwen-preflight.py" --python "$qwen_python" --stage runtime --timeout "${VOICE_IME_QWEN_ASR_VERIFY_TIMEOUT:-180}" 2>&1)"; then
              printf '%s\n' "$preflight_out"
              ok "sidecar 开发头文件、CUDA 实际运算、Triton 编译/初始化通过"
            else
              printf '%s\n' "$preflight_out"
              fail "Qwen 运行时验收失败：$(grep -m3 '\[FAIL' <<<"$preflight_out" | paste -sd'；' )。修复：先看具体阶段报错（不要先删除模型或重装 CUDA）；常见处理 ./scripts/setup-qwen-asr.sh 重建或 $0 fix"
              have_venv=0
            fi
          else
            # 无可用 GPU 时 CUDA/Triton 运行时验收不可能通过：跳过并说明，
            # 避免把「环境与后端不匹配」误报成「运行时验收失败」。
            skip_note "CUDA 运行时验收：本机无可用 NVIDIA GPU，跳过（见上方 FAIL 指引）"
          fi
        else
          printf '%s\n' "$static_out"
          if grep -q '\[FAIL:python_headers\]' <<<"$static_out"; then
            fail "sidecar 解释器缺 Python.h 开发头文件（旧版安装脚本用系统 Python 建 venv 的遗留问题；Triton 首次编译内核必失败）。一键修复：$0 fix（自动备份旧 venv、用自带头文件的 Python 3.12 重建；模型不重新下载）"
            FAILED_ITEMS+=(qwen-python-headers)
          else
            fail "Qwen 静态验收失败：$(grep -m3 '\[FAIL' <<<"$static_out" | paste -sd'；' )。修复：$0 fix 或运行 ./scripts/setup-qwen-asr.sh 重建 sidecar 环境"
            FAILED_ITEMS+=(qwen-setup)
          fi
          have_venv=0
        fi
      fi
      if [[ $have_model -eq 1 ]] && ! "$PYTHON" "$ROOT_DIR/scripts/qwen-preflight.py" --model-only --model "$model_dir"; then
        fail "模型配置/权重分片不完整"
        have_model=0
      fi
      if [[ $have_venv -eq 1 && $have_model -eq 1 && "$GPU_STATE" == "ok" ]]; then
        # 降噪：最近一次 smoke 验收 receipt 仍与当前解释器/依赖/模型清单完全
        # 匹配时，不必每次体检都提醒"不能证明推理可用"——降为 INFO 并给出
        # 强制复验命令；receipt 缺失或任一项漂移则保持原 WARN。
        local receipt="$ROOT_DIR/.venv-qwen-asr/verify-receipt.json" receipt_time
        receipt_time="$(verify_receipt_matches "$receipt" "$qwen_python" "$model_dir" "$ROOT_DIR/requirements-qwen-asr.txt")" || receipt_time=""
        if [[ -n "$receipt_time" ]]; then
          info "最近验收 ${receipt_time} 通过（配置未变）；强制复验：./scripts/setup-qwen-asr.sh --verify-only"
          # 环境预检导入的同标题 WARN 一并移除：同一问题不能一边 INFO
          # 一边仍计入 WARN 汇总（真机实测残留导致降噪不彻底）。
          local drop_title="Qwen 模型目录存在但不能证明推理可用" _i
          local keep_titles=() keep_impacts=() keep_guidance=()
          for _i in "${!WARN_TITLES[@]}"; do
            [[ "${WARN_TITLES[_i]#\[*\] }" == "$drop_title" ]] && continue
            keep_titles+=("${WARN_TITLES[_i]}"); keep_impacts+=("${WARN_IMPACTS[_i]}"); keep_guidance+=("${WARN_GUIDANCE[_i]}")
          done
          WARN_TITLES=("${keep_titles[@]}"); WARN_IMPACTS=("${keep_impacts[@]}"); WARN_GUIDANCE=("${keep_guidance[@]}")
          WARNINGS=${#WARN_TITLES[@]}
        else
          warn "Qwen 模型目录存在但不能证明推理可用" "分层验收设计：体检检查运行时与分片，不跑模型推理" "本机最近验收过可用 ./scripts/setup-qwen-asr.sh --verify-only 复验；否则运行 ./scripts/setup-qwen-asr.sh 完整安装/验收"
        fi
      fi
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
    warn "RNNoise 模型缺失" "录音降噪自动降级 notch 档，语音仍可用" "运行 ./scripts/fetch-rnnoise-model.sh；接受降级可忽略"
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
    if [[ -n "${VOICE_IME_LLM_CONFIG:-}" ]]; then
      # 区分"默认未配置"（OK）与"显式指定路径但文件缺失"（WARN）：
      # 后者多为 environment.d 写入了自定义路径后仓库迁移/文件被删，
      # 引擎侧 run-engine.sh 自愈会静默回落 XDG 默认值（LLM 关闭），
      # 只有这里能提示用户。
      warn "显式 LLM 配置文件不存在：$cfg" "LLM 润色已关闭，规则清理仍可用" "运行 ./scripts/setup-llm-cloud.sh 或清除 VOICE_IME_LLM_CONFIG；不用润色可忽略"
    else
      ok "未配置（默认关闭，走规则清理）。启用：./scripts/setup-llm-cloud.sh"
    fi
    return
  fi
  local perms
  perms="$(stat -c %a "$cfg" 2>/dev/null || echo '?')"
  if [[ "$perms" != "600" ]]; then
    warn "LLM 配置文件权限为 $perms：$cfg" "内含 API Key，其他本机用户可能读取" "运行 chmod 600 \"$cfg\" 限制权限"
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
        warn "LLM 配置文件未通过校验" "LLM 已自动停用，语音输入不受影响" "按上方字段错误修复 $cfg 或运行 ./scripts/setup-llm-cloud.sh；不用润色可忽略"
        printf '%s\n' "${out#INVALID$'\n'}" | sed 's/^/         /'
        printf '         修复：编辑 %s 或重跑 ./scripts/setup-llm-cloud.sh\n' "$cfg"
        ;;
      *) warn "LLM 配置校验输出异常" "不能证明润色配置可用：$out" "运行 ./scripts/setup-llm-cloud.sh 重建；不用润色可忽略" ;;
    esac
  else
    warn "无法运行 LLM 配置校验" "不能证明润色配置可用：$out" "运行 python3 --version 并检查 PYTHONPATH；不用润色可忽略"
  fi
}

# ------------------------------------------------------------ 桌面集成 --
check_desktop_integration() {
  header "桌面集成（GNOME）"
  command -v gsettings >/dev/null 2>&1 || { warn "无 gsettings" "无法检查 GNOME 输入源与热键集成" "在 GNOME 桌面安装 gsettings 后复查；非 GNOME 手工集成时可忽略"; return; }
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
            warn "快捷键指向其他副本" "热键执行的不是本仓库脚本：$cmd" "运行 ./install.sh 重新注册热键"
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
    warn "未检测到本仓库语音/粘贴热键" "全局快捷键兜底可能不可用" "运行 ./install.sh 注册热键；仅用引擎内热键可忽略"
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
    fail "以下脚本缺可执行位：$(for f in "${missing[@]}"; do printf '%s ' "${f#"$ROOT_DIR"/}"; done)。修复：chmod +x 这些脚本，或运行 $0 fix"
    for f in "${missing[@]}"; do printf '         %s\n' "${f#"$ROOT_DIR"/}"; done
    FAILED_ITEMS+=(exec-bits)
  fi
  # 仓库层：git 索引里以 100644 跟踪的 .sh（新 clone 会丢失可执行位的根因）
  if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    local tracked644
    tracked644="$(git -C "$ROOT_DIR" ls-files -s | awk '$1 == "100644" && $4 ~ /\.sh$/ {print $4}')"
    if [[ -n "$tracked644" ]]; then
      warn "git 索引中的 shell 脚本缺少可执行位" "新 clone 可能无法直接执行下列脚本" "维护者应运行 git update-index --chmod=+x <脚本路径> 后提交；当前本机已可执行时可忽略"
      printf '         %s\n' $tracked644
    fi
  fi
}

# ------------------------------------------------------------------ 修复 --
do_fix() {
  header "修复（doctor fix）"
  local item restarted=0 qwen_attempted=0
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
      ibus-registry)
        # 双层修复：① drop-in 保证 systemd 拉起的 daemon（开机）强制重扫
        # 组件路径——auto 模式会被系统级注册缓存短路，光修缓存挡不住下次
        # 开机复发；② write-cache 只原子替换 ~/.cache/ibus/bus/registry，
        # 兜底非 systemd 路径。重启由统一的 ibus-restart.sh 完成。
        echo "==> 加固 ibus 开机自愈（systemd drop-in）并重建组件注册缓存"
        "$ROOT_DIR/scripts/ensure-ibus-cache-refresh.sh" || true
        if IBUS_COMPONENT_PATH="$HOME/.local/share/ibus/component:/usr/share/ibus/component" \
           ibus write-cache >/dev/null 2>&1; then
          fixed "注册缓存已重建 + 开机强制重扫已加固；重启输入法后生效（见下方统一重启步骤）"
          restarted=1
        else
          fail "ibus write-cache 失败，请手动运行 ./scripts/ibus-restart.sh"
        fi
        ;;
      qwen-python-headers|qwen-setup|qwen-gpu-mismatch)
        # Header failure also queues qwen-setup; prepare only once per fix run.
        [[ $qwen_attempted -eq 0 ]] || continue
        qwen_attempted=1
        # 模式感知修复：本机无可用 NVIDIA GPU 时本地 Qwen 装不出可用链路，
        # 盲目重装注定失败（安装器自身也会拒绝）——只给出路，不做徒劳下载。
        GPU_STATE="" GPU_REASON="" GPU_ACTION="" GPU_BRIEF=""
        if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
          eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
        fi
        if [[ "$GPU_STATE" != "ok" ]]; then
          echo "==> 跳过本地 Qwen 重装：本机无可用 NVIDIA GPU（${GPU_REASON:-未检测到}），本地后端无法安装/验收"
          echo "    换云端（自备 API Key）：VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh（或 MiMo / 火山豆包）"
          echo "    仅用键盘可忽略语音相关 FAIL；切换后端后复查，体检将按新后端检查"
        else
          echo "==> 调用 setup-qwen-asr.sh：仓库内自动准备 uv/带头文件的 Python 3.12，备份并迁移旧 venv"
          echo "    保留渠道/模型/解释器覆盖配置；完整模型不重复下载。DOCTOR_PROXY 传给安装器。"
          if VOICE_IME_ENABLE_QWEN_ASR=0 "$ROOT_DIR/scripts/setup-qwen-asr.sh" ${DOCTOR_PROXY:+--proxy "$DOCTOR_PROXY"}; then
            local actual_python
            actual_python="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c 'from ibus_voice_ime.asr.qwen_asr_runtime import _python; print(_python())' 2>/dev/null || true)"
            if [[ "$actual_python" == "$ROOT_DIR/.venv-qwen-asr/bin/python" ]]; then
              fixed "默认 Qwen 环境准备完成；实际运行时与模型状态以下方复查为准（--force 无 GPU 时未验收推理）"
              restarted=1
            else
              warn "Qwen 实际解释器仍被配置覆盖" "默认 venv 已准备，但当前 sidecar 仍使用 $actual_python；未改此覆盖，不能声称当前 sidecar 已修复" "检查 asr.qwen3.python / VOICE_IME_QWEN_ASR_PYTHON；需要迁移时运行 ./scripts/switch-qwen-asr.sh"
              echo "         检查 environment.d / VOICE_IME_QWEN_ASR_PYTHON / config.json 的 asr.qwen3.python；需要迁移激活时手动运行 ./scripts/switch-qwen-asr.sh"
            fi
          else
            fail "setup-qwen-asr.sh 失败（见具体 uv/Python/pip/编译器/CUDA 错误）；不会自动安装系统包"
            FIX_FAILURES=$((FIX_FAILURES + 1))
          fi
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
    if ! "$ROOT_DIR/scripts/ibus-restart.sh"; then
      fail "桌面激活失败：修复资产已保留，请重新登录或手动重启 IBus"
      FIX_FAILURES=$((FIX_FAILURES + 1))
    fi
  fi
}

# ------------------------------------------------------------------- 主流程 --
# 会话环境自愈：旧克隆/旧 venv 残留的路径型渠道键指向已消失的路径时必须忽略，
# 与 run-engine.sh 的运行期语义一致——否则体检会拿一个不存在的解释器/模型去判定，
# 把一个「环境变量残留」报成本仓库缺文件（使用者实测的误报）。降级为 WARN 而非
# 静默忽略：用户仍需知道会话里还有残留、重开终端/重新登录后才彻底消失。
sanitize_session_env() {
  local unset_lines stale_keys=() line
  unset_lines="$("$ROOT_DIR/scripts/env-sanitize.sh" \
    VOICE_IME_QWEN_ASR_PYTHON VOICE_IME_QWEN_ASR_MODEL_PATH VOICE_IME_QWEN_ASR_MODELS_DIR \
    VOICE_IME_MIMO_ASR_PYTHON VOICE_IME_MIMO_ASR_MODELS_DIR \
    VOICE_IME_MIMO_ASR_MODEL_PATH VOICE_IME_MIMO_ASR_TOKENIZER_PATH)"
  [[ -n "$unset_lines" ]] || return 0
  eval "$unset_lines"
  while IFS= read -r line; do
    [[ "$line" == unset\ * ]] && stale_keys+=("${line#unset }")
  done <<< "$unset_lines"
  warn "会话环境含已失效的路径型渠道键（本次体检已忽略）" \
    "$(printf '%s ' "${stale_keys[@]}")指向已不存在的路径（旧克隆/旧 venv 残留）；它们随登录来自 environment.d 或 systemd 用户环境，只在本进程忽略不等于清除" \
    "运行 ./install.sh 清除 environment.d 与 systemd 用户环境中的对应行，然后重开终端或重新登录（已在运行的终端与 gnome-terminal-server 仍持有旧值）"
}

sanitize_session_env
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
  FAILS=$FIX_FAILURES; WARNINGS=0; FAILED_ITEMS=()
  WARN_TITLES=(); WARN_IMPACTS=(); WARN_GUIDANCE=(); FAIL_MSGS=()
  check_env_dependencies
  check_unified_config
  check_ibus_registration
  check_rime_assets
  check_asr_backend
  check_llm_cloud_config
  check_desktop_integration
  check_exec_bits
fi

printf '\n== 汇总 ==\n'
# 先复述每条 FAIL 的原因与修复方案（与明细一致，无需回翻上方输出）。
for ((i=0; i<${#FAIL_MSGS[@]}; i++)); do
  printf '  [FAIL] %s\n' "${FAIL_MSGS[i]}"
done
for ((i=0; i<${#WARN_TITLES[@]}; i++)); do
  printf '  [WARN] %s —— %s；%s\n' "${WARN_TITLES[i]}" "${WARN_IMPACTS[i]}" "${WARN_GUIDANCE[i]}"
done
printf 'FAIL: %s  WARN: %s' "$FAILS" "$WARNINGS"
[[ "$MODE" == "fix" ]] && printf '  FIXED: %s' "$FIXED"
printf '\n'
if [[ $FAILS -gt 0 ]]; then
  echo "仍有问题未解决：请按上方各 [FAIL] 条目的原因与修复方案处理后重跑本脚本；或提 issue 附本输出。"
  echo "EXIT: 1"
  exit 1
fi
echo "检查通过（WARN 为可选/降级提示）。日志：~/.local/share/ibus-voice-ime/*.log"
echo "EXIT: 0"
exit 0
