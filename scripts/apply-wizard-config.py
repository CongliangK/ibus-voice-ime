#!/usr/bin/env python3
"""应用安装向导的白名单配置；密钥与安装资产不写入用户配置。"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from ibus_voice_ime import config  # noqa: E402
from ibus_voice_ime.asr import voice_hotkey  # noqa: E402
from ibus_voice_ime.text import llm_cloud_config  # noqa: E402

PLANS = {
    "local-qwen": ("qwen3-asr", "qwen3"),
    "siliconflow": ("siliconflow-asr", "siliconflow"),
    "mimo-cloud": ("mimo-cloud", "mimo_cloud"),
    "volc": ("volc-bigmodel-asr", "volc"),
}
HOTKEY_PATHS = {
    "voice": "recording.hotkeys",
    "raw": "recording.raw_hotkeys",
    "paste": "recording.clipboard_hotkeys",
}


def read_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点必须为对象；未修改配置")
    return data


def normalize_hotkey(raw: str) -> str:
    """兼容 GTK 写法，但只放行当前引擎能实际路由的字母组合。

    分词镜像引擎 parse_hotkey_combos 的真实语义（红队 2026-10-11 发现）：
    逗号/分号分隔；不含 ``+`` 的项再按空白拆成多个裸字母（``v b`` 是
    两个合法组合）；含 ``+`` 的项整体是一个组合。与引擎不同的是这里
    逐 token 严格——引擎会静默丢弃解析失败的 token，向导必须拒绝
    （``garbage v`` 不得在丢掉 ``garbage`` 后侥幸放行）。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("热键不能为空")
    labels = []
    for item in re.split(r"[,;]+", raw):
        item = item.strip()
        if not item:
            continue
        tokens = [item] if "+" in item else [p for p in re.split(r"\s+", item) if p]
        for token in tokens:
            if token.startswith("<"):
                match = re.fullmatch(r"((?:<[A-Za-z]+>)+)([A-Za-z])", token)
                if not match:
                    raise ValueError("GTK 热键格式不合法")
                token = "+".join(re.findall(r"<([^>]+)>", match[1]) + [match[2]])
            combos = voice_hotkey.parse_hotkey_combos(token)
            if len(combos) != 1:
                raise ValueError(
                    f"热键不合法：{token}；请用 Ctrl+Alt+V、<Control><Alt>v、"
                    "v b（裸字母=Ctrl+Alt+字母）等形式")
            labels.append(combos[0].label())
    if not labels:
        raise ValueError("热键不能为空")
    return ", ".join(dict.fromkeys(labels))


def read_llm() -> dict | None:
    """共享运行时路径与 enabled 默认值；损坏文件不得被稀疏写入覆盖。"""
    path = llm_cloud_config.config_path()
    if not path.exists():
        return None
    try:
        data = read_object(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"llm.json 无法读取或已损坏：{exc}；拒绝修改") from exc
    if type(data.get("enabled", True)) is not bool:
        raise ValueError("llm.json 的 enabled 字段非法，必须为布尔值；拒绝修改")
    return data


