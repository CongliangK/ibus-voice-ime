#!/usr/bin/env bash
# uninstall-all.sh —— 彻底卸载 ibus-voice-ime 在本机用户目录写入/注册的全部内容。
# 它是 uninstall.sh 的完整替代（旧脚本保留不动）：覆盖组件、环境、GNOME 注册、
# systemd 用户服务、运行时进程、日志缓存与用户数据。
#
# 用法：
#   ./uninstall-all.sh                    # 默认 = --keep-user-data：保留个人数据
#   ./uninstall-all.sh --keep-user-data   # 同上（显式指定）
#   ./uninstall-all.sh --purge            # 彻底删除所有用户数据与配置（含 API key）
#   ./uninstall-all.sh -h | --help        # 显示本帮助
#
# 两种模式都删除（安装足迹，与个人数据无关）：
#   ~/.local/share/ibus/component/voice-custom.xml        IBus 组件注册
#   ~/.config/environment.d/ibus-voice-ime.conf           environment.d 持久环境
#   systemd 用户管理器 set-environment 中的 VOICE_IME_* / IBUS_COMPONENT_PATH /
#   PYTORCH_CUDA_ALLOC_CONF 键（动态扫描，覆盖全部历史渠道/LLM 键）；
#   LD_LIBRARY_PATH 仅当引用本仓库时清理；DBus 的两个 IBus/动态库环境键置空
#   ~/.config/autostart/ibus-voice-ime.desktop 与 ~/.local/bin/ibus-voice-ime-autostart
#   及其 ibus-voice-ime-autostart.bak.* 精确前缀备份
#   GNOME sources/mru-sources、IBus preload-engines/engines-order 中的 voice-custom
#   与自定义快捷键（列表及 dconf 子树，包括孤儿槽位）
#   systemd 用户服务 ibus-voice-ime-bt-play-restore.service 及其安装目录
#   ~/.local/lib/ibus-voice-ime/（bt-play-restore.py + __pycache__）
#   $XDG_RUNTIME_DIR/ibus-voice-ime/（voice.sock、clipboard-paste.txt）
#   各类日志/缓存（engine/error/ipc-client/qwen-asr-server/mimo/llm 日志、
#   mimo-offload/ 临时 offload 缓存）——keep 模式下也删除（属运行足迹）
#   运行中的进程：本仓库的 IBus 引擎进程、qwen/mimo sidecar、本仓库拉起的
#   llama-server（全部精确匹配命令行，不误杀其他输入法/无关进程）
#
# --keep-user-data（默认）额外保留（重装可复用，逐项打印）：
#   ~/.local/share/ibus-voice-ime/rime-user/              Rime 用户词库与方案数据
#   ~/.local/share/ibus-voice-ime/rime/                   旧版布局的 Rime 用户数据
#   ~/.local/share/ibus-voice-ime/voice-dictionary.txt    语音热词词典
#   ~/.local/share/ibus-voice-ime/chinese.json            中文记忆词典
#   ~/.local/share/ibus-voice-ime/english.json            英文记忆词典
#   ~/.config/ibus-voice-ime/*.json                     所有用户 JSON 配置（含自定义文件）
#   有效的 VOICE_IME_CONFIG / VOICE_IME_LLM_CONFIG 覆盖文件也保留（不限文件名）
#   ~/.config/ibus-voice-ime/llm.json                     云端 LLM 连接配置
#     ⚠ llm.json 可能含 API key——keep 模式保留时会醒目提示；要彻底清除请用 --purge
#
# --purge 额外删除：上表全部保留项，以及 ~/.local/share/ibus-voice-ime/ 与
#   ~/.config/ibus-voice-ime/ 两个目录本身（llm.json 中的 API key 一并清除）。
#
# 安全边界：绝不删除仓库自身内容（vendor/、.venv*、.qwen-asr-setup.state 等）；
# 绝不 sudo、不改 /usr 与系统文件；不触碰其他输入法的文件（~/.cache/ibus 下
# 其他引擎目录、~/.config/ibus/ 的 ibus 总线套接字、~/.config/pi-secrets/）。
# ibus 缓存通过 `ibus write-cache` 重建刷新（不整删 ~/.cache/ibus）。
# 每一步幂等：条目不存在时按“已跳过”处理；最后做验证扫描，有任何残留以退出码 1 报警。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/, ""); print; next} {exit}' "$0"; }

MODE="keep"
SAW_PURGE=0
SAW_KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge)           SAW_PURGE=1; MODE="purge"; shift ;;
    --keep-user-data)  SAW_KEEP=1; MODE="keep"; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
done
if [[ "$SAW_PURGE" -eq 1 && "$SAW_KEEP" -eq 1 ]]; then
  echo "错误：--purge 与 --keep-user-data 互斥，只能选一个（默认 keep）。" >&2
  exit 2
fi

# ------------------------------------------------------------ 基础与安全 --
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  echo "错误：请勿以 root 运行（会操作错误用户的 HOME；本脚本也绝不使用 sudo）。" >&2
  exit 2
fi
if [[ -z "${HOME:-}" || ! -d "$HOME" ]]; then
  echo "错误：HOME 未设置或不存在，拒绝操作。" >&2
  exit 2
fi

# 配置路径在任何删除之前从三个环境来源快照；不直接使用未经展开的 shell 值。
CONFIG_DIRS=()                 # 词法安装路径，绝不替换成符号链接目标
CONFIG_TARGETS=()              # 与 CONFIG_DIRS 同序的启动快照解析目标
UNTRUSTED_CONFIG_DIRS=()       # 无法证明所有权的目录别名：不清扫、不递归删除
PROJECT_DIR_FOOTPRINTS=("$HOME/.config/ibus-voice-ime" "$HOME/.local/share/ibus-voice-ime"
                        "$HOME/.local/lib/ibus-voice-ime" "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ibus-voice-ime")
COMPONENT_XML="$HOME/.local/share/ibus/component/voice-custom.xml"
ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
DATA_DIR="$HOME/.local/share/ibus-voice-ime"
SERVICE_NAME="ibus-voice-ime-bt-play-restore.service"
SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME"
BT_INSTALL_DIR="$HOME/.local/lib/ibus-voice-ime"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ibus-voice-ime"
IBUS_REGISTRY="$HOME/.cache/ibus/bus/registry"

AUTOSTART_DESKTOP="$HOME/.config/autostart/ibus-voice-ime.desktop"
AUTOSTART_BIN="$HOME/.local/bin/ibus-voice-ime-autostart"
HOME_REAL="$(realpath -e -- "$HOME")"
# 清理与验证统一用一个 glob；dotglob 也覆盖以 .. 开头的隐藏文件。
shopt -s dotglob nullglob

# 配置目录的所有 *.json 都是用户配置，不限于默认文件名。
KEEP_DATA_ENTRIES=(rime-user rime voice-dictionary.txt chinese.json english.json)
KEEP_CONFIG_ENTRIES=('*.json')
OVERRIDE_PATHS=()
CUSTOM_PATHS=()
HOTKEY_TOUCHED='[]'
ENV_TEMP_FILE="$ENV_FILE.new"
SERVICE_LINK="$HOME/.config/systemd/user/default.target.wants/$SERVICE_NAME"
BT_SCRIPT="$BT_INSTALL_DIR/bt-play-restore.py"
# keep 的有效覆盖文件、以及拒绝处理的覆盖路径，不能被父目录清扫间接删除。
# 共享缓存是硬保护：自身、祖先、后代均不得作为删除目标。
PROTECTED_PATHS=("$HOME/.cache/pip" "$HOME/.cache/modelscope" "$HOME/.triton/cache" "$HOME/.modelscope")

DELETED_COUNT=0
SKIPPED_COUNT=0
KEPT_COUNT=0
RESIDUES=()

removed() { printf '  [已删除] %s\n' "$1"; DELETED_COUNT=$((DELETED_COUNT + 1)); }
skipped() { printf '  [已跳过] %s（不存在）\n' "$1"; SKIPPED_COUNT=$((SKIPPED_COUNT + 1)); }
kept()    { printf '  [已保留] %s\n' "$1"; KEPT_COUNT=$((KEPT_COUNT + 1)); }
residue() { printf '  [残留]   %s\n' "$1"; RESIDUES+=("$1"); }

