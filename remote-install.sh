#!/usr/bin/env bash
# 远程安装引导器：克隆或安全更新仓库，再运行 setup.sh；不替代手动克隆安装。
# 用法（调用方须已有 git 与 curl，本脚本绝不 sudo）：
#   curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-install.sh | bash -s -- --defaults
# 所有参数原样透传给 setup.sh，重复运行会更新仓库并重跑幂等安装。
# 环境变量（显式空值或含空白的值均拒绝）：
#   IBUS_VOICE_IME_DIR  默认 $HOME/.local/share/ibus-voice-ime-repo（克隆目录；
#                       不得使用运行时数据根 ~/.local/share/ibus-voice-ime——
#                       首次安装后 rime-user 等用户数据会让克隆变脏，下次更新被拒）
#   IBUS_VOICE_IME_REPO 默认 https://github.com/CongliangK/ibus-voice-ime.git（可信镜像也可）
#   IBUS_VOICE_IME_REF  默认 main（分支名）
# 注意：curl | bash 的 stdin 是脚本管道，setup.sh 的 -t 0 会因此自动采用默认值。
# 有可打开的 /dev/tty 时把 setup 的 stdin 接回终端，保留交互向导；节点存在但
# 没有控制终端也视为无人值守，保持原 stdin，由 setup 自行非终端降级，不追加 --yes。
set -euo pipefail

fail() { printf '错误：%s\n' "$1" >&2; exit 1; }
network_hint() {
  printf '%s\n' '请检查网络、仓库地址和分支；可设置 https_proxy，或将 IBUS_VOICE_IME_REPO 指向可信镜像后重试。' >&2
}

# 安装再生目录：setup-rime-ice.sh 每次安装从上游 rime-ice 复制/改写整个
# vendor/rime/share/rime-data（含按 --no-zhwiki 等选项改写 rime_ice.dict.yaml
# 的 import_tables），枚举单个文件注定不完备（红队实测多条覆盖点）。
# 模型：该目录整体属于"安装再生的运行时数据"（全部保持 git 跟踪，离线
# 种子不丢）——更新时自动还原 tracked/暂存修改、清理非 ignored 的未跟踪
# 残留；用户不应在此目录内保存任何手工修改。
GENERATED_DIR='vendor/rime/share/rime-data'

# 单个 porcelain 路径（剥引号）是否在再生目录内。
is_generated_path() {
  local path="$1"
  path="${path%\"}"; path="${path#\"}"
  [[ "$path" == "$GENERATED_DIR"/* ]]
}

# 判断 porcelain 行（含重命名 old -> new 的两侧）的全部路径是否都在再生
# 目录内。绝不按空白拆词：带空格路径必须整段解析（红队 A2 发现）。
is_generated_line() {
  local line="$1" left right
  line="${line:3}"
  if [[ "$line" == *' -> '* ]]; then
    left="${line%% -> *}"
    right="${line#* -> }"
    is_generated_path "$left" && is_generated_path "$right"
  else
    is_generated_path "$line"
  fi
}

DIR="${IBUS_VOICE_IME_DIR-${HOME-}/.local/share/ibus-voice-ime-repo}"
REPO="${IBUS_VOICE_IME_REPO-https://github.com/CongliangK/ibus-voice-ime.git}"
REF="${IBUS_VOICE_IME_REF-main}"
# 去掉尾部斜杠与 /. 尾缀：/tmp/link/ 或 /tmp/link/. 的 -L 判定为假，
# 会绕过符号链接检查；数据根比对同理须用规范化路径（红队发现）。
while [[ "$DIR" != "/" ]]; do
  case "$DIR" in
    */) DIR="${DIR%/}" ;;
    */.) DIR="${DIR%/.}" ;;
    *) break ;;
  esac
done
for name in IBUS_VOICE_IME_DIR IBUS_VOICE_IME_REPO IBUS_VOICE_IME_REF; do
  case "$name" in
    IBUS_VOICE_IME_DIR) value="$DIR" ;;
    IBUS_VOICE_IME_REPO) value="$REPO" ;;
    IBUS_VOICE_IME_REF) value="$REF" ;;
  esac
  [[ -n "$value" && ! "$value" =~ [[:space:][:cntrl:]] && "$value" != -* ]] ||
    fail "$name 非法：不得为空、含空白或控制字符，也不得以短横线开头。"
