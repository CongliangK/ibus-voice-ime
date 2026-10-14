#!/usr/bin/env bash
# net-probe.sh — pip 网络失败分类诊断（也可独立执行做纯诊断）。
#
# 用法：
#   scripts/net-probe.sh [--package PKG] [--version VER]
#     独立诊断：报告代理环境（8 变量 + 有效代理）并主动探测索引页（直连 vs 经代理）。
#   scripts/net-probe.sh --analyze <stderr文件> [--index-url URL] \
#       [--package PKG] [--version VER] [--stage selfcheck|deps]
#     分析一份已捕获的 pip stderr，按判别表分类并给出最小修复建议。
#   被 setup-qwen-asr.sh 调用时传 --analyze + --index-url + --stage（selfcheck=pip
#   自升级、deps=依赖安装），包名/钉定版本由调用方或 requirements 解析，不硬编码。
#
# 判定依据（根因调查报告 2026-10-14 §6 判别表 + 实证）：stderr 签名 + 环境事实
# （8 个代理变量；pip 对 https 请求按 https_proxy→HTTPS_PROXY→all_proxy→ALL_PROXY
# 取有效代理，scheme 专属变量为空时回退 all）+ 主动探测（直连 vs 经代理对比）三者
# 互相印证；结论标注确定性（实证 = 主动探测/环境直接观测；推断 = 仅凭 stderr 签名）。
#
# 红线（实证教训，不得回退）：
#   * `from versions: none`（无 Retrying 行）在「包不存在(404)」与「代理内容级劫持
#     （403/200 垃圾页/空 200）」下逐字同形——仅凭 stderr 不可区分，不得断言
#     “索引可达/版本缺失”；有 --index-url 时必须主动探测区分。
#   * 验证命令必须同时给“直连 vs 经代理”两条：在被劫持的 shell 里单条 curl 与 pip
#     同样失败，会把用户推向“镜像缺包”的错误结论。
#   * curl 原生支持 SOCKS、pip 需要 PySocks：curl 探测通过 ≠ pip 可用，结论要写明
#     这一分歧；“未检测到 http/https 四变量”不等于 pip 直连（all_proxy 仍在生效）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
OFFICIAL_INDEX_URL="https://pypi.org/simple"
# pip（requests trust_env + urllib getproxies）实际读取的 8 个代理来源。
PROXY_VARS=(http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY)

# ---------------------------------------------------------------------------
# 代理环境工具
# ---------------------------------------------------------------------------
redact_url() {  # 凭据脱敏：scheme://user:pass@host → scheme://***@host（日志/报告安全）
  printf '%s' "$1" | sed -E 's#//[^/@:]+:[^/@]+@#//***@#g; s#//[^/@:]+@#//***@#g'
}

url_scheme() {  # 提取 scheme；无 :// 时输出空（schemeless 代理对 pip 是畸形值，须如实展示）
  case "$1" in *://*) printf '%s' "${1%%://*}" ;; *) printf '' ;; esac
}

EFFECTIVE_PROXY_URL="" EFFECTIVE_PROXY_VAR=""
resolve_effective_proxy() {  # [目标URL] — 缺省按 https 请求语义
  # pip（requests select_proxy）按目标 URL 的 scheme 取代理：先 scheme 专属变量，
  # 缺失时回退 all——残留的 all_proxy/ALL_PROXY 正是“关掉代理程序、unset 了
  # http/https 变量后仍报 SOCKS 错”的根源（根因调查 B1-B7 实证：该错误发生在
  # 建立连接之前，与代理进程死活无关）。大小写两值并存时 urllib 按环境出现顺序
  # 取后者，这里按小写优先近似，并如实打印实际采用的来源变量名。
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

socks_proxy_var_list() {  # 列出取值为 socks* 的变量名（修复建议里要 unset 的对象）
  local v s=""
  for v in http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY; do
    case "$(url_scheme "${!v:-}")" in socks5|socks5h|socks4|socks4a) s+="$v " ;; esac
  done
  printf '%s' "${s% }"
}

