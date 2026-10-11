#!/usr/bin/env bash
# 一键安装入口：配置向导 + init.sh（资产/注册）+ 语音验收 + 体检。
# 用法：
#   ./setup.sh                          # 交互配置；每题回车接受默认值
#   ./setup.sh --defaults               # 向导使用当前配置/默认值，不提问
#   ./setup.sh --reconfigure            # 已装机器改配置，不初始化/下载资产
#   ./setup.sh --keyboard-only          # 只装键盘输入，不下载语音模型
#   ./setup.sh --cloud siliconflow      # 云端 SiliconFlow（需 API Key）
#   ./setup.sh --cloud mimo             # 云端 MiMo（国内直连，需 API Key）
#   ./setup.sh --verify-only            # 不安装，只做语音验收 + 体检
#   ./setup.sh --dry-run                # 只打印实际选择与执行计划，不应用配置
# 资产选项：--no-rime-ice --no-zhwiki --no-moegirl --no-denoise
# 其他：--no-voice、--proxy URL、--skip-verify、--skip-doctor、--yes / -y、-h / --help
# 显式方案/资产选项优先，正常安装时跳过整个向导，维持既有脚本行为。
# 非终端或 --yes 自动使用 --defaults；所有安装子步骤幂等，可断点续装。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLOUD=""
VERIFY_ONLY=0 DRY_RUN=0 ASSUME_YES=0 SKIP_VERIFY=0 SKIP_DOCTOR=0
RECONFIGURE=0 DEFAULTS=0 EXPLICIT_SELECTION=0 AUTO_DEFAULTS=0
PROXY_ARGS=() INIT_ARGS=() WIZARD_ARGS=()
WIZARD_RESULT="" WIZARD_OWNED_RESULT="" HOTKEYS_CHANGED="未改动（跳过向导）" VOICE_CHANGED=0
LLM_SUMMARY="未改动（跳过向导）"
VOICE_PLAN=local-qwen
QWEN_SIZES="${VOICE_IME_QWEN_ASR_SIZES-0.6B 1.7B}"
QWEN_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keyboard-only|--no-voice)
      INIT_ARGS+=(--no-voice); WIZARD_ARGS+=(--voice-plan keyboard-only)
      VOICE_PLAN=keyboard-only; EXPLICIT_SELECTION=1 ;;
    --no-rime-ice|--no-zhwiki|--no-moegirl|--no-denoise)
      INIT_ARGS+=("$1"); WIZARD_ARGS+=("$1"); EXPLICIT_SELECTION=1 ;;
    --cloud)
      shift
      [[ "${1:-}" == siliconflow || "${1:-}" == mimo ]] || {
        echo "错误：--cloud 需要 siliconflow 或 mimo（收到：${1:-}）" >&2; exit 2; }
      CLOUD="$1"; INIT_ARGS+=(--no-voice); EXPLICIT_SELECTION=1
      if [[ "$CLOUD" == mimo ]]; then VOICE_PLAN=mimo-cloud; else VOICE_PLAN=siliconflow; fi
      WIZARD_ARGS+=(--voice-plan "$VOICE_PLAN") ;;
    --reconfigure) RECONFIGURE=1 ;;
    --defaults) DEFAULTS=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --skip-doctor) SKIP_DOCTOR=1 ;;
    --proxy)
      shift; [[ -n "${1:-}" ]] || { echo "错误：--proxy 需要参数" >&2; exit 2; }
      PROXY_ARGS+=(--proxy "$1"); INIT_ARGS+=(--proxy "$1") ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
  shift
done
[[ "$RECONFIGURE" -eq 0 || "$VERIFY_ONLY" -eq 0 ]] || {
  echo "错误：--reconfigure 与 --verify-only 不能同时使用。" >&2; exit 2; }
PYTHON="${VOICE_IME_WIZARD_PYTHON:-python3}"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
cleanup_wizard_result() {
  [[ -n "$WIZARD_OWNED_RESULT" ]] || return 0
  "$PYTHON" - "$WIZARD_OWNED_RESULT" <<'PY_CLEAN'
import sys, tempfile
from pathlib import Path
p = Path(sys.argv[1])
# 删除前核对绝对目录与前缀；用户显式指定的结果从不交给此 trap。
if p.parent.resolve() == Path(tempfile.gettempdir()).resolve() and p.name.startswith('ibus-voice-ime-wizard-') and p.suffix == '.json' and not p.is_symlink():
    p.unlink(missing_ok=True)
PY_CLEAN
}
trap cleanup_wizard_result EXIT

