#!/usr/bin/env bash
# 安装/重配置向导：只生成结果文件；用户配置由 apply-wizard-config.py 应用。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULTS=0 RECONFIGURE=0 VOICE_OVERRIDE=""
RIME_OVERRIDE="" ZHWIKI_OVERRIDE="" MOEGIRL_OVERRIDE="" DENOISE_OVERRIDE=""
usage() {
  printf '%s\n' '用法：scripts/config-wizard.sh [--defaults] [--reconfigure]' \
    '逐题配置；回车接受当前值，Ctrl+C 取消且不修改用户配置。' \
    '--defaults：使用当前配置或仓库默认值，不提问。' \
    '--reconfigure：跳过只用于安装的资产问题。' \
    '--voice-plan local-qwen|siliconflow|mimo-cloud|volc|keyboard-only：指定方案并跳过该题。' \
    '--no-rime-ice / --no-zhwiki / --no-moegirl / --no-denoise：指定不安装并跳过对应题。' \
    '结果路径为 stdout 最后一行；也可通过 VOICE_IME_WIZARD_RESULT 指定。' \
    'GTK 热键支持 <Control><Alt>v 或 Ctrl+Alt+V；当前引擎仅支持字母主键。'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --defaults) DEFAULTS=1 ;;
    --reconfigure) RECONFIGURE=1 ;;
    --voice-plan)
      shift; VOICE_OVERRIDE="${1:-}"
      case "$VOICE_OVERRIDE" in local-qwen|siliconflow|mimo-cloud|volc|keyboard-only) ;; *) echo '错误：语音方案不合法' >&2; exit 2 ;; esac ;;
    --no-rime-ice) RIME_OVERRIDE=n ;;
    --no-zhwiki) ZHWIKI_OVERRIDE=n ;;
    --no-moegirl) MOEGIRL_OVERRIDE=n ;;
    --no-denoise) DENOISE_OVERRIDE=n ;;
    -h|--help) usage; exit 0 ;;
    *) echo "错误：未知参数 $1（查看 --help）" >&2; exit 2 ;;
  esac
  shift
done
PYTHON="${VOICE_IME_WIZARD_PYTHON:-python3}"
GENERATED_RESULT=""
cleanup_result() {
  [[ -z "${VOICE_IME_WIZARD_RESULT:-}" && -n "$GENERATED_RESULT" ]] || return 0
  "$PYTHON" - "$GENERATED_RESULT" <<'PY_CLEAN'
import sys, tempfile
from pathlib import Path
p = Path(sys.argv[1])
# 只删除自己创建的、位于 TMPDIR 的匹配结果，绝不删除用户路径。
if p.parent.resolve() == Path(tempfile.gettempdir()).resolve() and p.name.startswith('ibus-voice-ime-wizard-') and p.suffix == '.json' and not p.is_symlink():
    p.unlink(missing_ok=True)
PY_CLEAN
}
trap cleanup_result EXIT
trap 'printf "\n已取消配置向导，用户配置未修改。\n" >&2; exit 130' INT
trap 'printf "\n配置向导已终止，用户配置未修改。\n" >&2; exit 143' TERM
DEFAULT_JSON="$("$PYTHON" "$ROOT_DIR/scripts/apply-wizard-config.py" --wizard-defaults)"
mapfile -t VALUES < <("$PYTHON" - "$DEFAULT_JSON" <<'PY'
import json, sys
v = json.loads(sys.argv[1])
for value in (v['voice_plan'], v['qwen_model'], v['hotkeys']['voice'],
              v['hotkeys']['raw'], v['hotkeys']['paste'],
              'null' if v['postprocess'] is None else ('y' if v['postprocess'] else 'n')):
    print(value)
PY
)
[[ ${#VALUES[@]} -eq 6 ]] || { echo '错误：无法读取向导默认配置' >&2; exit 1; }
VOICE_PLAN="${VALUES[0]}" QWEN_MODEL="${VALUES[1]}"
QWEN_DOWNLOAD="$QWEN_MODEL"
VOICE_HOTKEY="${VALUES[2]}" RAW_HOTKEY="${VALUES[3]}" PASTE_HOTKEY="${VALUES[4]}"
POSTPROCESS="${VALUES[5]}"
RIME=y ZHWIKI=y MOEGIRL=y DENOISE=y
ANSWER=""
ask() {
  local label="$1" current="$2" kind="$3" reply normalized
  while true; do
    printf '%s [当前/默认：%s]：' "$label" "$current" >&2
    if [[ "$DEFAULTS" -eq 1 ]]; then
      printf '采用默认\n' >&2
      reply="$current"
    else
      if ! IFS= read -r reply; then
        echo '输入已结束，向导取消；需要无人值守请使用 --defaults。' >&2
        exit 1
      fi
      [[ -n "$reply" ]] || reply="$current"
    fi
    case "$kind" in
      plan) case "$reply" in local-qwen|siliconflow|mimo-cloud|volc|keyboard-only) ANSWER="$reply"; return ;; esac ;;
      download) case "${reply,,}" in 1.7b|0.6b|both) ANSWER="${reply,,}"; return ;; esac ;;
      model) case "${reply,,}" in 1.7b|0.6b) ANSWER="${reply,,}"; return ;; esac ;;
      bool) case "${reply,,}" in y|n) ANSWER="${reply,,}"; return ;; esac ;;
      hotkey)
        if normalized="$("$PYTHON" "$ROOT_DIR/scripts/apply-wizard-config.py" --validate-hotkey "$reply" 2>/dev/null)"; then
          ANSWER="$normalized"; return
        fi ;;
    esac
    echo '输入不合法，请按题目给出的选项重试；热键仅支持修饰键加单字母或裸字母。' >&2
    [[ "$DEFAULTS" -ne 1 ]] || { echo '错误：现有配置不合法，请交互修正。' >&2; exit 1; }
  done
}
printf '== 配置向导（回车接受当前/默认值）==\n' >&2
if [[ -n "$VOICE_OVERRIDE" ]]; then
  VOICE_PLAN="$VOICE_OVERRIDE"
  printf '语音方案：%s（显式参数，跳过提问）\n' "$VOICE_PLAN" >&2
