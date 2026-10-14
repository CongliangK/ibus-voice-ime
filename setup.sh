#!/usr/bin/env bash
# 一键安装入口：配置向导 + init.sh（资产/注册）+ 语音验收 + 体检。
# 用法：
#   ./setup.sh                          # 交互配置；每题回车接受默认值
#   ./setup.sh --defaults               # 向导使用当前配置/默认值，不提问
#   ./setup.sh --non-interactive        # 全默认零提问（= --defaults --yes 且连 tty key 也不问），CI/管道适用
#   ./setup.sh --reconfigure            # 已装机器改配置，不初始化/下载资产
#   ./setup.sh --keyboard-only          # 只装键盘输入，不下载语音模型
#   ./setup.sh --qwen-sizes "0.6B 1.7B" # 本地 Qwen 下载集合（非空子集：0.6B / 1.7B）
#   ./setup.sh --qwen-model 0.6b         # 本地 Qwen 默认模型（0.6b / 1.7b）
#   ./setup.sh --cloud siliconflow      # 云端 SiliconFlow（需 API Key）
#   ./setup.sh --cloud mimo             # 云端 MiMo（国内直连，需 API Key）
#   ./setup.sh --verify-only            # 不安装，只做语音验收 + 体检
#   ./setup.sh --prepare-only           # 两段式第一阶段：只准备/验收资产（无桌面会话可用），
#                                        #   不注册不激活、不做语音验收/体检；第二阶段 ./install.sh
#   ./setup.sh --dry-run                # 只打印实际选择与执行计划，不应用配置
# 资产选项：--no-rime-ice --no-zhwiki --no-moegirl --no-denoise
# 其他：--strict-assets（CI：请求资产降级也失败）、--no-voice、--proxy URL、--skip-verify、--skip-doctor、--yes / -y、-h / --help
# 显式方案/资产选项优先，正常安装时跳过整个向导，维持既有脚本行为。
# 非终端或 --yes 自动使用 --defaults；所有安装子步骤幂等，可断点续装。
# 模式感知门控：语音验收（setup-qwen-asr.sh --verify-only）仅在所选方案为本地
# Qwen 且 init 语音步骤就绪（VOICE_IME_INIT_STATE voice=ok）时执行；无 GPU 跳过
# 并说明原因，--verify-only 显式调用不受此门控影响。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLOUD=""
VERIFY_ONLY=0 DRY_RUN=0 ASSUME_YES=0 SKIP_VERIFY=0 SKIP_DOCTOR=0 PREPARE_ONLY=0
RECONFIGURE=0 DEFAULTS=0 EXPLICIT_SELECTION=0 AUTO_DEFAULTS=0
PROXY_ARGS=() INIT_ARGS=() WIZARD_ARGS=()
INIT_LOG=""
WIZARD_RESULT="" WIZARD_OWNED_RESULT="" HOTKEYS_CHANGED="未改动（跳过向导）" VOICE_CHANGED=0
LLM_SUMMARY="未改动（跳过向导）"
# pip 包源向导值：official/tsinghua/absent（题目未出现，如云端/仅键盘方案）。
PIP_INDEX="absent"
VOICE_PLAN=local-qwen
QWEN_SIZES="${VOICE_IME_QWEN_ASR_SIZES-0.6B 1.7B}"
QWEN_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"

