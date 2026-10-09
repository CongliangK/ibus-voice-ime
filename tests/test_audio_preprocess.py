"""Unit tests for audio_preprocess (requires the system `sox` binary).

Synthesizes 16 kHz / mono / S16_LE test WAVs with sox, runs them through
preprocess_wav, and verifies normalization, high-pass behavior, and the
fail-safe fallbacks.  Tests skip themselves when sox is unavailable so they
remain safe to run on any CI box.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Make the project package importable when running from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import audio_preprocess  # noqa: E402

SOX = shutil.which("sox")
FFMPEG = shutil.which("ffmpeg")
SAMPLE_RATE = 16000


def _has_sox() -> bool:
    return SOX is not None


def _has_ffmpeg() -> bool:
    return FFMPEG is not None


def _synth_wav(path: str, *, sine_hz: float, gain_db: float, seconds: float = 1.0) -> None:
    """Create a 16k/mono/s16 WAV containing a sine tone at the given gain."""
    subprocess.run(
        [
            "sox", "-n", "-r", str(SAMPLE_RATE), "-c", "1", "-b", "16", path,
            "synth", f"{seconds:.3f}", "sine", f"{sine_hz:g}",
            "gain", f"{gain_db:g}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _peak_abs_sample(wav_path: str) -> int:
    """Return the max absolute int16 sample value in a 16k/mono/s16 WAV."""
    import wave
    with wave.open(wav_path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    n = len(frames) // 2
    vals = struct.unpack("<" + "h" * n, frames)
    return max(abs(v) for v in vals) if vals else 0


def _tail_rms(wav_path: str) -> float:
    """RMS of the second half of a 16k/mono/s16 WAV (steady state).

    陷波衰减验收必须看稳态：IIR 滤波器对起始瞬态几乎直通，时域峰值会被
    头几个周期支配而严重低估实际衰减（实测 -30dB 陷波峰值口径只显示 -4dB）。
    """
    import math
    import wave
    with wave.open(wav_path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    vals = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    tail = vals[len(vals) // 2:]
    if not tail:
        return 0.0
    return (sum(v * v for v in tail) / len(tail)) ** 0.5


@unittest.skipUnless(_has_sox(), "sox not installed")
class PreprocessWavTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="audio-pre-test-")
        self.tmp = self._tmp.name
        # Preserve env so each test starts from defaults.
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "VOICE_IME_AUDIO_PREPROCESS",
                "VOICE_IME_AUDIO_NORMALIZE",
                "VOICE_IME_AUDIO_HIGHPASS",
                "VOICE_IME_AUDIO_DENOISE",
                "VOICE_IME_AUDIO_HIGHPASS_FREQ",
                "VOICE_IME_AUDIO_NORMALIZE_HEADROOM",
                "VOICE_IME_AUDIO_DENOISE_AMOUNT",
                "VOICE_IME_AUDIO_NOISE_PROFILE_MS",
            )
        }
        # Reset to defaults for determinism.
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_normalizes_quiet_audio(self) -> None:
        """A heavily attenuated tone should end up near full scale.

        We disable denoise for this case because the synthesized clip is a pure
        tone: its head would be sampled as the "noise fingerprint" and spectral
        subtraction would suppress the very tone we are amplifying.  This
        isolates the normalize stage's behavior.
        """
        wav = str(Path(self.tmp) / "quiet.wav")
        _synth_wav(wav, sine_hz=440.0, gain_db=-35.0)  # very quiet
        before = _peak_abs_sample(wav)
        self.assertLess(before, 1500, "test tone should start quiet")
        os.environ["VOICE_IME_AUDIO_DENOISE"] = "0"

        audio_preprocess.preprocess_wav(wav)

        after = _peak_abs_sample(wav)
        # gain -n -3 targets -3 dBFS ≈ 0.707 * 32767 ≈ 23198.
        self.assertGreater(after, 20000, f"normalized peak too low: {after}")
        self.assertGreater(after, before * 5, "audio was not significantly amplified")

    def test_does_not_clip_on_hot_input(self) -> None:
        """Even a near-full-scale tone should not get pushed past 0 dBFS."""
        wav = str(Path(self.tmp) / "hot.wav")
        _synth_wav(wav, sine_hz=440.0, gain_db=-1.0)
        audio_preprocess.preprocess_wav(wav)
        after = _peak_abs_sample(wav)
        self.assertLessEqual(after, 32767, f"clipping: peak={after}")

    def test_highpass_attenuates_low_frequency(self) -> None:
        """A low rumble (50 Hz) should be attenuated more than a voice band (200 Hz)."""
        low_wav = str(Path(self.tmp) / "low.wav")
        high_wav = str(Path(self.tmp) / "high.wav")
        _synth_wav(low_wav, sine_hz=50.0, gain_db=-6.0)
        _synth_wav(high_wav, sine_hz=200.0, gain_db=-6.0)

        # Disable denoise + normalize so we isolate the highpass effect: with
        # only highpass applied, the low tone's peak must drop relative to the
        # voice-band tone.
        os.environ["VOICE_IME_AUDIO_DENOISE"] = "0"
        os.environ["VOICE_IME_AUDIO_NORMALIZE"] = "0"
        os.environ["VOICE_IME_AUDIO_HIGHPASS_FREQ"] = "100"

        audio_preprocess.preprocess_wav(low_wav)
        audio_preprocess.preprocess_wav(high_wav)

        low_peak = _peak_abs_sample(low_wav)
        high_peak = _peak_abs_sample(high_wav)
        self.assertLess(
            low_peak, high_peak,
            f"highpass did not attenuate 50Hz ({low_peak}) below 200Hz ({high_peak})",
        )

    def test_master_switch_off_returns_untouched(self) -> None:
        wav = str(Path(self.tmp) / "off.wav")
        _synth_wav(wav, sine_hz=440.0, gain_db=-30.0)
        before = _peak_abs_sample(wav)
        os.environ["VOICE_IME_AUDIO_PREPROCESS"] = "0"
        ret = audio_preprocess.preprocess_wav(wav)
        self.assertEqual(ret, wav)
        self.assertEqual(_peak_abs_sample(wav), before, "file should be untouched")

    def test_returns_same_path_and_valid_wav(self) -> None:
        wav = str(Path(self.tmp) / "ok.wav")
        _synth_wav(wav, sine_hz=440.0, gain_db=-20.0)
        ret = audio_preprocess.preprocess_wav(wav)
        self.assertEqual(ret, wav)
        # Output must remain a readable 16k/mono/s16 WAV.
        import wave
        with wave.open(wav, "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), SAMPLE_RATE)
            self.assertGreater(wf.getnframes(), 0)

    def test_missing_file_returns_path(self) -> None:
        missing = str(Path(self.tmp) / "nope.wav")
        self.assertEqual(audio_preprocess.preprocess_wav(missing), missing)

    def test_handles_short_recording(self) -> None:
        """A ~150 ms clip is shorter than the default noise-profile window."""
        wav = str(Path(self.tmp) / "short.wav")
        _synth_wav(wav, sine_hz=440.0, gain_db=-20.0, seconds=0.15)
        ret = audio_preprocess.preprocess_wav(wav)
        self.assertEqual(ret, wav)
        self.assertGreater(_peak_abs_sample(wav), 0)


@unittest.skipUnless(_has_sox(), "sox not installed")
class SoxFallbackTest(unittest.TestCase):
    """When sox is unavailable, preprocess_wav must silently return the input."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="audio-pre-nosox-")
        # Synthesize the test WAV *before* hiding sox, so _synth_wav still works.
        wav = str(Path(self._tmp.name) / "x.wav")
        _synth_wav(wav, sine_hz=440.0, gain_db=-20.0)
        self.wav = wav
        self._saved_path = os.environ.get("PATH")
        # Now make sox unfindable by stripping PATH.
        os.environ["PATH"] = ""

    def tearDown(self) -> None:
        if self._saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._saved_path
        self._tmp.cleanup()

    def test_no_sox_returns_original(self) -> None:
        before = _peak_abs_sample(self.wav)
        ret = audio_preprocess.preprocess_wav(self.wav)
        self.assertEqual(ret, self.wav)
        self.assertEqual(_peak_abs_sample(self.wav), before, "file must be untouched when sox missing")