# lstat 不把 EACCES/ENOTDIR/读取失败冒充不存在。0=存在，1=明确 ENOENT，2=无法读取。
path_state() {
  python3 - "$1" <<'PY'
import os
import sys
try:
    os.lstat(sys.argv[1])
except FileNotFoundError:
    raise SystemExit(1)
except OSError:
    raise SystemExit(2)
PY
}

# 启动即预取完整祖先链；/proc/self/status 的打开由本 bash 完成。
declare -A EXCLUDED_PIDS=()
collect_ancestors() {
  local pid="$BASHPID" next key value status='/proc/self/status'
  EXCLUDED_PIDS["$$"]=1
  EXCLUDED_PIDS["$PPID"]=1
  while [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 0 ]]; do
    [[ -z "${EXCLUDED_PIDS[$pid]:-}" || "$status" == '/proc/self/status' ]] || break
    EXCLUDED_PIDS["$pid"]=1
    next=''
    if [[ ! -r "$status" ]]; then
      residue "无法验证：读取祖先进程链 $status 失败；拒绝发信号"
      PROCESS_SIGNALS_OK=0
      break
    fi
    while read -r key value; do
      if [[ "$key" == 'PPid:' ]]; then next="$value"; break; fi
    done < "$status"
    if [[ ! "$next" =~ ^[0-9]+$ ]]; then
      residue "无法验证：祖先进程 PPid 无效；拒绝发信号"
      PROCESS_SIGNALS_OK=0
      break
    fi
    pid="$next"
    status="/proc/$pid/status"
    # PPID 预置在集合中，但仍必须沿它继续上溯。
    if [[ "$pid" == "$PPID" ]]; then unset 'EXCLUDED_PIDS[$pid]'; fi
  done
}
PROCESS_SIGNALS_OK=1
collect_ancestors

# 用安全 JSON 记录传回 bash；不 source/eval 环境文件，也不输出密钥。
snapshot_paths() {
  local snapshot decoded record kind value failed=0
  if ! snapshot="$(python3 - "$ENV_FILE" "$ENV_TEMP_FILE" "$ROOT_DIR" "$DATA_DIR" "${PROJECT_DIR_FOOTPRINTS[@]}" <<'PY'
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

env_file, temp_file, root, data, *footprints = sys.argv[1:]
records = []
# 所有权只认固定安装路径的词法身份，不把其任意链接目标认证为项目目录。
owned_dirs = {os.path.abspath(path) for path in footprints}

def emit(kind, value):
    if '\x00' in value or '\n' in value:
        records.append(('error', '无法验证：路径或环境记录含 NUL/换行'))
    elif (kind, value) not in records:
        records.append((kind, value))

def parse(raw, shell_quoted=False):
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # show-environment 可用 shell 引号包裹整条赋值；environment.d 通常不加引号。
        if shell_quoted and line.startswith(("'", '"')):
            parts = shlex.split(line)
            if len(parts) != 1:
                raise ValueError('环境赋值形状异常')
            line = parts[0]
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('环境赋值格式异常')
        value = value.strip()
        if shell_quoted and value.startswith(("'", '"')):
            value = ' '.join(shlex.split(value))
        result[key] = value
    return result

sources = [dict(os.environ)]
# .new 也可能保留已失效配置路径；在删除它之前同样快照。
for file in (env_file, temp_file):
    try:
        sources.append(parse(Path(file).read_text(encoding='utf-8')))
    except FileNotFoundError:
        pass
    except Exception:
        emit('error', f'无法验证：环境文件快照读取/解析失败 {file}')
try:
    raw = subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True)
    sources.append(parse(raw, shell_quoted=True))
except Exception:
    emit('error', '无法验证：systemctl show-environment 启动快照失败')

def normalize(raw, source):
    raw = raw.strip()
    # 与 config.py / llm_cloud_config.py 同源：只 strip + expanduser，不做 shell 展开。
    expanded = str(Path(raw).expanduser())
    return os.path.realpath(expanded)

bases = [os.path.abspath(os.path.expanduser('~/.config/ibus-voice-ime'))]
for source in sources:
    try:
        xdg = source.get('XDG_CONFIG_HOME', '').strip() or '~/.config'
        base = os.path.abspath(str(Path(xdg).expanduser()) + '/ibus-voice-ime')
        if base not in bases:
            bases.append(base)
        for key in ('VOICE_IME_CONFIG', 'VOICE_IME_LLM_CONFIG'):
            if source.get(key, '').strip():
                emit('config', normalize(source[key], source))
        for key, value in source.items():
            # 路径型覆盖而非 DEVICE_OFFLOAD_TARGET=cpu 等枚举。
            if (key.startswith('VOICE_IME_') and value.strip() and
                (key.endswith(('_DIR', '_LOG', '_USER_DICT', '_DICTIONARY', '_OFFLOAD_PATH'))
                 or ('OFFLOAD' in key and key.endswith(('_PATH', '_FOLDER'))))):
                path = normalize(value, source)
                if not (path == data or path.startswith(data + '/') or
                        path == root or path.startswith(root + '/')):
                    emit('custom', f'{key}: {path}')
                    emit('protect', path)
    except Exception:
        emit('error', '无法验证：配置路径规范化失败')
for base in bases:
    target = os.path.realpath(base)
    emit('base', base)
    # target 必须与 base 一一对应；不能被 emit 的去重打乱数组顺序。
    if '\x00' in target or '\n' in target:
        emit('error', '无法验证：配置目录目标含 NUL/换行')
    else:
        records.append(('target', target))
    if target != base and target not in owned_dirs:
        emit('untrusted-base', base)
        emit('protect', target)
    # 不同环境来源组合的默认路径均列入清理/验证，避免覆盖消失后失去定位。
    emit('config', base + '/config.json')
    emit('config', base + '/llm.json')