QWEN_SIZES_EXPLICIT=0 QWEN_MODEL_EXPLICIT=0
usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; }
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
    --qwen-sizes)
      shift
      read -r -a SELECTED_SIZES <<< "${1:-}" || true
      [[ ${#SELECTED_SIZES[@]} -gt 0 && "${1:-}" != *$'\n'* ]] || {
        echo '错误：--qwen-sizes 需要非空集合（空格分隔的 0.6B / 1.7B）。' >&2; exit 2; }
      for size in "${SELECTED_SIZES[@]}"; do
        [[ "$size" == 0.6B || "$size" == 1.7B ]] || {
          echo "错误：--qwen-sizes 非法分档：$size（支持 0.6B / 1.7B）。" >&2; exit 2; }
      done
      QWEN_SIZES="${SELECTED_SIZES[*]}"; QWEN_SIZES_EXPLICIT=1; EXPLICIT_SELECTION=1 ;;
    --qwen-model)
      shift
      [[ "${1:-}" == 0.6b || "${1:-}" == 1.7b ]] || {
        echo '错误：--qwen-model 需要 0.6b 或 1.7b。' >&2; exit 2; }
      QWEN_MODEL="$1"; QWEN_MODEL_EXPLICIT=1; EXPLICIT_SELECTION=1 ;;
    --reconfigure) RECONFIGURE=1 ;;
    --defaults) DEFAULTS=1 ;;
    # 经典非交互安装：等价 --defaults --yes 且彻底零提问（连 tty 下的
    # API Key 交互读取也跳过，缺 key 直接报错指引）；CI/远程管道场景用。
    --non-interactive|-n) DEFAULTS=1; ASSUME_YES=1; NONINTERACTIVE=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    # 两段式无人值守第一阶段：等价资产准备+验收（init --skip-install 语义，
    # Qwen 准备经 VOICE_IME_ENABLE_QWEN_ASR=0 不写配置不激活），跳过桌面
    # 激活/云端切换/语音验收/体检；第二阶段在 GNOME 会话补 ./install.sh。
    --prepare-only) PREPARE_ONLY=1; INIT_ARGS+=(--skip-install) ;;
    --dry-run) DRY_RUN=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    --strict-assets) INIT_ARGS+=(--strict-assets) ;;
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
if [[ "$QWEN_SIZES_EXPLICIT" -eq 1 || "$QWEN_MODEL_EXPLICIT" -eq 1 ]]; then
  [[ -z "$CLOUD" && "$VOICE_PLAN" != keyboard-only ]] || {
    echo '错误：--qwen-sizes / --qwen-model 选择本地 Qwen，不能与 --cloud / --keyboard-only / --no-voice 同用。' >&2; exit 2; }
  VOICE_PLAN=local-qwen
  WIZARD_ARGS+=(--voice-plan local-qwen)
  # 重配置仍借向导生成保留当前热键/LLM 的合并结果，但显式本地模型不再重复
  # 提问；当前向导无模型 override 接口，故整套问题用默认值，随后覆盖模型。
  [[ "$RECONFIGURE" -eq 0 ]] || WIZARD_ARGS+=(--defaults)
  case "${QWEN_MODEL,,}" in
    0.6|0.6b|qwen3-asr-0.6b) QWEN_MODEL=0.6b ;;
    1.7|1.7b|qwen3-asr-1.7b) QWEN_MODEL=1.7b ;;
    *) echo '错误：--qwen-* 的默认模型环境值非法（支持 0.6b / 1.7b）。' >&2; exit 2 ;;
  esac
  # 只显式选小档时默认也用小档，避免新机验收一个未请求下载的默认大档。
  if [[ "$QWEN_SIZES_EXPLICIT" -eq 1 && "$QWEN_MODEL_EXPLICIT" -eq 0 ]]; then
    case " $QWEN_SIZES " in
      *" $( [[ "$QWEN_MODEL" == 0.6b ]] && echo 0.6B || echo 1.7B ) "*) ;;
      *) QWEN_MODEL="${SELECTED_SIZES[0],,}" ;;
    esac
  fi
fi
[[ "$RECONFIGURE" -eq 0 || "$VERIFY_ONLY" -eq 0 ]] || {
  echo "错误：--reconfigure 与 --verify-only 不能同时使用。" >&2; exit 2; }
[[ "$PREPARE_ONLY" -eq 0 || "$VERIFY_ONLY" -eq 0 ]] || {
  echo "错误：--prepare-only 与 --verify-only 不能同时使用（prepare 只准备资产，验收在第二阶段）。" >&2; exit 2; }
