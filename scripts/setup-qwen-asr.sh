#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv-qwen-asr"
# 会话环境自愈（与 run-engine.sh 同语义）：旧克隆/旧 venv 残留的路径型渠道键
# 指向已消失的路径时必须忽略，否则验收会去执行一个不存在的解释器并误报失败。
# 必须在 MODELS_DIR 等派生变量之前执行。注意：父进程（如 setup.sh）的环境无法
# 被子进程修改——这里的 unset 只作用于本进程及其子进程；持久来源由 install.sh
# 清理 environment.d 与 systemd 用户环境，重开终端/重新登录后不再出现。
QWEN_PATH_KEYS=(VOICE_IME_QWEN_ASR_PYTHON VOICE_IME_QWEN_ASR_MODEL_PATH VOICE_IME_QWEN_ASR_MODELS_DIR)
eval "$("$ROOT_DIR/scripts/env-sanitize.sh" "${QWEN_PATH_KEYS[@]}")"
MODELS_DIR="${VOICE_IME_QWEN_ASR_MODELS_DIR:-$ROOT_DIR/vendor/models/qwen3-asr}"
# 共享模型缓存：分档真实数据统一放这里，仓库 vendor 下只建符号链接（既有
# vendor 路径语义不变，引擎/验收/REUSE 判据照旧读 vendor 路径）。跨克隆复用，
# 卸载或更换克隆目录不丢 6.2GB 权重；VOICE_IME_MODEL_CACHE 可自定义位置。
MODEL_CACHE="${VOICE_IME_MODEL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ibus-voice-ime/models}"
[[ "$MODEL_CACHE" == /* ]] || MODEL_CACHE="$ROOT_DIR/$MODEL_CACHE"
ENABLE_NOW="${VOICE_IME_ENABLE_QWEN_ASR:-1}"
BOOTSTRAP="$(command -v python3)"
CHECK="$ROOT_DIR/scripts/qwen-preflight.py"
FORCE=0 VERIFY_ONLY=0 PROXY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --proxy) [[ $# -ge 2 ]] || { echo "--proxy 需要参数" >&2; exit 2; }; PROXY="$2"; shift 2 ;;
    --proxy=*) PROXY="${1#--proxy=}"; shift ;;
    -h|--help)
      echo "用法：$0 [--verify-only] [--force] [--proxy http://127.0.0.1:7890]"
      echo "默认自动准备仓库内 Python 3.12（含头文件）及 uv；不安装系统包。--force 仅跳过 GPU 门控，无 GPU 时只准备不启用。"
      echo "--verify-only 不下载/不改配置：静态检查 + CUDA/Triton + 模型短音频推理。"
      echo "--proxy 也接受 socks5:///socks5h://（会先检查 venv PySocks，缺失时用 curl 受控自举，VOICE_IME_PIP_SOCKS_BOOTSTRAP=0 可关）。"
      exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
done
# --proxy 归一：校验 scheme 后把全部 6 个代理变量（http/https × 大小写 + all × 大小写）
# 统一为该值。为什么连 all_proxy 也要覆盖而不是只 export 4 个：pip 对 https 请求在
# scheme 专属变量为空时回退 all_proxy（requests select_proxy），残留的 socks
# all_proxy 会让 venv pip（无 PySocks）在建连前抛 Missing dependencies for SOCKS
# support（根因调查 B1-B7 实证：与代理进程死活无关，用户“关代理”无效）。
# no_proxy/NO_PROXY 保留用户取值——排除列表语义不受归一影响。
if [[ -n "$PROXY" ]]; then
  if [[ "$PROXY" != *"://"* ]]; then
    echo "--proxy 需要 scheme://host:port 形式（如 http://127.0.0.1:7890）：$PROXY" >&2
    exit 2
  fi
  case "${PROXY%%://*}" in
    http|https|socks5|socks5h) ;;
    *) echo "--proxy 不支持的协议：${PROXY%%://*}://（允许 http/https/socks5/socks5h；SOCKS 依赖 venv 内 PySocks，安装期会检查并自举）" >&2; exit 2 ;;
  esac
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" all_proxy="$PROXY" ALL_PROXY="$PROXY"
fi
# 只读验收（--verify-only）在任何下载集合/默认模型变量解析之前完成：
# 验收读取的是实际持久化的 sidecar 配置，不应被下载集合变量阻断（红队发现）。
TIMEOUT="${VOICE_IME_QWEN_ASR_VERIFY_TIMEOUT:-180}"
# 下载到缓存内的临时目录（.part-*）；中断/失败时由 finish() 统一清理。
PART_DIRS=()
# 共享缓存分档锁：安装锁（fd 9）是每克隆的，挡不住跨克隆对同一共享缓存的
# TOCTOU（红队实测并发 mv 嵌套）；分档 flock 在缓存目录上跨克隆串行化。
CACHE_LOCK_FD=8
cache_lock() {  # <size>
  mkdir -p -- "$MODEL_CACHE"
  eval "exec $CACHE_LOCK_FD>\"\$MODEL_CACHE/.lock-Qwen3-ASR-\$1\""
  flock "$CACHE_LOCK_FD" || { echo "无法锁定共享缓存（$MODEL_CACHE）。" >&2; exit 1; }
}
cache_unlock() {
  flock -u "$CACHE_LOCK_FD" 2>/dev/null || true
  eval "exec $CACHE_LOCK_FD>&-" 2>/dev/null || true
}

# 验收 receipt：记录「何时用哪套解释器/依赖/模型通过了 smoke 验收」，doctor
# 匹配当前实际（reqs sha256/解释器/模型清单）时把 WARN 降为 INFO。纯附加产物：
# 写入失败只提示，绝不阻断验收成功。
write_verify_receipt() {  # <python> <model_dir>
  local receipt="$VENV/verify-receipt.json"
  if [[ ! -d "$VENV" || -L "$VENV" ]]; then
    echo "验收 receipt 未写入：$VENV 不是常规目录（--verify-only 不创建 venv）。"
    return 0
  fi
  if ! "$BOOTSTRAP" - "$receipt" "$1" "$2" "$ROOT_DIR/requirements-qwen-asr.txt" \
      "$ROOT_DIR" "${GPU_BRIEF:-unknown}" "${VOICE_IME_QWEN_ASR_DTYPE:-bfloat16}" <<'PY'
import hashlib, json, os, sys, time
receipt_path, python, model_path, reqs_path, root_dir, gpu, dtype = sys.argv[1:8]
def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()
files = {}
for name in sorted(os.listdir(model_path)):
    full = os.path.join(model_path, name)
    if os.path.isfile(full):
        status = os.stat(full)
        # mtime_ns：秒级 mtime 挡不住同秒篡改（红队实测同长度替换+还原秒
        # 可骗过 INFO）；保留 mtime 字段仅为兼容旧 receipt（旧 receipt 会因
        # 无 mtime_ns 匹配失败 → 恢复 WARN，属安全方向的失效）。
        files[name] = {'size': status.st_size, 'mtime': int(status.st_mtime),
                       'mtime_ns': status.st_mtime_ns}
receipt = {
    'stage': 'smoke', 'python': python, 'reqs_sha256': sha256(reqs_path),
    'model_path': model_path, 'model_files': files, 'gpu': gpu, 'dtype': dtype,
    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'root_dir': root_dir,
}
tmp = receipt_path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as stream:
    json.dump(receipt, stream, ensure_ascii=False, indent=2, sort_keys=True)
os.replace(tmp, receipt_path)
PY
  then
    echo "⚠ 验收 receipt 写入失败（不影响验收结果）：$receipt" >&2
    return 0
  fi
  echo "验收 receipt 已写入：$receipt"
}

if [[ $VERIFY_ONLY -eq 1 ]]; then
  # Match the launcher's persisted overrides, without sourcing shell syntax or
  # ever modifying environment.d / JSON. Values containing spaces remain safe.
  if [[ -f "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
    eval "$(bash "$ROOT_DIR/scripts/env-file-load.sh" "$HOME/.config/environment.d/ibus-voice-ime.conf" | grep -E '^export (VOICE_IME_[A-Z0-9_]+|PYTORCH_CUDA_ALLOC_CONF)=' || true)"
  fi
  TARGETS="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$BOOTSTRAP" -c 'from ibus_voice_ime.asr import qwen_asr_runtime as r; print(r._python()); print(r.model_id())')"
  mapfile -t CHECK_TARGETS <<< "$TARGETS"
  [[ ${#CHECK_TARGETS[@]} -eq 2 ]] || { echo "无法解析实际 sidecar 配置" >&2; exit 1; }
  GPU_STATE="" GPU_REASON="" GPU_ACTION="" GPU_BRIEF=""
  if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
    eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
  fi
  if "$BOOTSTRAP" "$CHECK" --python "${CHECK_TARGETS[0]}" --stage smoke --model "${CHECK_TARGETS[1]}" --timeout "$TIMEOUT"; then
    write_verify_receipt "${CHECK_TARGETS[0]}" "${CHECK_TARGETS[1]}"
    exit 0
  fi
  exit 1
fi

# 未指定集合时保持旧行为：两档均下载。显式空集合与未知分档须在任何安装前拒绝。
QWEN_SIZES=()
SIZES_VALUE="${VOICE_IME_QWEN_ASR_SIZES-0.6B 1.7B}"
read -r -a QWEN_SIZES <<< "${SIZES_VALUE//$'\n'/ }" || true
[[ ${#QWEN_SIZES[@]} -gt 0 ]] || { echo "无效的 VOICE_IME_QWEN_ASR_SIZES：集合不能为空（支持 0.6B / 1.7B）" >&2; exit 2; }
for size in "${QWEN_SIZES[@]}"; do
  case "$size" in
    0.6B|1.7B) ;;
    *) echo "无效的 VOICE_IME_QWEN_ASR_SIZES：$size（支持空格分隔的 0.6B / 1.7B）" >&2; exit 2 ;;
  esac
done
DEFAULT_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"
case "${DEFAULT_MODEL,,}" in
  0.6|0.6b|qwen3-asr-0.6b) DEFAULT_MODEL=0.6b ;;
  1.7|1.7b|qwen3-asr-1.7b) DEFAULT_MODEL=1.7b ;;
  *) echo "无效的 VOICE_IME_QWEN_ASR_MODEL：$DEFAULT_MODEL（支持 0.6b / 1.7b）" >&2; exit 2 ;;
esac
DEFAULT_MODEL_DIR="$MODELS_DIR/Qwen3-ASR-$( [[ "$DEFAULT_MODEL" == "1.7b" ]] && echo 1.7B || echo 0.6B )"

# Single writer: init/doctor/manual installation must not mutate the same venv
# concurrently. Kernel lock is released even on interruption; no stale PID file.
command -v flock >/dev/null || { echo "缺少 flock（util-linux），无法安全串行安装。" >&2; exit 1; }
[[ ! -L "$ROOT_DIR/.qwen-asr-setup.lock" ]] || { echo "拒绝使用符号链接安装锁" >&2; exit 1; }
exec 9>"$ROOT_DIR/.qwen-asr-setup.lock"
flock -n 9 || { echo "另一个 Qwen 安装/修复正在运行，请等待完成。" >&2; exit 1; }

# pip index: torch CUDA wheels are multi-GB; the default TUNA mirror is a full
# PyPI replica and avoids crawling overseas PyPI. Override or disable per run:
#   VOICE_IME_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple ./scripts/setup-qwen-asr.sh
#   VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh   # 官方 PyPI
PIP_INDEX_URL="${VOICE_IME_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
[[ "$PIP_INDEX_URL" != "default" ]] || PIP_INDEX_URL="https://pypi.org/simple"
# 校验包源：必须是单个 http(s) URL。多个 URL（空格分隔）、贴错位置的网页链接
# （如 paste/bin 分享页）会让 pip 报出误导性的 "No matching distribution found"。
if [[ "$PIP_INDEX_URL" != https://* && "$PIP_INDEX_URL" != http://* ]] || [[ "$PIP_INDEX_URL" == *[[:space:]]* ]]; then
  echo "VOICE_IME_PIP_INDEX_URL 的值不是单个 http(s) URL：$VOICE_IME_PIP_INDEX_URL" >&2
  echo "请只填一个 PyPI 镜像地址（如 https://pypi.tuna.tsinghua.edu.cn/simple），多镜像/网页链接不能作为包源。" >&2
  exit 1
fi
# pip 子进程一律带 --isolated：不读任何 pip.conf，也不读 PIP_INDEX_URL/PIP_PROXY
# 等 PIP_* 环境变量——索引实际生效靠下面 PIP_INDEX_ARGS 里的 --index-url 参数，
# 代理只能来自继承的代理环境变量（见 report_proxy_sources）。PIP_CONFIG_FILE=
# /dev/null 仍是必要的双保险：显式指定的 PIP_CONFIG_FILE 即使在 --isolated 下也会
# 被读取（实测），指向 /dev/null 才彻底失效；export PIP_INDEX_URL 仅为让子进程
# 可观测到当前选源，对 pip 本身无影响。unset PIP_EXTRA_INDEX_URL 防止非 pip
# 子进程误读。
export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL
unset PIP_EXTRA_INDEX_URL
OFFICIAL_PIP_INDEX_URL="https://pypi.org/simple"
PIP_INDEX_ARGS=(--index-url "$PIP_INDEX_URL" --timeout 30 --retries 2)
# 探测/受控回退/诊断共用的目标包与钉定版本：从 requirements 动态解析（不硬编码，
# 镜像同步后无需改脚本；也绝不在此放松 requirements 的任何版本约束）。
QWEN_ASR_PKG="qwen-asr"
QWEN_ASR_VERSION="$(sed -nE 's/^[[:space:]]*qwen[-_]asr[[:space:]]*==[[:space:]]*([0-9A-Za-z.+_-]+).*/\1/p' "$ROOT_DIR/requirements-qwen-asr.txt" | head -n1)"
echo "pip 包源：$PIP_INDEX_URL（VOICE_IME_PIP_INDEX_URL=default 使用官方 PyPI；不是 Python 下载源）"

# Crash recovery journal: SIGKILL/power loss during an install can leave the
# user's original venv parked in a .bak- dir with a half-built replacement in
# place. The EXIT trap cannot help there, so the rename is journaled and the
# next run restores the original before doing anything else.
STATE_FILE="$ROOT_DIR/.qwen-asr-setup.state"
# 归零环境可能继承的同名值：finish() 兜底清理只应作用于本次运行 mktemp 的文件
# （前缀守卫验证命名空间，这里归零验证所有权——两者缺一不可）。
PIP_STDERR_FILE=""

# ---------------------------------------------------------------------------
# pip 代理环境归一、SOCKS 自举与索引可见性探测（根因调查 2026-10-14）。
# 本脚本的 pip 子进程全部 --isolated 且 PIP_CONFIG_FILE=/dev/null——PIP_PROXY 与
# 各级 pip.conf 均已被隔离，继承的代理环境变量成为唯一未受控输入，出过两类实证
# 故障：①残留 all_proxy/ALL_PROXY=socks5://… 使 venv pip（无 PySocks）在建连前抛
# Missing dependencies for SOCKS support（代理进程死活无关）；②代理对索引请求做
# “内容级”吞掉（403/200 垃圾页/空 200）后 pip 静默得到零候选，输出与“包不存在”
# 逐字同形。以下工具把这两类故障显式化：只检测、报告、诊断，不擅自改用户环境。
# ---------------------------------------------------------------------------
proxy_url_scheme() {  # 提取 scheme；无 :// 时输出空
  case "$1" in *://*) printf '%s' "${1%%://*}" ;; *) printf '' ;; esac
}

redact_url() {  # 凭据脱敏：scheme://user:pass@host → scheme://***@host（日志安全）
  printf '%s' "$1" | sed -E 's#//[^/@:]+:[^/@]+@#//***@#g; s#//[^/@:]+@#//***@#g'
}

EFFECTIVE_PROXY_URL="" EFFECTIVE_PROXY_VAR=""
resolve_effective_proxy() {  # [目标URL] — 缺省按 https 请求语义
  # pip（requests select_proxy）按目标 URL 的 scheme 取代理：先 scheme 专属变量
  # （http_proxy/HTTP_PROXY 或 https_proxy/HTTPS_PROXY），缺失时回退 all_proxy/
  # ALL_PROXY——残留的 socks all_proxy 正是“关掉代理程序、unset 四变量后仍报
  # SOCKS 错”的根源。大小写两值并存时 urllib 按环境出现顺序取后者，这里按小写
  # 优先近似，并如实打印实际采用的来源变量名。
  local target_scheme="https" first second cand
  if [[ $# -ge 1 && "$1" == *"://"* ]]; then target_scheme="${1%%://*}"; fi
  case "$target_scheme" in
    http) first=http_proxy; second=HTTP_PROXY ;;
    *)    first=https_proxy; second=HTTPS_PROXY ;;
  esac
  EFFECTIVE_PROXY_URL="" EFFECTIVE_PROXY_VAR=""
  for cand in "$first" "$second" all_proxy ALL_PROXY; do
    if [[ -n "${!cand:-}" ]]; then EFFECTIVE_PROXY_URL="${!cand}"; EFFECTIVE_PROXY_VAR="$cand"; return 0; fi
  done
}

report_proxy_sources() {
  # 枚举并打印 pip 实际生效的全部代理来源（8 变量）+ 隔离说明 + 按选代理解析出的
  # 有效代理（协议 scheme、来源变量名、凭据脱敏）。只报告，不修改任何用户变量。
  echo "== pip 代理环境（pip 子进程实际继承；PIP_PROXY/pip.conf 已被 --isolated + PIP_CONFIG_FILE=/dev/null 隔离）=="
  local v found=0
  for v in http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY; do
    if [[ -n "${!v:-}" ]]; then
      echo "  $v=$(redact_url "${!v}")"
      found=1
    fi
  done
  if [[ $found -eq 0 ]]; then
    echo "  （8 个代理相关变量均未设置：pip 将直连）"
  fi
  resolve_effective_proxy
  if [[ -n "$EFFECTIVE_PROXY_URL" ]]; then
    echo "  → https 请求有效代理：$(redact_url "$EFFECTIVE_PROXY_URL")（协议 $(proxy_url_scheme "$EFFECTIVE_PROXY_URL")，来源 \$$EFFECTIVE_PROXY_VAR；select_proxy 顺序 https_proxy→HTTPS_PROXY→all_proxy→ALL_PROXY）"
    case "$(proxy_url_scheme "$EFFECTIVE_PROXY_URL")" in
      socks5|socks5h)
        echo "    ⚠ socks 代理需要 venv 内 PySocks；缺失时 pip 在建连前即报 Missing dependencies for SOCKS support。"
        ;;
    esac
  else
    echo "  → 未解析到有效代理（含 all_proxy 来源均已检查）：pip https 请求将直连"
  fi
}

proxy_tcp_probe() {
  # 有效代理端口的 TCP 可达性探测（扩展到 all_proxy 来源——旧预检只查 4 个变量，
  # all_proxy 残留时零警告）。按索引 URL 的 scheme 解析 pip 实际会用的代理链。
  # 只测“端口在听”，测不出“能连但回垃圾”；结果仅作警告，不阻断安装——pip
  # 退出码才是权威。
  resolve_effective_proxy "$PIP_INDEX_URL"
  [[ -n "$EFFECTIVE_PROXY_URL" ]] || return 0
  local hp host port
  hp="${EFFECTIVE_PROXY_URL#*://}"; hp="${hp%%/*}"; hp="${hp##*@}"
  host="${hp%%:*}"; port="${hp##*:}"
  if [[ "$port" == "$hp" ]]; then
    case "$(proxy_url_scheme "$EFFECTIVE_PROXY_URL")" in
      https) port=443 ;; socks5|socks5h) port=1080 ;; *) port=80 ;;
    esac
  fi
  [[ -n "$host" ]] || return 0
  host="${host#[}" ; host="${host%]}"   # IPv6 字面量去括号（/dev/tcp 不支持冒号形式时仍会失败，仅尽力）
  if ! timeout 5 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
    echo "⚠ 警告：检测到有效代理（$(redact_url "$EFFECTIVE_PROXY_URL")，来源 \$$EFFECTIVE_PROXY_VAR），但当前无法连接该代理（$host:$port）。" >&2
    echo "  若 pip 随后报 No matching distribution found / versions: none，不是包不存在，而是所有请求都经过这个不可用的代理。" >&2
    echo "  请启动代理进程，或 unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY 后重跑本脚本。" >&2
  fi
}