done
[[ -n "${IBUS_VOICE_IME_DIR+x}" || -n "${HOME-}" ]] || fail 'HOME 未设置，请显式设置 IBUS_VOICE_IME_DIR。'
git check-ref-format --branch "$REF" >/dev/null 2>&1 || fail 'IBUS_VOICE_IME_REF 非法：请指定有效的分支名。'
# 运行时数据根（rime-user/词库/日志）与克隆必须分离：混用会让克隆在首次
# 安装后变脏，下一次引导器拒绝自动更新（红队 2026-10-11 发现）。
# 双侧 realpath -m 规范化后再比对，/./ 与 /../ 中缀不得绕过。
canon() { realpath -m -- "$1" 2>/dev/null || printf '%s' "$1"; }
[[ "$(canon "$DIR")" != "$(canon "${HOME-}/.local/share/ibus-voice-ime")" ]] ||
  fail 'IBUS_VOICE_IME_DIR 不得使用运行时数据根 ~/.local/share/ibus-voice-ime（安装数据会让克隆变脏）；请使用其他目录。'

if [[ -e "$DIR" || -L "$DIR" ]]; then
  [[ -d "$DIR" && ! -L "$DIR" && -e "$DIR/.git" ]] ||
    fail '安装目录已存在，但不是可更新的 Git 克隆（或是符号链接）；不会覆盖，请设置 IBUS_VOICE_IME_DIR 使用其他目录。'
  TOP="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null)" || fail '安装目录不是有效的 Git 工作仓库，不会覆盖。'
  DIR="$(cd -- "$DIR" && pwd -P)"
  [[ "$TOP" == "$DIR" ]] || fail '安装目录不是 Git 仓库根目录，不会覆盖。'
  ORIGIN="$(git -C "$DIR" remote get-url origin 2>/dev/null)" || fail '现有克隆缺少 origin 远端，不会覆盖。'
  [[ "$ORIGIN" == "$REPO" ]] || fail '现有克隆的 origin 与 IBUS_VOICE_IME_REPO 不匹配，不会覆盖；请检查仓库地址或使用其他目录。'
  # 变更分流：再生目录内的变更（tracked 修改/删除/暂存/未跟踪残留）更新时
  # 自动还原/清理；目录外的任何变更照旧拒绝。本地提交保护（marker）与
  # ignored 碰撞预检都先于还原执行——拒绝路径绝无副作用（红队发现顺序漏洞）。
  # porcelain -z + 整段 python 分类：NUL 记录（重命名/复制是两条），文件名
  # 含 " -> "、空格、换行、非 UTF-8 均不会被误解析（红队 A2 发现）。
  CLASSIFY="$(git -C "$DIR" status --porcelain -z --untracked-files=all | python3 -c '
import sys
GEN = "vendor/rime/share/rime-data/"
fields = sys.stdin.buffer.read().split(b"\0")
generated = other = 0
i = 0
while i < len(fields):
    rec = fields[i]; i += 1
    if not rec:
        continue
    paths = [rec[3:]]
    if rec[:1] in (b"R", b"C") and i < len(fields):
        paths.append(fields[i]); i += 1
    if all(p.decode("utf-8", "surrogateescape").startswith(GEN) for p in paths):
        generated += 1
    else:
        other += 1