# 四层配置也允许 JSON 内的自定义用户数据/日志目录，删除配置前先定位。
path_leaves = {
    ('rime', 'shared_data_dir'), ('rime', 'staging_dir'), ('rime', 'user_data_dir'),
    ('rime', 'log_dir'), ('logging', 'log_dir'), ('logging', 'error_log'),
    ('logging', 'llm_log'), ('text', 'voice_dictionary'),
    ('text', 'chinese_user_dict'), ('text', 'english_user_dict'),
}
for kind, file in list(records):
    if kind != 'config':
        continue
    try:
        config = json.loads(Path(file).read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValueError('配置根节点不是对象')
        for section, leaf in path_leaves:
            value = config.get(section, {})
            if not isinstance(value, dict):
                continue
            value = value.get(leaf)
            if isinstance(value, str) and value.strip():
                path = normalize(value, {})
                if not (path == data or path.startswith(data + '/') or
                        path == root or path.startswith(root + '/')):
                    emit('custom', f'{file} {section}.{leaf}: {path}')
                    emit('protect', path)
    except FileNotFoundError:
        pass
    except Exception:
        emit('error', f'无法验证：自定义数据路径配置读取/解析失败 {file}')
for kind, value in records:
    # shell command substitution 不保留 NUL，因此采用换行分隔安全的 JSON 记录。
    print(json.dumps([kind, value], ensure_ascii=False))
PY
)"; then
    residue "无法验证：配置路径快照失败；拒绝继续卸载"
    return 1
  fi
  # 转码也独立捕获状态；不要用进程替换吞掉子进程读取失败。
  if ! decoded="$(python3 -c 'import json,sys; [sys.stdout.write(k+"\n"+v+"\n") for k,v in map(json.loads, sys.argv[1].splitlines())]' "$snapshot")"; then
    residue "无法验证：配置路径快照转码失败；拒绝继续卸载"
    return 1
  fi
  while IFS= read -r kind && IFS= read -r value; do
    case "$kind" in
      base) CONFIG_DIRS+=("$value") ;;
      target) CONFIG_TARGETS+=("$value") ;;
      untrusted-base) UNTRUSTED_CONFIG_DIRS+=("$value") ;;
      config) OVERRIDE_PATHS+=("$value") ;;
      custom) CUSTOM_PATHS+=("$value") ;;
      protect) PROTECTED_PATHS+=("$value") ;;
      error) residue "$value"; failed=1 ;;
      *) residue "无法验证：未知配置快照记录"; failed=1 ;;
    esac
  done <<< "$decoded"
  if [[ "$failed" -ne 0 || ${#CONFIG_DIRS[@]} -ne ${#CONFIG_TARGETS[@]} ]]; then
    residue "无法验证：配置路径快照失败；拒绝继续卸载"
    return 1
  fi
  for record in "${CUSTOM_PATHS[@]}"; do residue "未清理的自定义路径：$record"; done
  return 0
}

# 固定安装目录也先解析边界（XDG 路径或目录符号链接可能来自外部）。
# 拒绝 HOME 本身、仓库内及其父目录；HOME 外只允许本用户固定运行时目录。
# 也不能经父目录间接删除受保护覆盖路径（同时检查词法路径和符号链接目标）。
safe_tree_target() {
  local target lexical protected resolved protected_lexical
  target="$(realpath -m -- "$1")" || return 1
  lexical="$(realpath -ms -- "$1")" || return 1
  [[ "$target" == "$HOME_REAL/"* || "$target" == "/run/user/$UID/ibus-voice-ime" ]] || return 1
  [[ "$target" != "$ROOT_DIR" && "$target" != "$ROOT_DIR/"* ]] || return 1
  # 目标也不得是仓库的父目录。
  [[ "$ROOT_DIR" != "$target/"* ]] || return 1
  for protected in "${PROTECTED_PATHS[@]}"; do
    resolved="$(realpath -m -- "$protected")" || return 1
    protected_lexical="$(realpath -ms -- "$protected")" || return 1
    if [[ "$resolved" == "$target" || "$protected_lexical" == "$lexical" ||
          "$target" == "$resolved/"* || "$lexical" == "$protected_lexical/"* ]]; then
      return 1
    fi
    if [[ "${2:-}" != "allow-protected-children" && ( "$resolved" == "$target/"* || "$protected_lexical" == "$lexical/"* ) ]]; then
      return 1
    fi
  done
  return 0
}

# unlink 只校验链接所在位置（含父目录的真实身份），绝不把目标当删除对象。
# 共享缓存目标可被安装路径引用，但位于共享缓存内部的链接本身也不能删除。
safe_link_target() {
  local lexical location protected resolved protected_lexical
  lexical="$(realpath -ms -- "$1")" || return 1
  location="$(realpath -m -- "$(dirname -- "$1")")/$(basename -- "$1")" || return 1
  [[ "$location" == "$HOME_REAL/"* || "$location" == "/run/user/$UID/ibus-voice-ime" ]] || return 1
  [[ "$location" != "$ROOT_DIR" && "$location" != "$ROOT_DIR/"* && "$ROOT_DIR" != "$location/"* ]] || return 1
  for protected in "${PROTECTED_PATHS[@]}"; do
    resolved="$(realpath -m -- "$protected")" || return 1
    protected_lexical="$(realpath -ms -- "$protected")" || return 1
    if [[ "$location" == "$resolved" || "$location" == "$resolved/"* || "$resolved" == "$location/"* ||
          "$lexical" == "$protected_lexical" || "$lexical" == "$protected_lexical/"* || "$protected_lexical" == "$lexical/"* ]]; then
      return 1
    fi
  done
}

# 配置目录必须仍与启动快照一致，且不是指向非项目目录的不可信别名。
config_tree_trusted() {
  local index alias current state=0
  path_state "$1" || state=$?
  [[ "$state" -ne 1 ]] || return 0
  [[ "$state" -eq 0 ]] || return 1
  for index in "${!CONFIG_DIRS[@]}"; do
    [[ "$1" == "${CONFIG_DIRS[$index]}" ]] || continue
    current="$(realpath -m -- "$1")" || return 1
    [[ "$current" == "${CONFIG_TARGETS[$index]}" ]] || return 1
    for alias in "${UNTRUSTED_CONFIG_DIRS[@]}"; do [[ "$1" != "$alias" ]] || return 1; done
  done
  return 0
}

rm_path() {  # 仅用于固定安装足迹/目录，绝不接收配置覆盖路径
  local state=0
  path_state "$1" || state=$?
  if [[ "$state" -ne 0 && "$state" -ne 1 ]]; then
    residue "无法验证：删除前读取路径失败 $1（$2）"
  elif [[ "$state" -eq 0 ]]; then
    if [[ -L "$1" ]]; then
      if ! safe_link_target "$1"; then
        residue "安全边界拒绝 unlink：$1（$2）"
      elif [[ -L "$1" ]] && rm -f -- "$1" 2>/dev/null; then
        removed "$1（$2；仅 unlink 符号链接）"
      else
        residue "无法 unlink：$1（$2）"
      fi
      return 0
    fi
    if ! config_tree_trusted "$1" || ! safe_tree_target "$1"; then
      residue "安全边界拒绝删除：$1（$2）"
    elif [[ ! -L "$1" ]] && rm -rf -- "$1" 2>/dev/null; then
      removed "$1（$2）"
    else
      residue "无法删除：$1（$2）"
    fi
  else
    skipped "$1（$2）"
  fi
}

# 环境覆盖配置只能是 HOME 内、仓库外的常规文件；不允许删除目录。
# 在数据目录清扫之前处理，以免 keep 误删自定义文件或 purge 绕过拒绝边界。
process_config_overrides() {
  local path resolved state index
  local delete_paths=() delete_targets=()
  # 先完成所有路径的预检/保护，再删除；后出现的拒绝路径也能保护前面的候选。
  for path in "${OVERRIDE_PATHS[@]}"; do
    state=0
    path_state "$path" || state=$?
    if [[ "$state" -eq 1 ]]; then skipped "$path（配置位置）"; continue; fi
    if [[ "$state" -ne 0 ]]; then
      PROTECTED_PATHS+=("$path")
      residue "无法验证：配置覆盖路径读取失败 $path"
      continue
    fi
    resolved="$(realpath -e -- "$path" 2>/dev/null)" || resolved=''
    if [[ ! -f "$path" || "$resolved" != "$HOME_REAL/"* || "$resolved" == "$ROOT_DIR" || "$resolved" == "$ROOT_DIR/"* ]]; then
      printf '  [警告]   拒绝配置覆盖路径：%s（须为 HOME 内、仓库外的常规文件）\n' "$path"
      PROTECTED_PATHS+=("$path")
      if [[ -e "$path" || -L "$path" ]]; then
        residue "未处理的不安全配置覆盖路径：$path"
      fi
      continue
    fi
    if [[ "$MODE" == "keep" ]]; then
      PROTECTED_PATHS+=("$path")
      kept "$path（配置覆盖位置）"
    else
      delete_paths+=("$path")
      delete_targets+=("$resolved")
    fi
  done
  for index in "${!delete_paths[@]}"; do
    path="${delete_paths[$index]}"
    resolved="${delete_targets[$index]}"
    state=0
    path_state "$path" || state=$?
    if [[ "$state" -eq 1 ]]; then skipped "$path（配置别名已删除）"; continue; fi
    if [[ "$state" -ne 0 ]]; then
      PROTECTED_PATHS+=("$path")
      residue "无法验证：删除前配置覆盖路径读取失败 $path"
      continue
    fi
    if ! safe_tree_target "$path"; then
      # 与 custom/拒绝路径/共享缓存做自身、祖先、后代双身份交集校验。
      PROTECTED_PATHS+=("$path")
      residue "安全边界拒绝删除配置覆盖文件：$path（与保护路径相交）"
    # 删除前重新检查类型、保护边界及解析结果；始终 rm -f，绝不递归。
    elif [[ -f "$path" && "$(realpath -e -- "$path" 2>/dev/null)" == "$resolved" ]] && safe_tree_target "$path" && rm -f -- "$path" 2>/dev/null; then
      removed "$path（配置覆盖位置；仅删除文件）"
    else
      PROTECTED_PATHS+=("$path")
      residue "无法安全删除配置覆盖文件：$path"
    fi
  done
}

regex_escape() { printf '%s' "$1" | sed -e 's/[][\\.*^$()+?{|]/\\&/g'; }
ESCAPED_ROOT="$(regex_escape "$ROOT_DIR")"

step() { printf '\n==> %s\n' "$1"; }

# stop/verify 唯一目标清单：五类仓库进程 + 固定安装的蓝牙守护。
PROCESS_LABELS=('IBus 引擎' 'run-engine.sh 启动器' 'Qwen3-ASR sidecar' 'MiMo sidecar' 'llama-server' '蓝牙恢复守护')
PROCESS_PATTERNS=("$ESCAPED_ROOT/src/ibus_voice_ime/engine\.py" "$ESCAPED_ROOT/run-engine\.sh"
                  'python.*[q]wen_asr_server\.py' 'python.*[m]imo_asr_server\.py'
                  '(^|/)[l]lama-server([[:space:]]|$)' "$(regex_escape "$BT_SCRIPT")")
PROCESS_KINDS=(repo repo repo repo repo bt)
CANDIDATE_PIDS=()

pid_excluded() {
  [[ "$1" == "$$" || "$1" == "$BASHPID" || -n "${EXCLUDED_PIDS[$1]:-}" ]]
}

# 0=归属本项目，1=明确不归属/已退出/祖先，2=无法读取。
pid_owned_by_repo() {
  local pid="$1" kind="${2:-repo}" state=0
  [[ "$pid" =~ ^[0-9]+$ ]] || return 2
  if pid_excluded "$pid"; then return 1; fi
  path_state "/proc/$pid" || state=$?
  [[ "$state" -ne 1 ]] || return 1
  [[ "$state" -eq 0 ]] || return 2
  python3 - "$pid" "$ROOT_DIR" "$BT_SCRIPT" "$kind" <<'PY'
import sys
from pathlib import Path
pid, root, bt, kind = sys.argv[1:]
try:
    args = Path('/proc/' + pid + '/cmdline').read_bytes().split(b'\0')
except FileNotFoundError:
    raise SystemExit(1)
except OSError:
    raise SystemExit(2)
needle = (bt if kind == 'bt' else root + '/').encode()
owned = any(needle in arg or (kind == 'repo' and arg == root.encode()) for arg in args)
raise SystemExit(0 if owned else 1)
PY
}

pid_starttime() {
  local stat rest
  IFS= read -r stat < "/proc/$1/stat" || return 1
  # comm 可能含空格和右括号；最后一个 ') ' 后为 field 3 开始。
  rest="${stat##*) }"
  local fields=()
  read -r -a fields <<< "$rest"
  [[ ${#fields[@]} -ge 20 && "${fields[19]}" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "${fields[19]}"
}

process_candidates() {
  local output rc=0 pid
  CANDIDATE_PIDS=()
  output="$(pgrep -f "$1" 2>/dev/null)" || rc=$?
  if [[ "$rc" -eq 1 ]]; then return 0; fi  # pgrep 的明确零结果
  if [[ "$rc" -ne 0 ]]; then residue "无法验证：pgrep 读取失败（exit=$rc）$2"; return 1; fi
  while IFS= read -r pid; do
    if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
      residue "无法验证：pgrep 返回异常 PID（$2）"; return 1
    fi
    CANDIDATE_PIDS+=("$pid")
  done <<< "$output"
}

signal_if_same() {
  local pid="$1" start="$2" kind="$3" signal="$4" current owned=0 state=0
  if [[ "$PROCESS_SIGNALS_OK" -ne 1 ]] || pid_excluded "$pid"; then return 0; fi
  pid_owned_by_repo "$pid" "$kind" || owned=$?
  if [[ "$owned" -eq 1 ]]; then return 0; fi
  if [[ "$owned" -ne 0 ]]; then residue "无法验证：$signal 前读取 PID $pid 归属失败"; return 0; fi
  if ! current="$(pid_starttime "$pid" 2>/dev/null)"; then
    path_state "/proc/$pid" || state=$?
    if [[ "$state" -ne 1 ]]; then residue "无法验证：$signal 前读取 PID $pid starttime 失败"; fi
    return 0
  fi
  if [[ "$current" != "$start" ]]; then
    residue "PID $pid 已复用（starttime 改变），拒绝 $signal"
    return 0
  fi
  # 紧邻信号再核祖先；不对 reused PID 发信号。
  if ! pid_excluded "$pid" && ! kill "-$signal" -- "$pid" 2>/dev/null; then
    residue "发信号失败：PID $pid SIG$signal"
  fi
}

stop_processes() {  # 标签、候选模式、归属类型来自同一清单
  local label="$1" pattern="$2" kind="$3" pid start owned state
  local pids=() starts=()
  if ! process_candidates "$pattern" "$label"; then return 0; fi
  for pid in "${CANDIDATE_PIDS[@]}"; do
    if pid_excluded "$pid"; then continue; fi
    state=0
    if ! start="$(pid_starttime "$pid" 2>/dev/null)"; then
      path_state "/proc/$pid" || state=$?
      if [[ "$state" -ne 1 ]]; then residue "无法验证：候选 PID $pid starttime 读取失败"; fi
      continue
    fi
    owned=0
    pid_owned_by_repo "$pid" "$kind" || owned=$?
    if [[ "$owned" -eq 2 ]]; then residue "无法验证：候选 PID $pid cmdline 读取失败"; fi
    if [[ "$owned" -ne 0 ]]; then continue; fi
    pids+=("$pid"); starts+=("$start")
    signal_if_same "$pid" "$start" "$kind" TERM
  done
  if [[ ${#pids[@]} -eq 0 ]]; then
    printf '  [已跳过] %s：无已确认归属的候选进程\n' "$label"
    return 0
  fi
  sleep 1
  for i in "${!pids[@]}"; do signal_if_same "${pids[$i]}" "${starts[$i]}" "$kind" KILL; done
  printf '  [待验证] %s：已处理 %s 个候选进程（最终状态见验证扫描）\n' "$label" "${#pids[@]}"
}

printf 'ibus-voice-ime 彻底卸载（模式：%s）\n' "$MODE"
printf '仓库（不触碰）：    %s\n' "$ROOT_DIR"
printf 'voice-custom 组件： %s\n' "$COMPONENT_XML"

# 所有环境快照先于任何删除；已发现的自定义路径不盲删、计入残留。
step "快照 shell / environment.d / systemd 配置路径"
snapshot_paths || exit 1
# 覆盖路径保护必须先于任何安装足迹删除。
step "检查并按模式处理配置覆盖路径（常规文件 / HOME / 仓库边界）"
process_config_overrides

# ------------------------------------------------- 1) systemd 守护服务 --
step "停止并禁用 systemd 用户服务（蓝牙恢复守护）"
if command -v systemctl >/dev/null 2>&1; then
  if UNIT_LOAD="$(systemctl --user show "$SERVICE_NAME" --property=LoadState --value 2>/dev/null)"; then
    if [[ "$UNIT_LOAD" == 'not-found' ]]; then
      printf '  [已跳过] %s（LoadState=not-found；最终仍检查 active/MainPID）\n' "$SERVICE_NAME"
    elif systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null; then
      printf '  [已停止] %s（disable --now；最终读回确认）\n' "$SERVICE_NAME"
    else
      residue "未确认：systemctl disable --now $SERVICE_NAME 失败"
    fi
  else
    residue "无法验证：读取服务 LoadState 失败 $SERVICE_NAME"
  fi
else
  residue "无法验证：systemctl 不可用（服务停止未确认）"
fi

# --------------------------------------- 2) 先解除桌面/IBus 注册（防 respawn） --
step "删除登录 autostart（防止下次登录重新注册输入法）"
rm_path "$AUTOSTART_DESKTOP" "GNOME 登录自启动项"
rm_path "$AUTOSTART_BIN" "IBus 自启动包装器"
for backup in "$AUTOSTART_BIN".bak.*; do
  rm_path "$backup" "IBus 自启动包装器的精确前缀备份"
done
# 清理和验证复用同一组四个键；只过滤精确目标，其他条目的值/顺序不变。
manage_input_sources() {  # $1=clean / verify
  python3 - "$1" <<'PY'
import ast
import subprocess
import sys

failed = False
for schema, key, entry in (
    ('org.gnome.desktop.input-sources', 'sources', ('ibus', 'voice-custom')),
    ('org.gnome.desktop.input-sources', 'mru-sources', ('ibus', 'voice-custom')),
    ('org.freedesktop.ibus.general', 'preload-engines', 'voice-custom'),
    ('org.freedesktop.ibus.general', 'engines-order', 'voice-custom'),
):
    try:
        raw = subprocess.check_output(['gsettings', 'get', schema, key], text=True).strip()
        if raw.startswith('@'):
            raw = raw.split(' ', 1)[1]  # @as [] / @a(ss) []
        values = ast.literal_eval(raw)
        if not isinstance(values, list):
            raise ValueError('不是数组')
        if isinstance(entry, tuple):
            valid = all(isinstance(value, tuple) and len(value) == 2 and
                        all(isinstance(part, str) for part in value) for value in values)
        else:
            valid = all(isinstance(value, str) for value in values)
        if not valid:
            raise ValueError('数组元素类型/形状异常（要求 tuple[str,str] 或 str）')
        remaining = [value for value in values if value != entry]
        if remaining == values:
            print(f'  [干净]   {schema} {key} 无 voice-custom')
        elif sys.argv[1] == 'clean':
            subprocess.run(['gsettings', 'set', schema, key, repr(remaining)], check=True)
            print(f'  [已删除] {schema} {key} 中的 voice-custom（保留其他条目及顺序）')
        else:
            failed = True
            print(f'  [残留]   {schema} {key} 仍含 voice-custom')
    except Exception as exc:
        failed = True
        print(f'  [无法验证/清理] {schema} {key}（{exc}）')
raise SystemExit(1 if failed else 0)
PY
}

step "移除 GNOME / IBus 四个输入源和引擎列表键中的 voice-custom"
if command -v gsettings >/dev/null 2>&1; then
  if ! manage_input_sources clean; then
    residue "GNOME / IBus 输入源键清理失败（见上方）"
  fi
else
  residue "无法验证：gsettings 不可用"
fi

# 在官方 helper 摘除列表之前 reset 子键；同一枚举也扫描 dconf 孤儿槽位。
manage_gnome_hotkeys() {  # $1=clean / verify
  ROOT_ARG="$ROOT_DIR" python3 - "$1" "$HOTKEY_TOUCHED" <<'PY'
import ast
import json
import os
import re
import shutil
import subprocess
import sys

root = os.environ['ROOT_ARG']
names = {'自定义语音输入法语音输入', '自定义语音输入法语音输入（原文免整理）',
         '自定义语音输入法输入法粘贴'}
media = 'org.gnome.settings-daemon.plugins.media-keys'
custom = media + '.custom-keybinding'
base = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'

def get(path, key):
    return ast.literal_eval(subprocess.check_output(
        ['gsettings', 'get', f'{custom}:{path}', key], text=True).strip())

def dconf_paths(path):
    raw = subprocess.check_output(['dconf', 'list', path], text=True)
    result = []
    for child in raw.splitlines():
        if child.endswith('/'):
            slot = path + child
            result.append(slot)
            result.extend(dconf_paths(slot))
    return result

failed = False
paths = []
registered_ok = False
touched = set(json.loads(sys.argv[2]))
# 本进程内已成功 reset（三键已空）的槽位 -> 原始三键值；列表提交失败时整体回滚。
reset_committed = {}
try:
    raw = subprocess.check_output(['gsettings', 'get', media, 'custom-keybindings'],
                                  text=True).strip()
    if raw.startswith('@'):
        raw = raw.split(' ', 1)[1]
    paths = ast.literal_eval(raw)
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError('custom-keybindings 不是路径数组')
    registered_ok = True
except Exception as exc:
    paths = []
    failed = True
    print(f'  [无法验证] 快捷键已注册列表读取失败（{exc}）')
# 枚举失败也不能阻断已注册槽位；已涉及槽位另行追踪，不依赖识别信息。
orphans = []
try:
    if shutil.which('dconf') is None:
        raise RuntimeError('dconf 不可用')
    orphans = dconf_paths(base)
except Exception as exc:
    failed = True
    print(f'  [无法验证] dconf 孤儿快捷键枚举失败（{exc}）')
all_paths = list(dict.fromkeys(paths + orphans + sorted(touched)))
drop = []
for path in all_paths:
    try:
        if not (path.startswith(base) and path.endswith('/') and
                all(part and re.fullmatch(r'[A-Za-z0-9_-]+', part)
                    for part in path[len(base):-1].split('/'))):
            raise ValueError('快捷键槽位路径异常')
        values = {key: get(path, key) for key in ('binding', 'command', 'name')}
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError('快捷键三键终值必须均为字符串')
        command, name = values['command'], values['name']
        own_command = (root + '/') in command
        own_name = name in names or any(name.startswith(n + '（') for n in names)
        if not (own_command or own_name or path in touched):
            continue
        touched.add(path)
        if sys.argv[1] == 'verify':
            print(f'  [读回]   {path} binding/command/name={values!r}')
            if any(values.values()) or path in paths:
                failed = True
                print(f'  [残留]   快捷键槽位三键或列表引用未清除 {path}')
            continue
        # 三键作为事务：首个失败立即停止；恢复原值以保留跨次运行的识别信息。
        slot_ok = True
        reset_keys = []
        for key in ('binding', 'command', 'name'):
            try:
                subprocess.run(['gsettings', 'reset', f'{custom}:{path}', key], check=True)
                reset_keys.append(key)
            except Exception as exc:
                slot_ok = False
                failed = True
                print(f'  [失败]   reset {path} {key}（{exc}）')
                for restore_key in reversed(reset_keys + [key]):
                    try:
                        subprocess.run(['gsettings', 'set', f'{custom}:{path}', restore_key,
                                        repr(values[restore_key])], check=True)
                    except Exception as restore_exc:
                        print(f'  [无法验证] 回滚 {path} {restore_key} 失败（{restore_exc}）')
                break
        if slot_ok:
            def restore_original(label):
                # 恢复原始三键，保住跨次运行的识别信息（避免空槽位被下次运行跳过）。
                failed_restore = False
                for restore_key in ('binding', 'command', 'name'):
                    try:
                        subprocess.run(['gsettings', 'set', f'{custom}:{path}', restore_key,
                                        repr(values[restore_key])], check=True)
                    except Exception as restore_exc:
                        failed_restore = True
                        print(f'  [无法验证] 回滚 {path} {restore_key} 失败（{restore_exc}）')
                if failed_restore:
                    print(f'  [残留]   快捷键槽位 {path} 回滚不完整，需人工复核')
            try:
                terminal = {key: get(path, key) for key in ('binding', 'command', 'name')}
            except Exception as readback_exc:
                failed = True
                print(f'  [无法验证] reset 后三键读回失败 {path}（{readback_exc}）')
                restore_original('readback')
                continue
            if any(value != '' for value in terminal.values()):
                failed = True
                print(f'  [残留]   reset 后三键终值非空 {path}')
                restore_original('terminal')
            else:
                reset_committed[path] = dict(values)
                drop.append(path)
                print(f'  [已删除] 快捷键子树 {path} 的 binding/command/name')
    except Exception as exc:
        failed = True
        print(f'  [无法验证/清理] 快捷键槽位 {path}（{exc}）')
if sys.argv[1] == 'clean' and registered_ok and drop:
    remaining = [path for path in paths if path not in set(drop)]
    if remaining != paths:
        try:
            subprocess.run(['gsettings', 'set', media, 'custom-keybindings', repr(remaining)],
                           check=True)
        except Exception as exc:
            failed = True
            print(f'  [无法验证/清理] 快捷键列表摘除失败（{exc}）')
            # 列表未摘成而三键已清空：恢复原值，保住下次运行的识别入口。
            for restore_path, original in reset_committed.items():
                restore_failed = False
                for restore_key in ('binding', 'command', 'name'):
                    try:
                        subprocess.run(['gsettings', 'set', f'{custom}:{restore_path}', restore_key,
                                        repr(original[restore_key])], check=True)
                    except Exception as restore_exc:
                        restore_failed = True
                        failed = True
                        print(f'  [无法验证] 回滚 {restore_path} {restore_key} 失败（{restore_exc}）')
                if not restore_failed:
                    print(f'  [回滚]   已恢复快捷键槽位原值 {restore_path}')
print('HOTKEY_TOUCHED=' + json.dumps(sorted(touched), ensure_ascii=False))
if not failed:
    print('  [干净]   快捷键列表及 dconf 孤儿槽位无本项目残留')
raise SystemExit(1 if failed else 0)
PY
}

step "移除 GNOME 自定义快捷键（先 reset 子树，再摘列表）"
HOTKEY_CLEAN_OK=0
if command -v gsettings >/dev/null 2>&1; then
  HOTKEY_OUTPUT=''
  if HOTKEY_OUTPUT="$(manage_gnome_hotkeys clean)"; then HOTKEY_CLEAN_OK=1; fi
  printf '%s\n' "$HOTKEY_OUTPUT"
  while IFS= read -r line; do
    if [[ "$line" == HOTKEY_TOUCHED=* ]]; then HOTKEY_TOUCHED="${line#HOTKEY_TOUCHED=}"; fi
  done <<< "$HOTKEY_OUTPUT"
  if [[ "$HOTKEY_CLEAN_OK" -ne 1 ]]; then residue "自定义快捷键事务失败或孤儿槽位无法扫描；不调用官方 helper"; fi
else
  residue "无法验证：gsettings 不可用（快捷键）"
fi
# 只有完整清理成功才允许 helper 摘列表；失败槽位保留识别信息。
if [[ "$HOTKEY_CLEAN_OK" -eq 1 ]]; then
  for helper in uninstall-gnome-voice-hotkey.sh uninstall-gnome-clipboard-paste-hotkey.sh; do
    if [[ -f "$ROOT_DIR/scripts/$helper" ]]; then
      if ! bash "$ROOT_DIR/scripts/$helper" 2>/dev/null; then residue "未确认：官方热键 helper 失败 $helper"; fi
    fi
  done
fi

step "移除 IBus 组件注册（voice-custom.xml）"
rm_path "$COMPONENT_XML" "IBus 组件"
# 重建 ibus 注册缓存（~/.cache/ibus/bus/registry），让 voice-custom 从缓存消失。
# 显式清掉 IBUS_COMPONENT_PATH，避免本 shell 继承值影响重建结果。
if command -v ibus >/dev/null 2>&1; then
  if env -u IBUS_COMPONENT_PATH env -u LD_LIBRARY_PATH ibus write-cache >/dev/null 2>&1; then
    printf '  [已刷新] ibus write-cache（最终读回 registry）\n'
  else
    residue "未确认：ibus write-cache 失败"
  fi
else
  residue "未确认：ibus 命令不可用，无法刷新注册缓存"
fi

# ------------------------------------------------------------ 3) 停进程 --
step "停止运行中的引擎与 sidecar 进程（精确匹配命令行）"
# 顺序在解除注册之后：组件/输入源已删，ibus-daemon 不会再拉起引擎。
for i in "${!PROCESS_LABELS[@]}"; do
  stop_processes "${PROCESS_LABELS[$i]}" "${PROCESS_PATTERNS[$i]}" "${PROCESS_KINDS[$i]}"
done

# ------------------------------------------- 4) systemd 服务文件与安装目录 --
step "删除 systemd 服务文件与守护脚本拷贝目录"
rm_path "$SERVICE_LINK" "蓝牙恢复 default.target.wants 精确链接"
rm_path "$SERVICE_FILE" "蓝牙恢复 systemd 用户服务"
rm_path "$BT_INSTALL_DIR" "bt-play-restore 脚本安装目录（含 __pycache__）"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user daemon-reload 2>/dev/null; then
    printf '  [已刷新] systemctl --user daemon-reload\n'
  else
    residue "未确认：systemctl daemon-reload 失败"
  fi
  # 不调用 reset-failed：已不存在的服务会返回失败，且不是卸载所需状态变更。
fi

# --------------------------------------- 5) environment.d 与用户管理器环境 --
step "删除 environment.d 配置并清理 systemd 用户管理器环境"
rm_path "$ENV_FILE" "environment.d 持久环境"
rm_path "$ENV_TEMP_FILE" "environment.d 安装中断临时文件（可能含历史密钥）"
manage_manager_environment() {  # clean / verify；解析失败不得当作无键
  python3 - "$1" "$ROOT_DIR" <<'PY'
import re
import shlex
import subprocess
import sys
try:
    raw = subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True)
    keys = []
    for line in raw.splitlines():
        if line.startswith(("'", '"')):
            parts = shlex.split(line)
            if len(parts) != 1:
                raise ValueError('环境赋值形状异常')
            line = parts[0]
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('环境赋值格式异常')
        if (key.startswith('VOICE_IME_') or key in ('IBUS_COMPONENT_PATH', 'PYTORCH_CUDA_ALLOC_CONF')
            or (key == 'LD_LIBRARY_PATH' and sys.argv[2] in value)):
            keys.append(key)
    if keys and sys.argv[1] == 'clean':
        subprocess.run(['systemctl', '--user', 'unset-environment', *keys], check=True)
        # environment.d 导入的"初始环境"键 unset 无效；env 文件已删除，
        # daemon-reexec 让管理器重读配置后这些键自然消失（对运行单元无影响）。
        raw2 = subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True)
        left = [line.partition('=')[0] for line in raw2.splitlines()
                if line and not line.startswith(("'", '"'))]
        left_quoted = []
        for line in raw2.splitlines():
            if line.startswith(("'", '"')):
                left_quoted.append(shlex.split(line)[0].partition('=')[0])
        remaining = [k for k in left + left_quoted if k in set(keys)]
        if remaining:
            subprocess.run(['systemctl', '--user', 'daemon-reexec'], check=True)
            # reexec 后再补一次 unset（清掉 DBus 置空产生的空值键等）。
            raw3 = subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True)
            still = []
            for line in raw3.splitlines():
                if line.startswith(("'", '"')):
                    line = shlex.split(line)[0]
                k = line.partition('=')[0]
                if k in set(keys):
                    still.append(k)
            if still:
                subprocess.run(['systemctl', '--user', 'unset-environment', *still], check=True)
        print(f'  [已清理] systemd 用户管理器环境：{len(keys)} 个键（最终读回确认）')
    elif keys:
        # 只打印键名，不泄露 API key 值。
        print('  [残留] 用户管理器环境键：' + ', '.join(keys))
        raise SystemExit(1)
    else:
        print('  [干净] systemd 用户管理器环境无相关键及仓库 LD_LIBRARY_PATH')
except Exception as exc:
    print(f'  [无法验证/清理] systemd 用户管理器环境（{type(exc).__name__}）')
    raise SystemExit(1)
PY
}
if ! manage_manager_environment clean; then residue "无法验证/清理：systemd 用户管理器环境"; fi

# DBus activation 环境没有 unset 接口；置空这两个变量，禁止随后激活继承旧路径。
if command -v dbus-update-activation-environment >/dev/null 2>&1; then
  if dbus-update-activation-environment IBUS_COMPONENT_PATH= LD_LIBRARY_PATH= 2>/dev/null; then
    # dbus-update-activation-environment 会同步写 systemd 用户管理器，
    # 置空键会以空值回流管理器环境；随后 unset 清掉这两把空键。
    systemctl --user unset-environment IBUS_COMPONENT_PATH LD_LIBRARY_PATH 2>/dev/null || true
    printf '  [已清理] DBus activation 环境 IBUS_COMPONENT_PATH / LD_LIBRARY_PATH（置空）\n'
  else
    residue "无法清空 DBus activation 环境 IBUS_COMPONENT_PATH / LD_LIBRARY_PATH"
  fi
else
  residue "未确认：dbus-update-activation-environment 不可用，无法清空激活环境"
fi

# keep 清扫与验证使用同一保留判定，支持 *.json 与目录内的有效覆盖文件。
entry_is_kept() {  # $1=条目 其余参数=保留模式
  local entry="$1" name keeper protected; shift
  name="${entry##*/}"
  for keeper in "$@"; do
    # 模式有意不加引号：只有调用方提供的固定保留模式会参与匹配。
    if [[ "$name" == $keeper ]]; then return 0; fi
  done
  if [[ -f "$entry" ]]; then
    for protected in "${PROTECTED_PATHS[@]}"; do
      if [[ "$entry" == "$protected" || "$(realpath -m -- "$entry")" == "$(realpath -m -- "$protected")" ]]; then
        return 0
      fi
    done
  fi
  return 1
}

# keep 覆盖文件的祖先目录需要递归清扫/验收，不把整个 cache 报作运行残留。
entry_contains_protected() {
  local lexical resolved protected protected_lexical protected_resolved
  lexical="$(realpath -ms -- "$1")" || return 1
  resolved="$(realpath -m -- "$1")" || return 1
  for protected in "${PROTECTED_PATHS[@]}"; do
    protected_lexical="$(realpath -ms -- "$protected")" || return 1
    protected_resolved="$(realpath -m -- "$protected")" || return 1
    if [[ "$protected_lexical" == "$lexical/"* || "$protected_resolved" == "$resolved/"* ]]; then return 0; fi
  done
  return 1
}

# 枚举目录必须确认读取成功；glob 空展开无法区分空目录与 EACCES。
DIR_ENTRIES=()
enumerate_dir() {
  local output decoded
  DIR_ENTRIES=()
  if ! output="$(python3 - "$1" <<'PY'
import json
import os
import sys
try:
    names = os.listdir(sys.argv[1])
    entries = [os.path.join(sys.argv[1], name) for name in names]
    if any('\n' in entry or '\x00' in entry for entry in entries):
        raise ValueError('条目含换行/NUL')
    print(json.dumps(entries, ensure_ascii=False))
except FileNotFoundError:
    print('[]')
except (OSError, ValueError):
    raise SystemExit(1)
PY
)"; then
    residue "无法验证：目录枚举失败 $1"
    return 1
  fi
  if ! decoded="$(python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])))' "$output")"; then
    residue "无法验证：目录枚举转码失败 $1"
    return 1
  fi
  if [[ -n "$decoded" ]]; then mapfile -t DIR_ENTRIES <<< "$decoded"; fi
  return 0
}

# keep 模式的目录清扫：保留白名单条目，其余删除（含隐藏文件）；目录被清空时顺手移除。
sweep_dir_whitelist() {  # $1=目录 其余参数=保留名单
  local dir="$1"; shift
  local entry
  # 不对目录链接 listdir：即使可信链接也只 unlink 安装路径，不清扫目标。
  if [[ -L "$dir" ]]; then rm_path "$dir" "配置/数据目录链接"; return 0; fi
  if ! config_tree_trusted "$dir" || ! safe_tree_target "$dir" allow-protected-children; then
    residue "安全边界拒绝清扫目录：$dir"
    return 0
  fi
  if ! enumerate_dir "$dir"; then return 0; fi
  for entry in "${DIR_ENTRIES[@]}"; do
    if entry_is_kept "$entry" "$@"; then
      kept "$entry"
    elif [[ -d "$entry" && ! -L "$entry" ]] && entry_contains_protected "$entry"; then
      sweep_dir_whitelist "$entry" "$@"
    else
      rm_path "$entry" "保留名单外的日志/缓存/套接字等运行足迹"
    fi
  done
  if safe_tree_target "$dir"; then rmdir "$dir" 2>/dev/null || true; fi
}

# ------------------------------------------------------------ 6) 用户数据 --
step "用户数据（模式：$MODE）"
if [[ "$MODE" == "purge" ]]; then
  rm_path "$DATA_DIR" "用户数据目录（词库/词典/日志/缓存全量）"
  for config_dir in "${CONFIG_DIRS[@]}"; do
    rm_path "$config_dir" "用户配置目录（全部 JSON 配置，含 API key）"
  done
else
  echo "  数据目录白名单保留（其余全部删除）："
  sweep_dir_whitelist "$DATA_DIR" "${KEEP_DATA_ENTRIES[@]}"
  for config_dir in "${CONFIG_DIRS[@]}"; do
    echo "  配置目录所有 *.json 与有效覆盖文件保留（其余删除）："
    sweep_dir_whitelist "$config_dir" "${KEEP_CONFIG_ENTRIES[@]}"
  done
  printf '  ⚠ 保留的 JSON/配置覆盖文件可能含 API key！彻底清除请运行：./uninstall-all.sh --purge\n'
fi

# ------------------------------------------------------ 7) 运行时目录 --
step "删除运行时目录（IPC 套接字 / 剪贴板暂存）"
rm_path "$RUNTIME_DIR" "XDG 运行时目录（voice.sock、clipboard-paste.txt）"

# ------------------------------------------------------------ 8) 重启 IBus --
step "重启 IBus 让组件移除生效"
# GNOME 沿用完整替换配方；其他桌面走 ibus restart（与 uninstall.sh 同款，
# 避免 --replace/--panel disable 影响 DE 自管的 ibus 会话）。
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* || "${XDG_CURRENT_DESKTOP:-}" == *gnome* ]]; then
  if command -v ibus-daemon >/dev/null 2>&1; then
    if env -u IBUS_COMPONENT_PATH env -u LD_LIBRARY_PATH ibus-daemon -drx --replace --panel disable --cache refresh >/dev/null 2>&1; then
      printf '  [已重启] ibus-daemon（GNOME 替换配方，--cache refresh）\n'
    else
      residue "未确认：ibus-daemon 重启失败"
    fi
  else
    residue "未确认：未找到 ibus-daemon，无法重启"
  fi
elif command -v ibus >/dev/null 2>&1; then
  if env -u IBUS_COMPONENT_PATH env -u LD_LIBRARY_PATH ibus restart >/dev/null 2>&1; then
    printf '  [已重启] ibus restart\n'
  else
    residue "未确认：ibus restart 失败"
  fi
else
  residue "未确认：未找到 ibus / ibus-daemon，无法重启"
fi

# ------------------------------------------------------------ 9) 验证扫描 --
step "验证扫描（残留将以非零退出码报警）"

verify_absent() {  # 只接受明确 ENOENT；权限不足或读取失败不算干净
  local state=0
  path_state "$1" || state=$?
  case "$state" in
    0) residue "仍存在：$1（$2）" ;;
    1) printf '  [干净]   %s\n' "$1" ;;
    *) residue "无法验证：路径读取失败 $1（$2）" ;;
  esac
}