# 重配置只允许已有配置或已注册引擎的机器；与配置模块共享路径规则（含 XDG）。
if [[ "$RECONFIGURE" -eq 1 ]]; then
  CONFIG_PATH="$("$PYTHON" -c 'from ibus_voice_ime import config; print(config.user_config_path())')"
  if [[ ! -f "$CONFIG_PATH" && ! -f "$HOME/.local/share/ibus/component/voice-custom.xml" ]]; then
    echo "错误：未找到用户配置且输入法引擎尚未安装，请先运行 ./setup.sh 正常安装，再用 --reconfigure 改配置。" >&2
    exit 1
  fi
  WIZARD_ARGS+=(--reconfigure)
fi

# 自动默认保留旧入口的本地模型下载集合；显式 --defaults 和交互以向导选择为准。
if [[ "$DEFAULTS" -eq 1 || "$ASSUME_YES" -eq 1 || ! -t 0 ]]; then
  WIZARD_ARGS+=(--defaults)
  if [[ "$DEFAULTS" -eq 0 && "$RECONFIGURE" -eq 0 ]]; then AUTO_DEFAULTS=1; fi
fi
if [[ "$VERIFY_ONLY" -eq 0 ]] && [[ "$RECONFIGURE" -eq 1 || "$EXPLICIT_SELECTION" -eq 0 ]]; then
  if [[ "$AUTO_DEFAULTS" -eq 1 ]]; then WIZARD_ARGS+=(--voice-plan local-qwen); fi
  if [[ -n "${VOICE_IME_WIZARD_RESULT:-}" ]]; then
    WIZARD_STDOUT="$("$ROOT_DIR/scripts/config-wizard.sh" "${WIZARD_ARGS[@]}")"
  else
    # setup 持有 mkstemp 的生命周期；子向导显式写该路径，避免子进程 EXIT 提前删除。
    WIZARD_OWNED_RESULT="$("$PYTHON" -c 'import os,tempfile; fd,p=tempfile.mkstemp(prefix="ibus-voice-ime-wizard-",suffix=".json"); os.close(fd); print(p)')"
    WIZARD_STDOUT="$(VOICE_IME_WIZARD_RESULT="$WIZARD_OWNED_RESULT" "$ROOT_DIR/scripts/config-wizard.sh" "${WIZARD_ARGS[@]}")"
  fi
  WIZARD_RESULT="${WIZARD_STDOUT##*$'\n'}"
  if [[ -n "$WIZARD_OWNED_RESULT" && "$WIZARD_RESULT" != "$WIZARD_OWNED_RESULT" ]]; then
    echo '错误：向导返回的结果路径与本次临时文件不一致。' >&2; exit 2
  fi
  [[ -f "$WIZARD_RESULT" ]] || { echo "错误：向导没有生成有效结果文件。" >&2; exit 1; }
  # JSON 是数据，不 source/eval；预览只读配置，不调用应用器、不注册引擎。
  SELECTION="$("$PYTHON" - "$WIZARD_RESULT" "$AUTO_DEFAULTS" "$QWEN_SIZES" "$QWEN_MODEL" <<'PY'
import json, sys
from pathlib import Path
from ibus_voice_ime import config
from ibus_voice_ime.text import llm_cloud_config
from ibus_voice_ime.asr.voice_hotkey import parse_hotkey_combos

def field(obj, name, kind, prefix=''):
    label = prefix + name
    if not isinstance(obj, dict) or name not in obj:
        raise ValueError(f'{label} 字段非法：缺少字段')
    value = obj[name]
    if type(value) is not kind:
        raise ValueError(f'{label} 字段非法：类型错误')
    return value

def normalized(value):
    try:
        combos = parse_hotkey_combos(value)
        return ', '.join(dict.fromkeys(c.label() for c in combos)) or None
    except (ValueError, TypeError, AttributeError):
        # 旧配置的坏键位不是预览失败；合法向导值可在 apply 中修复。
        return None