@unittest.skipUnless(_has_sox() and _has_ffmpeg(), "sox/ffmpeg not installed")
class DenoiseTierTest(unittest.TestCase):
    """VOICE_IME_DENOISE_TIER 三档链（none/notch/rnnoise，2026-08-26 新增）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="audio-pre-tier-")
        self.tmp = self._tmp.name
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "VOICE_IME_AUDIO_PREPROCESS",
                "VOICE_IME_AUDIO_NORMALIZE",
                "VOICE_IME_AUDIO_HIGHPASS",
                "VOICE_IME_AUDIO_DENOISE",
                "VOICE_IME_AUDIO_HIGHPASS_FREQ",
                "VOICE_IME_AUDIO_NOTCH",
                "VOICE_IME_DENOISE_TIER",
                "VOICE_IME_AUDIO_RNNOISE_MODEL",
            )
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _tone(self, name: str, hz: float, gain_db: float = -6.0) -> str:
        wav = str(Path(self.tmp) / name)
        _synth_wav(wav, sine_hz=hz, gain_db=gain_db)
        return wav

    def test_unknown_tier_falls_back_to_none(self) -> None:
        self.assertEqual(audio_preprocess._denoise_tier(), "rnnoise")  # 出厂默认档（缺 ffmpeg/模型时自动降级 notch）
        os.environ["VOICE_IME_DENOISE_TIER"] = "bogus"
        self.assertEqual(audio_preprocess._denoise_tier(), "none")

    def test_notch_tier_attenuates_mains_hum(self) -> None:
        """50Hz 工频哼声在 notch 档应被深度压制（尾部 RMS 稳态口径，见 _tail_rms）。"""
        os.environ["VOICE_IME_AUDIO_HIGHPASS"] = "0"
        os.environ["VOICE_IME_AUDIO_NORMALIZE"] = "0"
        os.environ["VOICE_IME_AUDIO_DENOISE"] = "0"  # 否则 none 档会把纯音整体当噪声扣除
        for tier, name in (("none", "h_none.wav"), ("notch", "h_notch.wav")):
            os.environ["VOICE_IME_DENOISE_TIER"] = tier
            audio_preprocess.preprocess_wav(self._tone(name, 50.0, gain_db=-6.0))
        ratio = _tail_rms(str(Path(self.tmp) / "h_notch.wav")) / max(
            1e-9, _tail_rms(str(Path(self.tmp) / "h_none.wav")))
        self.assertLess(ratio, 0.05, f"50Hz 陷波衰减不足: 稳态剩余比例 {ratio:.4f}")

    def test_notch_tier_spares_voice_band(self) -> None:
        """440Hz 语音频段在 notch 档应基本不受影响（稳态口径）。"""
        os.environ["VOICE_IME_AUDIO_HIGHPASS"] = "0"
        os.environ["VOICE_IME_AUDIO_NORMALIZE"] = "0"
        os.environ["VOICE_IME_AUDIO_DENOISE"] = "0"  # 纯音对照同样须关谱减
        for tier, name in (("none", "v_none.wav"), ("notch", "v_notch.wav")):
            os.environ["VOICE_IME_DENOISE_TIER"] = tier
            audio_preprocess.preprocess_wav(self._tone(name, 440.0, gain_db=-6.0))
        ratio = _tail_rms(str(Path(self.tmp) / "v_notch.wav")) / max(
            1e-9, _tail_rms(str(Path(self.tmp) / "v_none.wav")))
        self.assertGreater(ratio, 0.9, f"语音频段受损: 稳态剩余比例 {ratio:.4f}")

    def test_rnnoise_tier_produces_valid_wav(self) -> None:
        """rnnoise 档端到端：ffmpeg(陷波+arnndn) + sox(normalize)。缺模型自动降级。"""
        model = audio_preprocess._rnnoise_model_path()
        if not Path(model).exists():
            self.skipTest("rnnoise 模型不可用")
        os.environ["VOICE_IME_DENOISE_TIER"] = "rnnoise"
        wav = self._tone("r.wav", 440.0, gain_db=-20.0)
        ret = audio_preprocess.preprocess_wav(wav)
        self.assertEqual(ret, wav)
        import wave
        with wave.open(wav, "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getframerate(), SAMPLE_RATE)
            self.assertGreater(wf.getnframes(), 0)
        self.assertGreater(_peak_abs_sample(wav), 0)


if __name__ == "__main__":
    unittest.main()
