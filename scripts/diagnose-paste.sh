#!/usr/bin/env bash
# 粘贴功能三检查点诊断（CP1/CP2/CP3）。
#
# 检查点定义（与 engine.py / clipboard-paste.sh 中的 CP 日志锚点一致）：
#   CP1  剪贴板/测试文本 → 暂存文件       （clipboard-paste.sh 侧职责）
#   CP2  暂存文件 → 引擎（IPC paste-check，只读不提交，不注入任何窗口）
#   CP3  引擎 → 焦点应用（IPC paste-file）
#        CP3a = commit_text 已调用（error.log 里的引擎视角终点）
#        CP3b = CommitText 信号是否上了 IBus 总线（dbus-monitor 旁路观测）
#
# CP3 的三个差分用例对照「一直稳定的语音路径」逐个加回嫌疑变量：
#   A) 单行 + 200ms  —— 对齐语音路径（语音结果即长单行、约 200ms 延迟）
#   B) 多行 + 200ms  —— 加入「多行内容」变量
#   C) 单行 + 1000ms —— 加入「1000ms 时序窗口」变量
# 恢复舞蹈（clipboard-paste.sh 的引擎切换）在 A/B/C 中均不存在——若三例全过
# 而真实 Ctrl+Alt+P 仍失败，嫌疑自动收敛到助手层（恢复舞蹈/通知/焦点切换）。
#
# 用法：
#   ./scripts/diagnose-paste.sh           # 完整诊断（CP3 会向焦点窗口注入测试文本）
#   ./scripts/diagnose-paste.sh --cp12    # 只跑 CP1/CP2（不注入任何文本）
#
# 注意：paste-check 命令与 delay 参数需要新版引擎；若检测到旧引擎在跑，请先
# 执行 ./scripts/ibus-restart.sh（会短暂打断当前输入法）再重跑本脚本。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ibus-voice-ime"
SOCK="$RUNTIME_DIR/voice.sock"
DIAG_SINGLE="$RUNTIME_DIR/diagnose-paste-single.txt"
DIAG_MULTI="$RUNTIME_DIR/diagnose-paste-multi.txt"
ENGINE_LOG="$HOME/.local/share/ibus-voice-ime/error.log"

STAMP="$(date +%Y%m%d%H%M%S)"
# 多行用例每行以 # 开头：即使注入目标是本终端、换行被当作回车执行，也只是无害注释。
SINGLE_TEXT="ZCODE_DIAG_CP3_single_$STAMP"
MULTI_TEXT="#ZCODE_DIAG_CP3_multi_L1_$STAMP
#ZCODE_DIAG_CP3_multi_L2_$STAMP
#ZCODE_DIAG_CP3_multi_L3_$STAMP"

ONLY_CP12=0
[[ "${1:-}" == "--cp12" ]] && ONLY_CP12=1

PASS_CNT=0; FAIL_CNT=0; SKIP_CNT=0
declare -a REPORT

report() {  # report <检查点> <PASS|FAIL|SKIP> <说明>
  REPORT+=("[$2] $1：$3")
  case "$2" in
    PASS) PASS_CNT=$((PASS_CNT+1)) ;;
    FAIL) FAIL_CNT=$((FAIL_CNT+1)) ;;
    SKIP) SKIP_CNT=$((SKIP_CNT+1)) ;;
  esac
  printf '  [%s] %s\n      %s\n' "$2" "$1" "$3"
}

send_ipc() {  # send_ipc <命令行>；stdout=引擎响应
  python3 - "$SOCK" "$1" <<'PY'
import socket, sys
path, command = sys.argv[1], sys.argv[2]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3.0)
        s.connect(path)
        s.sendall(command.encode("utf-8"))
        print(s.recv(400).decode("utf-8", "ignore").strip())
except Exception as exc:
    print(f"IPC_ERROR: {exc}")
PY
}

fingerprint_of() {  # fingerprint_of <文件>
  PYTHONPATH="$ROOT_DIR/src" python3 - "$1" <<'PY'
import sys
from ibus_voice_ime.clipboard_paste import content_fingerprint
with open(sys.argv[1], encoding="utf-8") as f:
    print(content_fingerprint(f.read()))
PY
}

ibus_address() {
  if [[ -n "${IBUS_ADDRESS:-}" ]]; then printf '%s\n' "$IBUS_ADDRESS"; return 0; fi
  local f
  for f in "$HOME/.config/ibus/bus/"*-unix-wayland-0 "$HOME/.config/ibus/bus/"*-unix-0; do
    [[ -r "$f" ]] || continue
    grep -m1 '^IBUS_ADDRESS=' "$f" | cut -d= -f2- && return 0
  done
  return 1
}