# ---------------------------------------------------------------------------
# 六要素之 [2] 实际索引 + [3] 代理协议与来源（脱敏）
# ---------------------------------------------------------------------------
report_env_context() {  # <index_url>
  local index_url="$1" v found=0
  echo "  [2] 实际索引：$index_url"
  local line=""
  for v in "${PROXY_VARS[@]}"; do
    if [[ -n "${!v:-}" ]]; then line+=" $v=$(redact_url "${!v}")"; found=1; fi
  done
  if [[ $found -eq 1 ]]; then
    echo "  [3] 代理环境（pip 实际继承，凭据已脱敏）：$line"
  else
    # 只有 8 个变量全部为空才能说 pip 直连；只查 4 个变量的旧断言在 all_proxy
    # 残留时是假阴性（根因调查 B 类 100% 误报“直连”）。
    echo "  [3] 代理环境：8 个代理相关变量（http/https/all/no × 大小写）均未设置——pip 将直连。"
  fi
  resolve_effective_proxy "$index_url"
  if [[ -n "$EFFECTIVE_PROXY_URL" ]]; then
    # 按本次索引 URL 的 scheme 标注链路顺序（pip select_proxy 语义：scheme 专属
    # 变量优先，缺省回退 all_proxy/ALL_PROXY）。
    local chain="https"
    [[ "$index_url" != http://* ]] || chain="http"
    echo "      有效代理（对本次索引的 $chain 请求，${chain}_proxy→${chain^^}_PROXY→all_proxy→ALL_PROXY）："
    echo "        $(redact_url "$EFFECTIVE_PROXY_URL")（协议 $(url_scheme "$EFFECTIVE_PROXY_URL")，来源 \$$EFFECTIVE_PROXY_VAR）"
    case "$index_url" in
      *tuna.tsinghua*|*aliyun*|*ustc.edu*|*sjtug.sjtu*|*pypi.douban*)
        echo "        ⚠ 国内镜像通常无需代理——全局代理可能把索引请求劫持/拦截（内容级吞掉时 pip 只报 from versions: none）。"
        ;;
    esac
  else
    echo "      有效代理：未解析到（已按索引 scheme 与 all_proxy 来源检查）。"
  fi
  if [[ -n "${PIP_PROXY:-}" ]]; then
    echo "      PIP_PROXY=$(redact_url "$PIP_PROXY")（提示：setup-qwen-asr.sh 的 pip 子进程带 --isolated，此变量对其无效；直接运行 pip 时会生效）"
  fi
  echo "      PIP_PROXY 与各级 pip.conf：在 setup-qwen-asr.sh 场景已被 --isolated + PIP_CONFIG_FILE=/dev/null 隔离；独立运行 pip 时仍按本 shell 环境生效。"
}

# ---------------------------------------------------------------------------
# 六要素之 [4] pip 关键错误（全文已在上方原样输出，这里只抽最能定位问题的行）
# ---------------------------------------------------------------------------
print_key_errors() {  # <stderr文件>
  local f="$1" picked=""
  picked="$(grep -hE 'ERROR|WARNING|Retrying|Missing dependencies|from versions|No matching distribution|Requires-Python|Ignored the following|ResolutionImpossible|conflicting dependencies|No space left|Killed|SSLError|ProxyError' -- "$f" 2>/dev/null | head -n 6 || true)"
  if [[ -z "$picked" ]]; then
    picked="$(grep -hv '^[[:space:]]*$' -- "$f" 2>/dev/null | head -n 4 || true)"
  fi
  if [[ -n "$picked" ]]; then printf '%s\n' "$picked" | sed 's/^/      /'
  else echo "      （stderr 为空——多见于代理在内容层吞掉索引请求，pip 默认 verbosity 不打印任何诊断）"
  fi
}

# ---------------------------------------------------------------------------
# 主动探测：直连 vs 经代理分别拉取 <index>/<pkg>/ simple 页并分类原始响应。
# 为什么必须两条对比：代理内容劫持下“单条 curl”与 pip 同样失败；两条对比才能
# 定位差异在镜像侧还是代理侧。curl 仅用于观察原始响应（内容级事实），涉及
# SOCKS/TLS 的 pip 侧结论仍以 stderr 签名为准（curl 原生 SOCKS ≠ pip 可用）。
# ---------------------------------------------------------------------------
PROBE_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/net-probe.XXXXXX" 2>/dev/null || printf '')"
probe_cleanup() { [[ -n "$PROBE_TMP_DIR" && -d "$PROBE_TMP_DIR" ]] && rm -rf -- "$PROBE_TMP_DIR" || true; }
trap probe_cleanup EXIT

PROBE_PACKAGE="qwen-asr" PROBE_VERSION=""
PROBE_DIRECT_CODE="000" PROBE_DIRECT_KIND="unknown" PROBE_VIA_CODE="" PROBE_VIA_KIND="skipped"

curl_fetch_classify() {  # <DIRECT|VIA> <url> [curl 参数…]
  local tag="$1" url="$2"; shift 2
  local body="$PROBE_TMP_DIR/$tag.body" err="$PROBE_TMP_DIR/$tag.err" code kind
  code="$(curl -sS -L --max-time 12 -o "$body" -w '%{http_code}' "$@" "$url" 2>"$err")" || code="000"
  kind="unknown"
  case "$code" in
    200)
      if ! grep -qiE '<a[[:space:]]' -- "$body" 2>/dev/null; then
        kind="garbage"   # 200 但不是 simple 页：垃圾 HTML / 空体（captive-portal 形态）
      elif [[ -n "$PROBE_VERSION" ]] && { grep -Fq -- "-$PROBE_VERSION-" "$body" 2>/dev/null || grep -Fq -- "-$PROBE_VERSION." "$body" 2>/dev/null; }; then
        kind="page-has-version"
      elif [[ -z "$PROBE_VERSION" ]]; then
        kind="page-valid"
      else
        kind="page-missing-version"
      fi ;;
    404) kind="not-found" ;;
    000)
      if grep -qiE 'SSL|certificate' -- "$err" 2>/dev/null; then kind="tls-error"; else kind="network-error"; fi ;;
    *)   kind="http-error" ;;
  esac
  if [[ "$tag" == "DIRECT" ]]; then PROBE_DIRECT_CODE="$code"; PROBE_DIRECT_KIND="$kind"
  else PROBE_VIA_CODE="$code"; PROBE_VIA_KIND="$kind"; fi
}

