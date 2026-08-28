# -*- coding: utf-8 -*-
"""Cloud LLM post-processing configuration (OpenAI-compatible endpoint).

The LLM polish layer talks to any OpenAI-compatible ``/chat/completions``
endpoint.  Configuration lives in a single user-owned JSON file::

    ~/.config/ibus-voice-ime/llm.json      (override: VOICE_IME_LLM_CONFIG)

    {
      "enabled": true,
      "base_url": "https://api.your-provider.com/v1",
      "api_key": "sk-...",
      "model": "the-exact-model-id",
      "timeout": 15,
      "temperature": 0.1,
      "max_tokens": 1024,
      "min_chars": 50,
      "extra_body": {}
    }

``extra_body`` 里的键值会合并进请求体，用于服务商特有参数（例如 GLM
思考型模型关思考：``{"thinking": {"type": "disabled"}}``）——这是通用
OpenAI 兼容接口之外唯一的扩展点，避免为每家服务商写专用代码。

Design contract (deliberately minimal, deliberately reliable):

* The user supplies ``base_url`` + ``api_key`` + an **exact** model ID.
* There is **no model-list discovery** (no ``GET /models``): a wrong model ID
  must fail loudly at call time instead of being papered over by a picker.
* When this file is absent or invalid, the LLM layer stays disabled and the
  engine falls back to deterministic rule-based cleanup.

The file may contain a raw API key, so the setup script writes it with 0600
permissions and ``doctor`` warns when the permissions are looser.  The file
lives outside the repository and must never be committed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 15.0
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MIN_CHARS = 50

_PLACEHOLDER_MARKERS = ("填入", "填上", "your-api-key", "your-model", "REPLACE", "在这里", "example")


@dataclass(frozen=True)
class CloudConfig:
    """A validated cloud LLM endpoint configuration."""

    enabled: bool
    base_url: str
    api_key: str
    model: str
    timeout: float
    temperature: float
    max_tokens: int
    min_chars: int
    extra_body: dict
    source: Path


def config_path() -> Path:
    override = os.environ.get("VOICE_IME_LLM_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "ibus-voice-ime" / "llm.json"


def _looks_like_placeholder(value: str) -> bool:
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


def _validate(data: dict, problems: list[str]) -> dict:
    """Fill defaults / coerce types, appending human-readable problems."""
    out: dict = {}

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append("enabled 需为 true/false 布尔值")
        enabled = True
    out["enabled"] = enabled

    base_url = data.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        problems.append("缺少 base_url（OpenAI 兼容根地址，通常以 /v1 结尾）")
    else:
        base_url = base_url.strip()
        if _looks_like_placeholder(base_url):
            problems.append("base_url 还是占位符，请改成服务商真实地址")
        elif not (base_url.startswith("http://") or base_url.startswith("https://")):
            problems.append(f"base_url 需以 http(s):// 开头，当前：{base_url}")
        out["base_url"] = base_url

    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        problems.append("缺少 api_key（云端接口必填）")
    else:
        api_key = api_key.strip()
        if _looks_like_placeholder(api_key):
            problems.append("api_key 还是占位符，请改成真实 Key")
        out["api_key"] = api_key

    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        problems.append("缺少 model（必须填写与服务商完全一致的模型 ID，本工具不提供模型列表查询）")
    else:
        model = model.strip()
        if _looks_like_placeholder(model):
            problems.append("model 还是占位符；本工具不查询模型列表，必须填完全正确的 ID")
        out["model"] = model

    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        problems.append(f"timeout 需为数字（秒），当前：{data.get('timeout')!r}")
        timeout = DEFAULT_TIMEOUT
    else:
        if not 1.0 <= timeout <= 120.0:
            problems.append(f"timeout 超出合理范围 [1, 120] 秒，当前：{timeout}")
    out["timeout"] = timeout

    temperature = data.get("temperature", DEFAULT_TEMPERATURE)
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        problems.append(f"temperature 需为数字，当前：{data.get('temperature')!r}")
        temperature = DEFAULT_TEMPERATURE
    else:
        if not 0.0 <= temperature <= 2.0:
            problems.append(f"temperature 超出合理范围 [0, 2]，当前：{temperature}")
    out["temperature"] = temperature

    max_tokens = data.get("max_tokens", DEFAULT_MAX_TOKENS)
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        problems.append(f"max_tokens 需为整数，当前：{data.get('max_tokens')!r}")
        max_tokens = DEFAULT_MAX_TOKENS
    else:
        if not 16 <= max_tokens <= 8192:
            problems.append(f"max_tokens 超出合理范围 [16, 8192]，当前：{max_tokens}")
    out["max_tokens"] = max_tokens

    min_chars = data.get("min_chars", DEFAULT_MIN_CHARS)
    try:
        min_chars = int(min_chars)
    except (TypeError, ValueError):
        problems.append(f"min_chars 需为整数（触发 LLM 整理的最小文本长度），当前：{data.get('min_chars')!r}")
        min_chars = DEFAULT_MIN_CHARS
    else:
        if not 0 <= min_chars <= 10000:
            problems.append(f"min_chars 超出合理范围 [0, 10000]，当前：{min_chars}")
    out["min_chars"] = min_chars

    extra_body = data.get("extra_body", {})
    if not isinstance(extra_body, dict):
        problems.append(f"extra_body 需为 JSON 对象（合并进请求体的服务商特有参数），当前类型：{type(extra_body).__name__}")
        extra_body = {}
    out["extra_body"] = extra_body

    return out


def load_report() -> tuple[CloudConfig | None, list[str]]:
    """Load and validate the config file.

    Returns ``(config, problems)``: ``config`` is None when the file is absent
    or invalid; ``problems`` explains why (empty list when usable/absent by
    choice — absence is not an error, only a disabled feature).
    """
    path = config_path()
    if not path.is_file():
        return None, []

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"配置文件无法读取：{path}（{exc}）"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"JSON 解析失败：{path}（第 {exc.lineno} 行：{exc.msg}）"]
    if not isinstance(data, dict):
        return None, [f"配置根节点需为 JSON 对象：{path}"]

    problems: list[str] = []
    values = _validate(data, problems)
    if problems:
        return None, problems
    return (
        CloudConfig(
            enabled=values["enabled"],
            base_url=values["base_url"],
            api_key=values["api_key"],
            model=values["model"],
            timeout=values["timeout"],
            temperature=values["temperature"],
            max_tokens=values["max_tokens"],
            min_chars=values["min_chars"],
            extra_body=values["extra_body"],
            source=path,
        ),
        [],
    )


def load() -> CloudConfig | None:
    """Return the validated config, or None when absent/invalid/disabled."""
    config, _ = load_report()
    return config


__all__ = [
    "CloudConfig",
    "config_path",
    "load",
    "load_report",
]