echo "=============================================="
echo " 粘贴功能三检查点诊断  marker=$STAMP"
echo "=============================================="

# ---------- 步骤 0：环境 ----------
echo
echo "== 步骤 0：环境检查 =="
if pgrep -f "engine.py --ibus" >/dev/null 2>&1; then
  echo "  引擎进程：运行中 ($(pgrep -f 'engine.py --ibus' | head -1))"
else
  echo "  引擎进程：未运行！请先切到本输入法或运行 ./scripts/ibus-restart.sh"
fi
if [[ -S "$SOCK" ]]; then
  echo "  IPC socket：$SOCK"
else
  echo "  IPC socket：不存在（$SOCK）——CP2/CP3 将跳过"
fi
echo "  当前 ibus 引擎：$(ibus engine 2>/dev/null || echo 未知)"

# 探测引擎是否支持新诊断协议（旧引擎不认识 paste-check）
mkdir -p "$RUNTIME_DIR"
printf 'probe_%s\n' "$STAMP" > "$DIAG_SINGLE"  # 探测用的最小文件，CP1 会重写
PROBE_RESP="$(send_ipc "paste-check $DIAG_SINGLE" 2>/dev/null || true)"
if [[ "$PROBE_RESP" == ERROR\ unknown* ]]; then
  echo
  echo "  ⚠ 当前运行的是旧版引擎（不认识 paste-check 命令）。"
  echo "    请先执行：$ROOT_DIR/scripts/ibus-restart.sh"
  echo "    （会短暂打断当前输入法；重启后重新运行本脚本。）"
  exit 3
fi

# ---------- CP1：测试文本 → 暂存文件 ----------
echo
echo "== CP1：写入暂存文件（模拟 clipboard-paste.sh 的文件侧） =="
mkdir -p "$RUNTIME_DIR"
python3 - "$DIAG_SINGLE" "$DIAG_MULTI" "$SINGLE_TEXT" "$MULTI_TEXT" <<'PY'
import sys
single_path, multi_path, single_text, multi_text = sys.argv[1:5]
with open(single_path, "w", encoding="utf-8") as f:
    f.write(single_text + "\n")
with open(multi_path, "w", encoding="utf-8") as f:
    f.write(multi_text + "\n")
PY
FP_SINGLE_LOCAL="$(fingerprint_of "$DIAG_SINGLE" 2>/dev/null || echo none)"
FP_MULTI_LOCAL="$(fingerprint_of "$DIAG_MULTI" 2>/dev/null || echo none)"
if [[ "$FP_SINGLE_LOCAL" == sha=* ]] && grep -q "$STAMP" "$DIAG_SINGLE" && [[ "$FP_MULTI_LOCAL" == *lines=3* ]]; then
  report "CP1" PASS "暂存文件写入/读回正常 single($FP_SINGLE_LOCAL) multi($FP_MULTI_LOCAL)"
else
  report "CP1" FAIL "暂存文件写入或读回异常 single($FP_SINGLE_LOCAL) multi($FP_MULTI_LOCAL)——检查 $RUNTIME_DIR 权限/磁盘"
fi

# ---------- CP2：暂存文件 → 引擎（paste-check，只读不提交） ----------
echo
echo "== CP2：暂存文件 → 引擎（paste-check，只读不提交） =="
if [[ ! -S "$SOCK" ]]; then
  report "CP2" SKIP "无 IPC socket，无法测试"
else
  RESP_S="$(send_ipc "paste-check $DIAG_SINGLE")"
  RESP_M="$(send_ipc "paste-check $DIAG_MULTI")"
  if [[ "$RESP_S" == "OK $FP_SINGLE_LOCAL" && "$RESP_M" == "OK $FP_MULTI_LOCAL" ]]; then
    report "CP2" PASS "引擎读到文件且指纹逐字节一致（单行+多行）"
  elif [[ "$RESP_S" == IPC_ERROR* || "$RESP_M" == IPC_ERROR* ]]; then
    report "CP2" FAIL "socket 连接失败：$RESP_S / $RESP_M"
  elif [[ "$RESP_S" == NO_FOCUS* ]]; then
    report "CP2" FAIL "引擎无焦点实例（NO_FOCUS）——先切到本输入法再试"
  else
    report "CP2" FAIL "指纹不一致或读取失败 single=$RESP_S multi=$RESP_M（本地 $FP_SINGLE_LOCAL）"
  fi
fi

if [[ "$ONLY_CP12" == "1" ]]; then
  echo
  echo "== --cp12 模式结束 =="
  printf '%s\n' "${REPORT[@]}"
  exit 0
fi

