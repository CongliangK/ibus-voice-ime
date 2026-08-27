#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT_DIR/vendor/rime"

export LD_LIBRARY_PATH="$RUNTIME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export VOICE_IME_RIME_LIBRARY="$RUNTIME/lib/librime.so.1"
export VOICE_IME_RIME_SHARED_DATA_DIR="$RUNTIME/share/rime-data"
export VOICE_IME_RIME_STAGING_DIR="$RUNTIME/build"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

# 该测试依赖已编译的 build 产物。
# rime_ice 的 build 在真实 user_data_dir（setup-rime-ice.sh 产物）；luna_pinyin_simp 用 vendored build。
# 优先测 rime_ice；若未编译则回退 luna_pinyin_simp（mktemp 隔离的 user 目录）。
REAL_USER_DATA_DIR="${VOICE_IME_RIME_USER_DATA_DIR:-$HOME/.local/share/ibus-voice-ime/rime-user}"
TEMP_USER_DATA_DIR=""
cleanup() { [[ -n "$TEMP_USER_DATA_DIR" ]] && rm -rf "$TEMP_USER_DATA_DIR"; }
trap cleanup EXIT

if [[ -f "$REAL_USER_DATA_DIR/build/rime_ice.table.bin" ]]; then
  # rime_ice 已部署：直接用真实 user_data_dir（含编译产物）
  export VOICE_IME_RIME_USER_DATA_DIR="$REAL_USER_DATA_DIR"
  export VOICE_IME_RIME_SCHEMA="rime_ice"
else
  # 回退 luna_pinyin_simp：用临时隔离的 user 目录
  TEMP_USER_DATA_DIR="$(mktemp -d --tmpdir ibus-voice-ime-rime-test-XXXXXX)"
  export VOICE_IME_RIME_USER_DATA_DIR="$TEMP_USER_DATA_DIR"
  export VOICE_IME_RIME_SCHEMA="luna_pinyin_simp"
fi

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON" - <<'PY'
import os
from ibus_voice_ime.rime.rime_backend import RimeSession

cases = {
    "nihao": "你好",
    "zhongwen": "中文",
}
for code, expected in cases.items():
    r = RimeSession()
    for ch in code:
        assert r.process_key(ord(ch), 0), (code, ch)
    ctx = r.context()
    got_first = ctx.candidates[0].text if ctx.candidates else ""
    assert got_first == expected, (code, got_first, expected, ctx)
    assert r.process_key(0x20, 0), code
    commit = r.get_commit()
    assert commit == expected, (code, commit, expected)
    r.close()

r = RimeSession()
for ch in "nihao":
    r.process_key(ord(ch), 0)
assert r.process_key(ord("2"), 0)
second = r.get_commit()
assert second, "数字选词没有提交"
r.close()
schema = os.environ.get("VOICE_IME_RIME_SCHEMA", "rime_ice")
print(f"OK: vendored Rime runtime works (schema={schema})")
PY
