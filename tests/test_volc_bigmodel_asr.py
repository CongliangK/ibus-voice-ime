"""Unit tests for the Volcano Engine bigmodel ASR backend (stdlib only)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import volc_bigmodel_asr  # noqa: E402


class _Resp:
    def __init__(self, status_code: str, message: str, body: str, logid: str = "log-1"):
        self.headers = {
            "X-Api-Status-Code": status_code,
            "X-Api-Message": message,
            "X-Tt-Logid": logid,
        }
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class VolcBigmodelAsrTest(unittest.TestCase):
    def setUp(self) -> None:
        # Each test starts from a clean environment baseline.
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "VOICE_IME_ASR_BACKEND",
                "VOICE_IME_VOLC_BIGMODEL_ASR",
                "VOICE_IME_VOLC_API_KEY",
                "VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID",
                "VOICE_IME_VOLC_BIGMODEL_BASE_URL",
                "VOICE_IME_VOLC_BIGMODEL_LANGUAGE",
                "VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC",
                "VOICE_IME_VOLC_BIGMODEL_HOTWORDS",
                "VOICE_IME_VOLC_BIGMODEL_HOTWORDS_MAX",
                "VOICE_IME_VOICE_DICTIONARY",
                "VOICE_IME_VOICE_USE_ENGLISH_MEMORY",
            )
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_selected_by_backend_flag(self) -> None:
        self.assertFalse(volc_bigmodel_asr.selected())
        os.environ["VOICE_IME_ASR_BACKEND"] = "volc-bigmodel-asr"
        self.assertTrue(volc_bigmodel_asr.selected())

    def test_selected_by_explicit_flag(self) -> None:
        os.environ["VOICE_IME_VOLC_BIGMODEL_ASR"] = "1"
        self.assertTrue(volc_bigmodel_asr.selected())

    def test_endpoints(self) -> None:
        os.environ["VOICE_IME_VOLC_BIGMODEL_BASE_URL"] = "https://example.test/"
        self.assertEqual(
            volc_bigmodel_asr._submit_url(),
            "https://example.test/api/v3/auc/bigmodel/submit",
        )
        self.assertEqual(
            volc_bigmodel_asr._query_url(),
            "https://example.test/api/v3/auc/bigmodel/query",
        )

    def test_api_key_missing_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            volc_bigmodel_asr._api_key()

    def test_transcribe_submit_then_poll(self) -> None:
        os.environ["VOICE_IME_VOLC_API_KEY"] = "fake-key"
        # Submit + 1 processing poll + 1 success poll.
        success_body = (
            '{"result":{"text":"你好世界","utterances":'
            '[{"text":"你好世界"}]}}'
        )
        calls = iter(
            [
                _Resp("20000000", "ok", ""),  # submit
                _Resp("20000001", "processing", "{}"),  # poll 1
                _Resp("20000000", "ok", success_body),  # poll 2 (done)
            ]
        )

        def fake_urlopen(req, timeout):  # noqa: ARG001
            return next(calls)

        with mock.patch("ibus_voice_ime.asr.volc_bigmodel_asr.urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("ibus_voice_ime.asr.volc_bigmodel_asr.time.sleep"):
            import tempfile
            import wave

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                with wave.open(tmp.name, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(b"\x00\x00" * 1600)
                text = volc_bigmodel_asr.transcribe(tmp.name)
        self.assertEqual(text, "你好世界")


    def test_language_default_empty_for_mixing(self) -> None:
        # Empty/none/auto -> None (best Mandarin/English/dialect mixing).
        self.assertIsNone(volc_bigmodel_asr._language())
        os.environ["VOICE_IME_VOLC_BIGMODEL_LANGUAGE"] = "auto"
        self.assertIsNone(volc_bigmodel_asr._language())
        os.environ["VOICE_IME_VOLC_BIGMODEL_LANGUAGE"] = "mixed"
        self.assertIsNone(volc_bigmodel_asr._language())

    def test_language_pinned(self) -> None:
        os.environ["VOICE_IME_VOLC_BIGMODEL_LANGUAGE"] = "zh"
        self.assertEqual(volc_bigmodel_asr._language(), "zh-CN")
        os.environ["VOICE_IME_VOLC_BIGMODEL_LANGUAGE"] = "en"
        self.assertEqual(volc_bigmodel_asr._language(), "en-US")

    def test_request_options_ddc_default_on(self) -> None:
        opts = volc_bigmodel_asr._request_options()
        self.assertTrue(opts["enable_ddc"])
        self.assertTrue(opts["enable_punc"])
        self.assertTrue(opts["enable_itn"])

    def test_request_options_ddc_disabled(self) -> None:
        os.environ["VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC"] = "0"
        opts = volc_bigmodel_asr._request_options()
        self.assertFalse(opts["enable_ddc"])

    def test_request_options_skip_llm_forces_ddc_off(self) -> None:
        # 原文语音输入（Ctrl+Alt+B）路径：skip_llm=True 强制关 DDC，
        # 优先于 VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC 的默认开启。
        opts = volc_bigmodel_asr._request_options(skip_llm=True)
        self.assertFalse(opts["enable_ddc"])
        self.assertTrue(opts["enable_punc"])
        self.assertTrue(opts["enable_itn"])

    def test_request_options_skip_llm_overrides_env_ddc_on(self) -> None:
        os.environ["VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC"] = "1"
        self.assertFalse(volc_bigmodel_asr._request_options(skip_llm=True)["enable_ddc"])
        # 非 raw 路径不受影响：仍按环境变量（显式开启）走。
        self.assertTrue(volc_bigmodel_asr._request_options()["enable_ddc"])

    def test_transcribe_skip_llm_disables_ddc_in_submit_body(self) -> None:
        # 全链路验证：transcribe(skip_llm=True) -> _submit -> 请求体
        # request.enable_ddc 必须为 False（云端不做语义平滑改写）。
        import json as _json
        import tempfile
        import wave

        os.environ["VOICE_IME_VOLC_API_KEY"] = "fake-key"
        os.environ["VOICE_IME_VOLC_BIGMODEL_HOTWORDS"] = "0"
        captured = {}
        success_body = '{"result":{"text":"原文识别"}}'
        calls = iter(
            [
                _Resp("20000000", "ok", ""),  # submit
                _Resp("20000000", "ok", success_body),  # poll (done)
            ]
        )

        def fake_urlopen(req, timeout):  # noqa: ARG001
            if req.data != b"{}":  # query 的请求体恒为 {}，其余即 submit
                captured["body"] = _json.loads(req.data.decode("utf-8"))
            return next(calls)

        with mock.patch("ibus_voice_ime.asr.volc_bigmodel_asr.urllib.request.urlopen", side_effect=fake_urlopen):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                with wave.open(tmp.name, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(b"\x00\x00" * 160)
                text = volc_bigmodel_asr.transcribe(tmp.name, skip_llm=True)
        self.assertEqual(text, "原文识别")
        self.assertFalse(captured["body"]["request"]["enable_ddc"])

    def test_corpus_hotwords_from_dictionary(self) -> None:
        import tempfile
        d = tempfile.mkdtemp()
        dict_path = os.path.join(d, "voice-dictionary.txt")
        with open(dict_path, "w", encoding="utf-8") as f:
            f.write("Open Code | opencode | open cold\n")
            f.write("Rime\n")
        os.environ["VOICE_IME_VOICE_DICTIONARY"] = dict_path
        # disable english memory to keep the test deterministic
        os.environ["VOICE_IME_VOICE_USE_ENGLISH_MEMORY"] = "0"
        corpus = volc_bigmodel_asr._corpus()
        self.assertIsNotNone(corpus)
        self.assertIn("context", corpus)
        import json
        ctx = json.loads(corpus["context"])
        words = {h["word"] for h in ctx["hotwords"]}
        self.assertIn("Open Code", words)
        self.assertIn("opencode", words)
        self.assertIn("open cold", words)
        self.assertIn("Rime", words)

    def test_corpus_disabled(self) -> None:
        os.environ["VOICE_IME_VOLC_BIGMODEL_HOTWORDS"] = "0"
        self.assertIsNone(volc_bigmodel_asr._corpus())

    def test_submit_body_includes_language_when_pinned(self) -> None:
        import json as _json
        os.environ["VOICE_IME_VOLC_API_KEY"] = "fake-key"
        os.environ["VOICE_IME_VOLC_BIGMODEL_LANGUAGE"] = "zh"
        os.environ["VOICE_IME_VOLC_BIGMODEL_HOTWORDS"] = "0"
        captured = {}

        class _R:
            headers = {"X-Api-Status-Code": "20000000", "X-Api-Message": "ok", "X-Tt-Logid": "l"}

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def read(self):
                return b""

        def fake(req, timeout):  # noqa: ARG001
            captured["body"] = _json.loads(req.data.decode("utf-8"))
            return _R()

        with mock.patch("ibus_voice_ime.asr.volc_bigmodel_asr.urllib.request.urlopen", side_effect=fake):
            volc_bigmodel_asr._submit("task-1", "YQ==", "wav")
        self.assertEqual(captured["body"]["audio"]["language"], "zh-CN")
        self.assertEqual(captured["body"]["audio"]["data"], "YQ==")
        self.assertTrue(captured["body"]["request"]["enable_ddc"])


if __name__ == "__main__":
    unittest.main()
