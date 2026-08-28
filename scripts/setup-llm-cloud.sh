#!/usr/bin/env bash
# 配置云端 LLM 后处理（OpenAI 兼容接口）。
#
# 交互式生成 ~/.config/ibus-voice-ime/llm.json（权限 0600）：
#   base_url + api_key + 精确 model ID。
# 设计约定：不查询模型列表，model 必须与服务商的 ID 完全一致；
# 写错的 ID 会在第一次真实调用时得到明确的 HTTP 错误。
# 引擎在每次语音后处理前重新读取该文件，配置即刻生效，无需重启。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${VOICE_IME_LLM_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/ibus-voice-ime/llm.json}"

echo "==> 云端 LLM 后处理配置（OpenAI 兼容接口）"
echo "    配置文件：$CONFIG_FILE"
echo "    字段模板：${ROOT_DIR}/examples/llm-cloud.json.example"
echo

read -r -p "1/3 base_url（OpenAI 兼容根地址，通常以 /v1 结尾，例如 https://api.openai.com/v1）: " BASE_URL
BASE_URL="${BASE_URL//[[:space:]]/}"
while [[ -z "$BASE_URL" ]]; do
  read -r -p "   base_url 不能为空，请重新输入: " BASE_URL
  BASE_URL="${BASE_URL//[[:space:]]/}"
done

read -r -p "2/3 model（必须填写完全正确的模型 ID；本工具不做模型列表查询）: " MODEL
MODEL="${MODEL//[[:space:]]/}"
while [[ -z "$MODEL" ]]; do
  read -r -p "   model 不能为空，请重新输入: " MODEL
  MODEL="${MODEL//[[:space:]]/}"
done

read -r -s -p "3/3 api_key（输入不回显）: " API_KEY
echo
API_KEY="${API_KEY//[[:space:]]/}"
while [[ -z "$API_KEY" ]]; do
  read -r -s -p "   api_key 不能为空，请重新输入: " API_KEY
  echo
  API_KEY="${API_KEY//[[:space:]]/}"
done

mkdir -p "$(dirname "$CONFIG_FILE")"
PYTHONPATH="$ROOT_DIR/src" python3 - "$CONFIG_FILE" "$BASE_URL" "$MODEL" "$API_KEY" <<'PY'
import json
import os
import sys

path, base_url, model, api_key = sys.argv[1:5]
data = {
    "enabled": True,
    "base_url": base_url,
    "api_key": api_key,
    "model": model,
    "timeout": 15,
    "temperature": 0.1,
    "max_tokens": 1024,
    "min_chars": 50,
    "extra_body": {},
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.chmod(path, 0o600)
PY

echo
if ! OUT=$(PYTHONPATH="$ROOT_DIR/src" python3 -c '
from ibus_voice_ime.text import llm_cloud_config
cfg, problems = llm_cloud_config.load_report()
if cfg is None:
    print("配置未通过校验：")
    for p in problems:
        print("  - " + p)
    raise SystemExit(1)
print(f"配置有效：{cfg.model} @ {cfg.base_url}")
'); then
  echo "$OUT" >&2
  echo "错误：配置文件已写入但未通过校验，请编辑 $CONFIG_FILE 后重试。" >&2
  exit 1
fi
echo "$OUT"

echo
read -r -p "现在用一句测试文本验证连通性（会真实调用一次 API）？[y/N] " ANSWER
if [[ "$ANSWER" =~ ^[Yy] ]]; then
  echo "    正在调用，请稍候……"
  PYTHONPATH="$ROOT_DIR/src" python3 - <<'PY'
from ibus_voice_ime.text import llm_postprocess

result = llm_postprocess.refine("今天我们开个会讨论一下新版本的发布计划")
print("接口返回：" + result)
PY
fi

echo
echo "==> 完成。配置即刻生效（引擎每次后处理前重新读取，无需重启输入法）。"
echo "    体检：./scripts/doctor.sh    关闭：把 json 中 enabled 改为 false"
