# -*- coding: utf-8 -*-
"""Deterministic text cleanup for voice input.

This module runs before the optional LLM pass.  Keep it conservative: it should
only normalize obvious whitespace and voice-control words, not rewrite meaning.
"""
from __future__ import annotations

import os
import re

_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_SPACE_AROUND_CJK_PUNCT_RE = re.compile(r"[ \t\r\f\v]*([，。！？；：、])[ \t\r\f\v]*")
_CJK_SPACE_CJK_RE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")
# Spaces at CJK<->Latin boundaries.  A space is removed only when one neighbor
# is a CJK ideograph and the other is an ASCII letter/digit; this keeps
# "hello world" intact while collapsing "配合 opencode" / "opencode 编辑".
# The ASCII side is intentionally limited to [A-Za-z0-9] (not all punctuation)
# so spaces before/after punctuation are handled by _SPACE_AROUND_CJK_PUNCT_RE.
_CJK_SPACE_LATIN_RE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[A-Za-z0-9])")
_LATIN_SPACE_CJK_RE = re.compile(r"(?<=[A-Za-z0-9])[ \t]+(?=[\u4e00-\u9fff])")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_SENTENCE_END_RE = re.compile(r"[。！？.!?]$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_PUNCT_RE = re.compile(r"[，。！？；：、]")
_DUP_PUNCT_RUN_RE = re.compile(r"[，,、。.!?！？；;：:]{2,}")
_AUTO_COMMA_PHRASES: tuple[str, ...] = (
    "但是", "不过", "所以", "因此", "然后", "另外", "而且", "并且", "同时",
    "至少", "其实", "实际上", "事实上", "那么", "那", "好", "OK", "ok",
    "我希望", "我想", "我觉得", "我认为", "我需要", "能不能",
    "如果", "假设", "因为", "比如说", "比如", "例如", "或者", "以及", "就是",
    "也就是说", "换句话说", "接下来", "首先", "其次", "最后", "而不是",
)
_SOFT_SPLIT_AFTER_CHARS = "了呢吧吗么呀嘛啊哦"

# Phrase replacements for dictation commands.  Longer phrases first.
_COMMAND_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("新的一行", "\n"),
    ("另起一行", "\n"),
    ("换一行", "\n"),
    ("换行", "\n"),
    ("空一行", "\n\n"),
    ("逗号", "，"),
    ("句号", "。"),
    ("问号", "？"),
    ("感叹号", "！"),
    ("叹号", "！"),
    ("分号", "；"),
    ("冒号", "："),
    ("顿号", "、"),
    ("左括号", "（"),
    ("右括号", "）"),
    ("左引号", "“"),
    ("右引号", "”"),
    ("斜杠", "/"),
    ("反斜杠", "\\"),
    ("空格", " "),
)

# Fillers are removed before the LLM pass.  ASR often emits them inline in
# Chinese without spaces/punctuation (e.g. “这个金融都市啊”, “呃用到”).  Keep the
# patterns focused on discourse particles and avoid touching ASCII/code tokens.
_FILLER_RE = re.compile(r"(^|[\s，。！？；：、,.!?;:])(?:嗯+|呃+|啊+|额+|哦+|唔+|那个|这个|就是)(?=$|[\s，。！？；：、,.!?;:])")
_INLINE_FILLER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:嗯+|呃+|额+|啊+|哦+|唔+)(?![A-Za-z0-9_])")
_INLINE_PARTICLE_RE = re.compile(r"(?<=[\u4e00-\u9fff])(?:呢+|吧+|哈+|嘛+|呀+)(?=[\u4e00-\u9fff，。！？；：、,.!?;:]|$)")
_ASCII_FILLER_RE = re.compile(r"(^|[\s，。！？；：、,.!?;:])(?:a|r|er|uh|um)(?=$|[\s，。！？；：、,.!?;:])")
_SHORT_FRAGMENT_RE = re.compile(r"([，。！？；：、,.!?;:])(?:嗯+|呃+|额+|啊+|哦+|唔+|哈+|呢+|吧+|嘛+|呀+|接|就|那|这)(?=[，。！？；：、,.!?;:])")
_DISCOURSE_FILLER_RE = re.compile(r"(?:对吧|怎么说呢|然后呢|首先呢|其次呢|最后呢|除此以外呢|那么呢)")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _exact_voice_command(text: str) -> str | None:
    if not _env_bool("VOICE_IME_VOICE_COMMANDS", True):
        return None
    stripped = text.strip()
    for src, dst in _COMMAND_REPLACEMENTS:
        if stripped == src:
            return dst
    return None


