#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the config schema reference table for docs/configuration.md.

数据源是仓库内的两份权威文件：
- config/defaults.json —— 每个配置叶子的默认值；
- src/ibus_voice_ime/config.py 的 ENV_MAP —— 点路径 → 环境变量名映射。

用法：

    env python3 tools/gen-config-docs.py            # 打印 markdown 表到 stdout
    env python3 tools/gen-config-docs.py --write    # 更新 docs/configuration.md
                                                   # 中标记之间的表（其余内容不动）

默认值列会截断超长文本（内置提示词等），完整原文以 config/defaults.json 为准。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ibus_voice_ime.config import ENV_MAP  # noqa: E402

DOC_PATH = ROOT / "docs" / "configuration.md"
BEGIN_MARKER = "<!-- BEGIN: 由 tools/gen-config-docs.py 生成的 schema 表（勿手改） -->"
END_MARKER = "<!-- END: schema 表 -->"

# 精确路径 → 说明（重要键逐个写，其余按 GROUP 描述兜底）。
DESCRIPTIONS: dict[str, str] = {
    "asr.backend": "渠道唯一事实源：qwen3-asr / mimo-asr / mimo-cloud(-asr) / volc-bigmodel-asr / siliconflow(-asr) / faster-whisper / vosk / 自定义命令",
    "asr.command": "自定义 STT 命令（`{wav}` 占位），stdout 即识别文本",
    "asr.sidecar_max_body_bytes": "sidecar HTTP 请求体上限（字节）",
    "asr.qwen3.model": "本地 Qwen3-ASR 档位：0.6b / 1.7b",
    "asr.qwen3.python": "sidecar 专用 venv 的 python 路径（switch 脚本自动写入）",
    "asr.mimo_cloud.api_key_secret": "BWS 密钥名（不是密钥本体）；run-engine.sh 按名注入",
    "asr.volc.api_key_secret": "BWS 密钥名（不是密钥本体）；run-engine.sh 按名注入",
    "asr.siliconflow.api_key_secret": "BWS 密钥名（不是密钥本体）；run-engine.sh 按名注入",
    "asr.siliconflow.model": "模型名，如 FunAudioLLM/SenseVoiceSmall（免费）/ Qwen/Qwen3-ASR-1.7B",
    "asr.whisper.require_gpu": "显式要求 GPU STT，防误回退 CPU",
    "llm.postprocess": "LLM 后处理总开关",
    "llm.internal": "内部（引擎内嵌）LLM 路径开关；null = 跟随部署形态",
    "llm.base_url": "OpenAI-compatible LLM 端点；连接密钥走 llm.json，不放这里",
    "llm.timeout": "LLM 请求超时（秒）",
    "llm.min_chars": "短于该字符数的识别结果跳过 LLM 后处理",
    "llm.candidates": "LLM 标点候选数",
    "llm.extra_prompt": "追加到 system 提示词的额外要求",
    "llm.prompts.system": "LLM 后处理 system 提示词（多行文本，覆盖示例见文档）",
    "llm.prompts.candidate_system": "候选生成专用轻量 system 提示词",
    "llm.prompts.format_requirement": "输出格式要求片段",
    "llm.prompts.mode_instructions.dictation": "听写模式附加指令",
    "llm.prompts.mode_instructions.literal": "原样模式附加指令",
    "llm.prompts.mode_instructions.markdown": "Markdown 模式附加指令",
    "llm.prompts.mode_instructions.prompt": "提示词模式附加指令",
    "llm.prompts.mode_instructions.command": "命令模式附加指令",
    "asr.prompts.whisper_punctuation": "faster-whisper 标点提示词（多行文本）",
    "recording.trigger_mode": "toggle：按一次开始再按一次停止；fixed：固定时长",
    "recording.hotkeys": "语音热键（仅支持 Ctrl+Alt+字母）",
    "recording.raw_hotkeys": "原文语音热键（跳过 LLM 后处理）",
    "recording.max_seconds": "toggle 模式最长录音时长（秒）",
    "recording.record_seconds": "fixed 模式固定录音时长（秒）",
    "recording.voice_mode": "默认语音模式：dictation / literal / markdown / prompt / command",
    "recording.clipboard_hotkeys": "输入法内粘贴热键",
    "recording.arecord_device": "直采 ALSA 设备（如 plughw:M2,0）；空 = 系统默认源",
    "recording.vad_auto_stop": "fixed 模式静音自动停止",
    "recording.vad_fallback_fixed": "VAD 失败时回退固定时长录音",
    "audio.denoise_tier": "降噪档位：none / notch / rnnoise",
    "overlay.enabled": "独立语音状态弹窗开关",
    "ui.candidate_ui": "popup：传统 IBus/GNOME 竖向候选框；inline：行内候选",
    "ui.inline_candidates": "行内候选数量；null = 跟随 candidate_page_size",
    "ui.keyboard_backend": "rime：RIME 键盘引擎；demo：core.py 演示词库",
    "ui.start_ascii": "启动后先进入英文模式",
    "ui.shift_toggle_ascii": "按一下 Shift 切换中/英",
    "text.chinese_script": "simplified / traditional / none（最终提交前繁简转换）",
    "text.cjk_latin_space": "中文与拉丁字符间补空格",
    "text.voice_commands": "语音口令转符号（换行/逗号/句号…）",
    "text.inline_voice_commands": "识别非独立口令（默认关闭，防误触发）",
    "text.remove_fillers": "移除独立口头禅",
    "text.voice_dictionary": "热词/纠词词典路径（volc 热词直传也用它）",
    "rime.schema": "RIME 方案；未部署雾凇时自动回退 luna_pinyin_simp",
    "rime.library": "librime.so 路径（Fedora 可指系统库）",
    "logging.keep_lines": "每次启动裁剪日志保留行数；0 关闭",
    "ipc.enabled": "本地 IPC socket（voice-toggle.sh / 粘贴热键用）",
}