probe_index_page() {  # <index_url>：填充 PROBE_* 并打印探测过程
  local index_url="$1" page_url="${1%/}/$PROBE_PACKAGE/"
  PROBE_DIRECT_CODE="000" PROBE_DIRECT_KIND="unknown" PROBE_VIA_CODE="" PROBE_VIA_KIND="skipped"
  echo "  [探测] $page_url"
  curl_fetch_classify DIRECT "$page_url" --noproxy '*'
  echo "        直连（--noproxy '*'）：HTTP $PROBE_DIRECT_CODE / $PROBE_DIRECT_KIND"
  # 按目标 URL 的 scheme 解析 pip 实际会用的代理（http 索引走 http_proxy 链）。
  resolve_effective_proxy "$index_url"
  if [[ -n "$EFFECTIVE_PROXY_URL" ]]; then
    # 用 -x 显式走有效代理（含 all_proxy 来源）；命令行里引用变量名而非明文，避免凭据进日志。
    curl_fetch_classify VIA "$page_url" -x "$EFFECTIVE_PROXY_URL"
    echo "        经代理（-x \"\$$EFFECTIVE_PROXY_VAR\"=$(redact_url "$EFFECTIVE_PROXY_URL")）：HTTP ${PROBE_VIA_CODE:-000} / $PROBE_VIA_KIND"
  else
    echo "        经代理：无有效代理变量（与直连同路径；如需对比可先 export 代理再运行本脚本）"
  fi
}

print_compare_commands() {  # <index_url>：直连 vs 经代理两条对比命令（缺一不可）
  local page="${1%/}/$PROBE_PACKAGE/"
  echo "    对比/复现命令（在被劫持 shell 里单条 curl 会同样失败，必须两条对比）："
  echo "      直连：curl -sS --noproxy '*' -o /dev/null -w '%{http_code} %{size_download}\\n' '$page'"
  resolve_effective_proxy "$1"
  if [[ -n "$EFFECTIVE_PROXY_URL" ]]; then
    echo "      经代理：curl -sS -x \"\$$EFFECTIVE_PROXY_VAR\" -o /dev/null -w '%{http_code} %{size_download}\\n' '$page'（当前来源 \$$EFFECTIVE_PROXY_VAR）"
  else
    echo "      经代理：curl -sS -x \"\$https_proxy\" … '$page'（当前无代理变量，此条与直连等价，仅作对照）"
  fi
}

stage_label() {
  case "${1:-}" in
    selfcheck) echo "pip 自升级（selfcheck）" ;;
    deps)      echo "pip 依赖安装（deps）" ;;
    *)         echo "未知阶段（未传 --stage；setup-qwen-asr.sh 会传 selfcheck/deps）" ;;
  esac
}