def preview():
    path = Path(sys.argv[1]).expanduser()
    r = json.loads(path.read_text(encoding='utf-8'))
    plan = field(r, 'voice_plan', str)
    if plan not in ('local-qwen', 'siliconflow', 'mimo-cloud', 'volc', 'keyboard-only'):
        raise ValueError('voice_plan 字段非法：语音方案不合法')
    sizes = field(r, 'qwen_sizes', list)
    if not sizes or any(type(s) is not str or s not in ('0.6B', '1.7B') for s in sizes):
        raise ValueError('qwen_sizes 字段非法：下载分档不合法')
    if field(r, 'qwen_model', str) not in ('0.6b', '1.7b'):
        raise ValueError('qwen_model 字段非法：默认模型不合法')
    hotkeys = field(r, 'hotkeys', dict)
    for name in ('voice', 'raw', 'paste'):
        value = field(hotkeys, name, str, 'hotkeys.')
        if normalized(value) is None:
            raise ValueError(f'hotkeys.{name} 字段非法：热键不合法')
    if 'postprocess' not in r or (r['postprocess'] is not None and type(r['postprocess']) is not bool):
        raise ValueError('postprocess 字段非法：必须为布尔值或 null')
    assets = field(r, 'assets', dict)
    for name in ('rime_ice', 'zhwiki', 'moegirl', 'denoise'):
        field(assets, name, bool, 'assets.')
    field(r, 'reconfigure', bool)
    if sys.argv[2] == '1':
        # 旧非交互入口默认两档全下、激活 1.7b；保留显式环境覆盖。
        sizes = sys.argv[3].split()
        aliases = {'0.6': '0.6b', '0.6b': '0.6b', 'qwen3-asr-0.6b': '0.6b',
                   '1.7': '1.7b', '1.7b': '1.7b', 'qwen3-asr-1.7b': '1.7b'}
        model = aliases.get(sys.argv[4].lower())
        if not sizes or any(s not in ('0.6B', '1.7B') for s in sizes) or model is None:
            raise ValueError('本地 Qwen 分档或默认模型环境变量不合法')
        r['qwen_sizes'], r['qwen_model'] = sizes, model
    old_plan = {'qwen3-asr': 'local-qwen', 'siliconflow-asr': 'siliconflow',
                'mimo-cloud': 'mimo-cloud', 'volc-bigmodel-asr': 'volc'}.get(config.get('asr.backend'))
    changed = plan != old_plan or (plan == 'local-qwen' and r['qwen_model'] != config.get('asr.qwen3.model'))
    keys = (('voice', 'recording.hotkeys', 'recording.hotkey', 'Ctrl+Alt+V'),
            ('raw', 'recording.raw_hotkeys', '', 'Ctrl+Alt+B'),
            ('paste', 'recording.clipboard_hotkeys', 'recording.clipboard_hotkey', 'Ctrl+Alt+P'))
    hotkeys_changed = any(normalized(config.get(key) or config.get(legacy) or fallback) != normalized(hotkeys[name])
                          for name, key, legacy, fallback in keys)
    target = r['postprocess']
    llm_path = llm_cloud_config.config_path()
    if target is None:
        llm_summary = '未配置云端 LLM（llm.json 不存在）'
    else:
        if not llm_path.exists():
            raise ValueError('postprocess 字段非法：llm.json 不存在，不能修改云端 LLM 开关')
        try:
            llm = json.loads(llm_path.read_text(encoding='utf-8'))
            if not isinstance(llm, dict) or type(llm.get('enabled', True)) is not bool:
                raise ValueError('enabled 字段非法')
        except (OSError, ValueError) as exc:
            raise ValueError(f'llm.json 已损坏，拒绝修改：{exc}') from exc
        llm_summary = ('有改动' if llm.get('enabled', True) != target else '未改动')
        llm_summary += f'（目标：{"开" if target else "关"}；即刻生效，无需重注册）'
    if sys.argv[2] == '1':
        path.write_text(json.dumps(r, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(plan)
    print(' '.join(r['qwen_sizes']))
    print(r['qwen_model'])
    print('有改动' if hotkeys_changed else '未改动')
    print('1' if changed else '0')
    print(llm_summary)
    for name, flag in (('rime_ice', '--no-rime-ice'), ('zhwiki', '--no-zhwiki'),
                       ('moegirl', '--no-moegirl'), ('denoise', '--no-denoise')):
        if not assets[name]: print(flag)

try:
    preview()
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f'错误：向导结果 JSON 非法：{exc}；未应用配置。', file=sys.stderr)
    sys.exit(2)
PY
)"
  mapfile -t VALUES <<< "$SELECTION"
  [[ ${#VALUES[@]} -ge 6 ]] || { echo "错误：无法解析向导选择。" >&2; exit 1; }
  VOICE_PLAN="${VALUES[0]}" QWEN_SIZES="${VALUES[1]}" QWEN_MODEL="${VALUES[2]}"
  HOTKEYS_CHANGED="${VALUES[3]}" VOICE_CHANGED="${VALUES[4]}"
  # 云端 LLM 只读 llm.json；遗留 switch env 被 run-engine.sh 钉死为 0，不再导出误导值。
  LLM_SUMMARY="${VALUES[5]}"
  for ((i=6; i<${#VALUES[@]}; i++)); do INIT_ARGS+=("${VALUES[i]}"); done
  case "$VOICE_PLAN" in
    local-qwen) export VOICE_IME_QWEN_ASR_SIZES="$QWEN_SIZES" VOICE_IME_QWEN_ASR_MODEL="$QWEN_MODEL" ;;
    keyboard-only) INIT_ARGS+=(--no-voice); CLOUD="" ;;
    siliconflow) INIT_ARGS+=(--no-voice); CLOUD=siliconflow ;;
    mimo-cloud) INIT_ARGS+=(--no-voice); CLOUD=mimo ;;
    volc) INIT_ARGS+=(--no-voice); CLOUD=volc ;;
  esac
fi

PLAN=()
if [[ "$RECONFIGURE" -eq 1 ]]; then
  PLAN+=("稀疏合并向导配置：scripts/apply-wizard-config.py（自动备份）")
  if [[ "$HOTKEYS_CHANGED" == 有改动 || "$VOICE_CHANGED" -eq 1 ]]; then
    PLAN+=("热键/语音方案或模型变化：./install.sh 轻量重注册（不下载）")
  else
    PLAN+=("热键与语音方案/模型未变：无需重注册")
  fi
elif [[ "$VERIFY_ONLY" -eq 1 ]]; then
  PLAN+=("语音链路验收：scripts/setup-qwen-asr.sh --verify-only" "整体体检：scripts/doctor.sh")
else
  [[ -z "$WIZARD_RESULT" ]] || PLAN+=("稀疏合并向导配置：scripts/apply-wizard-config.py（自动备份）")
  PLAN+=("环境预检 + 资产初始化 + 输入法注册：./init.sh ${INIT_ARGS[*]:-（全量默认）}")
  case "$CLOUD" in
    siliconflow) PLAN+=("切换云端语音（SiliconFlow）：需 VOICE_IME_SILICONFLOW_API_KEY（终端可隐藏输入）") ;;
    mimo) PLAN+=("切换云端语音（MiMo，国内直连）：scripts/switch-mimo-cloud-asr.sh cn") ;;
    volc) PLAN+=("切换云端语音（火山豆包）：scripts/switch-volc-bigmodel-asr.sh") ;;
    *) if [[ "$SKIP_VERIFY" -ne 1 && "${INIT_ARGS[*]:-}" != *--no-voice* ]]; then
         PLAN+=("语音链路验收：scripts/setup-qwen-asr.sh --verify-only（本地 Qwen3-ASR，需 NVIDIA GPU）")
       fi ;;
  esac
  [[ "$SKIP_DOCTOR" -eq 1 ]] || PLAN+=("整体体检：scripts/doctor.sh")