[[ "$PREPARE_ONLY" -eq 0 || "$RECONFIGURE" -eq 0 ]] || {
  echo "错误：--prepare-only 与 --reconfigure 不能同时使用。" >&2; exit 2; }
[[ "$PREPARE_ONLY" -eq 0 || -z "$CLOUD" ]] || {
  echo "错误：--prepare-only 不支持 --cloud：云端切换会重启输入法，请在第二阶段（GNOME 会话）运行 ./setup.sh --cloud 或对应 switch 脚本。" >&2; exit 2; }
PYTHON="${VOICE_IME_WIZARD_PYTHON:-python3}"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
cleanup_wizard_result() {
  "$PYTHON" - "$WIZARD_OWNED_RESULT" "$INIT_LOG" <<'PY_CLEAN'
import sys, tempfile
from pathlib import Path
# 删除前核对绝对目录与前缀；用户显式指定的结果从不交给此 trap。
for value, prefix, suffix in ((sys.argv[1], 'ibus-voice-ime-wizard-', '.json'),
                              (sys.argv[2], 'ibus-voice-ime-init-', '.log')):
    if not value:
        continue
    p = Path(value)
    if p.parent.resolve() == Path(tempfile.gettempdir()).resolve() and p.name.startswith(prefix) and p.suffix == suffix and not p.is_symlink():
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
  SELECTION="$("$PYTHON" - "$WIZARD_RESULT" "$AUTO_DEFAULTS" "$QWEN_SIZES" "$QWEN_MODEL" "$QWEN_SIZES_EXPLICIT" "$QWEN_MODEL_EXPLICIT" <<'PY'
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
    # pip 包源：仅本地 Qwen 方案出现该题（键可缺省=云端/仅键盘方案未提问）。
    if 'pip_index' in r and r['pip_index'] not in ('tsinghua', 'official'):
        raise ValueError('pip_index 字段非法：pip 包源不合法')
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
    if sys.argv[5] == '1':
        # 显式 --qwen-sizes 只改下载集合：当前默认模型仍在集合内时必须
        # 保留（红队实测：当前 0.6b 曾被 shell 默认 1.7b 覆盖并清空
        # model_path）；仅当当前档不在集合时回落到集合首档。
        sizes = sys.argv[3].split()
        aliases = {'0.6': '0.6b', '0.6b': '0.6b', 'qwen3-asr-0.6b': '0.6b',
                   '1.7': '1.7b', '1.7b': '1.7b', 'qwen3-asr-1.7b': '1.7b'}
        current = aliases.get(str(config.get('asr.qwen3.model') or '').lower())
        size_of = {'0.6b': '0.6B', '1.7b': '1.7B'}
        r['qwen_sizes'] = sizes
        r['qwen_model'] = current if current and size_of.get(current) in sizes else sys.argv[4]
    if sys.argv[6] == '1':
        r['qwen_model'] = sys.argv[4]
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
    if sys.argv[2] == '1' or sys.argv[5] == '1' or sys.argv[6] == '1':
        path.write_text(json.dumps(r, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(plan)
    print(' '.join(r['qwen_sizes']))
    print(r['qwen_model'])
    print('有改动' if hotkeys_changed else '未改动')
    print('1' if changed else '0')
    print(llm_summary)
    print(r['pip_index'] if 'pip_index' in r else 'absent')
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
  [[ ${#VALUES[@]} -ge 7 ]] || { echo "错误：无法解析向导选择。" >&2; exit 1; }
  VOICE_PLAN="${VALUES[0]}" QWEN_SIZES="${VALUES[1]}" QWEN_MODEL="${VALUES[2]}"
  HOTKEYS_CHANGED="${VALUES[3]}" VOICE_CHANGED="${VALUES[4]}"
  # 云端 LLM 只读 llm.json；遗留 switch env 被 run-engine.sh 钉死为 0，不再导出误导值。
  LLM_SUMMARY="${VALUES[5]}"
  # pip 包源（仅本地 Qwen 方案出现该题）：official=显式选官方 PyPI，export
  # VOICE_IME_PIP_INDEX_URL=default 给 init.sh/语音验收/体检整条子进程链
  # （setup-qwen-asr.sh 尊重显式设置、绝不自动切回镜像）；tsinghua=显式选默认，
  # 清掉继承的旧覆盖（environment.d 持久行由 apply-wizard-config.py 清理）；
  # absent=题目未出现（云端/仅键盘），沿用现状不动环境（含用户自行 export 的值）。
  PIP_INDEX="${VALUES[6]}"
  case "$PIP_INDEX" in
    official) export VOICE_IME_PIP_INDEX_URL=default ;;
    tsinghua) unset VOICE_IME_PIP_INDEX_URL ;;
    absent) ;;
    *) echo "错误：向导结果的 pip 包源不合法：$PIP_INDEX" >&2; exit 2 ;;
  esac
  for ((i=7; i<${#VALUES[@]}; i++)); do INIT_ARGS+=("${VALUES[i]}"); done
  case "$VOICE_PLAN" in
    local-qwen) export VOICE_IME_QWEN_ASR_SIZES="$QWEN_SIZES" VOICE_IME_QWEN_ASR_MODEL="$QWEN_MODEL" ;;
    keyboard-only) INIT_ARGS+=(--no-voice); CLOUD="" ;;
    siliconflow) INIT_ARGS+=(--no-voice); CLOUD=siliconflow ;;
    mimo-cloud) INIT_ARGS+=(--no-voice); CLOUD=mimo ;;
    volc) INIT_ARGS+=(--no-voice); CLOUD=volc ;;
  esac
