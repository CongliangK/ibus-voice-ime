# -*- coding: utf-8 -*-
"""Tiny customizable input-method core used by the IBus prototype.

You can replace this file with your real algorithm later.  The engine calls
`suggest(code)` and commits one of the returned strings.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    text: str
    comment: str = ""


# Minimal demo dictionary.  Add/replace entries freely.
PINYIN_DICT: dict[str, list[Candidate]] = {
    "a": [Candidate("啊"), Candidate("阿"), Candidate("吖")],
    "ai": [Candidate("爱"), Candidate("哎"), Candidate("唉")],
    "an": [Candidate("安"), Candidate("按"), Candidate("案")],
    "ba": [Candidate("吧"), Candidate("把"), Candidate("爸")],
    "bei": [Candidate("被"), Candidate("北"), Candidate("倍")],
    "bu": [Candidate("不"), Candidate("部"), Candidate("步")],
    "de": [Candidate("的"), Candidate("得"), Candidate("地")],
    "fa": [Candidate("发"), Candidate("法")],
    "hao": [Candidate("好"), Candidate("号"), Candidate("浩")],
    "he": [Candidate("和"), Candidate("喝"), Candidate("何")],
    "hua": [Candidate("话"), Candidate("花"), Candidate("华")],
    "jie": [Candidate("界"), Candidate("接"), Candidate("节")],
    "ma": [Candidate("吗"), Candidate("妈"), Candidate("马")],
    "mei": [Candidate("没"), Candidate("美"), Candidate("每")],
    "ni": [Candidate("你"), Candidate("尼"), Candidate("呢")],
    "nihao": [Candidate("你好"), Candidate("拟好"), Candidate("尼号")],
    "shijie": [Candidate("世界"), Candidate("使节"), Candidate("视界")],
    "shi": [Candidate("是"), Candidate("时"), Candidate("事"), Candidate("市")],
    "wo": [Candidate("我"), Candidate("握"), Candidate("窝")],
    "xie": [Candidate("写"), Candidate("谢"), Candidate("些")],
    "zai": [Candidate("在"), Candidate("再"), Candidate("载")],
    "zhong": [Candidate("中"), Candidate("种"), Candidate("重")],
    "zhongwen": [Candidate("中文"), Candidate("种文")],
}


def suggest(code: str) -> list[Candidate]:
    """Return candidates for a raw input code.

    This intentionally stays simple.  Later you can plug in:
      - a trie / sqlite dictionary
      - user frequency
      - fuzzy pinyin
      - shape code rules
      - remote/local LLM reranking
    """
    code = code.lower().strip()
    if not code:
        return []
    if code in PINYIN_DICT:
        return PINYIN_DICT[code]

    # Tiny fallback: if `nihao` is not explicitly present, combine `ni` + `hao`.
    for i in range(1, len(code)):
        left, right = code[:i], code[i:]
        if left in PINYIN_DICT and right in PINYIN_DICT:
            return [Candidate(PINYIN_DICT[left][0].text + PINYIN_DICT[right][0].text, "组合")]

    # Keep the typed code as fallback so the engine is always usable.
    return [Candidate(code, "原样")]