socks_fix_advice() {  # SOCKS 缺 PySocks 的最小修复建议（自举失败/被关闭时共用）
  local v s="" socks_vars
  for v in http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY; do
    case "$(proxy_url_scheme "${!v:-}")" in socks5|socks5h|socks4|socks4a) s+="$v " ;; esac
  done
  socks_vars="${s% }"
  echo "  最小修复建议（任选其一，然后重跑同一命令——安装可续装）：" >&2
  echo "    1) 改用 HTTP 代理：$ROOT_DIR/scripts/setup-qwen-asr.sh --proxy http://<host>:<port>" >&2
  echo "    2) 清掉 socks 代理变量后重跑：unset ${socks_vars:-all_proxy ALL_PROXY http_proxy https_proxy HTTP_PROXY HTTPS_PROXY}" >&2
  echo "       （scheme 专属变量为空时 pip 回退 all_proxy——只 unset http/https 四个不够）" >&2
}

ensure_pip_socks_support() {
  # 检测到有效代理为 socks* 且 venv 缺 PySocks 时：明确诊断 + 受控自举。自举用
  # curl（原生 SOCKS 支持，与 pip 栈分歧恰是可利用点）从当前 pip 索引解析
  # PySocks 纯 Python wheel，pip install --no-index 装进项目 venv——不触碰系统/
  # 全局环境；失败给最小修复建议，不重试、不死循环、不静默。pip 退出码仍为权威。
  # pip 按目标 scheme 分链路取代理：https 链（https_proxy→HTTPS_PROXY→all）和
  # http 链（http_proxy→HTTP_PROXY→all）任一落到 socks* 都需要 PySocks，自举
  # 下载用该 socks 链路的代理。
  local scheme
  resolve_effective_proxy   # https 请求链（索引/包文件多为 https）
  scheme="$(proxy_url_scheme "$EFFECTIVE_PROXY_URL")"
  if [[ "$scheme" != "socks5" && "$scheme" != "socks5h" ]]; then
    resolve_effective_proxy "http://placeholder.invalid"   # http 请求链（http 索引场景）
    scheme="$(proxy_url_scheme "$EFFECTIVE_PROXY_URL")"
  fi
  case "$scheme" in socks5|socks5h) ;; *) return 0 ;; esac
  if "$PY" -c 'import socks' >/dev/null 2>&1; then
    echo "[代理] 有效代理为 $scheme://（来源 \$$EFFECTIVE_PROXY_VAR），venv 已有 PySocks：pip 可直接使用该 socks 代理。"
    return 0
  fi
  echo "⚠ [代理] 检测到 socks 代理（$(redact_url "$EFFECTIVE_PROXY_URL")，来源 \$$EFFECTIVE_PROXY_VAR）且 venv 缺 PySocks："
  echo "  pip 的 HTTP 栈会在建立连接前直接抛 Missing dependencies for SOCKS support——"
  echo "  与代理进程是否存活无关（残留变量即触发），所以“关掉代理程序”无效。"
  if [[ "${VOICE_IME_PIP_SOCKS_BOOTSTRAP:-1}" == "0" ]]; then
    echo "  PySocks 自举已被 VOICE_IME_PIP_SOCKS_BOOTSTRAP=0 关闭（默认开启）。"
    socks_fix_advice
    return 0
  fi
  local simple_url="${PIP_INDEX_URL%/}/pysocks/"
  echo "  受控自举：用 curl（原生 SOCKS）从当前索引 $simple_url 解析 PySocks 纯 Python wheel，"
  echo "  以 --no-index 装入项目 venv（不动系统/全局）……"
  local tmpdir page wheel_url wheel_file
  # -d 必不可少：simple.html/wheel 都写入该路径之下，缺 -d 时 mktemp 建的是
  # 普通文件，curl -o "$tmpdir/…" 恒以 curl(23) 失败，自举永远走不通（P1，回归测试 test_s8）。
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/qwen-asr-pysocks.XXXXXX")" || { socks_fix_advice; return 0; }
  page="$tmpdir/simple.html"
  if ! curl -fsSL --max-time 60 -x "$EFFECTIVE_PROXY_URL" -o "$page" "$simple_url"; then
    echo "  ✗ 拉取 PySocks simple 页失败（$simple_url，经 socks 代理）。" >&2
    rm -rf -- "$tmpdir"; socks_fix_advice; return 0
  fi
  wheel_url="$("$BOOTSTRAP" - "$page" "$simple_url" <<'PY'
