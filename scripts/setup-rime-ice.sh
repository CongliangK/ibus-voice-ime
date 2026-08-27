#!/usr/bin/env bash
# 编译部署 rime-ice（雾凇拼音）+ zhwiki + moegirl 词库到 vendored Rime 运行时。
#
# 用法：
#   ./scripts/setup-rime-ice.sh           # 默认：拉取 rime-ice + zhwiki + moegirl 并编译
#   ./scripts/setup-rime-ice.sh --no-zhwiki
#   ./scripts/setup-rime-ice.sh --no-moegirl
#   ./scripts/setup-rime-ice.sh --proxy http://127.0.0.1:7890
#
# 产物：
#   vendor/rime/share/rime-data/rime_ice.*           方案 + 词典引用
#   vendor/rime/share/rime-data/cn_dicts/            雾凇核心词库（base/ext/tencent/...）
#   vendor/rime/share/rime-data/lua/                 雾凇 lua 扩展
#   vendor/rime/share/rime-data/opencc/              OpenCC 数据
#   ~/.local/share/ibus-voice-ime/rime-user/build/   编译产物 *.table.bin / *.prism.bin
#
# 需要先运行 scripts/vendor-rime-runtime.sh 生成 vendor/rime/lib/librime.so.1。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_RIME="$ROOT_DIR/vendor/rime"
DATA_DIR="$VENDOR_RIME/share/rime-data"
LIB_DIR="$VENDOR_RIME/lib"

# ---- 参数解析 ----
WITH_ZHWIKI=1
WITH_MOEGIRL=1
GIT_PROXY_ARGS=()
CURL_PROXY_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-zhwiki) WITH_ZHWIKI=0; shift ;;
    --no-moegirl) WITH_MOEGIRL=0; shift ;;
    --proxy)
      GIT_PROXY_ARGS=(-c "http.proxy=$2" -c "https.proxy=$2")
      CURL_PROXY_ARGS=(-x "$2")
      shift 2 ;;
    --proxy=*)
      P="${1#--proxy=}"
      GIT_PROXY_ARGS=(-c "http.proxy=$P" -c "https.proxy=$P")
      CURL_PROXY_ARGS=(-x "$P")
      shift ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# ---- 前置检查 ----
if [[ ! -f "$LIB_DIR/librime.so.1" ]]; then
  echo "错误：缺少 $LIB_DIR/librime.so.1" >&2
  echo "请先运行：./scripts/vendor-rime-runtime.sh" >&2
  exit 1
fi
# 确保 librime-lua 插件就位（雾凇拼音依赖 lua_translator/lua_processor）
if [[ ! -f "$LIB_DIR/rime-plugins/librime-lua.so" ]]; then
  for ps in /usr/lib64/rime-plugins /usr/lib/rime-plugins /usr/lib/x86_64-linux-gnu/rime-plugins; do
    if [[ -f "$ps/librime-lua.so" ]]; then
      mkdir -p "$LIB_DIR/rime-plugins"
      cp -a "$ps/librime-lua.so" "$LIB_DIR/rime-plugins/"
      break
    fi
  done
fi
# Debian 的 liblua 命名为 liblua5.4.so.0（无连字符），用 liblua*.so* 覆盖两种命名。
if ! compgen -G "$LIB_DIR/liblua*.so*" >/dev/null; then
  for ll in /usr/lib64/liblua-*.so /lib64/liblua-*.so /usr/lib/liblua-*.so /lib/liblua-*.so /usr/lib/x86_64-linux-gnu/liblua*.so*; do
    [[ -f "$ll" ]] && cp -a "$ll" "$LIB_DIR/" && break
  done
fi
if ! command -v git >/dev/null 2>&1; then
  echo "错误：需要 git" >&2; exit 1
fi

PYTHON="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

TMP="$(mktemp -d --tmpdir rime-ice-setup-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DATA_DIR" "$DATA_DIR/cn_dicts" "$DATA_DIR/lua" "$DATA_DIR/opencc"

clone_shallow() {
  # clone_shallow <url> <dest>
  local url="$1" dest="$2"
  echo "==> 克隆 $url"
  if ! git "${GIT_PROXY_ARGS[@]}" clone --depth 1 "$url" "$dest" 2>&1 | sed 's/^/    /'; then
    echo "错误：克隆失败：$url" >&2
    echo "若网络不通，可用 --proxy http://127.0.0.1:7890 指定代理。" >&2
    return 1
  fi
}

# ============================================================
# 1. rime-ice（雾凇拼音）：核心方案 + 词库 + lua + opencc
# ============================================================
RIME_ICE_DIR="$TMP/rime-ice"
clone_shallow "https://github.com/iDvel/rime-ice.git" "$RIME_ICE_DIR"