# ---------------------------------------------------------------------------
# 分类核心：net_probe_analyze <stderr文件> <index_url> <stage>
# 优先级 = 信号精确度：SOCKS（签名+环境互证）> 依赖解析 > 兼容性 > 环境损坏 >
# TLS > 代理连接 > 网络通用 > 中断 > 包或版本不可用（需主动探测消歧）> 其他。
# pip 索引全挂时会先打 Retrying 再追加 No matching distribution——连接层签名
# 优先，才不会把网络/代理问题误判成“包不存在”（根因调查 §1 已实证同形性）。
# ---------------------------------------------------------------------------
net_probe_analyze() {  # <err_file> <index_url> <stage>
  local err_file="$1" index_url="$2" stage="$3"
  local text=""
  [[ -f "$err_file" ]] && text="$(<"$err_file")"

  echo ""
  echo "━━━ pip 失败诊断（scripts/net-probe.sh）━━━"
  echo "  [1] 失败阶段：$(stage_label "$stage")"
  report_env_context "$index_url"
  echo "  [4] pip 关键错误："
  print_key_errors "$err_file"
  if [[ -z "$text" ]]; then
    echo "  （未捕获到 pip stderr，按未知失败处理）"
  fi
  resolve_effective_proxy

  # ---- 1) SOCKS 支持缺失：签名精确（无 Retrying、建连前即抛）+ socks 环境互证 ----
  if printf '%s' "$text" | grep -q 'Missing dependencies for SOCKS support'; then
    local socks_vars certainty
    socks_vars="$(socks_proxy_var_list)"
    if [[ -n "$socks_vars" ]]; then certainty="实证：stderr 签名与 socks 代理变量（$socks_vars）互相印证"
    else certainty="推断：stderr 签名匹配，但当前未见 socks 代理变量（可能由 PIP_PROXY/pip.conf 注入——setup 场景已隔离，请检查本 shell）"
    fi
    cat <<EOF
  ▶ 分类：SOCKS 支持缺失 —— venv pip 没有 PySocks 却要走 socks 代理：请求在建立
    连接之前即失败，与代理进程是否存活无关（“关掉代理程序”无效，变量还在就触发）。
    确定性：$certainty。
  [6] 最小修复建议（任选其一后重跑同一命令，安装可续装）：
      1) 改用 HTTP 代理：./scripts/setup-qwen-asr.sh --proxy http://<host>:<port>
      2) 清掉 socks 代理变量后重跑：unset ${socks_vars:-all_proxy ALL_PROXY http_proxy https_proxy HTTP_PROXY HTTPS_PROXY}
         （scheme 专属变量为空时 pip 会回退 all_proxy——只 unset http/https 四个不够）
      3) 补装 PySocks：setup-qwen-asr.sh 默认会用 curl（原生 SOCKS）自动自举；
         手动等价命令：<venv>/bin/python -m pip install PySocks（或 VOICE_IME_PIP_SOCKS_BOOTSTRAP=1 保持默认开启）
    复现命令：env | grep -iE '^(all_)?proxy'；<venv>/bin/python -c 'import socks'
EOF
    return 0
  fi

  # ---- 2) 依赖解析类 ----
  if printf '%s' "$text" | grep -qiE 'ResolutionImpossible|ResolutionTooDeep|conflicting dependencies|Cannot install .* because'; then
    cat <<'EOF'
  ▶ 分类：依赖解析类 —— 版本约束互相冲突（ResolutionImpossible），不是网络问题。
    确定性：实证（pip stderr 签名唯一）。
  [6] 最小修复建议：
      1) 上报/排查：把 requirements-qwen-asr.txt 的改动与本段 pip 错误一起反馈；
      2) 若自己改过版本 pin，先恢复仓库版本：git checkout -- requirements-qwen-asr.txt 后重跑。
    复现命令：<venv>/bin/python -m pip --isolated install -r requirements-qwen-asr.txt --index-url '<索引URL>'
EOF
    return 0
  fi

  # ---- 3) 兼容性类（Requires-Python / 平台不匹配）----
  if printf '%s' "$text" | grep -qiE 'Requires-Python|Ignored the following versions'; then
    cat <<'EOF'
  ▶ 分类：兼容性类 —— 目标包版本 requires-python / 平台标签与当前解释器不匹配，
    pip 忽略了全部候选版本。确定性：实证（Ignored…Requires-Python 签名唯一）。
  [6] 最小修复建议：
      1) 安装器默认使用仓库内 CPython 3.12；若设置了 VOICE_IME_QWEN_ASR_SETUP_PYTHON
         指向其他版本，请核对该版本是否满足包的 requires-python；
      2) 不要为绕过而放松 requirements 版本约束——版本钉定经过完整验收。
    复现命令：<venv>/bin/python -V；<venv>/bin/python -m pip --isolated index versions <包名> --index-url '<索引URL>'
EOF
    return 0
  fi

  # ---- 4) 环境损坏类（pip 自身/venv 结构坏了）----
  if printf '%s' "$text" | grep -qiE 'No module named pip|pip: command not found|bad interpreter|AttributeError: module .pip.|ImportError: cannot import name .main. from .pip.'; then
    cat <<'EOF'
  ▶ 分类：环境损坏类 —— venv 的 pip 自身不可用/结构损坏（解释器路径失效、pip 模块
    缺失）。确定性：实证（stderr 签名）。
  [6] 最小修复建议：
      安装器会自动按“备份→重建→失败恢复”处理结构损坏；直接重跑同一命令即可：
        ./scripts/setup-qwen-asr.sh（保留原 VOICE_IME_* 环境变量）
    复现命令：<venv>/bin/python -m pip --version
EOF
    return 0
  fi

  # ---- 5) TLS 证书类 ----
  if printf '%s' "$text" | grep -qiE 'SSLCertVerificationError|CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLEOFError|SSL:|self-signed|hostname mismatch'; then
    cat <<EOF
  ▶ 分类：TLS 证书类 —— 证书校验失败/SSL 异常（自签证书、中间盒 MITM、代理隧道内
    异常断流）。确定性：实证（stderr 签名）。
  [6] 最小修复建议：
      1) 先看是否经代理：证书错误常由代理 MITM 引起（见 [3]），修代理优先于信任证书；
      2) 企业自签 CA 场景不建议关校验；确需排查证书链：curl -v '$index_url' -o /dev/null。