# 从 simple 页解析 wheel 链接：优先 py3-none-any（PySocks 是纯 Python 包），
# 相对 href 按 simple 页地址补全；fragment（#sha256=…）不参与下载 URL。
import re, sys, urllib.parse
html = open(sys.argv[1], encoding='utf-8', errors='replace').read()
base = sys.argv[2]
links = [urllib.parse.urljoin(base, m) for m in re.findall(r'href=["\']([^"\'#]+)', html)]
wheels = [u for u in links if u.endswith('.whl')]
pick = ([u for u in wheels if 'py3-none-any' in u] or [u for u in wheels if 'none-any' in u] or wheels)
print(pick[0] if pick else '')
PY
)"
  if [[ -z "$wheel_url" ]]; then
    echo "  ✗ 索引页未提供 PySocks wheel（页面内容异常？）。" >&2
    rm -rf -- "$tmpdir"; socks_fix_advice; return 0
  fi
  echo "  → PySocks wheel：$wheel_url"
  wheel_file="$tmpdir/$(basename "${wheel_url%%\?*}")"
  if ! curl -fsSL --max-time 120 -x "$EFFECTIVE_PROXY_URL" -o "$wheel_file" "$wheel_url"; then
    echo "  ✗ 下载 PySocks wheel 失败。" >&2
    rm -rf -- "$tmpdir"; socks_fix_advice; return 0
  fi
  if ! "$PY" -m pip --isolated install --no-index "$wheel_file"; then
    echo "  ✗ 安装 PySocks wheel 到 venv 失败。" >&2
    rm -rf -- "$tmpdir"; socks_fix_advice; return 0
  fi
  rm -rf -- "$tmpdir"
  if "$PY" -c 'import socks' >/dev/null 2>&1; then
    echo "  ✓ PySocks 已装入项目 venv：socks 代理现在可用于 pip。"
  else
    echo "  ✗ 安装后仍无法 import socks（wheel 异常？）。" >&2
    socks_fix_advice
  fi
  return 0
}

