# -*- coding: utf-8 -*-
"""librime dictionary/schema deployer for ibus-voice-ime.

Compiles Rime ``*.dict.yaml`` sources into the ``*.table.bin`` /
``*.prism.bin`` artifacts that librime loads at runtime, using the vendored
``librime.so.1`` through ``rime_get_api()``. No ``rime_deployer`` binary is
required; this mirrors the deploy path that ``fcitx5-rime`` / ``ibus-rime``
expose via their "重新部署" buttons.

Invoked by ``scripts/setup-rime-ice.sh`` to prebuild rime-ice + zhwiki +
moegirl before the IBus engine first loads them, so the user never sees the
slow first-run compilation.

Environment (defaults match install.sh / run-engine.sh):
    VOICE_IME_RIME_LIBRARY           librime.so.1 path
    VOICE_IME_RIME_SHARED_DATA_DIR   schema/dict sources
    VOICE_IME_RIME_USER_DATA_DIR     build output (./build subdir)
"""
from __future__ import annotations

import ctypes
import os

from ibus_voice_ime import config
import sys
import time
from pathlib import Path

Bool = ctypes.c_int


class RimeTraits(ctypes.Structure):
    _fields_ = [
        ("data_size", ctypes.c_int),
        ("shared_data_dir", ctypes.c_char_p),
        ("user_data_dir", ctypes.c_char_p),
        ("distribution_name", ctypes.c_char_p),
        ("distribution_code_name", ctypes.c_char_p),
        ("distribution_version", ctypes.c_char_p),
        ("app_name", ctypes.c_char_p),
        ("modules", ctypes.POINTER(ctypes.c_char_p)),
        ("min_log_level", ctypes.c_int),
        ("log_dir", ctypes.c_char_p),
        ("prebuilt_data_dir", ctypes.c_char_p),
        ("staging_dir", ctypes.c_char_p),
    ]


class RimeApi(ctypes.Structure):
    pass


# Only the function-pointer slots up to and including the deploy_* entries are
# declared; the rest are left as c_void_p placeholders to keep offsets correct.
RimeApi._fields_ = [
    ("data_size", ctypes.c_int),
    ("setup", ctypes.CFUNCTYPE(None, ctypes.POINTER(RimeTraits))),
    ("set_notification_handler", ctypes.c_void_p),
    ("initialize", ctypes.CFUNCTYPE(None, ctypes.POINTER(RimeTraits))),
    ("finalize", ctypes.CFUNCTYPE(None)),
    ("start_maintenance", ctypes.c_void_p),
    ("is_maintenance_mode", ctypes.c_void_p),
    ("join_maintenance_thread", ctypes.c_void_p),
    ("deployer_initialize", ctypes.CFUNCTYPE(None, ctypes.POINTER(RimeTraits))),
    ("prebuild", ctypes.c_void_p),
    ("deploy", ctypes.CFUNCTYPE(ctypes.c_int)),
    ("deploy_schema", ctypes.CFUNCTYPE(Bool, ctypes.c_char_p)),
    ("deploy_config_file", ctypes.CFUNCTYPE(Bool, ctypes.c_char_p, ctypes.c_char_p)),
    ("sync_user_data", ctypes.c_void_p),
    ("create_session", ctypes.c_void_p),
]


class DeployError(RuntimeError):
    pass


def _env(name: str, default: str) -> str:
    # defaults.json 里这些路径类键为空串（""=自动定位 vendored 运行时）；
    # json 空值或未配置时回落到本函数的计算式默认，保持旧行为。
    return config.env_str(name, "") or default


def _resolve_paths(root_dir: Path) -> tuple[Path, Path, Path]:
    lib = Path(_env("VOICE_IME_RIME_LIBRARY", str(root_dir / "vendor/rime/lib/librime.so.1")))
    shared = Path(_env("VOICE_IME_RIME_SHARED_DATA_DIR",
                        str(root_dir / "vendor/rime/share/rime-data")))
    user = Path(_env("VOICE_IME_RIME_USER_DATA_DIR",
                      str(Path.home() / ".local/share/ibus-voice-ime/rime-user")))
    return lib, shared, user


def deploy(root_dir: Path | None = None) -> None:
    root_dir = root_dir or Path(__file__).resolve().parents[3]
    lib_path, shared_dir, user_dir = _resolve_paths(root_dir)

    if not lib_path.is_file():
        raise DeployError(f"找不到 librime：{lib_path}（先运行 scripts/vendor-rime-runtime.sh）")
    if not shared_dir.is_dir():
        raise DeployError(f"找不到 shared_data_dir：{shared_dir}")

    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "build").mkdir(parents=True, exist_ok=True)

    # librime's non-glibc transitive deps (leveldb/marisa/opencc/glog/...) live
    # next to librime.so.1; ensure the loader can resolve them.
    lib_dir = lib_path.parent
    if str(lib_dir) not in os.environ.get("LD_LIBRARY_PATH", ""):
        os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{os.environ.get('LD_LIBRARY_PATH', '')}"

    try:
        lib = ctypes.CDLL(str(lib_path))
    except OSError as exc:
        # 内置 librime 是 Fedora 构建的，在更老 glibc 的发行版（Ubuntu 22.04 等）
        # 会报 GLIBC_x.xx not found；回退系统 librime（rime_backend 同款策略），
        # 让 check-environment.sh 建议的 VOICE_IME_RIME_LIBRARY=系统库 路径真正可用。
        try:
            lib = ctypes.CDLL("librime.so.1")
        except OSError:
            raise DeployError(
                f"无法加载 {lib_path}（{exc}）。内置库与系统不兼容；"
                "安装系统 librime 后重试：Fedora sudo dnf install librime / "
                "Debian系 sudo apt install librime，并在运行前设置 "
                f"VOICE_IME_RIME_LIBRARY 指向系统库（如 /usr/lib/x86_64-linux-gnu/librime.so.1）"
            ) from exc
    lib.rime_get_api.restype = ctypes.POINTER(RimeApi)
    api_ptr = lib.rime_get_api()
    if not api_ptr:
        raise DeployError("librime 没有返回有效 API")
    api = api_ptr.contents

    traits = RimeTraits()
    traits.data_size = ctypes.sizeof(RimeTraits) - ctypes.sizeof(ctypes.c_int)
    traits.shared_data_dir = str(shared_dir).encode("utf-8")
    traits.user_data_dir = str(user_dir).encode("utf-8")
    traits.distribution_name = b"ibus-voice-ime"
    traits.distribution_code_name = b"rime-ice"
    traits.distribution_version = b"0.1.0"
    traits.app_name = b"rime.ibus-voice-ime.deployer"
    traits.min_log_level = int(config.env_str("VOICE_IME_RIME_LOG_LEVEL", "1"))

    print(f"==> 部署 Rime 字典/方案")
    print(f"    shared_data_dir = {shared_dir}")
    print(f"    user_data_dir   = {user_dir}")
    api.deployer_initialize(ctypes.byref(traits))

    t0 = time.time()
    ret = api.deploy()  # 全量编译 default.yaml 中 schema_list 的所有方案
    api.finalize()
    elapsed = time.time() - t0
    if not ret:
        raise DeployError(f"Rime deploy 失败（返回 {ret}）")
    print(f"==> 编译完成，耗时 {elapsed:.1f}s，产物在 {user_dir / 'build'}")


def main() -> int:
    try:
        deploy()
    except DeployError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
