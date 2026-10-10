# -*- coding: utf-8 -*-
"""doctor.sh check_stale_engine_env 分类语义守卫。

2026-09 假阳性事故：run-engine.sh 无条件导出 VOICE_IME_LLM_CONFIG 默认路径
（llm.json 是可选连接文件，缺失 = LLM 润色关闭的合法状态），被本检查误报
为 FAIL——且 doctor fix 模式的"重启引擎"永远修不好（重启后再次导出同样的
不存在路径）。本测试直接抽取 doctor.sh 里的真实 case 分类块，在隔离 bash
中逐行验证语义边界：

  - VOICE_IME_LLM_CONFIG（可选连接文件）一律跳过；
  - 只检查以 / 开头的绝对路径值（URL 不参与 -e 判断）；
  - 冒号分隔的多路径值逐段检查（空段跳过）。

抽取方式保证测试对象就是仓库里实际部署的代码，而不是复制品。
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _extract_case_block() -> str:
    script = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    func = re.search(
        r"^check_stale_engine_env\(\) \{.*?^\}", script, re.M | re.S
    )
    if not func:
        raise AssertionError("doctor.sh 中找不到 check_stale_engine_env 函数")
    case = re.search(r'( {6}case "\$line" in\n.*?\n {6}esac)', func.group(0), re.S)
    if not case:
        raise AssertionError("check_stale_engine_env 中找不到 case 分类块")
    return case.group(1)


def _run_case_block(line: str) -> list[str]:
    """在隔离 bash 中对单行环境变量执行真实分类块，返回 fail() 消息列表。

    用真实的 for 循环包裹（case 块内的 `continue` 语义与 run_one 调用点
    一致，脱离循环上下文时 continue 只报错并掉入下一条语句），并开启
    `set -uo pipefail` 对齐 doctor.sh 自身的 shell 选项；stderr 非空视为
    harness 异常直接失败，防止吞掉块内报错造成假通过。
    """
    harness = (
        "set -uo pipefail\n"
        "fail() { echo \"FAILMATCH:$1\"; }\n"
        "ok() { :; }\n"
        "run_one() {\n"
        "  local line var val part dead=0 parts=()\n"
        "  for line in \"$1\"; do\n"
        + _extract_case_block()
        + "\n  done\n"
        "}\n"
        "run_one \"$1\"\n"
    )
    proc = subprocess.run(
        ["bash", "-c", harness, "--", line],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or proc.stderr:
        raise AssertionError(
            f"分类块执行异常（code={proc.returncode}）：{proc.stderr}"
        )
    return [l for l in proc.stdout.splitlines() if l.startswith("FAILMATCH:")]


class DoctorStaleEnvTest(unittest.TestCase):
    def test_optional_llm_config_absent_file_is_not_flagged(self) -> None:
        # 核心回归：llm.json 缺失 = 功能关闭，不得 FAIL。
        self.assertEqual(
            _run_case_block(
                "VOICE_IME_LLM_CONFIG=/home/x/.config/ibus-voice-ime/llm.json"
            ),
            [],
        )

    def test_optional_llm_config_custom_name_is_not_flagged(self) -> None:
        # 用户自定义文件名（同变量、路径含仓库名）同样跳过。
        self.assertEqual(
            _run_case_block(
                "VOICE_IME_LLM_CONFIG=/home/x/.config/ibus-voice-ime/llm-prod.json"
            ),
            [],
        )

    def test_stale_absolute_repo_path_is_flagged(self) -> None:
        msgs = _run_case_block(
            "VOICE_IME_RIME_LIBRARY=/nonexistent/ibus-voice-ime/lib/librime.so.1"
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("VOICE_IME_RIME_LIBRARY", msgs[0])
        self.assertIn("/nonexistent/ibus-voice-ime/lib/librime.so.1", msgs[0])

    def test_url_value_is_not_flagged(self) -> None:
        # 值必须以 / 开头才参与存在性判断（URL 含仓库名也不误报）。
        self.assertEqual(
            _run_case_block(
                "VOICE_IME_MIMO_CLOUD_BASE_URL=https://example.com/ibus-voice-ime/api"
            ),
            [],
        )

    def test_colon_separated_dead_segment_is_flagged(self) -> None:
        msgs = _run_case_block(
            "VOICE_IME_MULTI=/nonexistent/ibus-voice-ime/a:/usr/lib64"
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("/nonexistent/ibus-voice-ime/a", msgs[0])

    def test_colon_separated_live_segments_are_not_flagged(self) -> None:
        self.assertEqual(
            _run_case_block("VOICE_IME_MULTI=/usr/lib64:/tmp"), []
        )

    def test_trailing_colon_is_not_flagged(self) -> None:
        # 值为 "路径:" 时末尾空段必须跳过，且已存在路径不误报。
        self.assertEqual(_run_case_block("VOICE_IME_MULTI=/tmp:"), [])

    def test_relative_path_value_is_not_flagged(self) -> None:
        # 非绝对路径（相对路径/纯文件名）不参与存在性判断。
        self.assertEqual(
            _run_case_block("VOICE_IME_ODD=rel/ibus-voice-ime/x"), []
        )

    def test_backtracking_value_with_second_equals_is_not_flagged(self) -> None:
        # 通配回溯守卫：case 的 `*` 可跨 '=' 匹配（如带 token= 前缀的值），
        # 取 val 后必须强制"以 / 开头"不变式，否则对整串做 -e 会误报。
        self.assertEqual(
            _run_case_block(
                "VOICE_IME_FOO=token=/nonexistent/ibus-voice-ime/x"
            ),
            [],
        )

    def test_llm_config_prefix_collision_is_not_skipped(self) -> None:
        # 前缀冲突守卫：VOICE_IME_LLM_CONFIG_EXTRA 不是可选连接文件变量，
        # 跳过分支的字面量 '=' 定界不得把它一并放过（防未来写成 `*=` 笔误）。
        msgs = _run_case_block(
            "VOICE_IME_LLM_CONFIG_EXTRA=/nonexistent/ibus-voice-ime/lib.so"
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("VOICE_IME_LLM_CONFIG_EXTRA", msgs[0])

    def test_middle_empty_colon_segment_is_skipped(self) -> None:
        # 中间空段（::）必须跳过：真实代码用 continue，harness 已用循环
        # 包裹保证语义一致（曾因脱离循环上下文产生假 FAILMATCH）。
        self.assertEqual(_run_case_block("VOICE_IME_MULTI=/tmp::/usr"), [])


if __name__ == "__main__":
    unittest.main()
