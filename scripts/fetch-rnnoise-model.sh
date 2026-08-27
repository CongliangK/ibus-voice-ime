#!/usr/bin/env bash
# 下载 RNNoise 模型 bd.rnnn（ffmpeg arnndn 滤镜用，VOICE_IME_DENOISE_TIER=rnnoise 默认档）。
# 模型不进 git（vendor/models/ 在 .gitignore）；缺失时引擎自动降级 notch 档，本脚本补齐。
#
# 用法：
#   ./scripts/fetch-rnnoise-model.sh
#   ./scripts/fetch-rnnoise-model.sh --proxy http://127.0.0.1:7890
#
# 来源：GregorR/rnnoise-models（beguiling-drafter-2018-08-30，voice+recording 模型）。
# 上游声明：除 tools/ 与 README 外内容不构成创造性作品、不受版权保护。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$ROOT_DIR/vendor/models/rnnoise"
DEST="$DEST_DIR/bd.rnnn"
URL="https://github.com/GregorR/rnnoise-models/raw/master/beguiling-drafter-2018-08-30/bd.rnnn"
# md5 与开发者本机在用文件一致（2026-08-27 校验）
EXPECT_MD5="ed57f0d3983924d1478de32e0fc40e62"

CURL_ARGS=(--retry 3 --retry-delay 2 -fsSL)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy)
      [[ $# -ge 2 ]] || { echo "--proxy 需要参数" >&2; exit 2; }
      CURL_ARGS+=("--proxy" "$2")
      shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -f "$DEST" ]]; then
  actual="$(md5sum "$DEST" | awk '{print $1}')"
  if [[ "$actual" == "$EXPECT_MD5" ]]; then
    echo "RNNoise 模型已存在且校验一致：$DEST"
    exit 0
  fi
  echo "现有文件校验不一致，重新下载：$DEST"
fi

mkdir -p "$DEST_DIR"
echo "下载 $URL"
curl "${CURL_ARGS[@]}" "$URL" -o "$DEST"

actual="$(md5sum "$DEST" | awk '{print $1}')"
if [[ "$actual" != "$EXPECT_MD5" ]]; then
  echo "校验失败：期望 md5 $EXPECT_MD5，实际 $actual" >&2
  echo "已下载文件保留在 $DEST，可删除后重试。" >&2
  exit 1
fi
echo "完成：$DEST（md5 校验通过）"
