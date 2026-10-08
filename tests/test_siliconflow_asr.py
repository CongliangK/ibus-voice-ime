"""Unit tests for the SiliconFlow cloud ASR backend (stdlib only, hermetic).

全部用例 mock urllib.request.urlopen，绝不发起真实网络请求。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import siliconflow_asr  # noqa: E402


class _Resp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class SiliconflowAsrTest(unittest.TestCase):
    def setUp(self) -> None:
        # Each test starts from a clean environment baseline.
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "VOICE_IME_ASR_BACKEND",
                "VOICE_IME_SILICONFLOW_ASR",
                "VOICE_IME_SILICONFLOW_API_KEY",
                "SILICONFLOW_API_KEY",
                "VOICE_IME_SILICONFLOW_BASE_URL",
                "SILICONFLOW_BASE_URL",
                "VOICE_IME_SILICONFLOW_MODEL",
                "SILICONFLOW_MODEL",
                "VOICE_IME_SILICONFLOW_MAX_DATA_MB",
                "VOICE_IME_SILICONFLOW_TIMEOUT",
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

    def _make_audio(self, data: bytes, suffix: str = ".wav") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(os.unlink, path)
        return path

    # ------------------------------------------------------------ selected --

    def test_selected_backend_aliases(self) -> None:
        self.assertFalse(siliconflow_asr.selected())  # 未配置时 False
        for name in ("siliconflow", "SiliconFlow", "SILICONFLOW-ASR", "siliconflow-asr", "sf-asr"):
            os.environ["VOICE_IME_ASR_BACKEND"] = name
            self.assertTrue(siliconflow_asr.selected(), name)
        os.environ["VOICE_IME_ASR_BACKEND"] = "qwen3-asr"
        self.assertFalse(siliconflow_asr.selected())

    def test_selected_by_explicit_flag(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_ASR"] = "1"
        self.assertTrue(siliconflow_asr.selected())

    # -------------------------------------------------------- multipart --
    def test_multipart_structure(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        audio = b"\x01\x02\x03\x04RIFF-wav-bytes"
        path = self._make_audio(audio, ".wav")
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["data"] = req.data
            captured["timeout"] = timeout
            return _Resp('{"text": "你好硅基流动"}')

        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            text = siliconflow_asr.transcribe(path)
        self.assertEqual(text, "你好硅基流动")

        headers = {k.lower(): v for k, v in captured["headers"].items()}
        # 认证头 + multipart Content-Type（boundary 与 body 一致）。
        self.assertEqual(headers["authorization"], "Bearer fake-key")
        content_type = headers["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        body = captured["data"]
        self.assertIn(f"--{boundary}\r\n".encode(), body)
        # model 字段：名字 + 当前模型值。
        self.assertIn(b'name="model"', body)
        self.assertIn(b"FunAudioLLM/SenseVoiceSmall", body)
        # file 字段：名字 + 原始文件名 + wav mime + 原始音频字节完整出现。
        self.assertIn(b'name="file"', body)
        self.assertIn(f'filename="{os.path.basename(path)}"'.encode(), body)
        self.assertIn(b"Content-Type: audio/wav", body)
        self.assertIn(audio, body)
        # 结尾 boundary 闭合。
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertIn("https://api.siliconflow.cn/v1/audio/transcriptions", captured["url"])

    def test_multipart_filename_sanitized(self) -> None:
        # 防御性清洗：文件名里的 " 、\r、\n 不得进入 Content-Disposition。
        body, boundary = siliconflow_asr._multipart_body(
            "m", b"payload", 'bad"name\r\n.wav', "audio/wav"
        )
        self.assertNotIn(b'bad"name', body)  # 原始引号不得出现
        self.assertNotIn(b"\r\n.wav", body)  # \r\n 不得出现在文件名里
        self.assertIn(b'filename="bad_name__.wav"', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    # ------------------------------------------------------- happy path --

    def test_transcribe_happy_path(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        path = self._make_audio(b"\x00\x00" * 100, ".wav")
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=lambda req, timeout: _Resp('{"text": "  识别结果  "}'),  # noqa: ARG001
        ):
            text = siliconflow_asr.transcribe(path)
        self.assertEqual(text, "识别结果")  # strip 后返回

    def test_empty_text_raises(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        path = self._make_audio(b"\x00\x00" * 100, ".wav")
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=lambda req, timeout: _Resp('{"text": "   "}'),  # noqa: ARG001
        ):
            with self.assertRaises(RuntimeError) as ctx:
                siliconflow_asr.transcribe(path)
        self.assertIn("空结果", str(ctx.exception))

    def test_non_json_response_raises(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        path = self._make_audio(b"\x00\x00" * 100, ".wav")
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=lambda req, timeout: _Resp("Bad Gateway plain text"),  # noqa: ARG001
        ):
            with self.assertRaises(RuntimeError) as ctx:
                siliconflow_asr.transcribe(path)
        self.assertIn("非 JSON", str(ctx.exception))
        self.assertIn("Bad Gateway plain text", str(ctx.exception))

    # ------------------------------------------------------- HTTP 错误 --
    def _http_error(self, code: int, body: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://api.siliconflow.cn/v1/audio/transcriptions",
            code,
            "err",
            {},
            io.BytesIO(body.encode("utf-8")),
        )

    def _assert_http_error_message(self, code: int, body: str, *needles: str) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        path = self._make_audio(b"\x00\x00" * 10, ".wav")
        err = self._http_error(code, body)
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=err,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                siliconflow_asr.transcribe(path)
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))

    def test_error_401_invalid_token(self) -> None:
        self._assert_http_error_message(401, "Invalid token", "Key", "控制台")

    def test_error_429_rate_limited(self) -> None:
        self._assert_http_error_message(429, '{"code":429,"message":"rate limit"}', "限流")

    def test_error_429_includes_retry_after(self) -> None:
        # 429 时透传服务端 Retry-After（秒）到中文提示。
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        path = self._make_audio(b"\x00\x00" * 10, ".wav")
        err = urllib.error.HTTPError(
            "https://api.siliconflow.cn/v1/audio/transcriptions",
            429,
            "err",
            {"Retry-After": "30"},
            io.BytesIO(b"rate limit"),
        )
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=err,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                siliconflow_asr.transcribe(path)
        message = str(ctx.exception)
        self.assertIn("限流", message)
        self.assertIn("Retry-After", message)
        self.assertIn("30", message)

    def test_error_503_overloaded(self) -> None:
        self._assert_http_error_message(
            503, '{"code":50505,"message":"Model service overloaded"}', "过载"
        )

    def test_error_400_includes_body_and_model_hint(self) -> None:
        self._assert_http_error_message(
            400, '{"code":20012,"message":"Invalid multipart payload"}', "400", "20012", "模型"
        )

    # ---------------------------------------------------------- 密钥 --
    def test_missing_key_error_has_guidance(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            siliconflow_asr._api_key()
        message = str(ctx.exception)
        self.assertIn("cloud.siliconflow.cn", message)  # 注册指引
        self.assertIn("VOICE_IME_SILICONFLOW_API_KEY", message)  # 设置指引
        self.assertIn("switch-siliconflow-asr.sh", message)

    def test_key_from_generic_env_fallback(self) -> None:
        os.environ["SILICONFLOW_API_KEY"] = "sk-generic"
        self.assertEqual(siliconflow_asr._api_key(), "sk-generic")

    # ------------------------------------------------- env 覆盖与 URL --
    def test_base_url_and_model_env_overrides(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_BASE_URL"] = "https://sf-proxy.test/"
        self.assertEqual(
            siliconflow_asr._transcribe_url(),
            "https://sf-proxy.test/v1/audio/transcriptions",  # 尾斜杠正确拼接
        )
        # 完整端点作为 base_url 时不重复拼接。
        os.environ["VOICE_IME_SILICONFLOW_BASE_URL"] = (
            "https://sf-proxy.test/v1/audio/transcriptions"
        )
        self.assertEqual(
            siliconflow_asr._transcribe_url(),
            "https://sf-proxy.test/v1/audio/transcriptions",
        )
        # 无 VOICE_IME_ 前缀变量时的通用回退。
        os.environ.pop("VOICE_IME_SILICONFLOW_BASE_URL")
        os.environ["SILICONFLOW_BASE_URL"] = "https://sf-generic.test"
        self.assertEqual(siliconflow_asr.base_url(), "https://sf-generic.test")

        # MODEL 覆盖进 multipart body。
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        os.environ["VOICE_IME_SILICONFLOW_BASE_URL"] = "https://sf-proxy.test"
        os.environ["VOICE_IME_SILICONFLOW_MODEL"] = "Qwen/Qwen3-ASR-1.7B"
        self.assertEqual(siliconflow_asr.model_id(), "Qwen/Qwen3-ASR-1.7B")
        captured: dict = {}

        def fake_urlopen(req, timeout):  # noqa: ARG001
            captured["data"] = req.data
            captured["url"] = req.full_url
            return _Resp('{"text": "ok"}')

        path = self._make_audio(b"abc", ".wav")
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            siliconflow_asr.transcribe(path)
        self.assertIn(b"Qwen/Qwen3-ASR-1.7B", captured["data"])
        self.assertEqual(
            captured["url"], "https://sf-proxy.test/v1/audio/transcriptions"
        )

    # ------------------------------------------------------ 大小守卫 --
    def test_size_guard_rejects_oversized_audio(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        os.environ["VOICE_IME_SILICONFLOW_MAX_DATA_MB"] = "0.001"  # ~1KB
        path = self._make_audio(b"x" * 4096, ".wav")
        with mock.patch(
            "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen"
        ) as fake_urlopen:
            with self.assertRaises(RuntimeError) as ctx:
                siliconflow_asr.transcribe(path)
        self.assertIn("缩短录音", str(ctx.exception))
        fake_urlopen.assert_not_called()  # 超限请求不应发出

    def test_audio_not_found(self) -> None:
        with self.assertRaises(RuntimeError):
            siliconflow_asr.transcribe("/nonexistent/record.wav")

    # ----------------------------------------------------- mime 后缀 --
    def test_mime_by_suffix(self) -> None:
        os.environ["VOICE_IME_SILICONFLOW_API_KEY"] = "fake-key"
        cases = {
            ".mp3": b"Content-Type: audio/mpeg",
            ".m4a": b"Content-Type: audio/mp4",
            ".ogg": b"Content-Type: audio/ogg",
            ".bin": b"Content-Type: application/octet-stream",
        }
        for suffix, expected_header in cases.items():
            captured: dict = {}

            def fake_urlopen(req, timeout):  # noqa: ARG001
                captured["data"] = req.data
                return _Resp('{"text": "ok"}')

            path = self._make_audio(b"payload", suffix)
            with mock.patch(
                "ibus_voice_ime.asr.siliconflow_asr.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                siliconflow_asr.transcribe(path)
            self.assertIn(expected_header, captured["data"], suffix)


if __name__ == "__main__":
    unittest.main()