# keep 模式：目录允许存在，但白名单之外的条目必须已删净（含隐藏文件）。
verify_whitelist_clean() {  # $1=目录 其余参数=保留名单
  local dir="$1"; shift
  local entry dirty=0
  if [[ -L "$dir" ]] || ! config_tree_trusted "$dir" || ! safe_tree_target "$dir" allow-protected-children; then
    residue "安全边界拒绝验证清扫目录：$dir"
    return 0
  fi
  if ! enumerate_dir "$dir"; then return 0; fi
  for entry in "${DIR_ENTRIES[@]}"; do
    if entry_is_kept "$entry" "$@"; then
      continue
    elif [[ -d "$entry" && ! -L "$entry" ]] && entry_contains_protected "$entry"; then
      verify_whitelist_clean "$entry" "$@"
    else
      residue "仍存在：$entry（keep 模式下应已删除的运行足迹）"
      dirty=1
    fi
  done
  if [[ "$dirty" -eq 1 ]]; then
    return 0
  fi
  local state=0
  path_state "$dir" || state=$?
  if [[ "$state" -eq 0 && -d "$dir" ]]; then
    printf '  [干净]   %s（仅剩白名单条目）\n' "$dir"
  elif [[ "$state" -eq 1 ]]; then
    printf '  [干净]   %s（目录明确不存在）\n' "$dir"
  else
    residue "无法验证：白名单目录读取失败或类型变化 $dir"
  fi
}