def backup_file(path: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(path.name + ".bak." + stamp)
    shutil.copy2(path, backup)
    backup.chmod(0o600)
    return backup


def atomic_write(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        # 此路径仅来自本次 mkstemp；不清理用户指定的文件。
        temporary.unlink(missing_ok=True)


def clear_env_hotkeys() -> int:
    path = Path.home() / ".config/environment.d/ibus-voice-ime.conf"
    if not path.exists():
        return 0
    pattern = re.compile(r"^(VOICE_IME_HOTKEYS|VOICE_IME_RAW_HOTKEYS|VOICE_IME_CLIPBOARD_HOTKEYS)=")
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if not pattern.match(line)]
        count = len(lines) - len(kept)
        if count:
            backup = backup_file(path)
            atomic_write(path, "".join(kept))
            print(f"已清理 environment.d 热键覆盖 {count} 行；原文件备份：{backup}", file=sys.stderr)
        return count
    except OSError as exc:
        print(f"警告：environment.d 热键覆盖清理失败：{exc}；不能确认热键已生效，停止应用", file=sys.stderr)
        raise


def wizard_defaults() -> dict:
    path = config.user_config_path()
    if path.exists():
        read_object(path)  # 编辑器不得把损坏文件默默视为空配置。
    backend = config.get("asr.backend", default="qwen3-asr")
    plan = next((key for key, value in PLANS.items() if value[0] == backend), "local-qwen")
    model = str(config.get("asr.qwen3.model", default="1.7b")).lower()
    if model not in {"0.6b", "1.7b"}:
        model = "1.7b"
    llm = read_llm()
    return {
        "voice_plan": plan,
        "qwen_sizes": [model[:-1] + "B"],
        "qwen_model": model,
        "hotkeys": {
            "voice": config.get("recording.hotkeys") or config.get("recording.hotkey") or "Ctrl+Alt+V",
            "raw": config.get("recording.raw_hotkeys") or "Ctrl+Alt+B",
            "paste": config.get("recording.clipboard_hotkeys") or config.get("recording.clipboard_hotkey") or "Ctrl+Alt+P",
        },
        "postprocess": llm.get("enabled", True) if llm is not None else None,
        "assets": dict.fromkeys(("rime_ice", "zhwiki", "moegirl", "denoise"), True),
    }


def make_updates(result: dict) -> dict:
    plan = result.get("voice_plan")
    if plan not in {*PLANS, "keyboard-only"}:
        raise ValueError("语音方案不合法")
    updates = {}
    if plan != "keyboard-only":
        backend, selected = PLANS[plan]
        updates["asr.backend"] = backend
        for name in ("qwen3", "mimo", "mimo_cloud", "volc", "siliconflow"):
            updates[f"asr.{name}.enabled"] = name == selected
    model = result.get("qwen_model")
    if model not in {"0.6b", "1.7b"}:
        raise ValueError("默认 Qwen 模型不合法")
    updates["asr.qwen3.model"] = model
    # 改模型时不能让旧的显式 model_path 继续压过新模型名；仅在真的变更时清空。
    if config.get("asr.qwen3.model", default="1.7b") != model:
        updates["asr.qwen3.model_path"] = ""
    hotkeys = result.get("hotkeys")
    if not isinstance(hotkeys, dict):
        raise ValueError("缺少热键配置")
    for name, path in HOTKEY_PATHS.items():
        updates[path] = normalize_hotkey(hotkeys.get(name))
    if "postprocess" not in result or (result["postprocess"] is not None and type(result["postprocess"]) is not bool):
        raise ValueError("postprocess 字段非法，后处理开关必须为布尔值或 null")
    return updates


def validate_updates(updates: dict) -> None:
    """直接复用配置模块校验新写入项；既有未知键保持不动、不阻断升级。"""
    original = os.environ.get("VOICE_IME_CONFIG")
    with tempfile.TemporaryDirectory(prefix="voice-ime-wizard-check-") as directory:
        os.environ["VOICE_IME_CONFIG"] = str(Path(directory) / "config.json")
        try:
            config.reload()
            for path, value in updates.items():
                config.set(path, value)
            config.save()
            with contextlib.redirect_stdout(sys.stderr):
                if config.main(["validate"]) != 0:
                    raise ValueError("向导配置未通过仓库配置校验")
        finally:
            if original is None:
                os.environ.pop("VOICE_IME_CONFIG", None)
            else:
                os.environ["VOICE_IME_CONFIG"] = original
            config.reload()


def apply(result: dict) -> dict:
    path = config.user_config_path()
    if path.exists():
        read_object(path)
    config.reload()
    updates = make_updates(result)
    previous = wizard_defaults()["hotkeys"]
    changed = False
    for name, key in HOTKEY_PATHS.items():
        try:
            old = normalize_hotkey(previous[name])
        except ValueError:
            old = None  # 允许向导修复既有非法键位。
        changed = changed or old != updates[key]
    llm = read_llm() if result["postprocess"] is not None else None
    if result["postprocess"] is not None and llm is None:
        raise ValueError("llm.json 不存在，不能修改 postprocess；请先运行 scripts/setup-llm-cloud.sh")
    llm_changed = llm is not None and llm.get("enabled", True) != result["postprocess"]
    validate_updates(updates)
    # 清理失败时停止，不能写入配置后静默宣称新热键已生效。
    env_hotkeys_cleared = clear_env_hotkeys() if changed else 0
    backup = backup_file(path) if path.exists() else None
    if llm_changed:
        llm_path = llm_cloud_config.config_path()
        llm_backup = backup_file(llm_path)
        llm["enabled"] = result["postprocess"]
        atomic_write(llm_path, json.dumps(llm, ensure_ascii=False, indent=2) + "\n")
        print(f"云端 LLM 开关已更新，即刻生效、无需重注册；原文件备份：{llm_backup}", file=sys.stderr)
    for key, value in updates.items():
        config.set(key, value)
    config.save()
    config.reload()
    print(f"配置已稀疏合并：{path}", file=sys.stderr)
    if backup:
        print(f"原配置备份：{backup}", file=sys.stderr)
    llm_summary = "未配置云端 LLM" if result["postprocess"] is None else ("开" if result["postprocess"] else "关")
    print(f"生效摘要：语音方案 {result['voice_plan']}；Qwen {result['qwen_model']}；云端 LLM 后处理 {llm_summary}", file=sys.stderr)
    labels = {"voice": "语音", "raw": "原文", "paste": "粘贴"}
    for name, key in HOTKEY_PATHS.items():
        print(f"{labels[name]}热键：{updates[key]}", file=sys.stderr)
    return {"config_path": str(path), "backup_path": str(backup) if backup else None,
            "hotkeys_changed": changed, "llm_changed": llm_changed,
            "env_hotkeys_cleared": env_hotkeys_cleared}


def main() -> int:
    parser = argparse.ArgumentParser(description="稀疏合并安装向导配置（不保存密钥）")
    parser.add_argument("result", nargs="?", help="向导结果 JSON 路径")
    parser.add_argument("--wizard-defaults", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-hotkey", metavar="热键", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.wizard_defaults:
            print(json.dumps(wizard_defaults(), ensure_ascii=False))
        elif args.validate_hotkey is not None:
            print(normalize_hotkey(args.validate_hotkey))
        elif args.result:
            print(json.dumps(apply(read_object(Path(args.result).expanduser())), ensure_ascii=False))
        else:
            parser.error("请指定向导结果 JSON 路径")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"错误：{exc}；配置应用未完成", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