# 前缀 → 分组说明（最长前缀优先；表格“说明”列的兜底）。
GROUP_DESCRIPTIONS = [
    ("asr.qwen3.", "本地 Qwen3-ASR sidecar"),
    ("asr.mimo_cloud.", "MiMo 云端 ASR / Token Plan"),
    ("asr.mimo.", "本地 MiMo-V2.5-ASR sidecar"),
    ("asr.volc.", "火山引擎豆包 bigmodel ASR"),
    ("asr.siliconflow.", "硅基流动 SiliconFlow ASR（Beta）"),
    ("asr.whisper.", "faster-whisper 诊断/兜底后端"),
    ("asr.vosk.", "vosk 后端"),
    ("asr.prompts.", "ASR 提示词"),
    ("llm.llama_legacy.", "已弃用的本地 llama.cpp sidecar"),
    ("llm.prompts.", "LLM 后处理提示词"),
    ("recording.", "录音/热键/VAD"),
    ("audio.", "音频预处理"),
    ("overlay.", "语音状态弹窗"),
    ("ui.", "候选框/输入 UI"),
    ("text.", "文本规整"),
    ("rime.", "RIME 键盘后端"),
    ("logging.", "日志"),
    ("ipc.", "本地 IPC"),
]


def _iter_leaves(node: dict, prefix: str = ""):
    for key, value in node.items():
        if key.startswith("_") or key == "version":
            continue
        if isinstance(value, dict):
            yield from _iter_leaves(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value


def _describe(path: str) -> str:
    if path in DESCRIPTIONS:
        return DESCRIPTIONS[path]
    for prefix, text in GROUP_DESCRIPTIONS:
        if path.startswith(prefix):
            return text
    return ""


def _render_default(value) -> str:
    if value is None:
        return "`null`（动态默认/回退调用点）"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    text = str(value)
    if not text:
        return "`\"\"`（空）"
    if len(text) > 48:
        head = text[:40].replace("|", "\\|")
        return f"`\"{head}…\"`（长文本，全文见 defaults.json）"
    return f"`\"{text.replace('|', chr(92) + '|')}\"`"


def build_table() -> str:
    defaults = json.loads((ROOT / "config" / "defaults.json").read_text(encoding="utf-8"))
    path_to_env = {path: env for env, path in ENV_MAP.items()}
    lines = [
        "| 点路径 | 默认值 | 环境变量（最高优先覆盖） | 说明 |",
        "|---|---|---|---|",
    ]
    for path, value in _iter_leaves(defaults):
        env_name = path_to_env.get(path, "—")
        lines.append(f"| `{path}` | {_render_default(value)} | `{env_name}` | {_describe(path)} |")
    return "\n".join(lines)


def write_doc(table: str) -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    if BEGIN_MARKER not in doc or END_MARKER not in doc:
        raise SystemExit(f"{DOC_PATH} 缺少生成标记，拒绝写入")
    head, _, rest = doc.partition(BEGIN_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    DOC_PATH.write_text(
        head + BEGIN_MARKER + "\n\n" + table + "\n\n" + END_MARKER + tail,
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="更新 docs/configuration.md 中的标记段落")
    args = parser.parse_args(argv)
    table = build_table()
    if args.write:
        write_doc(table)
        print(f"已更新 {DOC_PATH}（{len(table.splitlines()) - 2} 个配置键）")
    else:
        print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