fi
# 显式模型 flags 正常安装跳过向导，也要透传实际选择给 init。
if [[ "$VOICE_PLAN" == local-qwen && "$VERIFY_ONLY" -eq 0 ]]; then
  export VOICE_IME_QWEN_ASR_SIZES="$QWEN_SIZES" VOICE_IME_QWEN_ASR_MODEL="$QWEN_MODEL"
fi
# 向导也可能选出云端方案（命令行未传 --cloud）：--prepare-only 同样拒绝，
# 云端切换会重启输入法，属于第二阶段（GNOME 会话）。
if [[ "$PREPARE_ONLY" -eq 1 && -n "$CLOUD" ]]; then
  echo "错误：--prepare-only 不支持云端方案（$CLOUD）：云端切换会重启输入法，请在第二阶段（GNOME 会话）运行 ./setup.sh --cloud 或对应 switch 脚本。" >&2
  exit 2
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
  if [[ "$PREPARE_ONLY" -eq 1 ]]; then
    # 两段式第一阶段：init --skip-install + Qwen 不激活（ENABLE_NOW=0）。
    PLAN+=("环境预检 + 资产初始化（不注册/不激活）：./init.sh ${INIT_ARGS[*]:-（全量默认）}")
    PLAN+=("语音环境只准备+验收（VOICE_IME_ENABLE_QWEN_ASR=0，不写配置不重启）")
    PLAN+=("第二阶段（GNOME 会话）：先 ./install.sh 注册激活，再可选 ./setup.sh --verify-only 验收+体检")
  else
    PLAN+=("环境预检 + 资产初始化 + 输入法注册：./init.sh ${INIT_ARGS[*]:-（全量默认）}")
    case "$CLOUD" in
      siliconflow) PLAN+=("切换云端语音（SiliconFlow）：需 VOICE_IME_SILICONFLOW_API_KEY（终端可隐藏输入）") ;;
      mimo) PLAN+=("切换云端语音（MiMo，国内直连）：scripts/switch-mimo-cloud-asr.sh cn") ;;
      volc) PLAN+=("切换云端语音（火山豆包）：scripts/switch-volc-bigmodel-asr.sh") ;;
      *) if [[ "$SKIP_VERIFY" -ne 1 && "${INIT_ARGS[*]:-}" != *--no-voice* ]]; then
           PLAN+=("语音链路验收：scripts/setup-qwen-asr.sh --verify-only（仅当方案为本地 Qwen 且 init 语音步骤就绪；无 GPU 自动跳过）")
         fi ;;
    esac
    [[ "$SKIP_DOCTOR" -eq 1 ]] || PLAN+=("整体体检：scripts/doctor.sh")
  fi
