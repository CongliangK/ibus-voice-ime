"""Unit tests for the arecord device fallback in voice.py.

Covers VOICE_IME_ARECORD_DEVICE handling: pass-through for existing ALSA
cards, graceful fallback to the system default source when the configured
card is absent (e.g. config carried over from another machine), and
pass-through when the card list cannot be determined.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# Make the project package importable when running from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import voice  # noqa: E402


class _EnvOverride:
    """Context manager that sets/restores one environment variable."""

    def __init__(self, name: str, value: str | None):
        self.name = name
        self.value = value
        self.saved: str | None = None

    def __enter__(self):
        self.saved = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.saved
        return False


class ArecordDeviceArgsTests(unittest.TestCase):
    def setUp(self):
        voice._ALSA_CARD_TOKENS = None

    def _set_cards(self, tokens):
        voice._ALSA_CARD_TOKENS = set(tokens)

    def test_empty_device_uses_default_source(self):
        with _EnvOverride("VOICE_IME_ARECORD_DEVICE", None):
            self._set_cards(["1", "M2"])
            self.assertEqual(voice._arecord_device_args(), [])

    def test_default_device_uses_default_source(self):
        with _EnvOverride("VOICE_IME_ARECORD_DEVICE", "default"):
            self._set_cards(["1", "M2"])
            self.assertEqual(voice._arecord_device_args(), [])

    def test_existing_card_name_passes_through(self):
        with _EnvOverride("VOICE_IME_ARECORD_DEVICE", "plughw:M2,0"):
            self._set_cards(["1", "M2"])
            self.assertEqual(voice._arecord_device_args(), ["-D", "plughw:M2,0"])

    def test_existing_card_index_passes_through(self):
        with _EnvOverride("VOICE_IME_ARECORD_DEVICE", "plughw:1,0"):
            self._set_cards(["1", "M2"])
            self.assertEqual(voice._arecord_device_args(), ["-D", "plughw:1,0"])

    def test_missing_card_falls_back_to_default_source(self):
        # Config from another machine (MOTU M2) on a box that only has HDA.
        with _EnvOverride("VOICE_IME_ARECORD_DEVICE", "plughw:M2,0"):
            self._set_cards(["0", "HDA"])
            self.assertEqual(voice._arecord_device_args(), [])

    def test_unknown_card_list_passes_through(self):
        # Card detection unavailable -> do not interfere with explicit config.
        with _EnvOverride("VOICE_IME_ARECORD_DEVICE", "plughw:M2,0"):
            voice._ALSA_CARD_TOKENS = None
            with mock.patch.object(voice.subprocess, "run", side_effect=OSError("no arecord")):
                self.assertEqual(voice._arecord_device_args(), ["-D", "plughw:M2,0"])

    def test_non_hw_device_passes_through(self):
        for device in ("pulse", "pipewire", "null"):
            with _EnvOverride("VOICE_IME_ARECORD_DEVICE", device):
                self._set_cards(["0", "HDA"])
                self.assertEqual(voice._arecord_device_args(), ["-D", device])

    def tearDown(self):
        voice._ALSA_CARD_TOKENS = None


class AlsaCardTokensParsingTests(unittest.TestCase):
    def setUp(self):
        voice._ALSA_CARD_TOKENS = None

    def test_parses_arecord_list_output(self):
        sample = (
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 0: PCH [HDA Intel PCH], device 0: ALC257 Analog [ALC257 Analog]\n"
            "card 1: M2 [MOTU M2], device 0: USB Audio [USB Audio]\n"
        )
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=sample)

        def fake_run(*args, **kwargs):
            return fake

        with mock.patch.object(voice.subprocess, "run", side_effect=fake_run):
            tokens = voice._alsa_card_tokens()
        self.assertIn("0", tokens)
        self.assertIn("PCH", tokens)
        self.assertIn("1", tokens)
        self.assertIn("M2", tokens)

    def test_caches_result(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="")

        def fake_run(*args, **kwargs):
            return fake

        with mock.patch.object(voice.subprocess, "run", side_effect=fake_run) as run:
            voice._alsa_card_tokens()
            voice._alsa_card_tokens()
        self.assertEqual(run.call_count, 1)

    def tearDown(self):
        voice._ALSA_CARD_TOKENS = None


if __name__ == "__main__":
    unittest.main()
