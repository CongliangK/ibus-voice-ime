#!/usr/bin/env bash
# net-probe.sh — pip 网络失败分类诊断（也可独立执行做纯诊断）。
#
# 用法：
#   scripts/net-probe.sh                                  # 纯诊断：探测索引可达性 + 代理环境
#   scripts/net-probe.sh --analyze <stderr文件> [--index-url <URL>]
#                                                         # 分析一份已捕获的 pip stderr 并分类
# 被 setup-qwen-asr.sh 调用时传 --analyze + --index-url（本次实际使用的包源）。
set -uo pipefail

DEFAULT_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"

# ---------------------------------------------------------------------------
# 通用：打印「本次实际使用的索引 URL」与代理环境变量检测（四类分类输出共用）
# ---------------------------------------------------------------------------
report_env_context() {
  local index_url="$1"
  echo "  • 本次实际使用的索引 URL：$index_url"
  local proxies=""
  [[ -n "${https_proxy:-}" ]] && proxies+=" https_proxy=$https_proxy"
  [[ -n "${HTTPS_PROXY:-}" ]] && proxies+=" HTTPS_PROXY=$HTTPS_PROXY"
  [[ -n "${http_proxy:-}" ]] && proxies+=" http_proxy=$http_proxy"
  [[ -n "${HTTP_PROXY:-}" ]] && proxies+=" HTTP_PROXY=$HTTP_PROXY"
  if [[ -n "$proxies" ]]; then
    echo "  • 检测到代理环境变量：$proxies"
    echo "    ⚠ 国内镜像（如清华源）本不需要代理——全局代理可能把这些请求也劫持/拦截了。"
    echo "    可先试：unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 后重跑；"
    echo "    确需代理则确认代理进程正在运行且规则放行该镜像域名。"
  else
    echo "  • 未检测到 https_proxy/http_proxy 环境变量（pip 直连）。"
    echo "    若直连不可达，可加 --proxy http://127.0.0.1:7890 重跑，或先 export https_proxy=... 再运行。"
  fi
}

# ---------------------------------------------------------------------------
# 分类核心：net_probe_analyze <stderr文件> <index_url>
# 优先级：连接类 > 中断类 > 版本缺失类 > 其他
# （pip 索引全挂时会在连接错误之后追加 "No matching distribution"，连接类优先
#   才不会把网络问题误判成“包不存在”；中断类信号独占，提前于版本缺失判断。）
# ---------------------------------------------------------------------------
net_probe_analyze() {
  local err_file="$1" index_url="$2"
  local text=""
  [[ -f "$err_file" ]] && text="$(<"$err_file")"

  echo ""
  echo "━━━ pip 失败诊断（scripts/net-probe.sh）━━━"
  if [[ -z "$text" ]]; then
    echo "  （未捕获到 pip stderr，按未知失败处理）"
  fi
  report_env_context "$index_url"

  if printf '%s' "$text" | grep -qiE 'Could not fetch URL|Retrying \(|Connection (reset|refused|timed out)|connection error|timed out|ReadTimeout|SSLError|SSL:|ProxyError|proxy|ECONNRESET|Connection kept alive|NewConnectionError'; then
    cat <<'EOF'
  ▶ 分类：连接类 —— 网络不可达或被代理干扰（不是包不存在）。
    探测命令（10 秒超时，返回 000/超时 = 不可达；200 = 索引本身正常）：
      curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 '<索引URL>'
    修复选项：
      1) 换包源重跑：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh
         （default = 官方 https://pypi.org/simple），或换成其他国内镜像 URL；
      2) 网络确需代理：./scripts/setup-qwen-asr.sh --proxy http://127.0.0.1:7890；
      3) 检查 https_proxy/http_proxy 是否污染：国内源通常无需代理，全局代理可能
         反而拦截了到清华源的请求（见上方检测到的代理值），可 unset 后重跑。
    注意：能 git clone GitHub ≠ pip 镜像可达——git 与 pip 走的源和代理规则可以不同。
EOF
    return 0
  fi

  if printf '%s' "$text" | grep -qiE 'No space left|Killed|Interrupted|KeyboardInterrupt|cancelled|canceled|Broken pipe'; then
    cat <<'EOF'
  ▶ 分类：中断类 —— 下载/安装被中断或磁盘/内存不足。
    检查磁盘与内存：
      df -h        # 确认根目录和仓库所在盘有足够空间（依赖+缓存峰值约 12GB，另加模型）
      dmesg | tail # 若见 oom-kill，说明内存不足而非网络问题
    直接重跑同一命令即可续装：安装是幂等的，已就绪的资产不会重复下载。
EOF
    return 0
  fi

  if printf '%s' "$text" | grep -qiE 'No matching distribution|from versions: none'; then
    cat <<EOF
  ▶ 分类：版本缺失类 —— 索引可达，但里面看不到所需的包/版本（镜像同步滞后或索引不含该包）。
    验证命令（应能在页面中 grep 到所需版本号，否则确认镜像滞后/缺包）：
      curl -sS --max-time 10 '${index_url%/}/qwen-asr/' | grep -o '0\.0\.6' | head
    修复选项：
      1) 换官方源试一次：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh
      2) 若镜像页确有该版本而 pip 仍报 none，多半是请求走了不可用代理被静默吞掉
         （见上方代理检测），先 unset 代理变量或修好代理再重跑。
EOF
    return 0
  fi

  cat <<'EOF'
  ▶ 分类：其他 —— 未匹配到已知网络失败特征，原样保留 pip 输出供排查。
    通用指引：先重跑一次（安装幂等、可续装）；仍失败请把上方完整 pip 错误
    与本段诊断一起反馈。可用 scripts/net-probe.sh 独立运行做纯网络诊断。
EOF
}

# ---------------------------------------------------------------------------
# 独立诊断模式：不带 --analyze 时执行
# ---------------------------------------------------------------------------
standalone_probe() {
  local index_url="${VOICE_IME_PIP_INDEX_URL:-$DEFAULT_INDEX_URL}"
  [[ "$index_url" != "default" ]] || index_url="https://pypi.org/simple"
  echo "pip 索引诊断（可设 VOICE_IME_PIP_INDEX_URL 覆盖）"
  report_env_context "$index_url"
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$index_url" 2>&1)" || true
  echo "探测 $index_url → ${code:-000}"
  [[ "$code" == "200" ]] && echo "索引可达。若 pip 仍失败，重点检查代理规则或包版本是否存在。" \
                       || echo "索引不可达/异常：换源、加 --proxy 或检查代理环境变量。"
}

# ---------------------------------------------------------------------------
case "${1:-}" in
  --analyze)
    [[ $# -ge 2 ]] || { echo "用法：$0 --analyze <stderr文件> [--index-url <URL>]" >&2; exit 2; }
    INDEX_URL="$DEFAULT_INDEX_URL"
    shift
    ERR_FILE="$1"; shift
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --index-url) INDEX_URL="$2"; shift 2 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
      esac
    done
    net_probe_analyze "$ERR_FILE" "$INDEX_URL"
    ;;
  -h|--help)
    echo "用法：$0 [--analyze <stderr文件> [--index-url <URL>]]"; exit 0 ;;
  "")
    standalone_probe ;;
  *)
    echo "未知参数：$1（$0 --help 查看用法）" >&2; exit 2 ;;
esac