fi
echo "== 执行计划 =="
if [[ "$VERIFY_ONLY" -eq 0 ]]; then
  echo "实际选择：语音方案 $VOICE_PLAN；Qwen 下载分档 $QWEN_SIZES；默认模型 $QWEN_MODEL；热键 $HOTKEYS_CHANGED；云端 LLM 后处理 $LLM_SUMMARY。"
fi
i=1; for p in "${PLAN[@]}"; do printf '  %d) %s\n' "$i" "$p"; i=$((i+1)); done
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "--dry-run：仅生成选择并打印计划，未应用配置、安装或下载任何资产。"
  exit 0
fi
if [[ "$ASSUME_YES" -ne 1 && -t 0 ]]; then
  printf '继续执行？[y/N] '
  read -r REPLY
  [[ "$REPLY" =~ ^[Yy]$ ]] || { echo "已取消。"; exit 0; }
fi
step() { printf '\n==> %s\n' "$1"; }

if [[ -n "$WIZARD_RESULT" ]]; then
  step "应用向导配置"
  APPLIED="$("$PYTHON" "$ROOT_DIR/scripts/apply-wizard-config.py" "$WIZARD_RESULT")"
  APPLY_CHANGED="$("$PYTHON" -c 'import json,sys; print("1" if json.loads(sys.argv[1])["hotkeys_changed"] else "0")' "$APPLIED")"
  if [[ "$APPLY_CHANGED" -eq 1 ]]; then HOTKEYS_CHANGED=有改动; else HOTKEYS_CHANGED=未改动; fi
