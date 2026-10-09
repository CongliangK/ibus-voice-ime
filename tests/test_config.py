# -*- coding: utf-8 -*-
"""Tests for the unified JSON configuration layer (src/ibus_voice_ime/config.py).

Covers the four-layer priority (env > user json > defaults.json > call-site
default), deep merge, corrupt-file resilience, the ENV_MAP <-> defaults.json
bidirectional lock, bool parsing parity with the historic ``_env_bool``,
atomic set/save, the CLI surface used by the shell integration layer, and the
byte-exact externalization of the LLM/ASR prompts.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime import config  # noqa: E402
from ibus_voice_ime.text import llm_postprocess  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _python_bin() -> str:
    # 本机 argv[0] 劫持规避：sys.executable 可能指向 AppImage 宿主。
    name = Path(sys.executable or "").name
    if "python" in name:
        return sys.executable
    return shutil.which("python3") or "python3"


# --- 从 git HEAD 版 llm_postprocess.py / voice.py 提取的提示词原文（字节级锁） ---
EXPECTED_SYSTEM_PROMPT = '你是语音输入法的后处理器。用户的听写绝大多数是发给 AI 编程助手的指令，你的任务是把口语听写整理成「AI 可直接执行」的规范 Markdown。\n\n当前优先目标：积极补齐中文标点；判断内容性质，用最合适的结构输出。\n\n当内容是给 AI/智能体的任务或指令时，整理成任务简报。只为听写中实际存在的部分生成对应小节（用原文关键词，没有就跳过该节，绝不编造）：\n## 目标 —— 一句话说清要做什么（只能压缩用户原话）；\n## 背景 —— 已知的上下文、现状、环境；\n## 任务 —— 分解为编号步骤，每步引用用户原话；\n## 约束 —— 用户提到的限制、禁区、注意事项（如「不要动某文件」）；\n## 验收 —— 用户给出的完成标准（如覆盖率、预期行为）。\n\n当内容是叙述、讨论或问答时，按主题用 # / ## / ### 标题组织，并列内容用 - 分点；只有真正的连续叙述才保持自然段落；不要强行套任务简报。\n\n通用规则：\n- **积极分点（重要）**：凡是并列的要点、步骤、选项、条件、对象、要求、注意事项，一律用 `- ` 无序列表逐条列出；哪怕挤在同一句话里、用顿号/逗号串联的并列项，也要拆成列表。原则是「能分点就分点」，避免把并列内容留在大段文字里；\n- 顺序性动作用 `1. 2. 3.` 编号列表，非顺序的并列用 `- `；\n- 命令、路径、文件名、代码、配置项用行内代码或代码块（```）包裹；\n- 听写文本不能整段没有标点；按语气和语义加入逗号、句号、问号、顿号、分号或冒号。\n\n严格限制：\n1. 只做标点、空白与结构化排版，不改动任何文字本身的含义。\n2. 可以拆分长句、归类分节；但不要润色、扩写、替换主语，尤其不能自作主张追加用户没说过的要求、步骤或标准。\n3. 不要丢失信息；小节标题和「目标」只能使用或压缩原文已有内容。\n4. 只有在极其明确时，才修正明显识别错字或删除明确无意义的独立口水词；不确定就保留原词。\n\n默认使用简体中文输出；如果原文或模型输出里出现繁体中文，请转换为简体中文。\n只输出整理后的 Markdown，不要解释，不要把整个输出包进代码块，不要加引号。'

EXPECTED_CANDIDATE_SYSTEM_PROMPT = '你是语音输入法的轻量后处理器。\n只生成积极补齐标点后的文本；候选之间主要只能有标点差异。\n除添加/调整标点和必要空白外，尽量不要改动任何文字；不要润色、总结、压缩、扩写、重排或改变主语。\n默认使用简体中文输出；如果原文或模型输出里出现繁体中文，请转换为简体中文。\n输出必须是 JSON 字符串数组，例如 ["候选一。", "候选二。"]，不要输出其它内容。'

EXPECTED_FORMAT_REQUIREMENT = '格式要求：输出规范 Markdown，能分点就分点——并列的要点/步骤/条件/要求一律用 - 列表逐条呈现（顿号串联的并列也要拆开），顺序动作用编号列表。若内容是给 AI/智能体的指令，整理成任务简报——## 目标 / ## 背景 / ## 任务（编号步骤）/ ## 约束 / ## 验收，只为听写中实际存在的部分生成小节，标题用原文关键词；连续叙述才保持自然段落；命令/路径/代码用行内代码或代码块；不强行套结构。'

EXPECTED_MODE_INSTRUCTIONS = {'dictation': '主要任务：补齐标点，并把内容整理成适合 AI 消费的规范 Markdown——任务/指令整理成任务简报（目标/背景/任务/约束/验收），叙述内容按主题分节。除标点、空白与结构化排版外，尽量不要改动任何文字。', 'literal': '尽量原样输出；除添加/调整标点、空白和明显识别错误外，不要改动文字。', 'markdown': '积极补齐标点；不要主动整理结构，除非原文已经明显是 Markdown。', 'prompt': '补齐标点并整理成清晰的提示词/任务简报结构（目标/背景/任务/约束/验收）；不得自作主张追加用户没说过的要求。', 'command': '尽量原样保留命令、路径、参数和英文符号；只在安全时补标点。'}

EXPECTED_WHISPER_PUNCTUATION = '请更积极地输出自然中文标点：根据语义加入逗号、句号、问号、顿号、分号或冒号，避免整段无标点；除识别文字和标点外不要额外解释。'


class _TempUserConfig(unittest.TestCase):
    """Base: point VOICE_IME_CONFIG at a fresh temp file and reset caches."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ime-config-test-")
        self.config_path = Path(self._tmp.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"VOICE_IME_CONFIG": str(self.config_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.reload()
        self.addCleanup(config.reload)

    def _write_user(self, payload) -> None:
        if isinstance(payload, str):
            self.config_path.write_text(payload, encoding="utf-8")
        else:
            self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        config.reload()


class PriorityLayersTest(_TempUserConfig):
    def test_env_beats_user_json_and_defaults(self) -> None:
        self._write_user({"asr": {"backend": "volc-bigmodel-asr"}})
        self.assertEqual(config.get("asr.backend"), "volc-bigmodel-asr")
        with mock.patch.dict(os.environ, {"VOICE_IME_ASR_BACKEND": "mimo-cloud"}):
            self.assertEqual(config.get("asr.backend", env="VOICE_IME_ASR_BACKEND"), "mimo-cloud")
            self.assertEqual(config.env_str("VOICE_IME_ASR_BACKEND", "x"), "mimo-cloud")

    def test_user_json_beats_defaults(self) -> None:
        self._write_user({"llm": {"timeout": 9.5}})
        self.assertEqual(config.env_float("VOICE_IME_LLM_TIMEOUT", 0.0), 9.5)

    def test_defaults_used_when_nothing_configured(self) -> None:
        self.assertEqual(config.get("llm.min_chars"), 50)
        self.assertEqual(config.env_int("VOICE_IME_LLM_MIN_CHARS", 0), 50)
        # 出厂默认（对拍基线 69e2c04 的 run-engine.sh 导出值）：candidates=1、
        # 降噪 rnnoise、qwen3 超时 180；llm_log 为 null（$HOME 不进 JSON，
        # 由 llm_postprocess 调用点兜底 ~/…/llm.json）。
        self.assertEqual(config.get("llm.candidates"), 1)
        self.assertEqual(config.get("audio.denoise_tier"), "rnnoise")
        self.assertEqual(config.get("asr.qwen3.start_timeout"), 180)
        self.assertIsNone(config.get("logging.llm_log"))

    def test_call_site_default_when_leaf_is_null(self) -> None:
        # llm.temperature 在 defaults.json 里是 null：动态默认由调用点给出。
        self.assertIsNone(config.get("llm.temperature"))
        self.assertEqual(config.env_float("VOICE_IME_LLM_TEMPERATURE", 0.25), 0.25)
        self.assertEqual(config.env_float("VOICE_IME_LLM_TEMPERATURE", 0.1), 0.1)

    def test_get_unknown_path_returns_default(self) -> None:
        self.assertIsNone(config.get("no.such.path"))
        self.assertEqual(config.get("no.such.path", default=7), 7)

    def test_typed_helpers_read_json_layer(self) -> None:
        self._write_user({"overlay": {"enabled": False}, "ui": {"candidate_page_size": 9}})
        self.assertFalse(config.env_bool("VOICE_IME_OVERLAY", True))
        self.assertEqual(config.env_int("VOICE_IME_CANDIDATE_PAGE_SIZE", 5), 9)
        self.assertEqual(config.env_str("VOICE_IME_CANDIDATE_UI", "inline"), "popup")


class DeepMergeTest(_TempUserConfig):
    def test_sibling_leaves_survive_partial_override(self) -> None:
        self._write_user({"llm": {"timeout": 30}})
        self.assertEqual(config.get("llm.timeout"), 30)          # 覆盖的叶子
        self.assertEqual(config.get("llm.min_chars"), 50)        # 兄弟叶子不受影响
        self.assertEqual(config.get("llm.candidates"), 1)
        # 其它顶层组也不受影响
        self.assertEqual(config.get("asr.qwen3.port"), 18081)


class CorruptUserJsonTest(_TempUserConfig):
    def test_corrupt_json_warns_and_uses_defaults(self) -> None:
        self.config_path.write_text("{ 不是合法 JSON !!!", encoding="utf-8")
        config.reload()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            value = config.env_int("VOICE_IME_LLM_MIN_CHARS", 0)
        self.assertEqual(value, 50)  # defaults 生效
        self.assertIn("无法解析", stderr.getvalue())
        # 绝不抛异常：再读几次也稳定
        self.assertEqual(config.get("asr.backend"), "qwen3-asr")


class EnvMapLockTest(unittest.TestCase):
    def _leaves(self, node, prefix=""):
        for key, value in node.items():
            if key.startswith("_") or key == "version":
                continue
            if isinstance(value, dict):
                yield from self._leaves(value, f"{prefix}{key}.")
            else:
                yield prefix + key

    def test_every_defaults_leaf_has_env_and_vice_versa(self) -> None:
        defaults = json.loads((ROOT / "config" / "defaults.json").read_text(encoding="utf-8"))
        leaves = set(self._leaves(defaults))
        mapped = set(config.ENV_MAP.values())
        self.assertEqual(leaves, mapped, "defaults.json 叶子与 ENV_MAP 值集必须双向锁死")
        self.assertEqual(len(config.ENV_MAP), len(mapped), "ENV_MAP 不得有重复 env 名指向同一路径")

    def test_env_map_env_names_are_prefixed(self) -> None:
        for env_name in config.ENV_MAP:
            self.assertTrue(env_name.startswith("VOICE_IME_"), env_name)


class BoolParsingParityTest(unittest.TestCase):
    """config.env_bool 必须与旧版 _env_bool 逐值一致（含大小写/空白）。"""

    _LEGACY_FALSY = {"0", "false", "no", "off", "disabled"}

    def _legacy(self, raw, default):
        if raw is None:
            return default
        return raw.strip().lower() not in self._LEGACY_FALSY

    def test_parity_matrix(self) -> None:
        raws = [
            None, "", " ", "0", "1", "true", "TRUE", "True", "false", "FALSE",
            "no", "No ", " off", "OFF", "disabled", "DISABLED", "yes", "on",
            "2", "junk", "\t0\n", " false ", "0x0",
        ]
        for raw in raws:
            for default in (True, False):
                env = {"VOICE_IME_TEST_FLAG": raw} if raw is not None else {"VOICE_IME_TEST_FLAG": ""}
                with mock.patch.dict(os.environ, env, clear=False):
                    if raw is None:
                        os.environ.pop("VOICE_IME_TEST_FLAG", None)
                    self.assertEqual(
                        config.env_bool("VOICE_IME_TEST_FLAG", default),
                        self._legacy(raw, default),
                        f"raw={raw!r} default={default!r}",
                    )

    def test_json_bool_layer(self) -> None:
        # json 布尔直接生效；未映射 env 时走 json 层。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"overlay": {"enabled": False}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"VOICE_IME_CONFIG": str(path)}):
                config.reload()
                os.environ.pop("VOICE_IME_OVERLAY", None)
                self.assertFalse(config.env_bool("VOICE_IME_OVERLAY", True))


class SetSaveAtomicTest(_TempUserConfig):
    def test_set_save_writes_valid_json_atomically(self) -> None:
        config.set("asr.backend", "mimo-cloud")
        config.set("llm.timeout", 12.5)
        config.save()
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "mimo-cloud")
        self.assertEqual(data["llm"]["timeout"], 12.5)
        # 重新加载后仍可读（用户层持久化生效）
        config.reload()
        self.assertEqual(config.get("asr.backend"), "mimo-cloud")
        self.assertEqual(config.get("llm.min_chars"), 50)  # 未写的键仍是默认

    def test_concurrent_reads_are_thread_safe(self) -> None:
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(50):
                    config.get("llm.prompts.system")
                    config.get("asr.qwen3.port")
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class CliTest(_TempUserConfig):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # 剥离宿主会话里的 VOICE_IME_*（env 层优先级最高，不剥离会压过测试用的用户 json）。
        env = {k: v for k, v in os.environ.items() if not k.startswith("VOICE_IME_")}
        env["PYTHONPATH"] = str(ROOT / "src")
        env["VOICE_IME_CONFIG"] = str(self.config_path)
        return subprocess.run(
            [_python_bin(), "-m", "ibus_voice_ime.config", *args],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60,
        )

    def test_get_prints_default_value(self) -> None:
        result = self._run("get", "llm.min_chars")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "50")

    def test_get_missing_path_exits_nonzero(self) -> None:
        result = self._run("get", "no.such.path")
        self.assertNotEqual(result.returncode, 0)

    def test_set_writes_user_layer(self) -> None:
        result = self._run("set", "asr.backend", "volc-bigmodel-asr")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "volc-bigmodel-asr")
        self.assertEqual(self._run("get", "asr.backend").stdout.strip(), "volc-bigmodel-asr")

    def test_set_int_and_bool_typing(self) -> None:
        self.assertEqual(self._run("set", "ui.candidate_page_size", "7", "--int").returncode, 0)
        self.assertEqual(self._run("set", "overlay.enabled", "0", "--bool").returncode, 0)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["ui"]["candidate_page_size"], 7)
        self.assertIs(data["overlay"]["enabled"], False)

    def test_init_creates_skeleton_once(self) -> None:
        first = self._run("init")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(self.config_path.is_file())
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("_说明", data)
        before = self.config_path.read_text(encoding="utf-8")
        second = self._run("init")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)  # 已存在则不动

    def test_validate_ok_and_broken(self) -> None:
        self._write_user({"llm": {"timeout": 8}})
        ok = self._run("validate")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self._write_user("{{ broken")
        broken = self._run("validate")
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("损坏", broken.stdout)

    def test_validate_flags_unknown_key_and_type_mismatch(self) -> None:
        self._write_user({"bogus_group": {"x": 1}, "llm": {"timeout": "not-a-number"}})
        result = self._run("validate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未知顶层键：bogus_group", result.stdout)
        self.assertIn("类型不符：llm.timeout", result.stdout)

    def test_migrate_env_file(self) -> None:
        env_file = Path(self._tmp.name) / "ibus-voice-ime.conf"
        env_file.write_text(
            "# comment line\n"
            "VOICE_IME_ASR_BACKEND=volc-bigmodel-asr\n"
            "VOICE_IME_OVERLAY=1\n"
            "SOME_UNRELATED_VAR=keep me\n",
            encoding="utf-8",
        )
        result = self._run("migrate-env-file", str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        # 迁移后的键不再出现在“清理后”的 env 文件里；其余行原样保留
        self.assertNotIn("VOICE_IME_ASR_BACKEND", result.stdout)
        self.assertNotIn("VOICE_IME_OVERLAY", result.stdout)
        self.assertIn("SOME_UNRELATED_VAR=keep me", result.stdout)
        self.assertIn("# comment line", result.stdout)
        # 值进入了用户 config.json（按 defaults 的叶子类型归型）
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "volc-bigmodel-asr")
        self.assertIs(data["overlay"]["enabled"], True)

    def test_env_command_prints_effective_values(self) -> None:
        self._write_user({"asr": {"backend": "siliconflow"}})
        result = self._run("env")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        self.assertEqual(lines.get("VOICE_IME_ASR_BACKEND"), "siliconflow")
        self.assertEqual(lines.get("VOICE_IME_LLM_MIN_CHARS"), "50")
        # null 叶子（无静态默认）不应输出
        self.assertNotIn("VOICE_IME_LLM_TEMPERATURE", lines)