EOF
    print_compare_commands "$index_url"
    return 0
  fi

  # ---- 6) 代理类（连接层：代理不可达/协议不匹配/隧道被拒）----
  if printf '%s' "$text" | grep -qiE "ProxyError|Cannot connect to proxy|Tunnel connection failed"; then
    cat <<EOF
  ▶ 分类：代理类 —— 代理不可达/协议不匹配：代理进程未监听、地址端口写错，或 CONNECT
    隧道被代理拒绝（403 等）。确定性：实证（Retrying + ProxyError/Tunnel 签名）。
  [6] 最小修复建议：
      1) 确认代理进程在运行且端口正确（见 [3] 的来源变量与取值）；
      2) 不需要代理时彻底清掉再重跑：unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY；
      3) 国内镜像通常无需代理：VOICE_IME_PIP_INDEX_URL 默认清华源可直连。
EOF
    print_compare_commands "$index_url"
    return 0
  fi

  # ---- 7) 网络通用失败：有有效代理归“代理类（经代理失败）”，否则“网络连接类” ----
  if printf '%s' "$text" | grep -qiE 'Could not fetch URL|Retrying \(|Connection (reset|refused|timed out)|connection error|timed out|ReadTimeout|NewConnectionError|ECONNRESET|Connection kept alive'; then
    if [[ -n "$EFFECTIVE_PROXY_URL" ]]; then
      cat <<EOF
  ▶ 分类：代理类 —— 所有请求都经过代理且连接失败（代理进程存活但不可用/规则拦截，
    或代理地址不可达）。确定性：实证（Retrying/连接错误签名 + 当前有效代理 \$$EFFECTIVE_PROXY_VAR）。
  [6] 最小修复建议：
      1) 检查代理是否放行 $index_url（国内镜像常被全局代理误拦）；
      2) 不需要代理时 unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY 后重跑；
      3) 确需代理则换可用的：./scripts/setup-qwen-asr.sh --proxy http://<host>:<port>。
EOF
    else
      cat <<EOF
  ▶ 分类：网络连接类 —— 无代理变量、直连不可达（镜像被墙/断网/DNS 故障）。
    确定性：实证（Retrying/连接错误签名 + 8 个代理变量均未设置）。
  [6] 最小修复建议：
      1) 加代理重跑：./scripts/setup-qwen-asr.sh --proxy http://127.0.0.1:7890；
      2) 或换包源：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh（官方 PyPI）。
EOF
    fi
    print_compare_commands "$index_url"
    return 0
  fi

  # ---- 8) 中断类 ----
  if printf '%s' "$text" | grep -qiE 'No space left|Killed|Interrupted|KeyboardInterrupt|cancelled|canceled|Broken pipe'; then
    cat <<'EOF'
  ▶ 分类：中断类 —— 下载/安装被中断或磁盘/内存不足。确定性：实证（stderr 签名）。
  [6] 最小修复建议：
      df -h        # 确认根目录和仓库所在盘有足够空间（依赖+缓存峰值约 12GB，另加模型）
      dmesg | tail # 若见 oom-kill，说明内存不足而非网络问题
    直接重跑同一命令即可续装：安装是幂等的，已就绪的资产不会重复下载。
EOF
    return 0
  fi

  # ---- 9) 包或版本不可用：唯一需要主动探测消歧的分类 ----
  if printf '%s' "$text" | grep -qiE 'No matching distribution|from versions:'; then
    if printf '%s' "$text" | grep -qE 'from versions: *[0-9]'; then
      # stderr 列出了旧版本：真·镜像滞后签名（根因调查 A11b 实证；劫持/包不存在形态是 none）
      cat <<EOF
  ▶ 分类：包或版本不可用 —— stderr 列出旧版本（$(printf '%s' "$text" | grep -oE 'from versions: [^)"(]*' | head -n1 | sed 's/from versions: //')），
    这是真实的镜像滞后/缺版本。确定性：实证（列出旧版本是镜像滞后专属签名）。
  [6] 最小修复建议：
      1) 换官方源重跑一次：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh；
         （安装器在“默认索引+版本不可用签名+官方源实证可见”时也会自动回退一次）
      2) 或等镜像同步后重跑同一命令（已下载资产会复用）。