fi
if [[ "$RECONFIGURE" -eq 1 ]]; then
  if [[ "$APPLY_CHANGED" -eq 1 || "$VOICE_CHANGED" -eq 1 ]]; then
    step "轻量重注册输入法与热键（install.sh，不下载）"
    "$ROOT_DIR/install.sh"
  fi
  echo "重配置已生效：语音方案 $VOICE_PLAN；默认 Qwen 模型 $QWEN_MODEL；热键 $HOTKEYS_CHANGED；云端 LLM 后处理 $LLM_SUMMARY。未初始化或下载任何资产。"
  echo "提示：重配置不会准备新后端资产或保存 API Key；切换到尚未准备的后端后，请按向导指引补齐运行条件。"
  exit 0
fi
if [[ "$VERIFY_ONLY" -eq 1 ]]; then
  step "语音链路验收"
  "$ROOT_DIR/scripts/setup-qwen-asr.sh" --verify-only "${PROXY_ARGS[@]}"
  step "整体体检"
  "$ROOT_DIR/scripts/doctor.sh"
  echo "验收完成。"
  exit 0
fi
step "环境预检 + 资产初始化 + 输入法注册（init.sh）"
INIT_RC=0
"$ROOT_DIR/init.sh" "${INIT_ARGS[@]}" || INIT_RC=$?
if [[ "$INIT_RC" -ne 0 ]]; then
  echo "⚠ init.sh 存在失败步骤（exit=$INIT_RC）；跳过语音验收，先跑体检诊断。" >&2
  if [[ "$SKIP_DOCTOR" -eq 1 ]]; then
    echo "（已按选项跳过体检。）"
  else
    step "整体体检（诊断）"
    "$ROOT_DIR/scripts/doctor.sh" || true
  fi
  echo "安装未完全成功：请按上方 ⚠ 提示修复后重跑 ./setup.sh（子步骤幂等，会续装）。"
  exit "$INIT_RC"
fi
case "$CLOUD" in
  siliconflow)
    step "切换云端语音（SiliconFlow）"
    SILICONFLOW_KEY="${VOICE_IME_SILICONFLOW_API_KEY:-}"
    if [[ -z "$SILICONFLOW_KEY" && -t 0 ]]; then
      read -rsp '请输入硅基流动 API Key（仅传给切换子进程，不保存）：' SILICONFLOW_KEY || true
      printf '\n'
    fi
    if [[ -z "$SILICONFLOW_KEY" ]]; then
      echo "错误：未设置 VOICE_IME_SILICONFLOW_API_KEY。注册 cloud.siliconflow.cn 后：" >&2
      echo "  VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' $ROOT_DIR/scripts/switch-siliconflow-asr.sh" >&2
      exit 1
    fi
    VOICE_IME_SILICONFLOW_API_KEY="$SILICONFLOW_KEY" "$ROOT_DIR/scripts/switch-siliconflow-asr.sh"
    unset SILICONFLOW_KEY
    ;;
  mimo)
    step "切换云端语音（MiMo）"
    "$ROOT_DIR/scripts/switch-mimo-cloud-asr.sh" cn ;;
  volc)
    step "切换云端语音（火山豆包）"
    "$ROOT_DIR/scripts/switch-volc-bigmodel-asr.sh" ;;
esac
if [[ "$SKIP_VERIFY" -ne 1 && -z "$CLOUD" && "${INIT_ARGS[*]:-}" != *--no-voice* ]]; then
  step "语音链路验收"
  "$ROOT_DIR/scripts/setup-qwen-asr.sh" --verify-only "${PROXY_ARGS[@]}"
fi
if [[ "$SKIP_DOCTOR" -eq 1 ]]; then
  echo "安装完成（已按选项跳过体检）。"
else
  step "整体体检"
  "$ROOT_DIR/scripts/doctor.sh"
  echo "安装完成。切换方式：GNOME 顶栏输入法菜单或 Super+Space；语音/原文/粘贴热键以本次生效配置为准。"
fi
