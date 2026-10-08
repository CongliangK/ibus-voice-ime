# -*- coding: utf-8 -*-
"""Regression tests for scripts/gpu-probe.sh (three-state NVIDIA detection).

Reproduces the 2026-10 GitHub incident classes with a fake PCI sysfs tree and
stub ``nvidia-smi`` binaries on PATH:

  ok       — nvidia-smi -L works
  partial  — NVIDIA hardware present but driver half-installed (the incident:
             only nvidia-modprobe/nvidia-settings userspace, no nvidia-smi),
             or nouveau bound, or nvidia-smi exists but kernel module mismatch
  none     — no NVIDIA hardware at all
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "gpu-probe.sh"


def _make_pci_device(sysfs: Path, vendor: str, klass: str, driver: str | None) -> None:
    dev = sysfs / "0000_01_00.0"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "class").write_text(klass + "\n")
    if driver is not None:
        (dev / "driver").symlink_to(f"/sys/bus/pci/drivers/{driver}")


def _make_stub_dir(binname: str, body: str) -> tempfile.TemporaryDirectory:
    stub = tempfile.TemporaryDirectory(prefix="gpu-probe-stub-")
    path = Path(stub.name) / binname
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return stub


def _run_probe(env_extra: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GPU_PROBE_SYSFS", None)
    env.update(env_extra)
    # 输出是 %q 转义的赋值（eval 安全），测试里同样用 eval 解回原始值，
    # 字段间用 \x1f 分隔（REASON/ACTION 可含换行）。
    script = (
        "eval \"$(bash '" + str(PROBE) + "' --env)\"; "
        "printf '%s\x1f%s\x1f%s\x1f%s' "
        "\"$GPU_STATE\" \"$GPU_BRIEF\" \"$GPU_REASON\" \"$GPU_ACTION\""
    )
    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    ).stdout
    parts = out.split("\x1f")
    keys = ("GPU_STATE", "GPU_BRIEF", "GPU_REASON", "GPU_ACTION")
    return {k: (parts[i] if i < len(parts) else "") for i, k in enumerate(keys)}


def _make_sandbox_bin(extra_stub: str | None = None) -> tempfile.TemporaryDirectory:
    """PATH 沙箱：只有 bash/head/basename/readlink（+可选 stub）。

    测试机本身装着真 nvidia-smi；验证“无 nvidia-smi”的分支必须把它从
    PATH 里排除，单纯前置 stub 目录只会被 /usr/bin 的真货遮蔽不了。
    """
    box = tempfile.TemporaryDirectory(prefix="gpu-probe-bin-")
    binroot = Path(box.name)
    for tool in ("bash", "head", "basename", "readlink"):
        real = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if real:
            (binroot / tool).symlink_to(real)
    if extra_stub:
        target = Path(extra_stub)
        (binroot / target.name).symlink_to(target)
    return box


class GpuProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._sysfs = tempfile.TemporaryDirectory(prefix="gpu-probe-sysfs-")
        self.sysfs = Path(self._sysfs.name) / "devices"
        self.sysfs.mkdir(parents=True, exist_ok=True)
        self._stubs: list[tempfile.TemporaryDirectory] = []

    def tearDown(self) -> None:
        self._sysfs.cleanup()
        for stub in self._stubs:
            stub.cleanup()

    def _stub(self, binname: str, body: str) -> str:
        stub = _make_stub_dir(binname, body)
        self._stubs.append(stub)
        return str(Path(stub.name))

    def _sandbox_env(self, stub_dir: str | None = None, stub_name: str | None = None) -> dict[str, str]:
        """无真 nvidia-smi 的 PATH 环境（half-installed / none 分支用）。"""
        extra = str(Path(stub_dir) / stub_name) if stub_dir and stub_name else None
        box = _make_sandbox_bin(extra)
        self._stubs.append(box)
        return {"GPU_PROBE_SYSFS": str(self.sysfs), "PATH": str(Path(box.name))}

    def _shadow_env(self, stub_dir: str) -> dict[str, str]:
        """保留系统 PATH 但用 stub 遮蔽同名命令（ok / kmod-mismatch 分支用）。"""
        return {
            "GPU_PROBE_SYSFS": str(self.sysfs),
            "PATH": stub_dir + ":" + os.environ.get("PATH", ""),
        }

    def test_ok_when_nvidia_smi_lists_gpu(self) -> None:
        stub = self._stub("nvidia-smi", "echo 'GPU 0: NVIDIA GeForce RTX 4080 (UUID: x)'\n")
        values = _run_probe(self._shadow_env(stub))
        self.assertEqual(values["GPU_STATE"], "ok")
        self.assertIn("RTX 4080", values["GPU_BRIEF"])

    def test_partial_half_installed_driver_is_the_incident(self) -> None:
        # 报障机器：装了 nvidia-settings/nvidia-modprobe 用户态工具（PATH 里能看到），
        # 但没有 nvidia-smi、PCI 设备无驱动绑定。
        settings_dir = self._stub("nvidia-settings", "exit 0\n")
        _make_pci_device(self.sysfs, "0x10de", "0x030000", driver=None)
        values = _run_probe(self._sandbox_env(settings_dir, "nvidia-settings"))
        self.assertEqual(values["GPU_STATE"], "partial")
        self.assertIn("只装了一半", values["GPU_REASON"])
        self.assertIn("akmod-nvidia", values["GPU_ACTION"])

    def test_partial_nouveau_bound(self) -> None:
        _make_pci_device(self.sysfs, "0x10de", "0x030000", driver="nouveau")
        values = _run_probe(self._sandbox_env())
        self.assertEqual(values["GPU_STATE"], "partial")
        self.assertIn("nouveau", values["GPU_REASON"])

    def test_partial_smi_exists_but_module_mismatch(self) -> None:
        # 内核升级后 kmod 未重编：nvidia-smi 在，-L 失败。
        stub = self._stub("nvidia-smi", "echo 'nvidia-smi: couldn\\'t communicate...' >&2\nexit 9\n")
        _make_pci_device(self.sysfs, "0x10de", "0x030000", driver="nvidia")
        values = _run_probe(self._shadow_env(stub))
        self.assertEqual(values["GPU_STATE"], "partial")
        self.assertIn("akmod", values["GPU_ACTION"])

    def test_partial_3d_controller_class_counts_as_hardware(self) -> None:
        # Optimus 独显直连/仅计算卡是 0x0302xx（3D controller）。
        _make_pci_device(self.sysfs, "0x10de", "0x030200", driver=None)
        values = _run_probe(self._sandbox_env())
        self.assertEqual(values["GPU_STATE"], "partial")

    def test_none_when_no_nvidia_hardware(self) -> None:
        # Intel 核显 + 没有 nvidia-smi。
        stub_dir = self._stub("nvidia-smi", "exit 1\n")
        (self.sysfs / "0000_00_02.0").mkdir(parents=True)
        (self.sysfs / "0000_00_02.0/vendor").write_text("0x8086\n")
        (self.sysfs / "0000_00_02.0/class").write_text("0x030000\n")
        values = _run_probe(self._sandbox_env(stub_dir, "nvidia-smi"))
        self.assertEqual(values["GPU_STATE"], "none")
        self.assertIn("switch-mimo-cloud-asr", values["GPU_ACTION"])

    def test_none_when_sysfs_missing(self) -> None:
        # 容器里连 sysfs 都没有：降级为 none 而不是崩。
        stub = self._stub("nvidia-smi", "exit 1\n")
        values = _run_probe({
            "GPU_PROBE_SYSFS": "/nonexistent-sysfs",
            "PATH": stub + ":" + os.environ.get("PATH", ""),
        })
        self.assertEqual(values["GPU_STATE"], "none")

    def test_human_output_mode(self) -> None:
        stub = self._stub("nvidia-smi", "echo 'GPU 0: NVIDIA A100'\n")
        env = os.environ.copy()
        env["GPU_PROBE_SYSFS"] = str(self.sysfs)
        env["PATH"] = stub + ":" + env.get("PATH", "")
        out = subprocess.run(
            ["bash", str(PROBE)], env=env, capture_output=True, text=True, check=True
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn("GPU: ok", out.stdout)


if __name__ == "__main__":
    unittest.main()