else
  ask '语音方案 local-qwen / siliconflow / mimo-cloud / volc / keyboard-only' "$VOICE_PLAN" plan
  VOICE_PLAN="$ANSWER"
fi
if [[ "$VOICE_PLAN" == local-qwen ]]; then
  if [[ "$RECONFIGURE" -eq 0 ]]; then
    ask '本地 Qwen 下载 1.7b / 0.6b / both' "$QWEN_DOWNLOAD" download
    QWEN_DOWNLOAD="$ANSWER"
    [[ "$QWEN_DOWNLOAD" == both ]] || QWEN_MODEL="$QWEN_DOWNLOAD"
  fi
  while true; do
    ask '默认 Qwen 模型 1.7b / 0.6b' "$QWEN_MODEL" model
    QWEN_MODEL="$ANSWER"
    if [[ "$RECONFIGURE" -eq 1 || "$QWEN_DOWNLOAD" == both || "$QWEN_MODEL" == "$QWEN_DOWNLOAD" ]]; then break; fi
    echo '默认模型必须在本次下载集合中，请重新选择。' >&2
    [[ "$DEFAULTS" -ne 1 ]] || exit 1
  done
fi
ask '语音热键（例如 Ctrl+Alt+V）' "$VOICE_HOTKEY" hotkey; VOICE_HOTKEY="$ANSWER"
ask '原文热键（例如 Ctrl+Alt+B）' "$RAW_HOTKEY" hotkey; RAW_HOTKEY="$ANSWER"
ask '粘贴热键（例如 Ctrl+Alt+P）' "$PASTE_HOTKEY" hotkey; PASTE_HOTKEY="$ANSWER"
if [[ "$POSTPROCESS" == null ]]; then
  echo '未配置云端 LLM（llm.json 不存在），可稍后运行 scripts/setup-llm-cloud.sh' >&2
else
  ask '启用 LLM 后处理？y/n' "$POSTPROCESS" bool; POSTPROCESS="$ANSWER"