# ---------- CP3：引擎 → 焦点应用（差分矩阵） ----------
echo
echo "== CP3：引擎 → 焦点应用（每例向焦点窗口注入测试文本） =="
ADDR="$(ibus_address || true)"
MON_OK=0
if [[ -n "$ADDR" ]] && command -v dbus-monitor >/dev/null 2>&1; then
  MON_OK=1
  echo "  CP3b 总线观测：$ADDR"
else
  echo "  CP3b 总线观测：不可用（无 IBUS_ADDRESS 或 dbus-monitor），仅凭目视判断"
fi
echo "  目标窗口说明：直接保持本终端聚焦=注入终端（多行为 # 注释，无害）；"
echo "               也可在倒计时内切到任意输入框。"

run_cp3() {  # run_cp3 <标签> <文件> <delay_ms>
  local label="$1" file="$2" delay="$3" cap mon_pid resp seen commits markers
  echo
  echo "  ---- 用例 $label（delay=${delay}ms） ----"
  read -r -p "  按回车开始（回车后有 4 秒切换焦点）..." _unused
  cap="$(mktemp)"
  if [[ "$MON_OK" == "1" ]]; then
    dbus-monitor --address "$ADDR" >"$cap" 2>/dev/null &
    mon_pid=$!
  fi
  sleep 4
  resp="$(send_ipc "paste-file $file $delay")"
  sleep $(( delay / 1000 + 3 ))
  if [[ "$MON_OK" == "1" ]]; then
    kill "$mon_pid" 2>/dev/null
    wait "$mon_pid" 2>/dev/null
    commits="$(grep -c 'CommitText' "$cap" 2>/dev/null || true)"
    markers="$(grep -c "$STAMP" "$cap" 2>/dev/null || true)"
    echo "  IPC 响应：$resp；总线 CommitText 信号：${commits:-0} 次（含标记文本：${markers:-0} 次）"
    rm -f "$cap"
  else
    echo "  IPC 响应：$resp"
  fi
  read -r -p "  目标窗口里出现测试文本了吗？(y/n) " seen
  declare "CP3_RESULT_${label}=${seen,,}"
  if [[ "${seen,,}" == "y" ]]; then
    report "CP3-$label" PASS "文字已出现"
  else
    report "CP3-$label" FAIL "文字未出现"
  fi
}

A_RESULT="n"; B_RESULT="n"; C_RESULT="n"
run_cp3 A "$DIAG_SINGLE" 200   || true
A_RESULT="$CP3_RESULT_A"
run_cp3 B "$DIAG_MULTI" 200    || true
B_RESULT="$CP3_RESULT_B"
run_cp3 C "$DIAG_SINGLE" 1000  || true
C_RESULT="$CP3_RESULT_C"

# ---------- 结论 ----------
echo
echo "=============================================="
echo " 诊断结论（marker=$STAMP）"
echo "=============================================="
printf '%s\n' "${REPORT[@]}"
echo
if [[ "$FAIL_CNT" == "0" && "$SKIP_CNT" == "0" ]]; then
  echo " 判定：CP1/CP2/CP3 全部通过——基础链路健康。"
  echo " 若真实 Ctrl+Alt+P 仍失败，嫌疑收敛到助手层差异（恢复舞蹈/通知/焦点），"
  echo " 请对照运行：wl-copy '测试' && $ROOT_DIR/scripts/clipboard-paste.sh"
else
  echo " 判定："
  if [[ "$A_RESULT" == "n" && "$B_RESULT" == "n" && "$C_RESULT" == "n" ]]; then
    echo "  - CP3 三例全败：基础提交链路问题。若总线上能看到 CommitText 信号，"
    echo "    则提交已发出、被应用侧丢弃（焦点/Electron 类客户端）；若无信号，"
    echo "    则引擎未真正提交（_FOCUSED_ENGINE 定位问题）。"
  elif [[ "$A_RESULT" == "y" && "$B_RESULT" == "n" ]]; then
    echo "  - 多行内容是触发条件（A 过 B 败）：应用丢弃多行 commit，重构方向=按行分批提交。"
  elif [[ "$A_RESULT" == "y" && "$B_RESULT" == "y" && "$C_RESULT" == "n" ]]; then
    echo "  - 1000ms 时序窗口是触发条件（A/B 过 C 败）：默认延迟落在了坏窗口，"
    echo "    重构方向=缩短/自适应延迟。"
  else
    echo "  - 部分通过，见上方逐例结果与差分变量对照。"
  fi
fi
echo
echo " 引擎侧 CP 日志（最近）："
grep -E "CP1|CP2|CP3a" "$ENGINE_LOG" 2>/dev/null | tail -12 || echo "  （无）"