fi
echo "== 执行计划 =="
if [[ "$VERIFY_ONLY" -eq 0 ]]; then
  # 包源仅在存在覆盖时展示（官方 PyPI 或用户自行 export 的值）；默认清华与现状一致不赘述。
  case "${VOICE_IME_PIP_INDEX_URL:-}" in
    "") PIP_INDEX_NOTE="" ;;
    default) PIP_INDEX_NOTE="；pip 包源 官方 PyPI（VOICE_IME_PIP_INDEX_URL=default）" ;;
    *) PIP_INDEX_NOTE="；pip 包源 ${VOICE_IME_PIP_INDEX_URL}" ;;
  esac
  echo "实际选择：语音方案 $VOICE_PLAN；Qwen 下载分档 $QWEN_SIZES；默认模型 $QWEN_MODEL；热键 $HOTKEYS_CHANGED；云端 LLM 后处理 $LLM_SUMMARY$PIP_INDEX_NOTE。"
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

# 所有成功的桌面出口共用一张卡片；另起 Python 读取安装/切换后生效的 JSON，
# 不沿用向导快照或下载环境变量。坏配置/坏热键只影响提示，不能把成功安装变失败。
print_start_card() {
  local details
  details="$("$PYTHON" - <<'PY_CARD'
from ibus_voice_ime import config
from ibus_voice_ime.asr.voice_hotkey import parse_hotkey_combos

def hotkeys(key, legacy, fallback):
    value = config.get(key) or (config.get(legacy) if legacy else None) or fallback
    try:
        combos = parse_hotkey_combos(value)
        return ' / '.join(combo.label() for combo in combos) or fallback
    except (TypeError, ValueError, AttributeError):
        return fallback

voice = hotkeys('recording.hotkeys', 'recording.hotkey', 'Ctrl+Alt+V')
raw = hotkeys('recording.raw_hotkeys', '', 'Ctrl+Alt+B')
paste = hotkeys('recording.clipboard_hotkeys', 'recording.clipboard_hotkey', 'Ctrl+Alt+P')
backend = config.get('asr.backend', default='qwen3-asr')
model = config.get('asr.qwen3.model', default='1.7b')
# 压平非热键配置中的控制字符，避免损坏配置伪造额外提示行。
def clean(value):
    return ' '.join(str(value).split())
print(f'③ 语音：{voice}；原文：{raw}；粘贴：{paste}（后端：{clean(backend)}；Qwen 模型：{clean(model)}）')
if backend == 'qwen3-asr':
    print('④ 首次语音：首次按键后 sidecar 冷启动、模型加载需数十秒，属正常；未安装或未验收语音后端时需先补齐。')
else:
    print('④ 首次语音：本地 Qwen sidecar 冷启动、模型加载需数十秒，属正常；当前云端/其他后端不加载本地 Qwen 模型。')
PY_CARD
)" || details=$'③ 语音：Ctrl+Alt+V；原文：Ctrl+Alt+B；粘贴：Ctrl+Alt+P（配置读取失败，回退默认）\n④ 首次语音：首次按键后 sidecar 冷启动、模型加载需数十秒，属正常；语音后端需先安装并验收。'
  printf '\n== 开始使用 ==\n① Super+Space 切到「自定义语音输入法」\n② 焦点一个文本框\n%s\n' "$details"
}

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
  RECONFIGURE_PIP_NOTE=""
  [[ "$PIP_INDEX" != official ]] || RECONFIGURE_PIP_NOTE="；pip 包源 官方 PyPI（已持久化）"
  echo "重配置已生效：语音方案 $VOICE_PLAN；默认 Qwen 模型 $QWEN_MODEL；热键 $HOTKEYS_CHANGED；云端 LLM 后处理 $LLM_SUMMARY$RECONFIGURE_PIP_NOTE。未初始化或下载任何资产。"
  echo "提示：重配置不会准备新后端资产或保存 API Key；切换到尚未准备的后端后，请按向导指引补齐运行条件。"
  print_start_card
  exit 0