print(generated, other)')" || fail '无法检查本地变更，拒绝自动更新。'
  GENERATED_DIRTY=0 OTHER_COUNT=0
  read -r GENERATED_COUNT OTHER_COUNT <<<"$CLASSIFY" || true
  [[ "${GENERATED_COUNT:-0}" -gt 0 ]] && GENERATED_DIRTY=1
  # 嵌套 git 仓库不会被 clean -f 删除（需 -ff）；安装不会在再生目录里创建
  # 仓库，出现即视为用户数据，拒绝而不是强删（红队 A2 发现）。
  if [[ -n "$(find "$DIR/$GENERATED_DIR" -name .git -print -quit 2>/dev/null)" ]]; then
    fail "再生目录 $GENERATED_DIR 内存在嵌套 Git 仓库（用户数据），拒绝自动更新；请自行处理后重试。"
  fi
  if [[ "${OTHER_COUNT:-1}" -ne 0 ]]; then
    fail "现有克隆有未提交变更（含未跟踪文件），拒绝自动覆盖；请把需要的文件备份到克隆目录之外，或删除整个克隆目录让引导器重新拉取（不要在克隆里提交：本地提交同样会被拒绝自动更新）。注：$GENERATED_DIR/ 内的变更属安装再生数据，会自动还原，不计入。"
  fi
  printf '%s\n' '正在更新已有的干净克隆……'
  # 显式分支映射支持浅克隆切换分支，避免 reset 到过时的远端跟踪引用。
  if ! git -C "$DIR" fetch --depth 1 origin "+refs/heads/$REF:refs/remotes/origin/$REF"; then
    network_hint
    fail '获取远端分支失败，尚未重置本地文件。'
  fi
  TARGET_REF="refs/remotes/origin/$REF"
  # 标签/简写歧义防护：一律用全限定 ref，避免名为 origin/<ref> 的标签
  # 让 reset 指向旧版本（红队 2026-10-11 发现）。
  NEW_HEAD="$(git -C "$DIR" rev-parse --verify "$TARGET_REF^{commit}")" || fail '远端分支解析失败，尚未重置本地文件。'
  # ignored 文件碰撞预检（fail-closed）：本地被忽略的文件若与新版本跟踪
  # 文件冲突，reset --hard 会静默覆盖/删除（用户数据丢失）。两类冲突都查：
  # 完全同名与路径前缀冲突（ignored 文件 a.local vs incoming 目录 a.local/x，
  # 或反之）。结果经退出码传递（0=无冲突 2=有冲突 其它=预检故障），不用
  # stdout 文本判据——文件名恰为换行等畸形名时命令替换会剥成空串导致假
  # 通过；内部子进程失败必须显式非零退出，否则 fail-open（红队注入实测）。
  COLLISION_RC=0
  COLLISION="$(
    DIR="$DIR" TARGET_REF="$TARGET_REF" python3 /dev/stdin <<'PY_COLLISION'
