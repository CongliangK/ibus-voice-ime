# -*- coding: utf-8 -*-
"""librime keyboard backend for the IBus voice IME.

This lets the custom engine reuse Rime's mature pinyin segmentation,
dictionaries, user frequency learning, schema system, fuzzy spelling rules, etc.
"""
from __future__ import annotations

import ctypes
import os
import threading
from dataclasses import dataclass
from pathlib import Path

Bool = ctypes.c_int
RimeSessionId = ctypes.c_size_t

# Same mask values as librime's key_table.h / X11 / IBus for common modifiers.
# Do not pass Lock/NumLock/Mod2..Mod5/IBus internal bits to librime: on many
# desktops they are always present in IBus `state`, and Rime treats e.g.
# `Mod2+space` as a different shortcut instead of committing the candidate.
RIME_SHIFT_MASK = 1 << 0
RIME_CONTROL_MASK = 1 << 2
RIME_ALT_MASK = 1 << 3
RIME_SUPER_MASK = 1 << 26
RIME_RELEASE_MASK = 1 << 30
RIME_SAFE_MODIFIER_MASK = RIME_SHIFT_MASK | RIME_CONTROL_MASK | RIME_ALT_MASK | RIME_SUPER_MASK | RIME_RELEASE_MASK


class RimeError(RuntimeError):
    pass


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


class RimeComposition(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_int),
        ("cursor_pos", ctypes.c_int),
        ("sel_start", ctypes.c_int),
        ("sel_end", ctypes.c_int),
        ("preedit", ctypes.c_char_p),
    ]


class RimeCandidate(ctypes.Structure):
    _fields_ = [
        ("text", ctypes.c_char_p),
        ("comment", ctypes.c_char_p),
        ("reserved", ctypes.c_void_p),
    ]


class RimeMenu(ctypes.Structure):
    _fields_ = [
        ("page_size", ctypes.c_int),
        ("page_no", ctypes.c_int),
        ("is_last_page", Bool),
        ("highlighted_candidate_index", ctypes.c_int),
        ("num_candidates", ctypes.c_int),
        ("candidates", ctypes.POINTER(RimeCandidate)),
        ("select_keys", ctypes.c_char_p),
    ]


class RimeContextStruct(ctypes.Structure):
    _fields_ = [
        ("data_size", ctypes.c_int),
        ("composition", RimeComposition),
        ("menu", RimeMenu),
        ("commit_text_preview", ctypes.c_char_p),
        ("select_labels", ctypes.POINTER(ctypes.c_char_p)),
    ]


class RimeCommit(ctypes.Structure):
    _fields_ = [("data_size", ctypes.c_int), ("text", ctypes.c_char_p)]


class RimeApi(ctypes.Structure):
    pass


RimeApi._fields_ = [
    ("data_size", ctypes.c_int),
    ("setup", ctypes.CFUNCTYPE(None, ctypes.POINTER(RimeTraits))),
    ("set_notification_handler", ctypes.c_void_p),
    ("initialize", ctypes.CFUNCTYPE(None, ctypes.POINTER(RimeTraits))),
    ("finalize", ctypes.CFUNCTYPE(None)),
    ("start_maintenance", ctypes.c_void_p),
    ("is_maintenance_mode", ctypes.c_void_p),
    ("join_maintenance_thread", ctypes.c_void_p),
    ("deployer_initialize", ctypes.c_void_p),
    ("prebuild", ctypes.c_void_p),
    ("deploy", ctypes.c_void_p),
    ("deploy_schema", ctypes.c_void_p),
    ("deploy_config_file", ctypes.c_void_p),
    ("sync_user_data", ctypes.c_void_p),
    ("create_session", ctypes.CFUNCTYPE(RimeSessionId)),
    ("find_session", ctypes.c_void_p),
    ("destroy_session", ctypes.CFUNCTYPE(Bool, RimeSessionId)),
    ("cleanup_stale_sessions", ctypes.c_void_p),
    ("cleanup_all_sessions", ctypes.c_void_p),
    ("process_key", ctypes.CFUNCTYPE(Bool, RimeSessionId, ctypes.c_int, ctypes.c_int)),
    ("commit_composition", ctypes.CFUNCTYPE(Bool, RimeSessionId)),
    ("clear_composition", ctypes.CFUNCTYPE(None, RimeSessionId)),
    ("get_commit", ctypes.CFUNCTYPE(Bool, RimeSessionId, ctypes.POINTER(RimeCommit))),
    ("free_commit", ctypes.CFUNCTYPE(Bool, ctypes.POINTER(RimeCommit))),
    ("get_context", ctypes.CFUNCTYPE(Bool, RimeSessionId, ctypes.POINTER(RimeContextStruct))),
    ("free_context", ctypes.CFUNCTYPE(Bool, ctypes.POINTER(RimeContextStruct))),
    ("get_status", ctypes.c_void_p),
    ("free_status", ctypes.c_void_p),
    ("set_option", ctypes.c_void_p),
    ("get_option", ctypes.c_void_p),
    ("set_property", ctypes.c_void_p),
    ("get_property", ctypes.c_void_p),
    ("get_schema_list", ctypes.c_void_p),
    ("free_schema_list", ctypes.c_void_p),
    ("get_current_schema", ctypes.c_void_p),
    ("select_schema", ctypes.CFUNCTYPE(Bool, RimeSessionId, ctypes.c_char_p)),
]