fi
if [[ "$VERIFY_ONLY" -eq 1 ]]; then
  step "语音链路验收"
  "$ROOT_DIR/scripts/setup-qwen-asr.sh" --verify-only "${PROXY_ARGS[@]}"
  step "整体体检"
  "$ROOT_DIR/scripts/doctor.sh"
  echo "验收完成。"
  print_start_card
  exit 0
fi
if [[ "$PREPARE_ONLY" -eq 1 ]]; then
  step "环境预检 + 资产初始化（init.sh --skip-install，不注册/不激活）"
else
  step "环境预检 + 资产初始化 + 输入法注册（init.sh）"
fi
INIT_RC=0
INIT_LOG="$("$PYTHON" -c 'import os,tempfile; fd,p=tempfile.mkstemp(prefix="ibus-voice-ime-init-",suffix=".log"); os.close(fd); print(p)')"
# tee 保持实时输出；固定前缀记录仅作为数据复述，绝不执行日志里的命令。
# --prepare-only：Qwen 准备走 setup-qwen-asr.sh 的 ENABLE_NOW=0 语义
# （VOICE_IME_ENABLE_QWEN_ASR=0：准备+推理验收，但不写配置、不切换后端、
# 不做桌面激活），配合 init --skip-install 实现无桌面会话的两段式安装。
INIT_ENV=()
[[ "$PREPARE_ONLY" -eq 0 ]] || INIT_ENV+=(VOICE_IME_ENABLE_QWEN_ASR=0)
if env "${INIT_ENV[@]}" "$ROOT_DIR/init.sh" "${INIT_ARGS[@]}" 2>&1 | tee "$INIT_LOG"; then
  INIT_RC=0
else
  INIT_PIPE_STATUS=("${PIPESTATUS[@]}")
  INIT_RC="${INIT_PIPE_STATUS[0]}"
  [[ "$INIT_RC" -ne 0 ]] || INIT_RC="${INIT_PIPE_STATUS[1]}"
fi
if [[ "$INIT_RC" -ne 0 ]]; then
  echo "⚠ init.sh 存在失败步骤（exit=$INIT_RC）；跳过语音验收。" >&2
  # --prepare-only 全程不体检（未注册未激活，体检桌面段只会叠加噪声）；
  # 红队发现失败诊断分支原先漏掉此门控。
  if [[ "$SKIP_DOCTOR" -eq 1 || "$PREPARE_ONLY" -eq 1 ]]; then
    echo "（已按当前模式跳过体检。）"
  else
    step "整体体检（诊断）"
    "$ROOT_DIR/scripts/doctor.sh" || true
  fi
  INIT_PROBLEMS=0
  while IFS=$'\t' read -r marker status title impact command; do
    [[ "$marker" == VOICE_IME_INIT_RESULT ]] || continue
    case "$status" in
      blocked) printf '安装失败：%s —— %s\n  续装命令：%s\n' "$title" "$impact" "$command" ;;
      degraded) printf '⚠ 降级项（严格资产模式时视为失败）：%s —— %s\n  续装命令：%s\n' "$title" "$impact" "$command" ;;
      *) continue ;;
    esac
    INIT_PROBLEMS=$((INIT_PROBLEMS + 1))
  done < "$INIT_LOG"
  if [[ "$INIT_PROBLEMS" -eq 0 ]]; then
    printf '安装失败：init.sh 未返回步骤汇总（exit=%s）；请结合上方错误与体检定位中断阶段。\n' "$INIT_RC"
  fi
  echo '子步骤幂等，续装不重跑向导/不重复下载已就绪资产。'
  exit "$INIT_RC"