verify_absent "$COMPONENT_XML" "IBus 组件"
verify_absent "$ENV_FILE" "environment.d 配置"
verify_absent "$ENV_TEMP_FILE" "environment.d 安装中断临时文件（历史密钥）"
verify_absent "$AUTOSTART_DESKTOP" "GNOME 登录自启动项"
verify_absent "$AUTOSTART_BIN" "IBus 自启动包装器"
if enumerate_dir "${AUTOSTART_BIN%/*}"; then
  for backup in "${DIR_ENTRIES[@]}"; do
    if [[ "$backup" == "$AUTOSTART_BIN".bak.* ]]; then
      verify_absent "$backup" "自启动包装器精确前缀备份"
    fi
  done
fi
verify_absent "$SERVICE_LINK" "default.target.wants 服务精确链接"
verify_absent "$SERVICE_FILE" "systemd 服务文件"
verify_absent "$BT_INSTALL_DIR" "守护脚本安装目录"
verify_absent "$RUNTIME_DIR" "运行时目录"
if [[ "$MODE" == "purge" ]]; then
  verify_absent "$DATA_DIR" "用户数据目录"
  for config_dir in "${CONFIG_DIRS[@]}"; do verify_absent "$config_dir" "用户配置目录"; done
  for config_path in "${OVERRIDE_PATHS[@]}"; do verify_absent "$config_path" "快照配置位置（可能含 API key）"; done