@dataclass(frozen=True)
class Candidate:
    text: str
    comment: str = ""


@dataclass(frozen=True)
class Context:
    preedit: str = ""
    cursor_pos: int = 0
    candidates: tuple[Candidate, ...] = ()
    highlighted_index: int = 0
    page_size: int = 0
    page_no: int = 0
    is_last_page: bool = True

    @property
    def composing(self) -> bool:
        return bool(self.preedit or self.candidates)


class RimeService:
    _lock = threading.Lock()
    _api: RimeApi | None = None
    _lib: ctypes.CDLL | None = None
    _initialized = False

    @classmethod
    def api(cls) -> RimeApi:
        with cls._lock:
            if cls._api is None:
                cls._initialize()
            assert cls._api is not None
            return cls._api

    @classmethod
    def _initialize(cls) -> None:
        libname = os.environ.get("VOICE_IME_RIME_LIBRARY", _vendored_library())
        _preload_vendored_dependencies()
        try:
            cls._lib = ctypes.CDLL(libname, mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            raise RimeError(f"无法加载 librime：{exc}") from exc

        cls._lib.rime_get_api.restype = ctypes.POINTER(RimeApi)
        api_ptr = cls._lib.rime_get_api()
        if not api_ptr:
            raise RimeError("librime 没有返回有效 API")
        cls._api = api_ptr.contents

        traits = RimeTraits()
        traits.data_size = ctypes.sizeof(RimeTraits) - ctypes.sizeof(ctypes.c_int)
        traits.shared_data_dir = _bytes(_shared_data_dir())
        traits.user_data_dir = _bytes(_user_data_dir())
        traits.distribution_name = b"ibus-voice-ime"
        traits.distribution_code_name = b"ibus-voice-ime"
        traits.distribution_version = b"0.1.0"
        traits.app_name = b"rime.ibus-voice-ime"
        traits.min_log_level = int(os.environ.get("VOICE_IME_RIME_LOG_LEVEL", "2"))
        traits.log_dir = _bytes(os.environ.get("VOICE_IME_RIME_LOG_DIR", ""))
        staging = _staging_dir()
        if staging:
            traits.staging_dir = _bytes(staging)

        cls._api.setup(ctypes.byref(traits))
        cls._api.initialize(ctypes.byref(traits))
        cls._initialized = True


def _bytes(value: str | Path) -> bytes:
    return os.fspath(value).encode("utf-8")


# THIS_DIR = .../src/ibus_voice_ime/rime; repo root is 3 levels up.
THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = THIS_DIR.parents[2]
VENDORED_RIME_DIR = _REPO_ROOT / "vendor" / "rime"


def _preload_vendored_dependencies() -> None:
    lib_dir = VENDORED_RIME_DIR / "lib"
    if not lib_dir.exists():
        return
    preferred_order = (
        "libgcc_s", "libstdc++", "libgflags", "libsnappy", "libglog",
        "libyaml-cpp", "libmarisa", "libopencc", "libleveldb",
    )
    libs = [p for p in lib_dir.iterdir() if p.name.endswith(tuple((".so", ".so.0", ".so.1", ".so.1.1", ".so.2.2", ".so.0.8", ".so.6"))) and not p.name.startswith("librime")]
    ordered: list[Path] = []
    for prefix in preferred_order:
        ordered.extend(sorted(p for p in libs if p.name.startswith(prefix) and p not in ordered))
    ordered.extend(sorted(p for p in libs if p not in ordered))
    for path in ordered:
        try:
            ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
        except OSError:
            # The launcher sets LD_LIBRARY_PATH, so preloading is best-effort.
            pass


def _vendored_library() -> str:
    for name in ("librime.so.1", "librime.so"):
        path = VENDORED_RIME_DIR / "lib" / name
        if path.exists():
            return str(path)
    if os.environ.get("VOICE_IME_RIME_ALLOW_SYSTEM", "0") == "1":
        return "librime.so.1"
    return str(VENDORED_RIME_DIR / "lib" / "librime.so.1")


def _shared_data_dir() -> str:
    return os.environ.get("VOICE_IME_RIME_SHARED_DATA_DIR", str(VENDORED_RIME_DIR / "share" / "rime-data"))


def _user_data_dir() -> str:
    path = Path(os.environ.get("VOICE_IME_RIME_USER_DATA_DIR", "~/.local/share/ibus-voice-ime/rime-user")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _staging_dir() -> str | None:
    explicit = os.environ.get("VOICE_IME_RIME_STAGING_DIR", "").strip()
    if explicit:
        return explicit
    vendored = VENDORED_RIME_DIR / "build"
    if vendored.exists():
        return str(vendored)
    if os.environ.get("VOICE_IME_RIME_ALLOW_SYSTEM", "0") == "1":
        for candidate in (
            "~/.config/ibus/rime/build",
            "~/.local/share/fcitx5/rime/build",
            "/usr/share/rime-data/build",
        ):
            path = Path(candidate).expanduser()
            if path.exists():
                return str(path)
    return None


def _decode(value: bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if value else ""


class RimeSession:
    def __init__(self, schema: str | None = None):
        self.api = RimeService.api()
        self.session_id = self.api.create_session()
        if not self.session_id:
            raise RimeError("创建 Rime 会话失败")
        schema = schema or os.environ.get("VOICE_IME_RIME_SCHEMA", "rime_ice")
        # rime-ice (default) needs scripts/setup-rime-ice.sh first. If its
        # compiled dict is absent (shared schema missing OR no rime_ice.table.bin
        # in the staging/user build dir), fall back to the always-present
        # luna_pinyin_simp so a fresh checkout still types instead of crashing.
        if schema == "rime_ice":
            shared_schema = Path(_shared_data_dir(), "rime_ice.schema.yaml")
            built = Path(_staging_dir() or _user_data_dir(), "rime_ice.table.bin")
            user_built = Path(_user_data_dir(), "build", "rime_ice.table.bin")
            if not shared_schema.is_file() or not (built.is_file() or user_built.is_file()):
                schema = "luna_pinyin_simp"
        self.api.select_schema(self.session_id, schema.encode("utf-8"))

    def close(self) -> None:
        if self.session_id:
            self.api.destroy_session(self.session_id)
            self.session_id = 0

    def process_key(self, keyval: int, state: int) -> bool:
        mask = int(state) & RIME_SAFE_MODIFIER_MASK
        return bool(self.api.process_key(self.session_id, int(keyval), mask))

    def commit_composition(self) -> str:
        self.api.commit_composition(self.session_id)
        return self.get_commit()

    def clear(self) -> None:
        self.api.clear_composition(self.session_id)

    def get_commit(self) -> str:
        commit = RimeCommit()
        commit.data_size = ctypes.sizeof(RimeCommit) - ctypes.sizeof(ctypes.c_int)
        try:
            if self.api.get_commit(self.session_id, ctypes.byref(commit)) and commit.text:
                return _decode(commit.text)
            return ""
        finally:
            self.api.free_commit(ctypes.byref(commit))

    def query_candidates(self, code: str, limit: int = 3) -> list[Candidate]:
        session = RimeSession()
        try:
            for ch in code:
                if ch.isascii():
                    session.process_key(ord(ch), 0)
            return list(session.context().candidates[:limit])
        finally:
            session.close()

    def context(self) -> Context:
        ctx = RimeContextStruct()
        ctx.data_size = ctypes.sizeof(RimeContextStruct) - ctypes.sizeof(ctypes.c_int)
        if not self.api.get_context(self.session_id, ctypes.byref(ctx)):
            return Context()
        try:
            candidates: list[Candidate] = []
            for i in range(max(0, ctx.menu.num_candidates)):
                cand = ctx.menu.candidates[i]
                candidates.append(Candidate(_decode(cand.text), _decode(cand.comment)))
            return Context(
                preedit=_decode(ctx.composition.preedit),
                cursor_pos=max(0, ctx.composition.cursor_pos),
                candidates=tuple(candidates),
                highlighted_index=max(0, ctx.menu.highlighted_candidate_index),
                page_size=max(0, ctx.menu.page_size),
                page_no=max(0, ctx.menu.page_no),
                is_last_page=bool(ctx.menu.is_last_page),
            )
        finally:
            self.api.free_context(ctypes.byref(ctx))