fi
# 读取 init 汇总末尾的机器可读状态标记（TSV，仅作数据解析）：恰两字段、
# 第二字段必须为 voice=<枚举>（红队建议收紧：裸值/畸形/未知枚举一律视为
# unknown，保守跳过验收）；最后一行受控标记生效。
VOICE_INIT_STATE="unknown"
while IFS=$'\t' read -r init_marker init_state_field extra; do
  [[ "$init_marker" == VOICE_IME_INIT_STATE ]] || continue
  [[ -z "${extra:-}" ]] || continue
  case "$init_state_field" in
    voice=ok|voice=failed|voice=gpu_none|voice=gpu_partial|voice=skip)
      VOICE_INIT_STATE="${init_state_field#voice=}" ;;
    *) VOICE_INIT_STATE="unknown" ;;
  esac
done < "$INIT_LOG"
if [[ "$PREPARE_ONLY" -eq 1 ]]; then
  # 两段式第一阶段到此为止：不切云端、不做语音验收、不体检（未注册未激活，
  # 体检的桌面段会按未注册 FAIL，属预期噪声）；提示第二阶段命令。
  echo "准备完成。第二阶段需在有 GNOME 会话时执行："
  echo "  1) ./install.sh              # 必须：注册组件/输入源/热键并重启输入法（激活）"
  echo "  2) ./setup.sh --verify-only  # 可选：语音验收 + 体检（只读，不激活）"
  echo "两者可重复运行，已就绪资产不重复下载。"
  exit 0
fi
case "$CLOUD" in
  siliconflow)
    step "切换云端语音（SiliconFlow）"
    SILICONFLOW_KEY="${VOICE_IME_SILICONFLOW_API_KEY:-}"
    # --yes 保留 tty 下的 key 交互（旧行为）；只有 --non-interactive
    # 才彻底零提问：缺 key 不挂 read，直接报错并给出环境变量指引。
    if [[ -z "$SILICONFLOW_KEY" && -t 0 && "${NONINTERACTIVE:-0}" -ne 1 ]]; then
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
  # 模式感知门控：仅当所选方案为本地 Qwen 且 init 语音步骤就绪（voice=ok）
  # 时才验收；无 GPU/驱动不可用导致的跳过不再盲目 --verify-only（无 GPU 干净
  # 安装会被验收打 FAIL，红队指出）。--verify-only 显式调用不受此门控影响。
  if [[ "$VOICE_PLAN" == local-qwen && "$VOICE_INIT_STATE" == ok ]]; then
    step "语音链路验收"
    "$ROOT_DIR/scripts/setup-qwen-asr.sh" --verify-only "${PROXY_ARGS[@]}"
  else
    echo "语音验收跳过：语音方案 $VOICE_PLAN，init 语音步骤状态 voice=${VOICE_INIT_STATE:-unknown}（仅方案 local-qwen 且 voice=ok 时自动验收）。"
    echo "  无 GPU/驱动不可用时本地语音未安装，属预期不算失败：换云端见安装输出中的替代命令；确要验收可显式运行 ./scripts/setup-qwen-asr.sh --verify-only。"
  fi
fi
if [[ "$SKIP_DOCTOR" -eq 1 ]]; then
  echo "安装完成（已按选项跳过体检）。"
else
  step "整体体检"
  "$ROOT_DIR/scripts/doctor.sh"
  echo "安装完成。"
fi
print_start_card
