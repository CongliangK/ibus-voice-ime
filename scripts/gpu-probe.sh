#!/usr/bin/env bash
# NVIDIA GPU 三态探测：区分「CUDA 链路可用 / 有 NVIDIA 硬件但驱动不可用 / 无 NVIDIA 硬件」。
#
# 背景：旧判定只看 `command -v nvidia-smi && nvidia-smi -L`，把四种环境折叠成同一句
# “未检测到 NVIDIA GPU”：①纯核显机；②有卡但只装了 nvidia-settings/nvidia-modprobe
# 等用户态工具（缺内核驱动，无 nvidia-smi）；③内核升级后 kmod 未重编（nvidia-smi 在
# 但 -L 失败）；④容器/WSL。用户明明插着 N 卡却被告知“无 GPU”（2026-10 GitHub 报障）。
#
# 用法：
#   scripts/gpu-probe.sh                    # 人读输出（1-3 行）
#   eval "$(scripts/gpu-probe.sh --env)"    # 注入 4 个变量，按 $GPU_STATE 分支
#     GPU_STATE  = ok | partial | none
#     GPU_BRIEF  = 型号描述（ok 时来自 nvidia-smi -L 首行）
#     GPU_REASON = partial/none 时的一句话原因
#     GPU_ACTION = partial/none 时的修复/替代建议（可多行）
# 退出码：0=ok  2=partial  1=none（供直接调用；--env 模式经 eval 使用，忽略退出码）
#
# 可测试性：GPU_PROBE_SYSFS 覆盖 PCI 扫描根（默认 /sys/bus/pci/devices，测试注入假树）；
#           nvidia-smi 走 PATH（测试用 stub 目录前置 PATH 即可）。
set -uo pipefail

SYSFS="${GPU_PROBE_SYSFS:-/sys/bus/pci/devices}"
STATE="none" BRIEF="" REASON="" ACTION=""

CLOUD_HINT="无 GPU 的语音替代（云端识别，自备 API Key）：
    VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh   # 硅基流动（无需 GPU/大陆直连；SenseVoiceSmall 官方标注免费，Qwen3-ASR 按秒计费）
    VOICE_IME_MIMO_API_KEY='tp-xxx' ./scripts/switch-mimo-cloud-asr.sh cn
    VOICE_IME_VOLC_API_KEY='xxx'  ./scripts/switch-volc-bigmodel-asr.sh"

# 遍历 PCI 设备找 NVIDIA 显卡（vendor 0x10de，class 0x03* 含 VGA 与 3D controller，
# 后者覆盖 Optimus 笔记本的独显直连/仅计算模式）。不依赖 lspci（容器也有 sysfs）。
# 结果：找到 → rc=0 且 HW_DRIVER=绑定驱动名（nouveau/nvidia/空=未绑定）；没找到 → rc=1。
HW_DRIVER=""
detect_hardware() {
  HW_DRIVER=""
  [[ -d "$SYSFS" ]] || return 1
  local d vendor class
  for d in "$SYSFS"/*; do
    [[ -r "$d/vendor" && -r "$d/class" ]] || continue
    # 注意：不能用 $(<file 2>/dev/null || true)——附加重定向/|| 会让 bash 把
    # <file 当无名命令，结果恒为空。read 是纯内建，也不依赖 PATH 里的 cat。
    IFS= read -r vendor < "$d/vendor" 2>/dev/null || vendor=""
    IFS= read -r class < "$d/class" 2>/dev/null || class=""
    if [[ "$vendor" == "0x10de" && "$class" == 0x03* ]]; then
      if [[ -L "$d/driver" ]]; then
        HW_DRIVER="$(basename "$(readlink "$d/driver")")"
      fi
      return 0
    fi
  done
  return 1
}

if command -v nvidia-smi >/dev/null 2>&1; then
  if BRIEF="$(nvidia-smi -L 2>/dev/null | head -n1)" && [[ -n "$BRIEF" ]]; then
    STATE="ok"
  elif detect_hardware; then
    STATE="partial"
    REASON="NVIDIA 驱动装了但不可用（内核升级后模块未编译完成，或模块未加载）"
    ACTION="等待 akmod 编译完成并重启后重试。Fedora 检查：modinfo nvidia || sudo akmods --force"
  else
    STATE="none"
    REASON="未检测到 NVIDIA GPU"
    ACTION="$CLOUD_HINT"
  fi
else
  if detect_hardware; then
    STATE="partial"
    if [[ "$HW_DRIVER" == "nouveau" ]]; then
      REASON="检测到 NVIDIA 显卡，但当前使用 nouveau 开源驱动（不支持 CUDA）"
    elif command -v nvidia-settings >/dev/null 2>&1 || command -v nvidia-modprobe >/dev/null 2>&1 || command -v nvidia-xconfig >/dev/null 2>&1; then
      REASON="NVIDIA 驱动只装了一半（nvidia-settings/nvidia-modprobe 等用户态工具在，内核驱动与 nvidia-smi 缺失）"
    else
      REASON="检测到 NVIDIA 显卡，但未安装 NVIDIA 驱动"
    fi
    ACTION="安装闭源驱动后重启（本地语音识别需要 CUDA）：
    Fedora(rpmfusion): sudo dnf install akmod-nvidia xorg-x11-drv-nvidia
    Debian/Ubuntu:     sudo apt install nvidia-driver
在修好前可用云端识别：
    VOICE_IME_MIMO_API_KEY='tp-xxx' ./scripts/switch-mimo-cloud-asr.sh cn"
  else
    STATE="none"
    REASON="未检测到 NVIDIA GPU"
    ACTION="$CLOUD_HINT"
  fi
fi

case "${1:-}" in
  --env)
    printf 'GPU_STATE=%q\n' "$STATE"
    printf 'GPU_BRIEF=%q\n' "$BRIEF"
    printf 'GPU_REASON=%q\n' "$REASON"
    printf 'GPU_ACTION=%q\n' "$ACTION"
    ;;
  "")
    case "$STATE" in
      ok) echo "GPU: ok — ${BRIEF}" ;;
      *)
        echo "GPU: ${STATE} — ${REASON}"
        [[ -n "$ACTION" ]] && printf '     %s\n' "$ACTION"
        ;;
    esac
    ;;
  *) echo "用法：$0 [--env]（--env 输出可 eval 的变量赋值）" >&2; exit 3 ;;
esac
case "$STATE" in
  ok) exit 0 ;;
  partial) exit 2 ;;
  *) exit 1 ;;
esac