echo "==> 复制 rime-ice 方案与词库到 $DATA_DIR"
# 用 rime-ice 的 default.yaml 覆盖（控制 schema_list，决定编译哪些方案），先备份原版
if [[ -f "$DATA_DIR/default.yaml" && ! -f "$DATA_DIR/default.yaml.luna-bak" ]]; then
  cp -a "$DATA_DIR/default.yaml" "$DATA_DIR/default.yaml.luna-bak"
fi
cp -a "$RIME_ICE_DIR/default.yaml" "$DATA_DIR/default.yaml"
# 顶层全部 schema/dict/辅助配置 yaml（symbols_caps_v / symbols_v / punct 等被 schema 引用）
cp -a "$RIME_ICE_DIR"/*.schema.yaml "$DATA_DIR/"
cp -a "$RIME_ICE_DIR"/*.dict.yaml "$DATA_DIR/" 2>/dev/null || true
# 辅助配置：symbols_caps_v.yaml / symbols_v.yaml（rime_ice 等方案通过 __include 引用）
for f in symbols_caps_v symbols_v; do
  [[ -f "$RIME_ICE_DIR/$f.yaml" ]] && cp -a "$RIME_ICE_DIR/$f.yaml" "$DATA_DIR/"
done
# 雾凇核心词库（base/ext/tencent/8105/41448/others，数十 MB）
cp -a "$RIME_ICE_DIR/cn_dicts/." "$DATA_DIR/cn_dicts/"
# 英文词库目录（rime-ice 用 en_dicts/，melt_eng 方案依赖）
if [[ -d "$RIME_ICE_DIR/en_dicts" ]]; then
  mkdir -p "$DATA_DIR/en_dicts"
  cp -a "$RIME_ICE_DIR/en_dicts/"*.dict.yaml "$DATA_DIR/en_dicts/" 2>/dev/null || true
fi
# 自定义短语
cp -a "$RIME_ICE_DIR/custom_phrase.txt" "$DATA_DIR/" 2>/dev/null || true
# lua 扩展（日期/计算/纠错/中英混排等，需要 librime-lua，已含在 librime.so.1）
if [[ -d "$RIME_ICE_DIR/lua" ]]; then
  mkdir -p "$DATA_DIR/lua"
  cp -a "$RIME_ICE_DIR/lua/." "$DATA_DIR/lua/"
fi
# OpenCC 数据（简繁/Emoji 转换）
if [[ -d "$RIME_ICE_DIR/opencc" ]]; then
  mkdir -p "$DATA_DIR/opencc"
  cp -a "$RIME_ICE_DIR/opencc/." "$DATA_DIR/opencc/"
fi

# ============================================================
# 2. zhwiki 词库（中文维基百科词条，几十万）
#    用 releases/expanded_assets 页面解析文件名（不限速，避开 GitHub API）
# ============================================================
GH_BASE="https://github.com"
if [[ "$WITH_ZHWIKI" -eq 1 ]]; then
  echo "==> 下载 zhwiki 词库（felixonmars/fcitx5-pinyin-zhwiki）"
  ZHWIKI_TAG="$(curl "${CURL_PROXY_ARGS[@]}" -fsSL \
    "https://github.com/felixonmars/fcitx5-pinyin-zhwiki/releases.atom" \
    | grep -oE 'Repository/[0-9]+/[0-9a-zA-Z._-]+' | head -1 | sed 's#.*/##')"
  ZHWIKI_FILE="$(curl "${CURL_PROXY_ARGS[@]}" -fsSL \
    "$GH_BASE/felixonmars/fcitx5-pinyin-zhwiki/releases/expanded_assets/$ZHWIKI_TAG" \
    | grep -oE 'zhwiki-[0-9]+\.dict\.yaml' | sort -u | tail -1)"
  if [[ -n "$ZHWIKI_FILE" ]]; then
    ZURL="$GH_BASE/felixonmars/fcitx5-pinyin-zhwiki/releases/download/$ZHWIKI_TAG/$ZHWIKI_FILE"
    echo "    $ZURL"
    # --retry 应对代理/网络偶发 TLS 中断
    curl "${CURL_PROXY_ARGS[@]}" --retry 3 --retry-delay 2 -fsSL "$ZURL" -o "$DATA_DIR/zhwiki.dict.yaml" || \
      rm -f "$DATA_DIR/zhwiki.dict.yaml"
  else
    echo "警告：无法解析 zhwiki release 文件名，跳过" >&2
  fi
fi

# ============================================================
# 3. moegirl 词库（萌娘百科/ACG/网络流行语，在 mw2fcitx 仓库）
# ============================================================
if [[ "$WITH_MOEGIRL" -eq 1 ]]; then
  echo "==> 下载 moegirl 词库（outloudvi/mw2fcitx）"
  MOEGIRL_TAG="$(curl "${CURL_PROXY_ARGS[@]}" -fsSL \
    "https://github.com/outloudvi/mw2fcitx/releases.atom" \
    | grep -oE 'Repository/[0-9]+/[0-9a-zA-Z._-]+' | head -1 | sed 's#.*/##')"
  MURL="$GH_BASE/outloudvi/mw2fcitx/releases/download/$MOEGIRL_TAG/moegirl.dict.yaml"
  echo "    $MURL"
  curl "${CURL_PROXY_ARGS[@]}" --retry 3 --retry-delay 2 -fsSL "$MURL" -o "$DATA_DIR/moegirl.dict.yaml" || \
    rm -f "$DATA_DIR/moegirl.dict.yaml"