else
  verify_whitelist_clean "$DATA_DIR" "${KEEP_DATA_ENTRIES[@]}"
  for config_dir in "${CONFIG_DIRS[@]}"; do verify_whitelist_clean "$config_dir" "${KEEP_CONFIG_ENTRIES[@]}"; done
fi

# 清理与验证同源：四个输入源键，以及列表内和孤儿快捷键子树。
if command -v gsettings >/dev/null 2>&1; then
  if ! manage_input_sources verify; then
    RESIDUES+=("GNOME / IBus 四个输入源键残留或无法验证")
  fi
  if ! manage_gnome_hotkeys verify; then
    RESIDUES+=("自定义快捷键列表/孤儿子树残留或无法验证")
  fi
else
  residue "无法验证：gsettings 不可用"
fi

# systemd 用户管理器环境与单元：单 unit 查询无 pipefail/grep -q SIGPIPE。
if ! manage_manager_environment verify; then residue "无法验证或残留：systemd 用户管理器环境"; fi
if command -v systemctl >/dev/null 2>&1; then
  UNIT_FILES_RC=0
  UNIT_FILES="$(systemctl --user list-unit-files "$SERVICE_NAME" --no-legend --no-pager 2>/dev/null)" || UNIT_FILES_RC=$?
  if [[ "$UNIT_FILES_RC" -eq 0 ]]; then
    if [[ -n "$UNIT_FILES" ]]; then residue "systemd 单元仍存在：$SERVICE_NAME"; else printf '  [干净]   单元文件列表无 %s\n' "$SERVICE_NAME"; fi
  elif [[ "$UNIT_FILES_RC" -eq 1 && -z "$UNIT_FILES" ]]; then
    # list-unit-files 对精确指定且不存在的单元返回 rc=1 且无输出（"No files found"）。
    printf '  [干净]   单元文件列表无 %s\n' "$SERVICE_NAME"
  else
    residue "无法验证：systemctl 单 unit 文件查询失败"
  fi
  verify_service_state() {
    local active_rc=0 active_state load='' main_pid='' file_state=0 link_state=0
    if ! load="$(systemctl --user show "$SERVICE_NAME" --property=LoadState --value 2>/dev/null)"; then
      residue "无法验证：服务 LoadState 读取失败"
      load=''
    fi
    if main_pid="$(systemctl --user show "$SERVICE_NAME" --property=MainPID --value 2>/dev/null)"; then
      if [[ "$main_pid" == '0' ]]; then printf '  [干净]   服务 MainPID=0\n'; else residue "服务 MainPID 未归零或无效：$main_pid"; fi
    else
      residue "无法验证：服务 MainPID 读取失败"
      main_pid=''
    fi
    path_state "$SERVICE_FILE" || file_state=$?
    path_state "$SERVICE_LINK" || link_state=$?
    active_state="$(systemctl --user is-active "$SERVICE_NAME" 2>/dev/null)" || active_rc=$?
    if [[ "$active_rc" -eq 0 && "$active_state" == 'active' ]]; then
      residue "systemd 服务仍 active：$SERVICE_NAME"
    # 已删除服务的独立成功条件；is-active 可返回 inactive/4，不能套用 inactive/3。
    elif [[ "$load" == 'not-found' && "$file_state" -eq 1 && "$link_state" -eq 1 && "$main_pid" == '0' ]]; then
      printf '  [干净]   %s 已删除（not-found；无 unit/wants；MainPID=0）\n' "$SERVICE_NAME"
    elif [[ "$active_state" == 'inactive' && "$active_rc" -eq 3 ]]; then
      printf '  [干净]   %s inactive\n' "$SERVICE_NAME"
    else
      residue "无法验证：服务必须确认为 inactive 或已删除（state=$active_state exit=$active_rc）"
    fi
  }
  verify_service_state