# ---------------------------------------------------------------------------
# 元数据级可见性探测：与 pip 安装子进程完全同栈（同一 venv Python、同一继承
# 环境、--isolated、同一 --index-url）。先 `pip index versions`（实证力最强），
# 失败/不支持时降级 urllib 拉 simple 页原始响应分类。
# PROBE_RESULT ∈ visible|page-missing-version|not-found|http-error|garbage|
#               tls-error|socks-error|conn-error|unparseable
# PROBE_CERTAINTY ∈ 实证|推断；PROBE_NOTE 附原始状态码/原因。
# ---------------------------------------------------------------------------
probe_result_label() {
  case "$1" in
    visible)             echo "目标版本 $QWEN_ASR_VERSION 在索引可见" ;;
    page-missing-version) echo "索引页有效但缺 $QWEN_ASR_VERSION" ;;
    not-found)           echo "包不存在（404）" ;;
    http-error)          echo "HTTP 错误（403/5xx：代理或镜像在内容级拒绝）" ;;
    garbage)             echo "索引页返回非 simple 内容（垃圾页/空体）" ;;
    tls-error)           echo "TLS 错误" ;;
    socks-error)         echo "SOCKS 依赖缺失" ;;
    conn-error)          echo "连接失败/超时" ;;
    *)                   echo "无法归类" ;;
  esac
}

probe_index_visibility() {  # <index_url>
  local index_url="$1" out="" rc=0 pip_no_versions=0
  PROBE_RESULT="unparseable"; PROBE_CERTAINTY="推断"; PROBE_NOTE=""
  # --retries 1 + timeout：探测要快，安装仍有自己的 --timeout 30 --retries 2。
  out="$("$PY" -m pip --isolated index versions "$QWEN_ASR_PKG" --index-url "$index_url" --timeout 15 --retries 1 2>&1)" || rc=$?
  if [[ $rc -eq 0 ]]; then
    local versions_list
    versions_list="$(printf '%s\n' "$out" | sed -n 's/^Available versions: //p' | head -n1)"
    if [[ -n "$versions_list" ]]; then
      PROBE_CERTAINTY="实证"
      if grep -qw "$QWEN_ASR_VERSION" <<<"$versions_list"; then
        PROBE_RESULT="visible"
      else
        PROBE_RESULT="page-missing-version"
        PROBE_NOTE="索引可见版本：$versions_list"
      fi
      return 0
    fi
    # rc==0 但未解析出版本列表（空输出/旧 pip 不支持等）：按承诺降级 urllib 拉
    # simple 页分类，不停留在「无法归类（推断）」（红队 2026-10-14 P2）。
    pip_no_versions=1
  fi
  # pip 侧已给出明确签名：直接归因（实证），不再叠加 urllib。
  if printf '%s' "$out" | grep -q 'Missing dependencies for SOCKS support'; then
    PROBE_RESULT="socks-error"; PROBE_CERTAINTY="实证"; return 0
  fi
  if printf '%s' "$out" | grep -qiE 'SSLError|SSL:|CERTIFICATE_VERIFY_FAILED'; then
    PROBE_RESULT="tls-error"; PROBE_CERTAINTY="实证"; return 0
  fi
  if printf '%s' "$out" | grep -qE 'Retrying|ProxyError|NewConnectionError|timed out|Could not fetch'; then
    PROBE_RESULT="conn-error"; PROBE_CERTAINTY="实证"; return 0
  fi
  # 版本类失败、pip index 子命令不存在（旧 pip）或 rc==0 无版本列表：urllib 拉
  # simple 页原始响应。urllib 与 pip 同样 trust_env（getproxies 会带上 all_proxy），
  # 分类只描述“页面原始响应长什么样”，不冒充 pip 的解析结论。
  local probe_out="" kind="" code="" note=""
  probe_out="$("$PY" - "$index_url" "$QWEN_ASR_PKG" "$QWEN_ASR_VERSION" <<'PY'
import re, sys, urllib.error, urllib.request
index, pkg, ver = sys.argv[1], sys.argv[2], sys.argv[3]
url = index.rstrip('/') + '/' + pkg + '/'
kind, code, note = 'unknown', '', ''
try:
    req = urllib.request.Request(url, headers={'User-Agent': 'ibus-voice-ime-index-probe'})
    with urllib.request.urlopen(req, timeout=12) as resp:
        body = resp.read(262144).decode('utf-8', 'replace')
        code = str(getattr(resp, 'status', resp.getcode()))
        if re.search(r'<a\s[^>]*href=', body, re.I):
            stem = pkg.replace('-', '_')
            if ver and re.search(re.escape('-' + ver + r'-') + '|' + re.escape('-' + ver + '.'), body):
                kind = 'visible'
            elif ver:
                kind = 'page-missing-version'
            else:
                kind = 'visible'  # 未指定版本：页面有效即可见
        else:
            kind = 'garbage'
except urllib.error.HTTPError as exc:
    code = str(exc.code)
    kind = 'not-found' if exc.code == 404 else 'http-error'
except urllib.error.URLError as exc:
    reason = str(getattr(exc, 'reason', exc))
    if 'SOCKS' in reason:
        kind = 'socks-error'
    elif 'ssl' in reason.lower() or 'certificate' in reason.lower():
        kind = 'tls-error'
    else:
        kind = 'conn-error'
    note = reason[:180]
except Exception as exc:
    kind = 'conn-error'
    note = str(exc)[:180]
print('KIND=%s' % kind)
print('CODE=%s' % code)
print('NOTE=%s' % note.replace('\n', ' '))
PY
)" || true
  kind="$(printf '%s\n' "$probe_out" | sed -n 's/^KIND=//p' | head -n1)"
  code="$(printf '%s\n' "$probe_out" | sed -n 's/^CODE=//p' | head -n1)"
  note="$(printf '%s\n' "$probe_out" | sed -n 's/^NOTE=//p' | head -n1)"
  case "$kind" in
    visible|page-missing-version|not-found|http-error|garbage|tls-error|socks-error|conn-error)
      PROBE_RESULT="$kind"; PROBE_CERTAINTY="实证"; PROBE_NOTE="HTTP ${code:-?} ${note}"
      [[ $pip_no_versions -eq 0 ]] || PROBE_NOTE="pip index versions 返回 0 但未输出版本列表，已降级 urllib 探测；$PROBE_NOTE" ;;
    *)
      PROBE_RESULT="unparseable"; PROBE_NOTE="urllib 探测无法归类 ${note}"
      [[ $pip_no_versions -eq 0 ]] || PROBE_NOTE="pip index versions 返回 0 但未输出版本列表；$PROBE_NOTE" ;;
  esac
  return 0
}