def _apply_voice_commands(text: str) -> str:
    if not _env_bool("VOICE_IME_VOICE_COMMANDS", True):
        return text

    # Be conservative by default.  A plain global replace turns ordinary text
    # like “解释一下换行的作用” into an actual newline, which feels like an
    # unauthorized action.  Exact commands and commands separated by whitespace
    # or punctuation still work; users who want the old inline behavior can set
    # VOICE_IME_INLINE_VOICE_COMMANDS=1.
    inline = _env_bool("VOICE_IME_INLINE_VOICE_COMMANDS", False)
    if inline:
        for src, dst in _COMMAND_REPLACEMENTS:
            text = text.replace(src, dst)
        return text

    stripped = text.strip()
    for src, dst in _COMMAND_REPLACEMENTS:
        if stripped == src:
            return dst
        pattern = re.compile(rf"(^|[\s，。！？；：、,.!?;:]){re.escape(src)}(?=$|[\s，。！？；：、,.!?;:])")
        text = pattern.sub(lambda m, repl=dst: m.group(1) + repl, text)
    return text


def _remove_fillers(text: str) -> str:
    if not _env_bool("VOICE_IME_REMOVE_FILLERS", True):
        return text
    previous = None
    while previous != text:
        previous = text
        text = _FILLER_RE.sub(lambda m: m.group(1), text)
        text = _INLINE_FILLER_RE.sub("", text)
        text = _INLINE_PARTICLE_RE.sub("", text)
        text = _ASCII_FILLER_RE.sub(lambda m: m.group(1), text)
        text = _SHORT_FRAGMENT_RE.sub(lambda m: m.group(1), text)
        text = _DISCOURSE_FILLER_RE.sub("", text)
    return text


def cleanup_punctuation(text: str) -> str:
    """Collapse punctuation artifacts created by ASR/filler/LLM stages.

    A common STT pattern is "，呃，".  After filler removal this becomes
    "，，" unless we normalize it.  Qwen3-ASR and the LLM can also already add
    punctuation, so this final guard prevents doubled/tripled separators.
    """
    if not text:
        return text

    text = _SPACE_AROUND_CJK_PUNCT_RE.sub(lambda m: m.group(1), text)
    # Some small local LLMs emit Chinese as "我 希望 你".  That looks broken
    # when committed by an input method, while spaces around Latin/code tokens
    # (e.g. "GPT 5.5", "Go 语言") should be preserved.
    text = _CJK_SPACE_CJK_RE.sub("", text)
    # Remove spaces at Chinese<->Latin boundaries (e.g. "配合 opencode" ->
    # "配合opencode", "opencode 编辑" -> "opencode编辑").  ASR often inserts
    # these in mixed CJK/Latin output, but for Chinese dictation they are
    # unwanted.  Spaces between two Latin tokens ("hello world") are preserved,
    # and spaces around CJK punctuation were already handled above.
    if _env_bool("VOICE_IME_CJK_LATIN_SPACE", True):
        # Delete a space when one side is CJK and the other is ASCII letter/digit.
        text = _CJK_SPACE_LATIN_RE.sub("", text)
        text = _LATIN_SPACE_CJK_RE.sub("", text)

    def repl(match: re.Match[str]) -> str:
        s = match.group(0)
        # Prefer the strongest sentence-final punctuation if present.
        if "？" in s or "?" in s:
            return "？"
        if "！" in s or "!" in s:
            return "！"
        if "。" in s or "." in s:
            return "。"
        if "；" in s or ";" in s:
            return "；"
        if "：" in s or ":" in s:
            return "："
        return "，"

    previous = None
    while previous != text:
        previous = text
        text = _DUP_PUNCT_RUN_RE.sub(repl, text)
        text = re.sub(r"[，,、]+([。！？；：])", r"\1", text)
        text = re.sub(r"([。！？；：])[，,、]+", r"\1", text)
        text = re.sub(r"(^|\n)[，,、]+", r"\1", text)
    return text


def _looks_like_code_or_command(text: str) -> bool:
    ascii_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_./:+\-]*", text)
    cjk_count = len(_CJK_RE.findall(text))
    if cjk_count < 6 and ascii_tokens:
        return True
    if re.search(r"(?:^|\s)(?:git|python|pip|npm|curl|sudo|cd|ls|export)\s", text):
        return True
    if re.search(r"(?:^|\s)(?:VOICE_IME_[A-Z0-9_]+|/[\w./-]+|~[/\w.-]+)", text):
        return True
    return False


def _insert_comma_before_phrases(text: str) -> str:
    for phrase in _AUTO_COMMA_PHRASES:
        start = 0
        while True:
            idx = text.find(phrase, start)
            if idx <= 0:
                if idx < 0:
                    break
                start = idx + len(phrase)
                continue
            prev = text[idx - 1]
            # Do not insert at the beginning of a sentence/line or after an
            # existing separator.
            if prev not in "，。！？；：、\n,.!?;: ":
                text = text[:idx] + "，" + text[idx:]
                start = idx + len(phrase) + 1
            else:
                start = idx + len(phrase)
    return text


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text))


def _index_after_nth_cjk(text: str, n: int) -> int:
    seen = 0
    for idx, ch in enumerate(text):
        if _CJK_RE.match(ch):
            seen += 1
            if seen >= n:
                return idx + 1
    return len(text)