else
  residue "无法验证：systemctl 不可用（服务状态）"
fi

# registry 使用完整读取而非 grep -q：即使命中也不掩盖后续 I/O 错误。
REGISTRY_STATE=0
path_state "$IBUS_REGISTRY" || REGISTRY_STATE=$?
if [[ "$REGISTRY_STATE" -eq 0 ]]; then
  REGISTRY_RC=0
  grep -a -e 'voice-custom' -e 'VoiceCustom' "$IBUS_REGISTRY" >/dev/null 2>&1 || REGISTRY_RC=$?
  case "$REGISTRY_RC" in
    0) residue "ibus 注册缓存仍含 voice-custom：$IBUS_REGISTRY" ;;
    1) printf '  [干净]   ibus 注册缓存无 voice-custom（%s）\n' "$IBUS_REGISTRY" ;;
    *) residue "无法验证：ibus registry 读取失败（exit=$REGISTRY_RC）" ;;
  esac
elif [[ "$REGISTRY_STATE" -eq 1 ]]; then
  printf '  [干净]   ibus registry 明确不存在（%s）\n' "$IBUS_REGISTRY"
else
  residue "无法验证：ibus registry 路径读取失败"
fi

# 进程残留：与 stop 完全同源（包括启动器/llama/bt），读取错误独立记账。
for i in "${!PROCESS_LABELS[@]}"; do
  label="${PROCESS_LABELS[$i]}"
  n=0; unknown=0
  if ! process_candidates "${PROCESS_PATTERNS[$i]}" "$label"; then continue; fi
  for pid in "${CANDIDATE_PIDS[@]}"; do
    owned=0
    pid_owned_by_repo "$pid" "${PROCESS_KINDS[$i]}" || owned=$?
    if [[ "$owned" -eq 0 ]]; then n=$((n + 1)); fi
    if [[ "$owned" -eq 2 ]]; then unknown=1; residue "无法验证：$label 候选 PID $pid cmdline 读取失败"; fi
  done
  if [[ "$n" -gt 0 ]]; then
    residue "$label 仍有 $n 个进程在运行"
  elif [[ "$unknown" -eq 0 ]]; then
    printf '  [干净]   %s：无本项目运行进程\n' "$label"
  fi