import os, subprocess, sys
repo, target_ref = os.environ["DIR"], os.environ["TARGET_REF"]
def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)
def git_bytes(*args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if proc.returncode != 0:
        die("git " + " ".join(args[:2]) + " failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout
incoming = set(p.decode("utf-8", "surrogateescape") for p in git_bytes("ls-tree", "-r", "--name-only", "-z", target_ref).split(b"\0") if p)
proc = subprocess.run(["git", "-C", repo, "ls-files", "-o", "-i", "--exclude-standard", "-z"],
                      capture_output=True)
if proc.returncode != 0:
    die("git ls-files failed: " + proc.stderr.decode("utf-8", "replace").strip())
ignored = [p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\0") if p]
hits = set()
for g in ignored:
    if g in incoming:
        hits.add(g)
        continue
    parts = g.split("/")
    if any("/".join(parts[:i]) in incoming for i in range(1, len(parts))):
        hits.add(g)
        continue
    if any(t.startswith(g + "/") for t in incoming):
        hits.add(g)
for h in sorted(hits):
    name = h.encode("utf-8", "surrogateescape")
    name = name.replace(b"\n", b"LF-ESC").replace(b"\r", b"CR-ESC")
    sys.stdout.buffer.write(name + b"\n")
sys.exit(2 if hits else 0)
PY_COLLISION
  )" || COLLISION_RC=$?
  case "$COLLISION_RC" in
    0) ;;
    2) fail "本地被忽略的文件与新版本跟踪路径冲突，拒绝覆盖：$COLLISION" ;;
    *) fail 'ignored 碰撞预检失败（fail-closed），拒绝自动更新。' ;;
  esac
  # 本地提交保护：引导器用 .git 内的同步标记识别自己管理的克隆；HEAD
  # 与标记不一致说明用户提交过本地变更，浅克隆无法区分落后/分叉，
  # 一律拒绝自动重置（红队 2026-10-11 发现本地提交会被 reset 删除）。
  MARKER="$DIR/.git/ibus-voice-ime-bootstrap"
  HEAD_NOW="$(git -C "$DIR" rev-parse HEAD)"
  if [[ "$HEAD_NOW" != "$NEW_HEAD" ]]; then
    [[ -f "$MARKER" && "$(cat -- "$MARKER")" == "$HEAD_NOW" ]] ||
      fail '克隆的 HEAD 与引导器上次同步的不一致（可能存在本地提交）；为防数据丢失拒绝自动更新，请自行处理后再试。'
  fi
  # 所有拒绝性检查（origin/变更分流/碰撞/本地提交）均已通过才动工作区：
  # ① reset -q 先把该目录的 index 条目还原到 HEAD（清掉暂存的新增/重命名
  # ——checkout HEAD -- dir 不移除 HEAD 没有的 index 项，红队实测 staged-new
  # 残留假成功）；② checkout HEAD -- dir 恢复工作区；③ clean 清非 ignored
  # 未跟踪残留（下载产物多为 ignored，不受影响）；reset 前清理避免阻塞。
  if [[ $GENERATED_DIRTY -eq 1 ]]; then
    git -C "$DIR" reset -q -- "$GENERATED_DIR" ||
      fail "还原安装再生目录 $GENERATED_DIR 失败（reset），拒绝自动更新。"
    git -C "$DIR" checkout HEAD -- "$GENERATED_DIR" ||
      fail "还原安装再生目录 $GENERATED_DIR 失败，拒绝自动更新。"
    git -C "$DIR" clean -fdq -- "$GENERATED_DIR" ||
      fail "清理安装再生目录 $GENERATED_DIR 失败，拒绝自动更新。"
    printf '已还原安装再生目录 %s（setup-rime-ice.sh 每次安装会从上游覆盖它；目录内手工修改不保留）。\n' "$GENERATED_DIR"
  fi
  if [[ "$HEAD_NOW" != "$NEW_HEAD" ]]; then
    git -C "$DIR" reset --hard "$TARGET_REF" || fail '更新本地克隆失败，请检查目录权限与 Git 状态。'
  fi
  printf '%s\n' "$NEW_HEAD" > "$MARKER"
else
  printf '%s\n' '正在克隆安装仓库……'
  if ! git clone --depth 1 -b "$REF" -- "$REPO" "$DIR"; then
    network_hint
    fail '克隆失败，安装尚未开始；若留下半成品目录，请检查后自行处理或指定其他目录。'
  fi
  DIR="$(cd -- "$DIR" && pwd -P)"
  printf '%s\n' "$(git -C "$DIR" rev-parse HEAD)" > "$DIR/.git/ibus-voice-ime-bootstrap"
fi

[[ -f "$DIR/setup.sh" ]] || fail '仓库缺少 setup.sh，请检查仓库地址与分支。'
# 为克隆设置的 https_proxy 会留在本 shell 并被安装器（pip/模型下载）继承；
# 代理进程随后关闭时，pip 会报出误导性的 "No matching distribution found"。
# 这里提醒一句，避免"克隆时代理可用、装依赖时代理已关"的残留坑。
if [[ -n "${https_proxy:-}${http_proxy:-}${HTTPS_PROXY:-}${HTTP_PROXY:-}" ]]; then
  printf '提示：当前 shell 设置了代理（%s）。安装器会继承它；若稍后关闭代理进程，\n' "${https_proxy:-${http_proxy:-${HTTPS_PROXY:-${HTTP_PROXY:-}}}}"
  printf '请先 unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 再继续安装。\n'
fi
cd -- "$DIR"
# /dev/tty 通常始终存在；须同时探测能否打开，避免真正无人值守时退出。
# 放在同一复合命令中：bash 必须先从脚本管道读完 exec，再切换 stdin，
# 否则 curl | bash 会在切换后去终端读取脚本的下一行，导致安装入口未执行。
{
  if [[ -e /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
    exec </dev/tty
  fi
  exec bash "$DIR/setup.sh" "$@"
}
