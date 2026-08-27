#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT_DIR/vendor/rime"
LIB_DIR="$VENDOR/lib"
DATA_DIR="$VENDOR/share/rime-data"
OPENCC_DIR="$VENDOR/share/opencc"
BUILD_DIR="$VENDOR/build"

mkdir -p "$LIB_DIR" "$DATA_DIR" "$OPENCC_DIR" "$BUILD_DIR"

copy_dir_contents() {
  local src="$1" dst="$2"
  if [[ ! -d "$src" ]]; then
    echo "缺少目录：$src" >&2
    return 1
  fi
  mkdir -p "$dst"
  cp -a "$src"/. "$dst"/
}

copy_lib() {
  local lib="$1"
  [[ -f "$lib" ]] || return 0
  local name
  name="$(basename "$lib")"
  cp -L "$lib" "$LIB_DIR/$name"
}

echo "==> 复制 Rime 数据"
copy_dir_contents /usr/share/rime-data "$DATA_DIR"

# librime-lua 插件（雾凇拼音等现代方案的 lua_translator/lua_processor 依赖）
# Fedora 在 /usr/lib64/rime-plugins；Debian/Ubuntu 的 multiarch 在 /usr/lib/x86_64-linux-gnu/rime-plugins
PLUGIN_SRC=""
for candidate in /usr/lib64/rime-plugins /usr/lib/rime-plugins /usr/lib/x86_64-linux-gnu/rime-plugins; do
  if [[ -d "$candidate" ]]; then
    PLUGIN_SRC="$candidate"
    break
  fi
done
if [[ -n "$PLUGIN_SRC" ]]; then
  echo "==> 复制 Rime 插件（librime-lua 等）：$PLUGIN_SRC"
  mkdir -p "$LIB_DIR/rime-plugins"
  cp -a "$PLUGIN_SRC/"*.so "$LIB_DIR/rime-plugins/" 2>/dev/null || true
fi

if [[ -d /usr/share/opencc ]]; then
  echo "==> 复制 OpenCC 数据"
  copy_dir_contents /usr/share/opencc "$OPENCC_DIR"
fi

src_build=""
for candidate in \
  "$HOME/.config/ibus/rime/build" \
  "$HOME/.local/share/fcitx5/rime/build" \
  /usr/share/rime-data/build; do
  if [[ -d "$candidate" ]] && compgen -G "$candidate/*.schema.yaml" >/dev/null; then
    src_build="$candidate"
    break
  fi
done

if [[ -n "$src_build" ]]; then
  echo "==> 复制已部署的 Rime build 数据：$src_build"
  copy_dir_contents "$src_build" "$BUILD_DIR"
else
  echo "警告：没有找到已部署的 Rime build 数据。首次运行可能需要系统 librime 部署能力。" >&2
fi

echo "==> 复制 librime 及运行依赖"
main_lib=""
for candidate in /usr/lib64/librime.so.1 /usr/lib/librime.so.1 /lib64/librime.so.1 /lib/librime.so.1 /usr/lib/x86_64-linux-gnu/librime.so.1; do
  if [[ -f "$candidate" ]]; then
    main_lib="$candidate"
    break
  fi
done
if [[ -z "$main_lib" ]]; then
  echo "错误：没有找到系统 librime.so.1，无法生成独立运行时。" >&2
  exit 1
fi

# ldd prints transitive dependencies too.  We skip glibc/loader/kernel pseudo libs
# and keep C++/Rime/OpenCC/LevelDB/etc. private in vendor/rime/lib.
while read -r lib; do
  case "$lib" in
    /lib*/libc.so.*|/usr/lib*/libc.so.*|/lib*/libm.so.*|/usr/lib*/libm.so.*|/lib*/ld-linux*.so.*|/usr/lib*/ld-linux*.so.*) continue ;;
  esac
  copy_lib "$lib"
done < <(ldd "$main_lib" | awk '/=> \/.*\.so/ {print $3} /^\s*\/.*\.so/ {print $1}')
copy_lib "$main_lib"

# librime-lua 插件的运行时依赖 liblua（雾凇拼音的 lua_translator/lua_processor 需要）
# Debian 命名为 liblua5.4.so.0（无连字符），用 liblua*.so* 覆盖两种命名。
for lua_lib in /usr/lib64/liblua-*.so /usr/lib/liblua-*.so /lib64/liblua-*.so /lib/liblua-*.so /usr/lib/x86_64-linux-gnu/liblua*.so*; do
  [[ -f "$lua_lib" ]] && copy_lib "$lua_lib"
done

# Provide stable names used by rime_backend/run-engine.sh.
if [[ -f "$LIB_DIR/librime.so.1" ]]; then
  ln -sfn librime.so.1 "$LIB_DIR/librime.so"
fi

cat > "$VENDOR/README.txt" <<EOF
Vendored Rime runtime for ibus-voice-ime.
Generated from this machine by scripts/vendor-rime-runtime.sh.

Contents:
- lib/: librime and non-glibc runtime dependencies
- share/rime-data/: Rime schemas/dictionaries
- share/opencc/: OpenCC conversion data, if available
- build/: prebuilt Rime deployment data
EOF

chmod +x "$ROOT_DIR/run-engine.sh"

echo
echo "完成：$VENDOR"
echo "可运行 scripts/test-rime-runtime.sh 验证独立 Rime 输入核心。"