FALLBACK_DECISION=""
should_fallback_to_official() {
  # 受控回退官方源的三条件（缺一不可，单次重试、不无限）：
  # ① 本次索引是程序默认（VOICE_IME_PIP_INDEX_URL 未设置）——用户显式指定包源时
  #    绝不静默切换，只打印建议命令；
  # ② pip 失败签名为“版本不可用”：stderr 列出旧版本（真·镜像滞后专属签名），或
  #    versions: none 且主动探测确认“页面有效缺版本 / 包不存在(404)”；
  # ③ 官方 https://pypi.org/simple 探测确认目标版本可见。
  # 探测为劫持/网络/SOCKS/TLS 类时不回退——那是代理问题，不是镜像问题，换源无效。
  # VOICE_IME_PIP_INDEX_FALLBACK=0|off 可禁用（默认 auto）。模型权重下载源
  # （modelscope）保持独立，不受此开关影响。
  FALLBACK_DECISION=""
  case "${VOICE_IME_PIP_INDEX_FALLBACK:-auto}" in
    0|off|no) FALLBACK_DECISION="回退已被 VOICE_IME_PIP_INDEX_FALLBACK=${VOICE_IME_PIP_INDEX_FALLBACK} 禁用"; return 1 ;;
  esac
  if [[ -n "${VOICE_IME_PIP_INDEX_URL:-}" ]]; then
    FALLBACK_DECISION="用户显式设置了 VOICE_IME_PIP_INDEX_URL=$VOICE_IME_PIP_INDEX_URL——绝不静默切换（见下方建议命令）"
    return 1
  fi
  if [[ -z "$QWEN_ASR_VERSION" ]]; then
    FALLBACK_DECISION="未能从 requirements 解析钉定版本，无法做版本可见性判定"; return 1
  fi
  local sig=""
  if grep -qE 'from versions: *[0-9]' "$PIP_STDERR_FILE" 2>/dev/null; then
    sig="stderr 列出旧版本（真·镜像滞后签名）"
  elif grep -qiE 'No matching distribution|from versions: none' "$PIP_STDERR_FILE" 2>/dev/null; then
    # versions: none 与“代理内容劫持/包不存在”同形：必须主动探测确认，不许猜。
    probe_index_visibility "$PIP_INDEX_URL"
    case "$PROBE_RESULT" in
      page-missing-version) sig="versions: none + 探测确认索引页有效但缺该版本（$PROBE_CERTAINTY）" ;;
      not-found)            sig="versions: none + 探测确认包不存在 404（$PROBE_CERTAINTY）" ;;
      *) FALLBACK_DECISION="当前索引探测结论：“$(probe_result_label "$PROBE_RESULT")”（$PROBE_CERTAINTY）——属代理/网络/索引内容问题而非镜像缺版本，换源无效，不回退"; return 1 ;;
    esac
  else
    FALLBACK_DECISION="pip 失败签名不是“版本不可用”类（网络/代理/解析等其他失败）"; return 1
  fi
  probe_index_visibility "$OFFICIAL_PIP_INDEX_URL"
  if [[ "$PROBE_RESULT" != "visible" ]]; then
    FALLBACK_DECISION="官方源探测未确认 $QWEN_ASR_PKG $QWEN_ASR_VERSION 可见（$(probe_result_label "$PROBE_RESULT")）——回退无意义，不回退"
    return 1
  fi
  FALLBACK_DECISION="版本不可用（$sig）且官方源实证可见 $QWEN_ASR_PKG $QWEN_ASR_VERSION"
  return 0
}

# ---------------------------------------------------------------------------
# 留档轮转：.failed- / .failed-crash- / .bak- 三类目录各只保留最新 1 份。
# 目录名含时间戳（+pid），字典序即时间序。半成品 venv/旧备份动辄数 GB，重试链
# 会成对累积（bak+failed 每轮 +2 份）；清理旧档时打印路径，保留最新 1 份供排查。
# ---------------------------------------------------------------------------
rotate_archive_family() {  # <family: failed|failed-crash|bak>
  local family="$1" pattern keep entry
  case "$family" in
    failed)       pattern='.venv-qwen-asr.failed-[0-9]*' ;;   # 数字开头区分 failed-crash
    failed-crash) pattern='.venv-qwen-asr.failed-crash-*' ;;
    bak)          pattern='.venv-qwen-asr.bak-*' ;;
    *) return 0 ;;
  esac
  local -a dirs=()
  # pattern 必须在引号外参与 bash pathname 展开（展开结果本身按字典序排序，
  # 即时间序）；无匹配时展开为字面量，用 -e/-L 过滤掉。ROOT_DIR 含空格须引号。
  for entry in "$ROOT_DIR"/$pattern; do
    if [[ -e "$entry" || -L "$entry" ]]; then dirs+=("$entry"); fi
  done
  [[ ${#dirs[@]} -gt 1 ]] || return 0
  keep="${dirs[${#dirs[@]}-1]}"
  for entry in "${dirs[@]}"; do
    # 最新 1 份保留；符号链接不是档案目录（防御，不在此清理）。
    if [[ "$entry" == "$keep" || -L "$entry" ]]; then continue; fi
    echo "清理旧留档（$family 只保留最新 1 份：$keep）：$entry"
    rm -rf -- "$entry"
  done
  return 0
}

rotate_archives() {
  rotate_archive_family failed
  rotate_archive_family failed-crash
  rotate_archive_family bak
}

recover_interrupted() {
  [[ ! -L "$STATE_FILE" ]] || { echo "拒绝使用符号链接安装状态文件：$STATE_FILE" >&2; exit 1; }
  [[ -f "$STATE_FILE" ]] || return 0
  [[ ! -L "$VENV" ]] || { echo "拒绝恢复到符号链接 venv：$VENV" >&2; exit 1; }
  local recorded
  recorded="$(sed -n 's/^backup=//p' "$STATE_FILE" | head -n1)"
  # A journal is data, not authority to move arbitrary user directories.
  [[ "$recorded" == "$ROOT_DIR"/.venv-qwen-asr.bak-* && "${recorded##*/}" != *..* && ! -L "$recorded" && "$(dirname "$recorded")" == "$ROOT_DIR" ]] || {
    echo "拒绝恢复非预期/符号链接备份：$recorded（保留状态文件供检查）" >&2; exit 1;
  }
  if [[ -n "$recorded" && -d "$recorded" ]]; then
    if [[ -d "$VENV" && ! -L "$VENV" ]]; then
      local failed="$ROOT_DIR/.venv-qwen-asr.failed-crash-$(date +%Y%m%d-%H%M%S)-$$"
      mv -- "$VENV" "$failed"
      echo "上次安装被中断：半成品环境移至 $failed" >&2
    elif [[ -e "$VENV" ]]; then
      echo "venv 路径异常（$VENV），请手动检查后删除状态文件 $STATE_FILE" >&2
      exit 1
    fi
    mv -- "$recorded" "$VENV"
    echo "已恢复上次中断前的原环境：$recorded" >&2
  else
    echo "发现过期的安装状态文件（备份已不存在），已忽略：$STATE_FILE" >&2
  fi
  rm -f "$STATE_FILE"
}
recover_interrupted
# 留档轮转放在 journal 消费之后：恢复流程刚接管的备份不会被误删，同时每次运行
# 都清理历史累积的 failed/failed-crash/bak 旧档（成功路径也能瘦身）。
rotate_archives

GPU_STATE="" GPU_REASON="" GPU_ACTION=""
if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
fi
if [[ "$GPU_STATE" != "ok" ]]; then
  if [[ $FORCE -eq 0 && "${VOICE_IME_FORCE_QWEN_SETUP:-0}" != "1" ]]; then
    echo "无法安装可用的本地 Qwen 后端：${GPU_REASON:-GPU 探测未通过}" >&2
    [[ -n "$GPU_ACTION" ]] && printf '%s\n' "$GPU_ACTION" >&2
    echo "仅准备调试环境可用 --force；不会宣称语音就绪。" >&2
    exit 1
  fi
  ENABLE_NOW=0
  echo "[WARN] 强制准备环境，GPU 未通过检查：不做 GPU 推理验收、不启用后端。"
fi

# Verify the SAME device/dtype that activation will write, not stale JSON
# settings from another backend. Repair-only mode leaves current settings intact.
if [[ "$ENABLE_NOW" != "0" ]]; then
  export VOICE_IME_QWEN_ASR_DEVICE_MAP="${VOICE_IME_QWEN_ASR_DEVICE_MAP:-cuda:0}"
  export VOICE_IME_QWEN_ASR_DTYPE="${VOICE_IME_QWEN_ASR_DTYPE:-bfloat16}"
fi

# 下载预算只作提示，不改变缓存、下载集合或完整性判据。模型常量来自
# Qwen/Qwen3-ASR-{0.6B,1.7B} 发布快照的权重/配套文件体积（十进制 GB，
# 四舍五入：约 1.8 / 4.4GB）；依赖按 torch CUDA 13 全家桶典型安装估算，
# 并非精确承诺，版本/平台、已有缓存和重建备份都会改变实际峰值。
MODEL_TENTHS=0 SEEN_SIZES=" "
printf '\n== 下载预算（估算，缓存命中会减少下载）==\n'
for size in "${QWEN_SIZES[@]}"; do
  [[ "$SEEN_SIZES" != *" $size "* ]] || continue
  SEEN_SIZES+="$size "
  case "$size" in
    0.6B) echo '  模型 0.6B ≈1.8GB'; MODEL_TENTHS=$((MODEL_TENTHS + 18)) ;;
    1.7B) echo '  模型 1.7B ≈4.4GB'; MODEL_TENTHS=$((MODEL_TENTHS + 44)) ;;
  esac
