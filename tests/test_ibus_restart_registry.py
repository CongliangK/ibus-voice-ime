# -*- coding: utf-8 -*-
"""ibus-restart.sh 注册缓存自愈守卫。

2026-10 事故：IBus 1.5.3x 的 ibus_registry_load 在进程环境没有
IBUS_COMPONENT_PATH 时只扫系统组件目录（用户目录支持在上游源码中被
#if 0 禁用），而 GNOME/systemd 开机拉起的 ibus-daemon 走 --cache auto，
直接信任 ~/.cache/ibus/bus/registry 缓存、仅缓存失效才重扫。因此任何一次
无该变量的 daemon 重启（refresh 重扫覆写 / auto 失效重扫写缓存）都会把
用户组件从 registry 抹掉，之后 auto daemon 认定缓存有效、永不再扫用户
目录——症状为重启电脑后输入法"未注册"且不自愈（doctor fix 与用户手动
重启曾是覆写入口）。本测试用 stub ibus/ibus-daemon 驱动真实脚本验证：

  - 组件 XML 存在时：自带 IBUS_COMPONENT_PATH（推导用户组件目录+系统目录），
    并在重启前先执行 ibus write-cache 固化缓存；
  - 调用方显式传入的 IBUS_COMPONENT_PATH 优先（沿用既有 env 前缀调用）；
  - XML 缺失时不注入路径、不写缓存（未安装状态不触碰注册缓存）；
  - 非 GNOME 分支（ibus restart）同样先固化缓存。

另含 doctor.sh 新检测项的静态守卫：daemon 视角 FAIL 归入 ibus-registry，
fix 分派存在对应重建分支。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ibus-restart.sh"

STUB = """#!/usr/bin/env bash
printf '%s argv=[%s] ICP=[%s]\\n' "$1" "$*" "${IBUS_COMPONENT_PATH:-<unset>}" >> "$STUB_LOG"
exit 0
"""


class IBusRestartRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.bin = Path(self._tmp.name) / "bin"
        (self.home / ".local/share/ibus/component").mkdir(parents=True)
        self.bin.mkdir(parents=True)
        for name in ("ibus", "ibus-daemon"):
            stub = self.bin / name
            stub.write_text(STUB, encoding="utf-8")
            stub.chmod(0o755)
        self.log = Path(self._tmp.name) / "stub.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, home: Path, xml_exists: bool, env_extra: dict | None = None,
             desktop: str | None = "GNOME") -> str:
        if xml_exists:
            (home / ".local/share/ibus/component/voice-custom.xml").write_text(
                "<component/>", encoding="utf-8")
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(home),
            "STUB_LOG": str(self.log),
        }
        if desktop is not None:
            env["XDG_CURRENT_DESKTOP"] = desktop
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            ["bash", str(SCRIPT)], env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_xml_present_derives_path_and_writes_cache(self) -> None:
        log = self._run(self.home, xml_exists=True)
        expected = f"{self.home}/.local/share/ibus/component:/usr/share/ibus/component"
        self.assertIn(f"write-cache argv=[write-cache] ICP=[{expected}]", log)
        self.assertIn("--cache refresh", log)
        self.assertIn(expected, log.splitlines()[-1])

    def test_caller_supplied_path_wins(self) -> None:
        log = self._run(self.home, xml_exists=True,
                        env_extra={"IBUS_COMPONENT_PATH": "/custom/path"})
        self.assertIn("ICP=[/custom/path]", log.splitlines()[0])
        self.assertNotIn(f"{self.home}/.local/share/ibus/component:", log.splitlines()[0])

    def test_xml_absent_leaves_path_unset_and_skips_cache(self) -> None:
        log = self._run(self.home, xml_exists=False)
        self.assertNotIn("write-cache", log)
        daemon_line = log.splitlines()[-1]
        self.assertIn("argv=[-drx --replace --panel disable --cache refresh]", daemon_line)
        self.assertIn("ICP=[<unset>]", daemon_line)

    def test_non_gnome_branch_also_writes_cache(self) -> None:
        log = self._run(self.home, xml_exists=True, desktop=None)
        self.assertIn("write-cache", log)
        self.assertIn("argv=[restart]", log)


class DoctorRegistryDetectionStaticGuard(unittest.TestCase):
    """doctor.sh 的 ibus-registry 检测与 fix 分派标记（防回归）。"""

    def test_detection_and_fix_dispatch_present(self) -> None:
        script = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
        self.assertIn("FAILED_ITEMS+=(ibus-registry)", script)
        self.assertIn("ibus-registry)", script)
        self.assertIn("voice-custom", script.split("ibus-registry)")[0].split("IBus 注册")[-1])


if __name__ == "__main__":
    unittest.main()