class PromptByteLockTest(unittest.TestCase):
    """defaults.json 里的 5 组提示词必须与外置前的源码原文逐字节相等。"""

    def test_system_prompt(self) -> None:
        self.assertEqual(config.get_prompt("system"), EXPECTED_SYSTEM_PROMPT)

    def test_candidate_system_prompt(self) -> None:
        self.assertEqual(config.get_prompt("candidate_system"), EXPECTED_CANDIDATE_SYSTEM_PROMPT)

    def test_format_requirement(self) -> None:
        self.assertEqual(config.get_prompt("format_requirement"), EXPECTED_FORMAT_REQUIREMENT)

    def test_mode_instructions(self) -> None:
        for mode, expected in EXPECTED_MODE_INSTRUCTIONS.items():
            self.assertEqual(
                config.get_prompt(f"llm.prompts.mode_instructions.{mode}"), expected, mode
            )

    def test_whisper_punctuation(self) -> None:
        self.assertEqual(config.get_prompt("whisper_punctuation"), EXPECTED_WHISPER_PUNCTUATION)

    def test_llm_postprocess_aliases_match(self) -> None:
        # 保留的弃用别名（模块属性）也必须实时读 config 且与原文一致。
        self.assertEqual(llm_postprocess.SYSTEM_PROMPT, EXPECTED_SYSTEM_PROMPT)
        self.assertEqual(llm_postprocess.CANDIDATE_SYSTEM_PROMPT, EXPECTED_CANDIDATE_SYSTEM_PROMPT)
        self.assertEqual(llm_postprocess.FORMAT_REQUIREMENT, EXPECTED_FORMAT_REQUIREMENT)
        self.assertEqual(llm_postprocess.MODE_INSTRUCTIONS, EXPECTED_MODE_INSTRUCTIONS)