done
echo '  依赖 venv 典型 ~6GB（torch cu13 全家桶；下载缓存/临时解包另占空间）。'
if [[ -d "$HOME/.cache/pip" ]]; then
  echo '  检测到 ~/.cache/pip：同机重装依赖通常直接命中缓存（不扫描缓存大小，实际命中以 pip 为准）。'
else
  echo '  pip 缓存未检测到：首次可能需下载数 GB 依赖；后续同机重装可复用缓存。'
fi
# 保守预留：新 venv 6GB + pip 缓存/临时解包约 12GB + 所选模型；旧备份另计。
PEAK_TENTHS=$((MODEL_TENTHS + 180))
printf '  预计磁盘峰值（新装保守预留）：约 %d.%dGB；重建保留旧 venv 时还需额外 ~6GB，实际依版本/缓存而变。\n' "$((PEAK_TENTHS / 10))" "$((PEAK_TENTHS % 10))"
echo '  中断可续装：重跑同一命令，已就绪依赖/模型会复用；尚未完成的临时模型下载可能重下。'

# CPython downloads are independent from the pip package mirror. Default is
# repository-local managed 3.12, including headers, even if distro 3.12 exists.
# The helper bootstraps local uv when absent; explicit choices never fall back.
PYTHON_BIN="$("$BOOTSTRAP" "$ROOT_DIR/scripts/setup-qwen-python.py")"
# Check before moving an existing venv or downloading dependencies/models.
"$BOOTSTRAP" "$CHECK" --python "$PYTHON_BIN" --stage static
if ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
  echo "缺少目标解释器的 ensurepip/venv：请安装 python3.12-venv（Debian/Ubuntu），或使用 uv managed Python。" >&2
  exit 1
fi

BACKUP=""
COMMITTED=0
# Verify exact targets before any rename. Never follow a venv symlink to another
# project, delete user environments, or move the repository itself.
verify_venv_target() {
  [[ "$VENV" == "$ROOT_DIR/.venv-qwen-asr" && ! -L "$VENV" ]] || {
    echo "拒绝修改非预期/符号链接 venv：$VENV" >&2; exit 1;
  }
}
finish() {
  local rc=$?
  if [[ $COMMITTED -eq 0 && -n "$BACKUP" && -d "$BACKUP" ]]; then
    if [[ -d "$VENV" && ! -L "$VENV" ]]; then
      local failed="$ROOT_DIR/.venv-qwen-asr.failed-$(date +%Y%m%d-%H%M%S)-$$"
      mv -- "$VENV" "$failed"
      echo "失败环境保留供排查：$failed" >&2
      rotate_archive_family failed
    fi
    mv -- "$BACKUP" "$VENV"
    echo "已恢复原 venv；未切换后端。" >&2
  fi
  if [[ ${#PART_DIRS[@]} -gt 0 ]]; then
    rm -rf -- "${PART_DIRS[@]}"
  fi
  # pip stderr 捕获临时文件：Ctrl-C/异常退出经 EXIT trap 兜底清理
  # （SIGKILL/断电无法触发 trap，残留文件名固定为 /tmp/qwen-asr-pip-stderr.*，
  #  可安全手工删除；前缀守卫防止误删环境继承的无关路径）。
  case "${PIP_STDERR_FILE:-}" in
    /tmp/qwen-asr-pip-stderr.*) rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true ;;
  esac
  rm -f "$STATE_FILE"
  exit "$rc"
}
trap finish EXIT
verify_venv_target
# 三档处置梯度（venv 恢复机制，2026-10-14 起）：
#   REUSE     完全满足 requirements → 跳过 pip，直接运行时/模型验收；
#   INCREMENT 解释器匹配、结构完好、已装包全部满足版本约束、差距仅为缺包
#             （PackageNotFoundError）→ 不再整体重建：在原 venv 内增量
#             `pip install -r`（“增量补装”），避免一次缺包就重装 ~6GB 依赖；
#   REBUILD   解释器不匹配/结构损坏/版本冲突 → 维持原“备份→重建→失败恢复”
#             原子路径（该路径时序经审计无缺陷，勿动）。
REBUILD=0 REUSE=0 INCREMENT=0
if [[ -d "$VENV" ]]; then
  if [[ ! -x "$VENV/bin/python" || ! -x "$VENV/bin/pip" ]] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    # 结构完好性：python/pip 存在且 pip 自身可运行（pip 模块损坏走重建）。
    REBUILD=1
  elif ! "$BOOTSTRAP" - "$PYTHON_BIN" "$VENV/bin/python" "$ROOT_DIR/src" <<'PY'
import sys
sys.path.insert(0, sys.argv[3])
from ibus_voice_ime.asr.qwen_preflight import interpreter_info
wanted = interpreter_info(sys.argv[1])
actual = interpreter_info(sys.argv[2])
sys.exit(0 if wanted['version'][:2] == actual['version'][:2] and wanted['base'] == actual['base'] else 1)
PY
  then
    REBUILD=1
  fi
  if [[ $REBUILD -eq 0 ]] && ! "$VENV/bin/python" -m pip --isolated check >/dev/null 2>&1; then
    # 已装包元数据损坏/依赖冲突：补装修复不了冲突，必须走原子重建路径。
    echo "[REUSE 失败] pip check 报告已装包存在冲突/损坏，按整体重建处理。"
    REBUILD=1
  fi
  if [[ $REBUILD -eq 0 ]]; then
    reuse_rc=0
    "$VENV/bin/python" - "$ROOT_DIR/requirements-qwen-asr.txt" - <<'PY' || reuse_rc=$?
# REUSE 判据直接读 requirements 文件逐项核验（packaging.Requirement），
# 不再手写第二份基线——两处漂移会让旧 venv 绕过新增的 pin（红队 2026-10-11 发现）。
# 三态结论：0=全部满足（REUSE）；3=仅缺包（INCREMENT 增量补装）；1=版本冲突/
# 元数据损坏（REBUILD）。缺包与冲突并存时按冲突处理——冲突环境下盲目补装可能
# 放大破坏。
import importlib.metadata as m
import sys
try:
    from packaging.requirements import Requirement
    from packaging.version import Version
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version
missing = []
problem = None
with open(sys.argv[1]) as f:
    for raw in f:
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        try:
            version = Version(m.version(req.name))
        except m.PackageNotFoundError:
            missing.append(req.name)
            continue
        except Exception as exc:
            # 元数据损坏（如非正常版本字符串）按不满足处理，不让核验自身崩溃。
            problem = f'{req.name} 版本无法解析：{exc}'
            break
        if not req.specifier.contains(version, prereleases=True):
            problem = f'{req.name} {version} 不满足 {req.specifier}'
            break
if problem:
    print(f'  [REUSE 失败] {problem}')
    sys.exit(1)
if missing:
    print('  [增量补装] 现有 venv 仅缺包：' + '、'.join(missing))
    sys.exit(3)
sys.exit(0)
PY
    case "$reuse_rc" in
      0)
        REUSE=1
        echo "解释器和依赖基线匹配：不升级现有包，仍执行运行时及模型验收。"
        ;;
      3) INCREMENT=1 ;;
      *) REBUILD=1 ;;
    esac
  fi
  if [[ $REBUILD -eq 1 ]]; then
    BACKUP="$ROOT_DIR/.venv-qwen-asr.bak-$(date +%Y%m%d-%H%M%S)-$$"
    [[ ! -e "$BACKUP" ]] || { echo "备份目标已存在：$BACKUP" >&2; exit 1; }
    echo "解释器不匹配或 venv 不完整，备份后重建：$BACKUP"
    # journal BEFORE the rename: a crash between the two leaves a recoverable state
    printf 'backup=%s\n' "$BACKUP" > "$STATE_FILE"
    mv -- "$VENV" "$BACKUP"
    # 新备份落位后轮转：只保留本份 bak，清掉更早的旧档（打印路径）。
    rotate_archive_family bak
  fi
