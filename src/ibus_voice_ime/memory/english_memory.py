# -*- coding: utf-8 -*-
"""Small persistent English-word memory for the custom IME.

Use case: type an English token such as ``opencode`` and press Enter.  The
engine commits the raw token and stores it here; next time the token/prefix is
shown as an English candidate and can be committed with Space/number keys.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EnglishCandidate:
    text: str
    comment: str = "英文"
    freq: int = 0
    last: float = 0.0
    exact: bool = False


TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.#-]{1,63}$")

# Standard Mandarin pinyin syllables, enough for deciding whether an ASCII token
# is probably pinyin.  English tokens that are valid pinyin are still learned,
# but are ranked after Rime's Chinese candidates so they do not steal Space.
PINYIN_SYLLABLES = {
    "a", "ai", "an", "ang", "ao",
    "ba", "bai", "ban", "bang", "bao", "bei", "ben", "beng", "bi", "bian", "biao", "bie", "bin", "bing", "bo", "bu",
    "ca", "cai", "can", "cang", "cao", "ce", "cen", "ceng", "cha", "chai", "chan", "chang", "chao", "che", "chen", "cheng", "chi", "chong", "chou", "chu", "chua", "chuai", "chuan", "chuang", "chui", "chun", "chuo", "ci", "cong", "cou", "cu", "cuan", "cui", "cun", "cuo",
    "da", "dai", "dan", "dang", "dao", "de", "dei", "den", "deng", "di", "dia", "dian", "diao", "die", "ding", "diu", "dong", "dou", "du", "duan", "dui", "dun", "duo",
    "e", "ei", "en", "eng", "er",
    "fa", "fan", "fang", "fei", "fen", "feng", "fo", "fou", "fu",
    "ga", "gai", "gan", "gang", "gao", "ge", "gei", "gen", "geng", "gong", "gou", "gu", "gua", "guai", "guan", "guang", "gui", "gun", "guo",
    "ha", "hai", "han", "hang", "hao", "he", "hei", "hen", "heng", "hong", "hou", "hu", "hua", "huai", "huan", "huang", "hui", "hun", "huo",
    "ji", "jia", "jian", "jiang", "jiao", "jie", "jin", "jing", "jiong", "jiu", "ju", "juan", "jue", "jun",
    "ka", "kai", "kan", "kang", "kao", "ke", "ken", "keng", "kong", "kou", "ku", "kua", "kuai", "kuan", "kuang", "kui", "kun", "kuo",
    "la", "lai", "lan", "lang", "lao", "le", "lei", "leng", "li", "lia", "lian", "liang", "liao", "lie", "lin", "ling", "liu", "lo", "long", "lou", "lu", "lv", "lue", "luan", "lun", "luo",
    "ma", "mai", "man", "mang", "mao", "me", "mei", "men", "meng", "mi", "mian", "miao", "mie", "min", "ming", "miu", "mo", "mou", "mu",
    "na", "nai", "nan", "nang", "nao", "ne", "nei", "nen", "neng", "ni", "nian", "niang", "niao", "nie", "nin", "ning", "niu", "nong", "nou", "nu", "nv", "nue", "nuan", "nuo",
    "o", "ou",
    "pa", "pai", "pan", "pang", "pao", "pei", "pen", "peng", "pi", "pian", "piao", "pie", "pin", "ping", "po", "pou", "pu",
    "qi", "qia", "qian", "qiang", "qiao", "qie", "qin", "qing", "qiong", "qiu", "qu", "quan", "que", "qun",
    "ran", "rang", "rao", "re", "ren", "reng", "ri", "rong", "rou", "ru", "rua", "ruan", "rui", "run", "ruo",
    "sa", "sai", "san", "sang", "sao", "se", "sen", "seng", "sha", "shai", "shan", "shang", "shao", "she", "shei", "shen", "sheng", "shi", "shou", "shu", "shua", "shuai", "shuan", "shuang", "shui", "shun", "shuo", "si", "song", "sou", "su", "suan", "sui", "sun", "suo",
    "ta", "tai", "tan", "tang", "tao", "te", "teng", "ti", "tian", "tiao", "tie", "ting", "tong", "tou", "tu", "tuan", "tui", "tun", "tuo",
    "wa", "wai", "wan", "wang", "wei", "wen", "weng", "wo", "wu",
    "xi", "xia", "xian", "xiang", "xiao", "xie", "xin", "xing", "xiong", "xiu", "xu", "xuan", "xue", "xun",
    "ya", "yan", "yang", "yao", "ye", "yi", "yin", "ying", "yo", "yong", "you", "yu", "yuan", "yue", "yun",
    "za", "zai", "zan", "zang", "zao", "ze", "zei", "zen", "zeng", "zha", "zhai", "zhan", "zhang", "zhao", "zhe", "zhei", "zhen", "zheng", "zhi", "zhong", "zhou", "zhu", "zhua", "zhuai", "zhuan", "zhuang", "zhui", "zhun", "zhuo", "zi", "zong", "zou", "zu", "zuan", "zui", "zun", "zuo",
}


def is_english_token(token: str) -> bool:
    return bool(TOKEN_RE.fullmatch(token.strip()))


# Pinyin initials (声母).  Used by is_plausible_pinyin_code to recognise
# abbreviated/unfinished tail syllables like the trailing `w` in `bangw`.
PINYIN_INITIALS = {
    "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "r", "z", "c", "s", "y", "w", "zh", "ch", "sh",
}

# All non-empty prefixes of every pinyin syllable, so a still-in-progress tail
# like `x` (from `yunx`) can be recognised as a valid syllable prefix.
_PINYIN_SYLLABLE_PREFIXES = {
    syllable[:i]
    for syllable in PINYIN_SYLLABLES
    for i in range(1, len(syllable))
}


def is_probably_pinyin(token: str) -> bool:
    s = token.lower().replace("'", "")
    if not s.isalpha():
        return False
    memo: dict[int, bool] = {len(s): True}

    def dp(i: int) -> bool:
        if i in memo:
            return memo[i]
        for j in range(i + 1, min(len(s), i + 6) + 1):
            if s[i:j] in PINYIN_SYLLABLES and dp(j):
                memo[i] = True
                return True
        memo[i] = False
        return False

    return dp(0)


def _full_syllable_ends(s: str) -> set[int]:
    """Indices where a maximal run of complete pinyin syllables ends in s."""
    reachable = {0}
    for i in range(len(s) + 1):
        if i not in reachable:
            continue
        for j in range(i + 1, min(len(s), i + 6) + 1):
            if s[i:j] in PINYIN_SYLLABLES:
                reachable.add(j)
    return reachable


def _is_initial_sequence(s: str) -> bool:
    """Whether s is composed entirely of pinyin initials (abbreviated input)."""
    if not s:
        return False
    memo: dict[int, bool] = {len(s): True}

    def dp(i: int) -> bool:
        if i in memo:
            return memo[i]
        for initial in PINYIN_INITIALS:
            if s.startswith(initial, i) and dp(i + len(initial)):
                memo[i] = True
                return True
        memo[i] = False
        return False

    return dp(0)


def is_plausible_pinyin_code(raw: str) -> bool:
    """Whether raw input can be a normal Rime pinyin/prefix/abbrev code.

    Mainstream pinyin IMEs do not require every segment to be a complete
    syllable while the user is still typing: ``yunx`` means ``yun`` + an
    unfinished/abbreviated ``x...`` syllable and Rime can rank ``运行`` from its
    language model.  This is used by engine.py to decide whether learned
    memory candidates may overtake Rime's candidates.
    """
    s = raw.lower().strip().replace("'", "")
    if not s or not s.isalpha():
        return False
    ends = _full_syllable_ends(s)
    if len(s) in ends:
        return True
    for end in ends:
        if end == len(s):
            continue
        tail = s[end:]
        if tail in _PINYIN_SYLLABLE_PREFIXES or _is_initial_sequence(tail):
            return True
    return False


class EnglishMemory:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = os.environ.get("VOICE_IME_ENGLISH_USER_DICT", "~/.local/share/ibus-voice-ime/english.json")
        self.path = Path(path).expanduser()
        self.words: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.words = {str(k).lower(): v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self.words = {}
        except Exception:
            self.words = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.words, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def learn(self, token: str) -> bool:
        token = token.strip()
        if not is_english_token(token):
            return False
        key = token.lower()
        item = self.words.get(key, {})
        item["text"] = token
        item["freq"] = int(item.get("freq", 0)) + 1
        item["last"] = time.time()
        item["pinyin_like"] = is_probably_pinyin(token)
        self.words[key] = item
        self._save()
        return True

    def suggest(self, query: str, limit: int = 3) -> list[EnglishCandidate]:
        query = query.strip()
        if not query or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+.#-]{0,63}", query):
            return []
        q = query.lower()
        matches = []
        for key, item in self.words.items():
            if not key.startswith(q):
                continue
            text = str(item.get("text") or key)
            freq = int(item.get("freq", 0))
            last = float(item.get("last", 0.0))
            exact = key == q
            matches.append((0 if exact else 1, -freq, -last, text, freq))
        matches.sort()
        result = []
        for exact_rank, _freq_rank, last_rank, text, freq in matches[:limit]:
            comment = "英文" if freq <= 1 else f"英文×{freq}"
            result.append(EnglishCandidate(text, comment, freq, -last_rank, exact_rank == 0))
        return result

    def prioritize_before_chinese(self, query: str) -> bool:
        return bool(query and not is_probably_pinyin(query))