EOF
      print_compare_commands "$index_url"
      return 0
    fi
    # from versions: none —— 包不存在(404) / 代理内容劫持 / 网络层静默失败 三者同形，
    # 必须主动探测（直连 vs 经代理）区分；探测不了时如实说明不可区分。
    if [[ -n "$index_url" && -n "$PROBE_PACKAGE" ]]; then
      probe_index_page "$index_url"
      local verdict="" certainty="实证"
      if [[ -n "$PROBE_VIA_CODE" ]]; then
        # 经代理探测失败分两层归因（红队 2026-10-14 P2：连接层失败不是内容劫持）：
        # 连接层（HTTP 000/network-error/tls-error/超时）= 代理不可达/连接失败——请求
        # 根本没拿到内容；内容层（403/5xx 等 HTTP 状态码、200 垃圾页、404/缺版本页）
        # = 代理在内容级吞掉/改写响应。两层都要求直连可见目标版本才构成实证。
        case "$PROBE_VIA_KIND" in
          network-error|tls-error)
            case "$PROBE_DIRECT_KIND" in
              page-has-version|page-valid) verdict="proxy-conn" ;;
              *) : ;;
            esac ;;
          http-error|garbage|not-found|page-missing-version)
            case "$PROBE_DIRECT_KIND" in
              page-has-version|page-valid)
                verdict="hijack" ;;
              *) : ;;
            esac ;;
        esac
      fi
      if [[ -z "$verdict" ]]; then
        if [[ "$PROBE_DIRECT_KIND" == "not-found" && ( -z "$PROBE_VIA_CODE" || "$PROBE_VIA_KIND" == "not-found" ) ]]; then
          verdict="not-found"
        elif [[ "$PROBE_DIRECT_KIND" == "page-missing-version" && ( -z "$PROBE_VIA_CODE" || "$PROBE_VIA_KIND" == "page-missing-version" ) ]]; then
          verdict="page-missing-version"
        elif [[ "$PROBE_DIRECT_KIND" == "network-error" || "$PROBE_DIRECT_KIND" == "tls-error" ]]; then
          verdict="network"
        elif [[ "$PROBE_DIRECT_KIND" == "page-has-version" || "$PROBE_DIRECT_KIND" == "page-valid" ]]; then
          verdict="conflict-no-proxy"   # 页面可见但 pip 拿到 none 且无代理变量
        elif [[ "$PROBE_DIRECT_KIND" == "garbage" || "$PROBE_DIRECT_KIND" == "http-error" ]]; then
          verdict="garbage-no-proxy"
        fi
      fi
      case "$verdict" in
        hijack)
          cat <<EOF
  ▶ 分类：代理类 —— 内容劫持（实证）：直连能拿到含目标版本的索引页，经代理
    （\$$EFFECTIVE_PROXY_VAR）却得到 HTTP ${PROBE_VIA_CODE:-000} / $PROBE_VIA_KIND——代理在
    “内容级”吞掉了索引请求，pip 因此静默得到零候选（from versions: none）。
    这不是镜像缺包，也不是版本钉错；换镜像无法解决。
  [6] 最小修复建议：
      1) 修代理规则放行包源域名，或彻底 unset 代理变量后重跑同一命令；
      2) 国内镜像通常无需代理：unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY；
      3) 确需代理则换 HTTP 代理：./scripts/setup-qwen-asr.sh --proxy http://<host>:<port>。
EOF
          print_compare_commands "$index_url"
          ;;
        proxy-conn)
          cat <<EOF
  ▶ 分类：代理类 —— 代理不可达/连接失败（实证）：直连能拿到含目标版本的索引页，
    经代理（\$$EFFECTIVE_PROXY_VAR）却在连接层失败（HTTP ${PROBE_VIA_CODE:-000} / $PROBE_VIA_KIND）——
    代理进程未监听、地址端口不可达或隧道异常断流，请求根本没拿到任何内容，
    pip 因此静默得到零候选（from versions: none）。这是连接层失败，不是内容级
    吞掉，也不是镜像缺包；换镜像无法解决。
  [6] 最小修复建议：
      1) 确认代理进程在运行且端口正确（见 [3] 的来源变量与取值）；
      2) 不需要代理时彻底清掉再重跑：unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY；
      3) 确需代理则换可用的：./scripts/setup-qwen-asr.sh --proxy http://<host>:<port>。
EOF
          print_compare_commands "$index_url"
          ;;
        not-found)
          cat <<EOF
  ▶ 分类：包或版本不可用 —— 包不存在（实证）：直连与经代理均 404，索引上没有
    $PROBE_PACKAGE（包名拼写/镜像未收录，而非版本滞后）。
  [6] 最小修复建议：
      换官方源核实并安装：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh。
      安装器在默认索引+本签名下探测确认 404 时，会受控回退官方源重试一次。
EOF
          print_compare_commands "$index_url"
          ;;
        page-missing-version)
          cat <<EOF
  ▶ 分类：包或版本不可用 —— 页面有效但缺目标版本 $PROBE_VERSION（实证：直连与
    经代理看到同一份有效索引页，均不含该版本）——真·镜像同步滞后。
  [6] 最小修复建议：
      1) 换官方源重跑一次：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh；
      2) 或等镜像同步后重跑同一命令（已下载资产会复用）。