done

# ------------------------------------------------------------ 汇总 --
printf '\n============================================\n'
printf '卸载完成（模式：%s）。\n' "$MODE"
printf '已删除 %s 项；已跳过（本不存在）%s 项；已保留 %s 项。\n' \
  "$DELETED_COUNT" "$SKIPPED_COUNT" "$KEPT_COUNT"
if [[ "$MODE" == "keep" ]]; then
  printf '保留的个人数据（重装可复用）：\n'
  printf '  %s/{%s}\n' "$DATA_DIR" "$(IFS=,; echo "${KEEP_DATA_ENTRIES[*]}")"
  for config_dir in "${CONFIG_DIRS[@]}"; do printf '  %s/{%s}\n' "$config_dir" "$(IFS=,; echo "${KEEP_CONFIG_ENTRIES[*]}")"; done
fi
printf '共享缓存未清理（跨项目共用）：\n'
printf '  %s\n' "$HOME/.triton/cache" "$HOME/.cache/pip" "$HOME/.cache/modelscope" "$HOME/.modelscope"
printf '注意：仓库本身未做任何改动（vendor/、.venv* 等仍在 %s）。\n' "$ROOT_DIR"
if [[ ${#RESIDUES[@]} -gt 0 ]]; then
  printf '\n⚠ 发现 %s 处残留（见上方 [残留] 行），请人工处理：\n' "${#RESIDUES[@]}"
  for item in "${RESIDUES[@]}"; do printf '  - %s\n' "$item"; done
  exit 1
fi
printf '已列出的卸载目标验证通过；keep 个人数据、仓库与上述共享缓存不在删除范围。\n'
exit 0