elif [[ -e "$VENV" ]]; then
  echo "venv 路径不是目录：$VENV" >&2; exit 1
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
PY="$VENV/bin/python"
echo "Qwen3-ASR venv Python：$("$PY" -V 2>&1)"
if [[ $REUSE -eq 0 ]]; then
  if [[ $INCREMENT -eq 1 ]]; then
    echo "增量补装：解释器匹配、venv 结构完好、已装包均满足版本约束——在原 venv 内"
    echo "补装缺失依赖，不整体重建。补装失败保留现状退出；网络恢复后重跑同一命令即可续装"
    echo "（无需手动删除 venv/配置）。"
  fi
  # torch 范围内最新版（当前 2.12.x）捆绑 CUDA 13 运行库，NVIDIA 官方要求驱动 >=580；
  # gpu-probe 只验证 nvidia-smi 可用，不校验驱动版本，旧驱动会在下载数 GB 后运行时失败
  # （红队 2026-10-11 P1）。这里在安装前给出明确预警与降档指引，不硬性阻断。
  if NVIDIA_DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1)" \
     && [[ "$NVIDIA_DRIVER" =~ ^([0-9]+) ]] && (( BASH_REMATCH[1] < 580 )); then
    echo "⚠ 检测到 NVIDIA 驱动 $NVIDIA_DRIVER < 580：requirements 中 torch 将解析为 2.12.x（捆绑 CUDA 13，"
    echo "  官方要求驱动 >=580）。若后续运行时验收（CUDA/Triton 矩阵运算）失败，请在 requirements-qwen-asr.txt"
    echo "  临时收紧为 torch==2.9.0（CUDA 12.8 线，要求驱动 >=525）后重跑本脚本。" >&2
  fi
  # ---- 网络预检（与 pip 同栈，2026-10-14 重做）----
  # 旧预检的两个实证缺陷：①代理检测只查 4 个变量（all_proxy 残留时零警告）；
  # ②裸 curl 预检与 pip 栈不同（curl 原生支持 SOCKS，curl 过 ≠ pip 过）。现改为：
  # 枚举全部代理来源 + 有效代理 TCP 探测（仅警告）+ SOCKS/PySocks 诊断与受控自举
  # + 用 venv pip 本身做元数据级可见性探测。全部只诊断不阻断——pip 退出码才权威。
  report_proxy_sources
  proxy_tcp_probe
  ensure_pip_socks_support
  if [[ -n "$QWEN_ASR_VERSION" ]]; then
    probe_index_visibility "$PIP_INDEX_URL"
    echo "[预检] 索引可见性（venv pip --isolated，与安装同栈）：$QWEN_ASR_PKG $QWEN_ASR_VERSION @ $PIP_INDEX_URL → $(probe_result_label "$PROBE_RESULT")（确定性：$PROBE_CERTAINTY${PROBE_NOTE:+；$PROBE_NOTE}）"
    if [[ "$PROBE_RESULT" != "visible" ]]; then
      echo "  ⚠ 以上仅是预检诊断，不阻断安装（pip 退出码为准）。镜像类问题可换源：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh；代理类问题见上方代理报告。"
    fi
  fi
  # pip 自升级失败不致命：旧 pip 仍可能完成依赖安装，真正的安装错误由下一行报告。
  # stderr 同样捕获并过 net-probe 分类（stage=selfcheck）——SOCKS/代理类故障在自
  # 升级第一现场就有诊断包裹，而不是裸错误。
  PIP_STDERR_FILE="$(mktemp /tmp/qwen-asr-pip-stderr.XXXXXX)"
  if ! "$PY" -m pip --isolated install --upgrade pip "${PIP_INDEX_ARGS[@]}" 2>"$PIP_STDERR_FILE"; then
    cat "$PIP_STDERR_FILE" >&2
    NETPROBE_ARGS=(--analyze "$PIP_STDERR_FILE" --index-url "$PIP_INDEX_URL" --package "$QWEN_ASR_PKG" --stage selfcheck)
    if [[ -n "$QWEN_ASR_VERSION" ]]; then NETPROBE_ARGS+=(--version "$QWEN_ASR_VERSION"); fi
    bash "$ROOT_DIR/scripts/net-probe.sh" "${NETPROBE_ARGS[@]}" >&2 || true
    rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
    echo "⚠ pip 自升级失败（网络？），继续使用现有 pip 安装依赖……" >&2
  else
    cat "$PIP_STDERR_FILE" >&2
    rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
  fi
  # 依赖安装：失败时按“stderr 签名 + 主动探测”决定是否受控回退官方源重试一次。
  # 不放松版本约束、不忽略 pip 非零退出码；用户显式指定包源时绝不静默切换。
  PIP_STDERR_FILE="$(mktemp /tmp/qwen-asr-pip-stderr.XXXXXX)"
  if ! "$PY" -m pip --isolated install -r "$ROOT_DIR/requirements-qwen-asr.txt" --index-url "$PIP_INDEX_URL" --timeout 30 --retries 2 2>"$PIP_STDERR_FILE"; then
    cat "$PIP_STDERR_FILE" >&2
    if should_fallback_to_official; then
      echo "════════ 受控回退：当前镜像看不到目标版本，官方 PyPI 已实证可见 ════════" >&2
      echo "  决策原因：$FALLBACK_DECISION" >&2
      echo "  $PIP_INDEX_URL → $OFFICIAL_PIP_INDEX_URL（仅重试一次；版本约束不变）" >&2
      echo "══════════════════════════════════════════════════════════════════════" >&2
      rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
      PIP_STDERR_FILE="$(mktemp /tmp/qwen-asr-pip-stderr.XXXXXX)"
      if ! "$PY" -m pip --isolated install -r "$ROOT_DIR/requirements-qwen-asr.txt" --index-url "$OFFICIAL_PIP_INDEX_URL" --timeout 30 --retries 2 2>"$PIP_STDERR_FILE"; then
        cat "$PIP_STDERR_FILE" >&2
        NETPROBE_ARGS=(--analyze "$PIP_STDERR_FILE" --index-url "$OFFICIAL_PIP_INDEX_URL" --package "$QWEN_ASR_PKG" --stage deps)
        if [[ -n "$QWEN_ASR_VERSION" ]]; then NETPROBE_ARGS+=(--version "$QWEN_ASR_VERSION"); fi
        bash "$ROOT_DIR/scripts/net-probe.sh" "${NETPROBE_ARGS[@]}" >&2 || true
        rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
        echo "依赖安装失败（官方源重试仍未通过）：检查上方 pip 错误与网络诊断（scripts/net-probe.sh 可独立运行复测）。旧环境如有备份会恢复。" >&2
        exit 1
      fi
      cat "$PIP_STDERR_FILE" >&2
      rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
      echo "官方源重试成功：依赖安装完成（镜像侧缺版本，官方源有）。"
    else
      echo "未回退官方源：$FALLBACK_DECISION" >&2
      if [[ -n "${VOICE_IME_PIP_INDEX_URL:-}" ]]; then
        echo "建议（不自动切换你显式指定的包源）：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh（官方 PyPI）" >&2
      fi
      NETPROBE_ARGS=(--analyze "$PIP_STDERR_FILE" --index-url "$PIP_INDEX_URL" --package "$QWEN_ASR_PKG" --stage deps)
      if [[ -n "$QWEN_ASR_VERSION" ]]; then NETPROBE_ARGS+=(--version "$QWEN_ASR_VERSION"); fi
      bash "$ROOT_DIR/scripts/net-probe.sh" "${NETPROBE_ARGS[@]}" >&2 || true
      rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
      echo "依赖安装失败：检查上方 pip 错误与网络诊断（scripts/net-probe.sh 可独立运行复测）。旧环境如有备份会恢复。" >&2
      exit 1
    fi
  else
    cat "$PIP_STDERR_FILE" >&2
    rm -f -- "$PIP_STDERR_FILE" 2>/dev/null || true
  fi