fi

# ============================================================
# 4. 把 zhwiki/moegirl 挂进 rime_ice 词典（仅当文件下载成功时）
# ============================================================
RIME_ICE_DICT="$DATA_DIR/rime_ice.dict.yaml"
if [[ -f "$RIME_ICE_DICT" ]]; then
  ADD_ZHWIKI=0; ADD_MOEGIRL=0
  [[ "$WITH_ZHWIKI" -eq 1 && -f "$DATA_DIR/zhwiki.dict.yaml" ]] && ADD_ZHWIKI=1
  [[ "$WITH_MOEGIRL" -eq 1 && -f "$DATA_DIR/moegirl.dict.yaml" ]] && ADD_MOEGIRL=1
  if [[ "$ADD_ZHWIKI" -eq 1 || "$ADD_MOEGIRL" -eq 1 ]]; then
    echo "==> 将已下载的 zhwiki / moegirl 加入 rime_ice.dict.yaml 的 import_tables"
    python3 - "$RIME_ICE_DICT" "$ADD_ZHWIKI" "$ADD_MOEGIRL" <<'PY'
import sys
path, zw, mg = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(path, encoding="utf-8") as f:
    txt = f.read()
adds = []
if zw and "  - zhwiki\n" not in txt:
    adds.append("  - zhwiki     # 中文维基百科词条")
if mg and "  - moegirl\n" not in txt:
    adds.append("  - moegirl    # 萌娘百科/网络流行语")
if not adds:
    sys.exit(0)
marker = "  - cn_dicts/others"
if marker in txt:
    txt = txt.replace(marker, marker + "\n" + "\n".join(adds))
else:
    import re
    txt = re.sub(r"(import_tables:\s*\n)", r"\1" + "\n".join(adds) + "\n", txt, count=1)
with open(path, "w", encoding="utf-8") as f:
    f.write(txt)
print("    已追加：", ", ".join(a.strip() for a in adds))
PY
  fi
  if [[ "$WITH_ZHWIKI" -eq 1 && "$ADD_ZHWIKI" -eq 0 ]]; then
    echo "警告：zhwiki 未下载成功，跳过挂载" >&2
  fi
  if [[ "$WITH_MOEGIRL" -eq 1 && "$ADD_MOEGIRL" -eq 0 ]]; then
    echo "警告：moegirl 未下载成功，跳过挂载" >&2
  fi
fi

# ============================================================
# 5. 用 vendored librime 编译 .bin
# ============================================================
echo "==> 编译 Rime 字典（首次约 1-3 分钟，腾讯大词库较慢）"
export LD_LIBRARY_PATH="$LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export VOICE_IME_RIME_LIBRARY="$LIB_DIR/librime.so.1"
export VOICE_IME_RIME_SHARED_DATA_DIR="$DATA_DIR"
export VOICE_IME_RIME_USER_DATA_DIR="${VOICE_IME_RIME_USER_DATA_DIR:-$HOME/.local/share/ibus-voice-ime/rime-user}"
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
if ! "$PYTHON" "$ROOT_DIR/src/ibus_voice_ime/rime/rime_deploy.py"; then
  echo "错误：rime_deploy.py 编译失败" >&2
  exit 1
fi

# 把编译产物同步进 vendor/rime/build（与 run-engine.sh 的 STAGING_DIR 一致），
# 这样 IBus 运行时 select_schema 能在 staging_dir 找到 rime_ice 的 .bin。
BUILD_SRC="$VOICE_IME_RIME_USER_DATA_DIR/build"
BUILD_DST="$VENDOR_RIME/build"
if [[ -d "$BUILD_SRC" ]]; then
  echo "==> 同步编译产物到 $BUILD_DST"
  mkdir -p "$BUILD_DST"
  cp -a "$BUILD_SRC/"*.bin "$BUILD_DST/" 2>/dev/null || true
  cp -a "$BUILD_SRC/"*.schema.yaml "$BUILD_DST/" 2>/dev/null || true
fi

# ============================================================
# 6. 完成提示
# ============================================================
echo
echo "完成。雾凇拼音 + zhwiki + moegirl 已部署。"
echo "  方案/词库源：$DATA_DIR"
echo "  编译产物：  $VOICE_IME_RIME_USER_DATA_DIR/build"
echo
echo "默认方案已切换为 rime_ice。若 IBus 正在运行，重启以加载新方案："
echo "  ibus restart"
echo
echo "可运行验证：./scripts/test-rime-runtime.sh"