EOF
          print_compare_commands "$index_url"
          ;;
        network)
          cat <<EOF
  ▶ 分类：网络连接类 ——（实证：主动探测也连不上：直连 HTTP $PROBE_DIRECT_CODE / $PROBE_DIRECT_KIND）。
    pip 静默得到空候选属网络层失败的罕见形态。
  [6] 最小修复建议：
      1) 加代理重跑：./scripts/setup-qwen-asr.sh --proxy http://127.0.0.1:7890；
      2) 或换包源：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh。
EOF
          print_compare_commands "$index_url"
          ;;
        conflict-no-proxy)
          certainty="推断"
          cat <<EOF
  ▶ 分类：包或版本不可用 —— 索引页实际可见目标版本 $PROBE_VERSION，与 pip 的
    from versions: none 冲突（当前无代理变量；可能是透明网关对 pip 请求做差异化
    处理或缓存污染）。确定性：$certainty。
  [6] 最小修复建议：
      1) 先重跑一次（排除瞬时抖动）；仍失败则用下方对比命令自证差异；
      2) 换官方源：VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh。
EOF
          print_compare_commands "$index_url"
          ;;
        garbage-no-proxy)
          cat <<EOF
  ▶ 分类：代理类 —— 内容异常（实证：直连即得 HTTP $PROBE_DIRECT_CODE / $PROBE_DIRECT_KIND，
    非索引内容；当前无代理变量——疑似透明劫持/网关拦截/captive portal）。
  [6] 最小修复建议：
      核对网络出口（公共 WiFi 认证页？公司网关？）；换网络或加可信代理后重跑。
EOF
          print_compare_commands "$index_url"
          ;;
        *)
          cat <<EOF
  ▶ 分类：包或版本不可用 —— 探测结果不典型（直连 $PROBE_DIRECT_CODE/$PROBE_DIRECT_KIND，
    经代理 ${PROBE_VIA_CODE:-未测}/$PROBE_VIA_KIND），无法进一步归因。确定性：推断。
  [6] 最小修复建议：用下方对比命令人工判断差异在哪一侧，再按代理类/网络连接类处理。