class PromptOverrideTest(_TempUserConfig):
    def test_user_json_overrides_system_prompt(self) -> None:
        self._write_user({"llm": {"prompts": {"system": "我的自定义提示词。"}}})
        self.assertEqual(config.get_prompt("system"), "我的自定义提示词。")
        # 其它提示词不受影响
        self.assertEqual(config.get_prompt("candidate_system"), EXPECTED_CANDIDATE_SYSTEM_PROMPT)

    def test_env_beats_user_json_for_prompt(self) -> None:
        self._write_user({"llm": {"prompts": {"system": "json 版"}}})
        with mock.patch.dict(os.environ, {"VOICE_IME_PROMPT_SYSTEM": "env 版"}):
            self.assertEqual(config.get_prompt("system"), "env 版")


class StalePathSelfHealingTest(_TempUserConfig):
    """路径型叶子：config 层的非空失效路径视为未配置（回落下一层）。"""

    def setUp(self) -> None:
        super().setUp()
        # 开发机会话常驻 VOICE_IME_*（env 层最高优先会压过测试用的用户 json）。
        # 注意保留 VOICE_IME_CONFIG 本身（父类刚指向临时文件）。
        scrubbed = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("VOICE_IME_", "MIMO", "VOLC", "SILICONFLOW"))
        }
        scrubbed["VOICE_IME_CONFIG"] = str(self.config_path)
        patcher = mock.patch.dict(os.environ, scrubbed, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stale_model_path_falls_through_to_default(self) -> None:
        self._write_user({"asr": {"qwen3": {"model_path": "/nonexistent/moved-repo/model"}}})
        self.assertEqual(
            config.get("asr.qwen3.model_path", default="/fresh/default/model"),
            "/fresh/default/model",
        )
        self.assertEqual(
            config.env_str("VOICE_IME_QWEN_ASR_MODEL_PATH", "/fresh/default/model"),
            "/fresh/default/model",
        )

    def test_live_path_value_is_returned(self) -> None:
        self._write_user({"rime": {"library": str(ROOT / "run-engine.sh")}})  # 真实存在的文件
        self.assertEqual(config.get("rime.library", default="x"), str(ROOT / "run-engine.sh"))

    def test_empty_string_path_keeps_explicit_empty_semantics(self) -> None:
        # rime.library="" 表示"走系统默认"，是显式空值，不是失效路径。
        self._write_user({"rime": {"library": ""}})
        self.assertEqual(config.get("rime.library", default="fallback"), "")

    def test_env_layer_value_is_not_guarded(self) -> None:
        # env 值是救援输入（run-engine.sh 有自己的 env 自愈段），原样返回。
        self._write_user({"asr": {"qwen3": {"model_path": str(ROOT)}}})
        with mock.patch.dict(os.environ, {"VOICE_IME_QWEN_ASR_MODEL_PATH": "/nonexistent/rescue"}):
            self.assertEqual(
                config.env_str("VOICE_IME_QWEN_ASR_MODEL_PATH", "d"), "/nonexistent/rescue"
            )


def _run_config_cli(config_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICE_IME_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["VOICE_IME_CONFIG"] = str(config_path)
    return subprocess.run(
        [_python_bin(), "-m", "ibus_voice_ime.config", *args],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60,
    )


class MigrateReportingTest(_TempUserConfig):
    """migrate-env-file：路径型拒迁、密钥/无对应键点名报告。"""

    def test_path_typed_keys_refuse_migration(self) -> None:
        env_file = Path(self._tmp.name) / "env.conf"
        env_file.write_text(
            "VOICE_IME_ASR_BACKEND=qwen3-asr\n"
            "VOICE_IME_QWEN_ASR_MODEL_PATH=/old/repo/model\n"
            "VOICE_IME_RIME_LIBRARY=/old/repo/librime.so.1\n",
            encoding="utf-8",
        )
        result = _run_config_cli(self.config_path, "migrate-env-file", str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        # 渠道键正常迁移；路径型键留在"清理后"输出里、不进 config.json。
        self.assertIn("VOICE_IME_QWEN_ASR_MODEL_PATH=/old/repo/model", result.stdout)
        self.assertIn("VOICE_IME_RIME_LIBRARY=/old/repo/librime.so.1", result.stdout)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "qwen3-asr")
        self.assertNotIn("model_path", data["asr"].get("qwen3", {}))
        self.assertNotIn("library", data.get("rime", {}))
        # stderr 报告逐行说明去向。
        self.assertIn("路径型键不迁移", result.stderr)
        self.assertIn("VOICE_IME_QWEN_ASR_MODEL_PATH", result.stderr)

    def test_raw_key_and_unmapped_lines_are_named_in_report(self) -> None:
        env_file = Path(self._tmp.name) / "env.conf"
        env_file.write_text(
            "VOICE_IME_ASR_BACKEND=siliconflow-asr\n"
            "VOICE_IME_SILICONFLOW_API_KEY=sk-fake-not-a-real-key-000000\n"
            "VOICE_IME_MIMO_BASE_URL=https://legacy.example/v1\n",
            encoding="utf-8",
        )
        result = _run_config_cli(self.config_path, "migrate-env-file", str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        # 渠道键迁移成功；两行未迁移键在 stderr 逐行点名（不再静默黑洞）。
        self.assertIn("已迁移到 config.json：VOICE_IME_ASR_BACKEND", result.stderr)
        self.assertIn("VOICE_IME_SILICONFLOW_API_KEY", result.stderr)
        self.assertIn("密钥本体不迁入", result.stderr)
        self.assertIn("VOICE_IME_MIMO_BASE_URL", result.stderr)
        self.assertIn("无对应配置键", result.stderr)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "siliconflow-asr")

class SecretDefenseTest(_TempUserConfig):
    """密钥防御纵深：validate 告警 + env 输出脱敏。"""

    def test_validate_flags_plaintext_api_key_value(self) -> None:
        self._write_user({"llm": {"base_url": "sk-abcdef0123456789abcdef"}})
        result = _run_config_cli(self.config_path, "validate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("疑似明文 API Key", result.stdout)
        self.assertIn("llm.base_url", result.stdout)

    def test_env_command_masks_secret_like_values(self) -> None:
        self._write_user({"text": {"voice_dictionary": "sk-abcdef0123456789abcdef"}})
        result = _run_config_cli(self.config_path, "env")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VOICE_IME_VOICE_DICTIONARY=sk-****", result.stdout)
        self.assertNotIn("sk-abcdef0123456789abcdef", result.stdout)

if __name__ == "__main__":
    unittest.main()
