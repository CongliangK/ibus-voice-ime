#!/usr/bin/env bash
# 指定路径键的会话自愈；stdout 仅输出安全的 unset 行，供调用方 eval。
# 非路径值、空值和真实存在的路径均保留，不扫描渠道/密钥/RIME 等其他键。
# 用法：eval "$(scripts/env-sanitize.sh VOICE_IME_QWEN_ASR_PYTHON ...)"
#       scripts/env-sanitize.sh --from-file <KEY=VALUE 文件> [键...]
#         —— 检验文件里的值（而非当前会话环境），每个键取最后一次出现的值
#         （与 systemd environment.d 后者覆盖前者一致）。install.sh 用它剔除
#         environment.d 中指向已消失路径的路径型渠道行：这类行会在登录后进入
#         会话环境并压过新安装（doctor 永久 FAIL），必须清除而不是写回。
#
# 存在性判定与各键的真实消费者逐一同语义（红队 B2 实测 Bash [[:space:]]
# 与 Python strip 在不同 locale 下集合不同、且并非所有消费者都 strip）：
#   - QWEN/MIMO 的 PYTHON / MODEL_PATH / TOKENIZER_PATH：消费者先 strip()
#     再 expanduser()（qwen_asr_runtime.py:114/137；mimo_asr_runtime.py
#     :52/86/97）。
#   - MIMO MODELS_DIR：消费者直接 expanduser() 不 strip（:61-62）——
#     末尾空格是目录名的一部分。
# 检验用 python3 完成（locale 无关）；仅用于检验，生效值保持原样。
# python3 不可用时回退 bash 近似（已知局限：ASCII 空白 + 无 ~user）。
# 注意：值只要形如路径（含 / 或以 ~ 开头）即按路径检验——含 / 的 repo id
# 缺失时会回落 JSON/默认（与 Qwen MODEL_PATH 原有分支行为一致）。
set -euo pipefail
# --from-file：值改从文件读取（文件不可读视为无值，不报错）。
FROM_FILE=""
if [[ "${1:-}" == "--from-file" ]]; then
  FROM_FILE="${2:-}"
  [[ -n "$FROM_FILE" ]] || { echo "--from-file 需要文件参数" >&2; exit 2; }
  shift 2
  [[ -r "$FROM_FILE" ]] || FROM_FILE=""
fi
# 消费者会 strip 的键（逐一对照源码，红队三轮补齐 MIMO 三键）。
STRIP_KEYS=' VOICE_IME_QWEN_ASR_PYTHON VOICE_IME_QWEN_ASR_MODEL_PATH VOICE_IME_MIMO_ASR_PYTHON VOICE_IME_MIMO_ASR_MODEL_PATH VOICE_IME_MIMO_ASR_TOKENIZER_PATH '
for key in "$@"; do
  # 输出会被 eval：键必须是合法标识符，绝不把环境值拼成 shell 语法。
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  if [[ -n "$FROM_FILE" ]]; then
    value=""
    # 逐行解析而非 source：值含空格/元字符时不执行任何内容。
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        "$key="*) value="${line#*=}" ;;
      esac
    done < "$FROM_FILE"
  else
    value="${!key-}"
  fi
  case "$value" in
    /*|\~*|*/*) ;;
    *) continue ;;
  esac
  if command -v python3 >/dev/null 2>&1; then
    do_strip=0
    [[ "$STRIP_KEYS" == *" $key "* ]] && do_strip=1
    if python3 -c 'import pathlib, sys
sys.exit(0 if pathlib.Path(sys.argv[2].strip() if sys.argv[1] == "1" else sys.argv[2]).expanduser().exists() else 1)' \
        "$do_strip" "$value" 2>/dev/null; then
      continue
    fi
  else
    # 回退：bash 近似（ASCII 空白 + ~ / ~/ 展开；无 ~user）。
    path="$value"
    if [[ "$STRIP_KEYS" == *" $key "* ]]; then
      path="${path#"${path%%[![:space:]]*}"}"
      path="${path%"${path##*[![:space:]]}"}"
    fi
    case "$path" in
      '~') path="$HOME" ;;
      '~/'*) path="$HOME/${path#\~/}" ;;
      '~'*) continue ;;  # ~user 无法检验：保守保留
    esac
    [[ -e "$path" ]] && continue
  fi
  printf 'unset %s\n' "$key"
  # 警告值单行化且不可携带控制序列：LF/CR/VT/FF/ESC/DEL/Unicode 行分隔
  # 符全部可见转义——ANSI 光标序列与 VT/FF 在标准终端可伪造独立视觉
  # 日志行（红队 B2/第三轮发现）。
  shown="${value//$'\n'/⏎}"
  shown="${shown//$'\r'/␍}"
  shown="${shown//$'\x0b'/␋}"
  shown="${shown//$'\x0c'/␌}"
  shown="${shown//$'\x1b'/␛}"
  shown="${shown//$'\x7f'/␡}"
  shown="${shown//$'\u2028'/⟨LS⟩}"
  shown="${shown//$'\u2029'/⟨PS⟩}"
  if [[ -n "$FROM_FILE" ]]; then
    printf '⚠ 环境文件 %s 中的 %s 指向不存在的路径，应清除：%s（旧安装残留？）\n' "$FROM_FILE" "$key" "$shown" >&2
  else
    printf '⚠ 会话环境 %s 指向不存在的路径，已忽略：%s（旧安装残留？）\n' "$key" "$shown" >&2
  fi
done