EOF
          print_compare_commands "$index_url"
          ;;
      esac
      return 0
    fi
    cat <<EOF
  ▶ 分类：包或版本不可用 —— \`from versions: none\` 在「包不存在(404)」「代理内容级
    劫持（403/200 垃圾页/空 200）」「网络层静默失败」三者下逐字同形，仅凭 stderr
    不可区分。确定性：推断（未提供 --index-url，无法主动探测）。
  [6] 最小修复建议：用本脚本带 --index-url 重跑获得主动探测结论：
      scripts/net-probe.sh --analyze <stderr文件> --index-url '$index_url' --stage deps
EOF
    print_compare_commands "$index_url"
    return 0
  fi

  # ---- 10) 其他 ----
  cat <<'EOF'
  ▶ 分类：其他 —— 未匹配到已知网络失败特征，原样保留 pip 输出供排查。确定性：推断。
  [6] 最小修复建议：
      先重跑一次（安装幂等、可续装）；仍失败请把上方完整 pip 错误与本段诊断一起反馈。
      可用 scripts/net-probe.sh 独立运行做纯网络诊断。
EOF
}

# ---------------------------------------------------------------------------
# 独立诊断模式：不带 --analyze 时执行（探测索引页 + 代理环境报告）
# ---------------------------------------------------------------------------
standalone_probe() {
  local index_url="$1"
  echo "pip 索引诊断（可设 VOICE_IME_PIP_INDEX_URL 覆盖；目标包 $PROBE_PACKAGE ${PROBE_VERSION:+钉定版本 $PROBE_VERSION}）"
  report_env_context "$index_url"
  probe_index_page "$index_url"
  resolve_effective_proxy
  local verdict=""
  if [[ -n "$PROBE_VIA_CODE" ]]; then
    # 与 analyze 模式同口径：连接层失败（000/网络/TLS）≠ 内容劫持，分层归因。
    case "$PROBE_VIA_KIND" in
      network-error|tls-error)
        case "$PROBE_DIRECT_KIND" in
          page-has-version|page-valid) verdict="proxy-conn" ;;
        esac ;;
      http-error|garbage)
        case "$PROBE_DIRECT_KIND" in
          page-has-version|page-valid) verdict="hijack" ;;
        esac ;;
    esac
  fi
  if [[ -z "$verdict" ]]; then
    case "$PROBE_DIRECT_KIND" in
      page-has-version) verdict="visible" ;;
      page-valid)       verdict="valid" ;;
      page-missing-version) verdict="page-missing-version" ;;
      not-found)        verdict="not-found" ;;
      garbage|http-error) verdict="garbage" ;;
      *)                verdict="network" ;;
    esac
  fi
  case "$verdict" in
    visible)  echo "结论（实证）：索引页可见 $PROBE_PACKAGE $PROBE_VERSION——若 pip 仍报 versions: none，重点排查代理（见上方有效代理与对比命令）。" ;;
    valid)    echo "结论（实证）：索引页有效（未指定版本，未做版本核对）。" ;;
    page-missing-version) echo "结论（实证）：索引页有效但不含 $PROBE_VERSION——镜像同步滞后；可 VOICE_IME_PIP_INDEX_URL=default 换官方源。" ;;
    not-found) echo "结论（实证）：404——索引上没有 $PROBE_PACKAGE；换官方源核实。" ;;
    hijack)   echo "结论（实证）：代理内容劫持——直连可见目标版本、经代理得到 HTTP ${PROBE_VIA_CODE:-000}/$PROBE_VIA_KIND。修代理或 unset 代理变量，换镜像无效。" ;;
    proxy-conn) echo "结论（实证）：代理不可达/连接失败——直连可见目标版本、经代理连接层失败（HTTP ${PROBE_VIA_CODE:-000}/$PROBE_VIA_KIND），未拿到任何内容。确认代理进程/端口或 unset 代理变量，换镜像无效。" ;;
    garbage)  echo "结论（实证）：索引响应不是 simple 页（HTTP $PROBE_DIRECT_CODE/$PROBE_DIRECT_KIND）——劫持/网关拦截或镜像异常。" ;;
    *)        echo "结论（实证）：探测失败（HTTP $PROBE_DIRECT_CODE/$PROBE_DIRECT_KIND）——网络不可达或 TLS 异常。" ;;
  esac
  print_compare_commands "$index_url"
}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
usage() {
  cat <<'EOF'
用法：
  scripts/net-probe.sh [--package PKG] [--version VER]
  scripts/net-probe.sh --analyze <stderr文件> [--index-url URL] [--package PKG] [--version VER] [--stage selfcheck|deps]
说明：包名缺省 qwen-asr；版本缺省从 requirements-qwen-asr.txt 解析钉定版本。
EOF
}

MODE="standalone" ERR_FILE="" INDEX_URL="" INDEX_URL_PROVIDED=0 PKG="" VER="" STAGE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --analyze)
      [[ $# -ge 2 ]] || { echo "--analyze 需要 <stderr文件> 参数" >&2; exit 2; }
      MODE="analyze"; ERR_FILE="$2"; shift 2 ;;
    --index-url)
      [[ $# -ge 2 ]] || { echo "--index-url 需要 URL 参数" >&2; exit 2; }
      INDEX_URL="$2"; INDEX_URL_PROVIDED=1; shift 2 ;;
    --package)
      [[ $# -ge 2 ]] || { echo "--package 需要 PKG 参数" >&2; exit 2; }
      PKG="$2"; shift 2 ;;
    --version)
      [[ $# -ge 2 ]] || { echo "--version 需要 VER 参数" >&2; exit 2; }
      VER="$2"; shift 2 ;;
    --stage)
      [[ $# -ge 2 ]] || { echo "--stage 需要 selfcheck|deps 参数" >&2; exit 2; }
      case "$2" in selfcheck|deps) ;; *) echo "--stage 仅支持 selfcheck|deps：$2" >&2; exit 2 ;; esac
      STAGE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（$0 --help 查看用法）" >&2; exit 2 ;;
  esac
done

# 缺省包/版本：requirements-qwen-asr.txt 的 qwen-asr 钉定版本（不硬编码版本号，
# 镜像同步后无需改本脚本）。
if [[ -z "$PKG" ]]; then PKG="qwen-asr"; fi
if [[ -z "$VER" && -f "$REPO_DIR/requirements-qwen-asr.txt" ]]; then
  VER="$(sed -nE 's/^[[:space:]]*qwen[-_]asr[[:space:]]*==[[:space:]]*([0-9A-Za-z.+_-]+).*/\1/p' "$REPO_DIR/requirements-qwen-asr.txt" | head -n1)"
fi
PROBE_PACKAGE="$PKG" PROBE_VERSION="${VER:-}"

case "$MODE" in
  analyze)
    # 未显式传 --index-url 时按默认镜像推断（setup-qwen-asr.sh 总会传实际值）。
    [[ $INDEX_URL_PROVIDED -eq 1 ]] || INDEX_URL="$DEFAULT_INDEX_URL"
    net_probe_analyze "$ERR_FILE" "$INDEX_URL" "$STAGE"
    ;;
  standalone)
    INDEX_URL="${VOICE_IME_PIP_INDEX_URL:-$DEFAULT_INDEX_URL}"
    [[ "$INDEX_URL" != "default" ]] || INDEX_URL="$OFFICIAL_INDEX_URL"
    standalone_probe "$INDEX_URL"
    ;;
esac
exit 0