fi
"$PY" -m pip --isolated check
"$BOOTSTRAP" "$CHECK" --python "$PY" --stage static
if [[ "$GPU_STATE" == "ok" ]]; then
  # Fail early, BEFORE downloading multi-GB assets.
  "$BOOTSTRAP" "$CHECK" --python "$PY" --stage runtime --timeout "$TIMEOUT"
fi

MS_CLI="$VENV/bin/modelscope"
[[ -x "$MS_CLI" ]] || { echo "venv 未安装 modelscope CLI。" >&2; exit 1; }
mkdir -p "$MODELS_DIR" "$MODEL_CACHE"
echo "共享模型缓存：$MODEL_CACHE（VOICE_IME_MODEL_CACHE 可自定义；vendor 下建符号链接）"

# 分档完整性判定（配置和全部权重分片存在）：下载前、下载后、缓存复用三处共用，
# 与验收器 --model-only 同一逻辑，vendor 符号链接会透明穿透到缓存目录。
model_ready() {
  "$BOOTSTRAP" "$CHECK" --model-only --model "$1" >/dev/null 2>&1
}

for size in "${QWEN_SIZES[@]}"; do
  model_dir="$MODELS_DIR/Qwen3-ASR-$size"
  cache_dir="$MODEL_CACHE/Qwen3-ASR-$size"
  if [[ -L "$model_dir" ]]; then
    if model_ready "$model_dir"; then
      echo "模型配置及全部分片已存在：$model_dir"
      continue
    fi
    # 链接目标不完整：指向本缓存的删链接走下方缓存补齐流程；指向其他位置
    # （用户自定义布局）保持旧语义在原位补齐，不擅自改写。
    if [[ "$(readlink -- "$model_dir")" == "$cache_dir" ]]; then
      rm -f -- "$model_dir"
    else
      echo "下载/补齐 Qwen/Qwen3-ASR-$size（保留已有文件）……"
      "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$model_dir"
      "$BOOTSTRAP" "$CHECK" --model-only --model "$model_dir"
      continue
    fi
  fi
  if [[ -d "$model_dir" ]]; then
    # 旧布局：vendor 下真实目录。完整则迁入共享缓存并在原位留符号链接；
    # 迁移与缓存发布共用分档 flock（跨克隆串行，防 TOCTOU 嵌套）；
    # mv 失败或缓存位已被占用时保留原状，不阻断安装。
    if model_ready "$model_dir"; then
      echo "模型配置及全部分片已存在：$model_dir"
      cache_lock "$size"
      if [[ ! -e "$cache_dir" ]] && mv -- "$model_dir" "$cache_dir"; then
        ln -s -- "$cache_dir" "$model_dir"
        echo "已迁移到共享缓存：$cache_dir（vendor 内留符号链接；卸载/更换克隆目录不丢模型）"
      else
        echo "共享缓存迁移跳过（缓存目录已存在或移动失败）：继续使用 $model_dir"
      fi
      cache_unlock
      continue
    fi
    echo "下载/补齐 Qwen/Qwen3-ASR-$size（保留已有文件）……"
    "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$model_dir"
    "$BOOTSTRAP" "$CHECK" --model-only --model "$model_dir"
    continue
  fi
  cache_lock "$size"
  if model_ready "$cache_dir"; then
    echo "模型配置及全部分片已在共享缓存（跳过下载，重建符号链接）：$cache_dir"
    ln -s -- "$cache_dir" "$model_dir"
    cache_unlock
    continue
  fi
  if [[ ! -e "$cache_dir" ]]; then
    # 全新下载：mktemp 临时目录（不依赖 $$ 的跨 PID 唯一性）；分档 flock
    # 串行化并发克隆——先在 .part 内通过真实 model-only 校验才发布，
    # CLI 伪成功（rc0 但缺权重）不会把半成品定稿成正式分档（红队实测）。
    part_dir="$(mktemp -d "$MODEL_CACHE/.part-Qwen3-ASR-${size}-XXXXXX")"
    PART_DIRS=("$part_dir")
    echo "下载 Qwen/Qwen3-ASR-$size 到共享缓存（临时目录校验通过后落位）……"
    if ! "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$part_dir"; then
      rm -rf -- "$part_dir"; PART_DIRS=(); cache_unlock
      echo "下载 Qwen/Qwen3-ASR-$size 失败（临时目录已清理，可重试续装）。" >&2
      exit 1
    fi
    if ! "$BOOTSTRAP" "$CHECK" --model-only --model "$part_dir"; then
      rm -rf -- "$part_dir"; PART_DIRS=(); cache_unlock
      echo "临时目录未通过完整性校验，已清理；不发布半成品到共享缓存。" >&2
      exit 1
    fi
    if [[ ! -e "$cache_dir" ]]; then
      mv -- "$part_dir" "$cache_dir"
    else
      rm -rf -- "$part_dir"   # 并发克隆已抢先定稿：复用其成果
    fi
    PART_DIRS=()
  else
    echo "补齐共享缓存中的 Qwen/Qwen3-ASR-$size（保留已有文件）……"
    "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$cache_dir"
  fi
  "$BOOTSTRAP" "$CHECK" --model-only --model "$cache_dir"
  ln -s -- "$cache_dir" "$model_dir"
  cache_unlock
done
if [[ "$GPU_STATE" == "ok" ]]; then
  VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=16 "$BOOTSTRAP" "$CHECK" --python "$PY" \
    --stage smoke --model "$DEFAULT_MODEL_DIR" --timeout "$TIMEOUT"
  write_verify_receipt "$PY" "$DEFAULT_MODEL_DIR"
fi
# Preserve exact resolved dependency versions for issue reports; not a universal
# CUDA lock. No user configuration is changed before the gates above pass.
"$PY" -m pip --isolated freeze > "$VENV/installed-requirements.txt"
COMMITTED=1
# The environment is now committed. Never restore an old venv after a crash
# during configuration/desktop activation (which can already reference the new one).
rm -f "$STATE_FILE"
if [[ "$ENABLE_NOW" != "0" ]]; then
  # Reuse JSON configuration/legacy-env cleanup rather than introducing a second
  # competing backend source. Custom model directories are explicitly retained.
  # Do NOT let ibus-daemon inherit fd 9 and hold the install lock indefinitely.
  if ! VOICE_IME_QWEN_SWITCH_MODEL_PATH="$DEFAULT_MODEL_DIR" \
    VOICE_IME_QWEN_SWITCH_DEVICE_MAP="$VOICE_IME_QWEN_ASR_DEVICE_MAP" \
    VOICE_IME_QWEN_SWITCH_DTYPE="$VOICE_IME_QWEN_ASR_DTYPE" \
    "$ROOT_DIR/scripts/switch-qwen-asr.sh" "$DEFAULT_MODEL" 9>&-; then
    # COMMITTED=1: the environment itself is verified and kept; only activation failed.
    echo "错误：环境与模型均已验收，但配置写入或桌面激活失败（见上方具体错误）；不能视为桌面语音就绪。" >&2
    echo "环境已保留，修复后单独重试：./scripts/switch-qwen-asr.sh $DEFAULT_MODEL" >&2
    exit 1
  fi
fi
if [[ "$GPU_STATE" == "ok" ]]; then
  echo "Qwen3-ASR 配置完成：CUDA/Triton、模型加载和短音频推理已验收。"
else
  echo "Qwen 环境和模型已准备，但语音未验证、未启用（GPU 不可用）。"
fi
[[ -z "$BACKUP" ]] || echo "原环境备份保留：$BACKUP"
echo "Python：$PY"
echo "依赖版本记录：$VENV/installed-requirements.txt"