def _find_soft_split(text: str, target: int) -> int:
    """Find a nearby safe-ish comma position for a long Chinese run."""
    if len(text) <= 2:
        return len(text)
    start = max(1, target - 8)
    end = min(len(text) - 1, target + 8)

    # Prefer splitting after natural particles/function words near the target.
    for i in range(min(end, len(text) - 1), start - 1, -1):
        if text[i - 1] in _SOFT_SPLIT_AFTER_CHARS and not re.match(r"[A-Za-z0-9_]", text[i]):
            return i

    # Then split before common discourse markers if one is near the target.
    for phrase in _AUTO_COMMA_PHRASES:
        idx = text.find(phrase, start, end + 1)
        if idx > 1:
            return idx

    # If there is no nearby natural boundary, do not force a comma through the
    # middle of a word such as “后处理”.  Phrase-based commas and sentence-final
    # punctuation will still be applied.
    return 0


def _split_long_clause(clause: str, max_cjk: int) -> str:
    if _cjk_count(clause) <= max_cjk:
        return clause
    pieces: list[str] = []
    rest = clause
    while _cjk_count(rest) > max_cjk:
        target = _index_after_nth_cjk(rest, max_cjk)
        split = _find_soft_split(rest, target)
        if split <= 0:
            break
        head = rest[:split].strip()
        tail = rest[split:].strip()
        if not head or not tail:
            break
        pieces.append(head)
        rest = tail
    pieces.append(rest.strip())
    return "，".join(p for p in pieces if p)


def _split_long_unpunctuated_runs(text: str) -> str:
    max_cjk = max(8, _env_int("VOICE_IME_AUTO_PUNCT_MAX_CJK_PER_CLAUSE", 24))
    parts = re.split(r"([，。！？；：、\n])", text)
    out: list[str] = []
    for part in parts:
        if not part or re.fullmatch(r"[，。！？；：、\n]", part):
            out.append(part)
            continue
        if _looks_like_code_or_command(part):
            out.append(part)
        else:
            out.append(_split_long_clause(part, max_cjk))
    return "".join(out)


def auto_punctuate(text: str, *, mode: str | None = None) -> str:
    """Compatibility no-op for the removed local punctuation-insertion rule."""
    return text


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def apply_replacement_table(text: str) -> str:
    """Apply deterministic whole-word replacements from the voice dictionary.

    Uses the third column (confusions / common mis-recognitions) of
    voice-dictionary.txt: if ASR produced a confusion form as a standalone
    token, replace it with the canonical form.  Only whole-word/phrase matches
    bounded by whitespace or punctuation are replaced, so a confusion substring
    inside a larger word is left untouched.  This complements the Volcano
    ``corpus.context`` hotword biasing (which is soft): the hotword handles
    fully-pronounced rare words, while this layer fixes near-homophones the
    hotword cannot pull back.

    Disabled when VOICE_IME_VOICE_REPLACEMENTS=0.
    """
    if not _env_bool("VOICE_IME_VOICE_REPLACEMENTS", True):
        return text
    try:
        from ibus_voice_ime.memory import voice_terms  # local import; avoids a hard import-time cycle
    except Exception:
        return text
    # CJK characters are valid token boundaries on both sides: in mixed ASR
    # output an English token like "Rim" is a standalone unit whether the
    # neighbor is a CJK char, space, or punctuation.  But a following/leading
    # ASCII letter/digit is NOT a boundary (so "Rims" / "XRim" are untouched).
    boundary = r"^|\s|[，。！？；：、,.!?;:\n\u4e00-\u9fff]"
    trailing = r"$|\s|[，。！？；：、,.!?;:\n\u4e00-\u9fff]"
    for term in voice_terms.load_terms():
        if not term.confusions:
            continue
        dst = term.canonical
        for src in term.confusions:
            src = src.strip()
            if not src or src == dst:
                continue
            pattern = re.compile(rf"({boundary}){re.escape(src)}(?={trailing})")
            text = pattern.sub(lambda m, repl=dst: m.group(1) + repl, text)
    return text


def normalize(text: str, *, mode: str | None = None) -> str:
    """Return a lightly normalized transcript.

    Modes:
      - literal: only whitespace cleanup; no command/filler processing.
      - dictation/markdown/prompt/command: apply voice commands and fillers.
    """
    mode = (mode or os.environ.get("VOICE_IME_VOICE_MODE", "dictation")).strip().lower()
    text = (text or "").strip()
    if not text:
        return ""
    if mode != "literal":
        exact_command = _exact_voice_command(text)
        if exact_command is not None:
            return exact_command

    text = text.replace("\u3000", " ")
    text = _SPACE_RE.sub(" ", text)

    if mode != "literal":
        text = _apply_voice_commands(text)
        text = _remove_fillers(text)
        text = apply_replacement_table(text)
        text = cleanup_punctuation(text)

    # Clean spaces around Chinese punctuation but keep spaces around ASCII/code.
    text = cleanup_punctuation(text)
    text = re.sub(r" *\n *", "\n", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = text.strip()
    return cleanup_punctuation(text).strip()


__all__ = ["auto_punctuate", "cleanup_punctuation", "normalize"]