fi
if [[ "$RECONFIGURE" -eq 0 ]]; then
  if [[ -n "$RIME_OVERRIDE" ]]; then RIME="$RIME_OVERRIDE"; else ask '安装 rime-ice 词库？y/n' "$RIME" bool; RIME="$ANSWER"; fi
  if [[ -n "$ZHWIKI_OVERRIDE" ]]; then ZHWIKI="$ZHWIKI_OVERRIDE"; else ask '安装 zhwiki 扩展词典？y/n' "$ZHWIKI" bool; ZHWIKI="$ANSWER"; fi
  if [[ -n "$MOEGIRL_OVERRIDE" ]]; then MOEGIRL="$MOEGIRL_OVERRIDE"; else ask '安装 moegirl 扩展词典？y/n' "$MOEGIRL" bool; MOEGIRL="$ANSWER"; fi
  if [[ -n "$DENOISE_OVERRIDE" ]]; then DENOISE="$DENOISE_OVERRIDE"; else ask '安装 RNNoise 降噪资产？y/n' "$DENOISE" bool; DENOISE="$ANSWER"; fi
fi
case "$VOICE_PLAN" in
  siliconflow)
    echo "安装完成后用 VOICE_IME_SILICONFLOW_API_KEY='你的密钥' ./scripts/switch-siliconflow-asr.sh 设置；密钥只用于该子进程，向导结果与 config.json 不保存密钥。" >&2 ;;
  mimo-cloud)
    echo "安装完成后用 VOICE_IME_MIMO_API_KEY='你的密钥' ./scripts/switch-mimo-cloud-asr.sh cn 设置，或通过 BWS 注入 XIAOMI_TOKEN_PLAN_CN_API_KEY；向导不保存密钥。" >&2 ;;
  volc)
    echo "安装完成后用 VOICE_IME_VOLC_API_KEY='你的密钥' ./scripts/switch-volc-bigmodel-asr.sh 设置，或通过 BWS 注入 VOLC_BIGMODEL_ASR_API_KEY；向导不保存密钥。" >&2 ;;
esac
# 只有全部回答有效时才写结果；临时文件为 0600，stdout 不含提示或密钥。
if [[ -z "${VOICE_IME_WIZARD_RESULT:-}" ]]; then
  GENERATED_RESULT="$("$PYTHON" -c 'import os,tempfile; fd,p=tempfile.mkstemp(prefix="ibus-voice-ime-wizard-",suffix=".json"); os.close(fd); print(p)')"
fi
RESULT_OUTPUT="$("$PYTHON" - "$ROOT_DIR" "${VOICE_IME_WIZARD_RESULT:-$GENERATED_RESULT}" "$VOICE_PLAN" "$QWEN_DOWNLOAD" "$QWEN_MODEL" \
  "$VOICE_HOTKEY" "$RAW_HOTKEY" "$PASTE_HOTKEY" "$POSTPROCESS" \
  "$RIME" "$ZHWIKI" "$MOEGIRL" "$DENOISE" "$RECONFIGURE" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
root, path, plan, download, model, voice, raw, paste, postprocess, rime, zhwiki, moegirl, denoise, reconfigure = sys.argv[1:]
sys.path.insert(0, str(Path(root) / 'src'))
from ibus_voice_ime import config
result = {
    'voice_plan': plan,
    'qwen_sizes': ['0.6B', '1.7B'] if download == 'both' else [download[:-1] + 'B'],
    'qwen_model': model,
    'hotkeys': {'voice': voice, 'raw': raw, 'paste': paste},
    'postprocess': None if postprocess == 'null' else postprocess == 'y',
    'assets': {'rime_ice': rime == 'y', 'zhwiki': zhwiki == 'y', 'moegirl': moegirl == 'y', 'denoise': denoise == 'y'},
    'reconfigure': reconfigure == '1',
}
if path:
    target = Path(path).expanduser().absolute()
    config_path = config.user_config_path()
    if target.resolve() == config_path.resolve() or (target.exists() and config_path.exists() and target.samefile(config_path)):
        print('错误：向导结果路径不能是用户配置文件；用户配置未修改。', file=sys.stderr)
        sys.exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 指定结果文件通常为 setup 预创建的 mktemp；拒绝符号链接。
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
else:
    fd, name = tempfile.mkstemp(prefix='ibus-voice-ime-wizard-', suffix='.json')
    target = Path(name)
with os.fdopen(fd, 'w', encoding='utf-8') as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2)
    stream.write('\n')
print(target)
PY
)"
printf '%s\n' "$RESULT_OUTPUT"
