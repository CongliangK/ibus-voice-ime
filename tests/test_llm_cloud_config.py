# -*- coding: utf-8 -*-
"""Cloud LLM post-processing: JSON config loading, gating, endpoint resolution.

Covers the contract requested for the cloud path:
* a user-owned JSON file (base_url + api_key + exact model id) opts the LLM
  layer in and overrides all VOICE_IME_LLM_* environment defaults;
* no config / broken config -> LLM stays off, rule-based path unaffected;
* the client only ever calls ``/chat/completions`` — no ``/models`` listing.
"""
from __future__ import annotations

import json
import os
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from ibus_voice_ime.asr import voice
from ibus_voice_ime.text import llm_cloud_config, llm_postprocess, llm_runtime

ROOT = Path(__file__).resolve().parents[1]

VALID_CONFIG = {
    "enabled": True,
    "base_url": "https://api.provider.test/v1",
    "api_key": "sk-unittest-1234567890",
    "model": "provider-model-x",
    "timeout": 9,
    "temperature": 0.2,
    "max_tokens": 512,
}


def _clean_env(**overrides) -> dict[str, str]:
    """Environment without any inherited VOICE_IME_LLM* defaults."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICE_IME_LLM")}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


class _ConfigFileTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="llm-cloud-cfg-")
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "llm.json"

    def write_config(self, data) -> None:
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def patch_env(self, **overrides):
        env = _clean_env(VOICE_IME_LLM_CONFIG=str(self.config_path), **overrides)
        return mock.patch.dict(os.environ, env, clear=True)


class CloudConfigLoadingTest(_ConfigFileTest):
    def test_missing_file_is_not_an_error(self) -> None:
        with self.patch_env():
            self.assertIsNone(llm_cloud_config.load())
            config, problems = llm_cloud_config.load_report()
            self.assertIsNone(config)
            self.assertEqual(problems, [])

    def test_valid_config_full_fields(self) -> None:
        self.write_config(VALID_CONFIG)
        with self.patch_env():
            config = llm_cloud_config.load()
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.base_url, "https://api.provider.test/v1")
        self.assertEqual(config.api_key, "sk-unittest-1234567890")
        self.assertEqual(config.model, "provider-model-x")
        self.assertEqual(config.timeout, 9.0)
        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.max_tokens, 512)
        self.assertTrue(config.enabled)
        self.assertEqual(config.source, self.config_path)

    def test_optional_fields_get_defaults(self) -> None:
        self.write_config({
            "base_url": "https://api.provider.test/v1",
            "api_key": "sk-unittest-1234567890",
            "model": "provider-model-x",
        })
        with self.patch_env():
            config = llm_cloud_config.load()
        assert config is not None
        self.assertTrue(config.enabled)
        self.assertEqual(config.timeout, llm_cloud_config.DEFAULT_TIMEOUT)
        self.assertEqual(config.temperature, llm_cloud_config.DEFAULT_TEMPERATURE)
        self.assertEqual(config.max_tokens, llm_cloud_config.DEFAULT_MAX_TOKENS)
        self.assertEqual(config.min_chars, llm_cloud_config.DEFAULT_MIN_CHARS)

    def test_min_chars_validation(self) -> None:
        data = dict(VALID_CONFIG, min_chars=10)
        self.write_config(data)
        with self.patch_env():
            config = llm_cloud_config.load()
        assert config is not None
        self.assertEqual(config.min_chars, 10)

        self.write_config(dict(VALID_CONFIG, min_chars=-5))
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        self.assertTrue(any("min_chars" in p for p in problems))

        self.write_config(dict(VALID_CONFIG, min_chars="很多"))
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        self.assertTrue(any("min_chars" in p for p in problems))

    def test_extra_body_validation(self) -> None:
        self.write_config(dict(VALID_CONFIG, extra_body={"thinking": {"type": "disabled"}}))
        with self.patch_env():
            config = llm_cloud_config.load()
        assert config is not None
        self.assertEqual(config.extra_body, {"thinking": {"type": "disabled"}})

        self.write_config(dict(VALID_CONFIG, extra_body="disabled"))
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        self.assertTrue(any("extra_body" in p for p in problems))

    def test_broken_json_reports_problem(self) -> None:
        self.config_path.write_text("{not json", encoding="utf-8")
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        self.assertTrue(any("JSON" in p for p in problems))

    def test_missing_required_fields(self) -> None:
        self.write_config({"enabled": True})
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        joined = "\n".join(problems)
        for field in ("base_url", "api_key", "model"):
            self.assertIn(field, joined)

    def test_placeholder_values_rejected(self) -> None:
        self.write_config(json.loads(
            (ROOT / "examples" / "llm-cloud.json.example").read_text(encoding="utf-8")
        ))
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        self.assertTrue(problems, "example placeholders must not pass validation")

    def test_out_of_range_numbers_rejected(self) -> None:
        data = dict(VALID_CONFIG, timeout=500, temperature=9, max_tokens=99999)
        self.write_config(data)
        with self.patch_env():
            config, problems = llm_cloud_config.load_report()
        self.assertIsNone(config)
        joined = "\n".join(problems)
        self.assertIn("timeout", joined)
        self.assertIn("temperature", joined)
        self.assertIn("max_tokens", joined)

    def test_enabled_false_keeps_config_but_disables(self) -> None:
        self.write_config(dict(VALID_CONFIG, enabled=False))
        with self.patch_env():
            config = llm_cloud_config.load()
            self.assertIsNotNone(config)
            self.assertFalse(config.enabled)
            self.assertFalse(llm_postprocess.enabled())


class EnabledGatingTest(_ConfigFileTest):
    def test_cloud_config_beats_forced_env_off(self) -> None:
        # run-engine.sh pins VOICE_IME_LLM_POSTPROCESS=0; a valid cloud config
        # must still turn the layer on.
        self.write_config(VALID_CONFIG)
        with self.patch_env(VOICE_IME_LLM_POSTPROCESS=0, VOICE_IME_LLM_INTERNAL=0):
            self.assertTrue(llm_postprocess.enabled())

    def test_env_optin_without_config(self) -> None:
        with self.patch_env(VOICE_IME_LLM_POSTPROCESS=1):
            self.assertTrue(llm_postprocess.enabled())

    def test_default_off(self) -> None:
        with self.patch_env():
            self.assertFalse(llm_postprocess.enabled())

    def test_sidecar_never_shadows_cloud_config(self) -> None:
        self.write_config(VALID_CONFIG)
        with self.patch_env(VOICE_IME_LLM_INTERNAL=1):
            self.assertFalse(llm_runtime.internal_enabled())


class EndpointResolutionTest(_ConfigFileTest):
    LONG_TEXT = (
        "这是一段足够长的听写文本用来触发后处理润色逻辑分支"
        "因为云端整理只对超过一定字数的文本启用短句直接走规则清理"
        "所以这个测试用例必须凑够五十个字以上才能覆盖到真正的调用路径"
    )
    SHORT_TEXT = "短句不需要整理"

    def test_cloud_endpoint_overrides_env_defaults(self) -> None:
        self.write_config(VALID_CONFIG)
        captured = {}

        def fake_chat(messages, *, base_url, model, timeout, temperature, max_tokens,
                      api_key="", extra_body=None):
            captured.update(base_url=base_url, model=model, timeout=timeout,
                            temperature=temperature, max_tokens=max_tokens, api_key=api_key,
                            extra_body=extra_body)
            return "润色后的文本。"

        with self.patch_env(
            VOICE_IME_LLM_BASE_URL="http://127.0.0.1:18080/v1",
            VOICE_IME_LLM_API_KEY="local",
            VOICE_IME_LLM_MODEL="qwen3.5-0.8b",
            VOICE_IME_LLM_TIMEOUT=4,
        ), mock.patch.object(llm_postprocess, "_chat_completion", side_effect=fake_chat):
            result = llm_postprocess.refine(self.LONG_TEXT, mode="dictation")

        self.assertEqual(result, "润色后的文本。")
        self.assertEqual(captured["base_url"], "https://api.provider.test/v1")
        self.assertEqual(captured["model"], "provider-model-x")
        self.assertEqual(captured["api_key"], "sk-unittest-1234567890")
        self.assertEqual(captured["timeout"], 9.0)
        self.assertEqual(captured["temperature"], 0.2)
        self.assertEqual(captured["max_tokens"], 512)
        self.assertEqual(captured["extra_body"], {})

    def test_extra_body_sent_through_to_payload(self) -> None:
        # 服务商特有参数（如 GLM 关思考）必须从 JSON 配置一路进请求体。
        self.write_config(dict(VALID_CONFIG, extra_body={"thinking": {"type": "disabled"}}))

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return json.dumps({"choices": [{"message": {"content": "文本。"}}]}).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with self.patch_env(), mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            out = llm_postprocess.refine(self.LONG_TEXT, mode="dictation")

        self.assertEqual(out, "文本。")
        self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["body"]["model"], "provider-model-x")

    def test_endpoint_url_building(self) -> None:
        self.assertEqual(
            llm_postprocess._endpoint("https://api.provider.test/v1"),
            "https://api.provider.test/v1/chat/completions",
        )
        self.assertEqual(
            llm_postprocess._endpoint("https://api.provider.test/v1/"),
            "https://api.provider.test/v1/chat/completions",
        )
        # Already-complete endpoints pass through untouched.
        full = "https://api.provider.test/v1/chat/completions"
        self.assertEqual(llm_postprocess._endpoint(full), full)

    def test_short_text_skips_llm_entirely(self) -> None:
        # <50 字的短句不该产生任何网络调用：直接返回原文。
        self.write_config(VALID_CONFIG)
        with self.patch_env(), \
                mock.patch.object(llm_postprocess, "_chat_completion") as chat:
            result = llm_postprocess.refine(self.SHORT_TEXT, mode="dictation")
        chat.assert_not_called()
        self.assertEqual(result, self.SHORT_TEXT)

    def test_config_min_chars_override(self) -> None:
        self.write_config(dict(VALID_CONFIG, min_chars=5))
        with self.patch_env(), \
                mock.patch.object(llm_postprocess, "_chat_completion",
                                  return_value="整理后的短句。") as chat:
            result = llm_postprocess.refine(self.SHORT_TEXT, mode="dictation")
        chat.assert_called_once()
        self.assertEqual(result, "整理后的短句。")

    def test_env_min_chars_default_without_config(self) -> None:
        # 无 JSON 配置的遗留 env 路径：默认阈值同为 50。
        medium = "这句听写只有二十个字左右而已用来验证默认阈值行为"
        with self.patch_env(VOICE_IME_LLM_POSTPROCESS=1), \
                mock.patch.object(llm_postprocess, "_chat_completion") as chat:
            result = llm_postprocess.refine(medium, mode="dictation")
        chat.assert_not_called()
        self.assertEqual(result, medium)

    def test_chat_completion_never_lists_models(self) -> None:
        requests: list[urllib.request.Request] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return json.dumps({
                    "choices": [{"message": {"content": "带标点的文本。"}}]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            return FakeResponse()

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            out = llm_postprocess._chat_completion(
                [{"role": "user", "content": "hi"}],
                base_url="https://api.provider.test/v1",
                model="provider-model-x",
                timeout=5.0,
                temperature=0.1,
                max_tokens=64,
                api_key="sk-unittest-1234567890",
            )

        self.assertEqual(out, "带标点的文本。")
        self.assertEqual(len(requests), 1)
        url = requests[0].full_url
        self.assertTrue(url.endswith("/chat/completions"), url)
        self.assertNotIn("/models", url)
        self.assertEqual(
            requests[0].get_header("Authorization"), "Bearer sk-unittest-1234567890"
        )
        body = json.loads(requests[0].data.decode("utf-8"))
        self.assertEqual(body["model"], "provider-model-x")
        self.assertFalse(body["stream"])

    def test_empty_result_diagnoses_token_exhaustion(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return json.dumps({
                    "choices": [{"message": {"content": ""}, "finish_reason": "length"}]
                }).encode()

        with mock.patch.object(urllib.request, "urlopen", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError) as ctx:
                llm_postprocess._chat_completion(
                    [{"role": "user", "content": "hi"}],
                    base_url="https://api.provider.test/v1",
                    model="provider-model-x",
                    timeout=5.0,
                    temperature=0.1,
                    max_tokens=64,
                )
        self.assertIn("finish_reason=length", str(ctx.exception))
        self.assertIn("max_tokens", str(ctx.exception))

    def test_http_error_carries_actionable_hint(self) -> None:
        class FakeHttpError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("https://api.provider.test/v1/chat/completions",
                                 404, "Not Found", None, None)

            def read(self) -> bytes:
                return b'{"error":{"message":"model not found"}}'

        def fake_urlopen(req, timeout=None):
            raise FakeHttpError()

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                llm_postprocess._chat_completion(
                    [{"role": "user", "content": "hi"}],
                    base_url="https://api.provider.test/v1",
                    model="wrong-id",
                    timeout=5.0,
                    temperature=0.1,
                    max_tokens=64,
                )
        self.assertIn("404", str(ctx.exception))
        self.assertIn("模型 ID", str(ctx.exception))


class VoicePostprocessWiringTest(_ConfigFileTest):
    def test_postprocess_polishes_when_configured(self) -> None:
        self.write_config(VALID_CONFIG)
        seen = {}

        def fake_refine(text, *, mode=None):
            seen["text"] = text
            seen["mode"] = mode
            return "润色结果。"

        with self.patch_env(VOICE_IME_VOICE_MODE="dictation"), \
                mock.patch.object(llm_postprocess, "refine_with_fallback", side_effect=fake_refine):
            result = voice.postprocess("这是一段没有任何标点的听写文本内容")

        self.assertEqual(result, "润色结果。")
        self.assertEqual(seen["mode"], "dictation")
        self.assertTrue(seen["text"], "refine must receive the normalized text")

    def test_postprocess_skips_llm_without_config(self) -> None:
        with self.patch_env(VOICE_IME_VOICE_MODE="dictation"), \
                mock.patch.object(llm_postprocess, "refine_with_fallback") as refine:
            result = voice.postprocess("这是一段没有任何标点的听写文本内容")
        refine.assert_not_called()
        self.assertTrue(result)

    def test_postprocess_survives_llm_failure(self) -> None:
        self.write_config(VALID_CONFIG)

        def boom(text, *, mode=None):
            raise RuntimeError("LLM 连接失败：refused")

        with self.patch_env(VOICE_IME_VOICE_MODE="dictation"), \
                mock.patch.object(llm_postprocess, "refine_with_fallback", side_effect=boom):
            result = voice.postprocess("这是一段没有任何标点的听写文本内容")
        self.assertTrue(result)
        self.assertNotIn("润色", result)


class ModelLabelTest(_ConfigFileTest):
    def test_label_prefers_cloud_model(self) -> None:
        self.write_config(VALID_CONFIG)
        with self.patch_env(VOICE_IME_LLM_MODEL="env-model"):
            self.assertEqual(llm_postprocess.active_model_label(), "provider-model-x")

    def test_label_falls_back_to_env(self) -> None:
        with self.patch_env(VOICE_IME_LLM_MODEL="env-model"):
            self.assertEqual(llm_postprocess.active_model_label(), "env-model")


if __name__ == "__main__":
    unittest.main()
