# -*- coding: utf-8 -*-
"""Optional LLM cleanup/reranking for voice dictation.

Uses an OpenAI-compatible Chat Completions endpoint via stdlib urllib, so it
works with the built-in llama.cpp sidecar, Ollama, LM Studio, llama.cpp server,
OpenAI-compatible cloud APIs, and does not add runtime dependencies.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ibus_voice_ime.memory import voice_terms
from ibus_voice_ime.text import llm_runtime, text_postprocess

DEFAULT_BASE_URL = llm_runtime.DEFAULT_BASE_URL
DEFAULT_MODEL = llm_runtime.DEFAULT_MODEL_ALIAS

MODE_INSTRUCTIONS: dict[str, str] = {
    "dictation": "主要任务是积极补齐标点。除添加/调整标点和必要空白外，尽量不要改动任何文字。",
    "literal": "尽量原样输出；除添加/调整标点、空白和明显识别错误外，不要改动文字。",
    "markdown": "积极补齐标点；不要主动整理结构，除非原文已经明显是 Markdown。",
    "prompt": "积极补齐标点；不要重写成更强的提示词，除标点外尽量不改文字。",
    "command": "尽量原样保留命令、路径、参数和英文符号；只在安全时补标点。",
}

SYSTEM_PROMPT = """你是语音输入法的轻量后处理器。

当前优先目标：积极补齐中文标点。听写文本不能整段没有标点；应该根据语气和语义加入逗号、句号、问号、顿号、分号或冒号。

严格限制：
1. 除添加/调整标点符号和必要空白外，尽量不要改动任何文字。
2. 不要润色、总结、压缩、扩写、改写句式、重排内容、替换主语，不能把用户的话改成命令。
3. 不要丢失信息，不要新增信息，不要改变主语、人称、语气、顺序、命令/疑问/否定含义。
4. 只有在极其明确时，才修正明显识别错字或删除明确无意义的独立口水词；不确定就保留原词，只补标点。

默认使用简体中文输出；如果原文或模型输出里出现繁体中文，请转换为简体中文。
只输出处理后的文本，不要解释，不要加引号。
""".strip()

CANDIDATE_SYSTEM_PROMPT = """你是语音输入法的轻量后处理器。
只生成积极补齐标点后的文本；候选之间主要只能有标点差异。
除添加/调整标点和必要空白外，尽量不要改动任何文字；不要润色、总结、压缩、扩写、重排或改变主语。
默认使用简体中文输出；如果原文或模型输出里出现繁体中文，请转换为简体中文。
输出必须是 JSON 字符串数组，例如 ["候选一。", "候选二。"]，不要输出其它内容。
""".strip()

_PROTECTED_TOKEN_RE = re.compile(
    r"https?://\S+|~?/(?:[\w.\-]+/?)+|[A-Za-z_][A-Za-z0-9_./:+\-]*|\d+(?:\.\d+)?[A-Za-z%]*|Ctrl\+Alt\+\w+|/[A-Za-z][\w\-]*"
)
_CJK_PUNCT_RE = re.compile(r"[，。！？；：、]")
_FILLER_TOKEN_RE = re.compile(r"(?:嗯+|呃+|啊+|额+|哦+|唔+|哈+|呢+|吧+|嘛+|呀+|那个|这个|就是|然后呢|怎么说呢|对吧|(?<![A-Za-z0-9_])(?:a|r|er|uh|um)(?![A-Za-z0-9_]))")
_WEAK_FRAGMENT_RE = re.compile(r"[，。！？；：、,.!?;:](?:接|就|那|这|呃|啊|嗯|哦|哈|呢|吧)(?=[，。！？；：、,.!?;:])")
_SUBJECT_RE = re.compile(r"我们|你们|他们|她们|它们|本人|我|你|您|他|她|它|系统|用户")
_INTENT_MARKER_RE = re.compile(r"不要|不能|不想|不需要|不希望|不是|没有|别|请|希望|想要|需要|应该|必须|可以|能否|是否|吗|么|怎样|如何|为什么")
_UNREQUESTED_ACTION_RE = re.compile(r"执行|运行|打开|关闭|删除|清空|覆盖|替换|提交|发送|安装|卸载|重启|创建|修改|修复|调用")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def enabled() -> bool:
    # Hard policy: LLM post-processing is disabled regardless of environment
    # variables.  Keep this false so status/UI paths and direct callers cannot
    # accidentally enable the LLM rewrite layer.
    return False


def rerank_enabled() -> bool:
    return _env_bool("VOICE_IME_LLM_RERANK", False)


def trust_llm_output() -> bool:
    """Whether to commit the single LLM result directly.

    This is the default path: keep prompting simple and trust the model instead
    of generating multiple candidates and falling back to the raw transcript.
    """
    return _env_bool("VOICE_IME_LLM_TRUST_OUTPUT", True)


def _aggressive_enabled(mode: str) -> bool:
    """Whether semantic rewrite/polish is allowed.

    Stability is more important for an input method than elegance: the default
    is conservative for every mode.  Users can opt in with
    VOICE_IME_LLM_AGGRESSIVE=1 when they explicitly want semantic polishing.
    literal/command always remain safe.
    """
    if mode in {"literal", "command"}:
        return False
    return _env_bool("VOICE_IME_LLM_AGGRESSIVE", False)


def _endpoint(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"


def _resolve_base_url() -> str:
    configured = os.environ.get("VOICE_IME_LLM_BASE_URL", "").strip()
    if llm_runtime.internal_enabled():
        return llm_runtime.ensure_server()
    return configured or DEFAULT_BASE_URL


def _load_custom_dictionary() -> str:
    # Merged explicit voice dictionary + frequently typed English memory.
    # The name is kept for compatibility with the rest of this module.
    try:
        return voice_terms.build_llm_context(max_chars=4000)
    except Exception:
        return ""


def _build_messages(raw_text: str, mode: str) -> list[dict[str, str]]:
    dictionary = _load_custom_dictionary()
    mode_instruction = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["dictation"])
    extra = os.environ.get("VOICE_IME_LLM_EXTRA_PROMPT", "").strip()

    user_parts = [
        f"模式：{mode}",
        f"要求：{mode_instruction}",
        "标点要求：要更积极地补标点，不能整段无标点；优先添加逗号、句号、问号、顿号、分号或冒号。",
        "文字要求：别的都不改；除标点和必要空白外，不要润色、总结、压缩、扩写、重排或改写主语；默认输出简体中文。",
    ]
    if dictionary:
        user_parts.append("请优先正确保留这些用户词典/技术词：\n" + dictionary)
    if extra:
        user_parts.append("用户额外要求：\n" + extra)
    user_parts.append("原始识别文本：\n" + raw_text)

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _build_candidate_messages(raw_text: str, mode: str, count: int) -> list[dict[str, str]]:
    dictionary = _load_custom_dictionary()
    mode_instruction = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["dictation"])
    parts = [
        f"模式：{mode}",
        f"要求：{mode_instruction}",
        f"请生成 {count} 个候选。候选之间主要只能有标点差异；要积极补齐标点，除标点和必要空白外尽量不要改字，不能改变原意。"
    ]
    if dictionary:
        parts.append("必须优先保留这些用户词典/技术词：\n" + dictionary)
    parts.append("STT 原文：\n" + raw_text)
    return [
        {"role": "system", "content": CANDIDATE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return "".join(pieces).strip()
    text = choices[0].get("text")
    return text.strip() if isinstance(text, str) else ""


def _chat_completion(messages: list[dict[str, str]], *, base_url: str, model: str, timeout: float,
                     temperature: float, max_tokens: int) -> str:
    api_key = os.environ.get("VOICE_IME_LLM_API_KEY", "local")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(_endpoint(base_url), data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured local/API endpoint
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc

    result = json.loads(body)
    content = _extract_content(result)
    if not content:
        raise RuntimeError("LLM 返回空结果")
    return content.strip()


def _strip_wrappers(text: str) -> str:
    text = text.strip().strip("`｣「『』\"'").strip()
    return text_postprocess.cleanup_punctuation(text).strip()


def _parse_candidate_array(text: str) -> list[str]:
    text = text.strip()
    # Remove common markdown fences from small models.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    candidates: list[str] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            candidates = [str(x).strip() for x in parsed if str(x).strip()]
        elif isinstance(parsed, dict):
            for key in ("candidates", "候选", "texts"):
                value = parsed.get(key)
                if isinstance(value, list):
                    candidates = [str(x).strip() for x in value if str(x).strip()]
                    break
    except Exception:
        # Fallback: split numbered/bulleted lines.
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
            if line:
                candidates.append(line)
    return [_strip_wrappers(x) for x in candidates if _strip_wrappers(x)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = re.sub(r"\s+", "", item)
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def generate_candidates(raw_text: str, *, mode: str, base_url: str, model: str,
                        timeout: float, temperature: float, max_tokens: int) -> list[str]:
    count = max(1, min(_env_int("VOICE_IME_LLM_CANDIDATES", 3), 5))
    if count <= 1:
        content = _chat_completion(
            _build_messages(raw_text, mode),
            base_url=base_url,
            model=model,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return [_strip_wrappers(content)]

    content = _chat_completion(
        _build_candidate_messages(raw_text, mode, count),
        base_url=base_url,
        model=model,
        timeout=timeout,
        temperature=max(temperature, 0.2),
        max_tokens=max_tokens,
    )
    candidates = _parse_candidate_array(content)
    if not candidates:
        candidates = [_strip_wrappers(content)]
    return _dedupe(candidates[:count])


def _protected_tokens(text: str) -> list[str]:
    if not _env_bool("VOICE_IME_LLM_PROTECT_TOKENS", True):
        return []
    tokens = [m.group(0) for m in _PROTECTED_TOKEN_RE.finditer(text)]
    tokens = [tok for tok in tokens if tok not in {"a", "r", "er", "uh", "um"}]
    return _dedupe(tokens)


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def _multiset(pattern: re.Pattern[str], text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in pattern.findall(text):
        counts[item] = counts.get(item, 0) + 1
    return counts


def _missing_items(raw_counts: dict[str, int], cand_counts: dict[str, int]) -> list[str]:
    missing: list[str] = []
    for item, count in raw_counts.items():
        if cand_counts.get(item, 0) < count:
            missing.extend([item] * (count - cand_counts.get(item, 0)))
    return missing


def _extra_items(raw_counts: dict[str, int], cand_counts: dict[str, int]) -> list[str]:
    extra: list[str] = []
    for item, count in cand_counts.items():
        if raw_counts.get(item, 0) < count:
            extra.extend([item] * (count - raw_counts.get(item, 0)))
    return extra


def _safety_penalty(raw_text: str, candidate: str, *, mode: str, aggressive: bool) -> tuple[float, list[str]]:
    """Penalize LLM outputs that look like over-editing or authority escalation."""
    if mode in {"literal", "command"}:
        strict = True
    else:
        strict = not aggressive or _env_bool("VOICE_IME_LLM_STRICT_SAFETY", True)
    if not strict:
        return 0.0, []

    penalty = 0.0
    reasons: list[str] = []
    raw_subjects = _multiset(_SUBJECT_RE, raw_text)
    cand_subjects = _multiset(_SUBJECT_RE, candidate)
    missing_subjects = _missing_items(raw_subjects, cand_subjects)
    extra_subjects = _extra_items(raw_subjects, cand_subjects)
    if missing_subjects or extra_subjects:
        p = min(95.0, 38.0 * (len(missing_subjects) + len(extra_subjects)))
        penalty += p
        detail = ",".join((missing_subjects + extra_subjects)[:6])
        reasons.append(f"subject_changed({detail}):-{p:.1f}")

    raw_intents = _multiset(_INTENT_MARKER_RE, raw_text)
    cand_intents = _multiset(_INTENT_MARKER_RE, candidate)
    changed_intents = _missing_items(raw_intents, cand_intents) + _extra_items(raw_intents, cand_intents)
    if changed_intents:
        p = min(70.0, 14.0 * len(changed_intents))
        penalty += p
        reasons.append(f"intent_marker_changed({','.join(changed_intents[:5])}):-{p:.1f}")

    raw_actions = set(_UNREQUESTED_ACTION_RE.findall(raw_text))
    cand_actions = set(_UNREQUESTED_ACTION_RE.findall(candidate))
    extra_actions = sorted(cand_actions - raw_actions)
    if extra_actions:
        p = min(90.0, 28.0 * len(extra_actions))
        penalty += p
        reasons.append(f"extra_action({','.join(extra_actions[:5])}):-{p:.1f}")

    # Avoid turning a question/uncertainty into an imperative sentence.
    if re.search(r"[？?]|(?:吗|么|能否|是否|怎样|如何|为什么)", raw_text) and not re.search(r"[？?]|(?:吗|么|能否|是否|怎样|如何|为什么)", candidate):
        penalty += 35.0
        reasons.append("question_to_statement:-35")
    return penalty, reasons


def _score_candidate(raw_text: str, candidate: str, *, mode: str) -> tuple[float, list[str]]:
    reasons: list[str] = []
    raw_compact = re.sub(r"\s+", "", raw_text)
    cand_compact = re.sub(r"\s+", "", candidate)
    if not cand_compact:
        return -9999.0, ["empty"]

    aggressive = _aggressive_enabled(mode)
    score = 100.0
    raw_len = max(len(raw_compact), 1)
    ratio = len(cand_compact) / raw_len
    default_max_expansion = 2.4 if aggressive else 1.6
    max_expansion = _env_float("VOICE_IME_LLM_MAX_EXPANSION_RATIO", default_max_expansion)

    if ratio > max_expansion:
        penalty = min(80.0, (ratio - max_expansion) * 80.0)
        score -= penalty
        reasons.append(f"too_long:-{penalty:.1f}")
    min_ratio = 0.32 if aggressive else 0.55
    if ratio < min_ratio and len(raw_compact) >= 10:
        penalty = 35.0 if aggressive else 45.0
        score -= penalty
        reasons.append(f"too_short:-{penalty:.0f}")

    raw_numbers = _numbers(raw_text)
    cand_numbers = _numbers(candidate)
    if raw_numbers != cand_numbers:
        miss = len([x for x in raw_numbers if x not in cand_numbers])
        extra = len([x for x in cand_numbers if x not in raw_numbers])
        penalty = 25.0 * (miss + extra)
        score -= penalty
        reasons.append(f"number_changed:-{penalty:.1f}")

    missing_tokens = [
        tok for tok in _protected_tokens(raw_text)
        if tok not in candidate and not voice_terms.is_allowed_hotword_replacement(raw_text, candidate, tok)
    ]
    if missing_tokens:
        penalty = min(80.0, 12.0 * len(missing_tokens))
        score -= penalty
        reasons.append(f"missing_tokens({','.join(missing_tokens[:4])}):-{penalty:.1f}")

    raw_latin = set(re.findall(r"[A-Za-z_][A-Za-z0-9_./:+\-]*", raw_text))
    cand_latin = set(re.findall(r"[A-Za-z_][A-Za-z0-9_./:+\-]*", candidate))
    extra_latin = cand_latin - raw_latin
    if extra_latin and (mode in {"literal", "command"} or (mode == "dictation" and not aggressive)):
        penalty = min(30.0, 5.0 * len(extra_latin))
        score -= penalty
        reasons.append(f"extra_latin:-{penalty:.1f}")

    if re.search(r"(?:以下是|候选|润色后|解释|JSON|```)", candidate):
        score -= 60.0
        reasons.append("wrapper:-60")

    similarity = difflib.SequenceMatcher(None, raw_compact, cand_compact).ratio()
    if mode in {"literal", "command"}:
        penalty = (1.0 - similarity) * 80.0
    elif aggressive:
        # In opt-in rewrite mode, a good rewrite may legitimately move words
        # around or remove many filler words, so only reject very distant text.
        penalty = max(0.0, 0.50 - similarity) * 45.0
    elif mode == "dictation":
        penalty = max(0.0, 0.82 - similarity) * 85.0
    else:
        penalty = max(0.0, 0.72 - similarity) * 65.0
    if penalty:
        score -= penalty
        reasons.append(f"semantic_distance:-{penalty:.1f}")

    safety_penalty, safety_reasons = _safety_penalty(raw_text, candidate, mode=mode, aggressive=aggressive)
    if safety_penalty:
        score -= safety_penalty
        reasons.extend(safety_reasons)

    hotword_bonus, hotword_reasons = voice_terms.hotword_score(raw_text, candidate)
    if hotword_bonus:
        score += hotword_bonus
        reasons.extend(hotword_reasons)

    if mode == "dictation" and len(raw_compact) >= 12:
        raw_has_punct = bool(_CJK_PUNCT_RE.search(raw_text))
        cand_has_punct = bool(_CJK_PUNCT_RE.search(candidate))
        if cand_has_punct:
            bonus = 18.0 if aggressive and len(raw_compact) >= 20 else (14.0 if len(raw_compact) >= 20 else 8.0)
            score += bonus
            reasons.append(f"punct:+{bonus:.0f}")
            if re.search(r"[。！？]$", candidate):
                score += 4.0
                reasons.append("terminal_punct:+4")
        elif raw_has_punct or len(raw_compact) >= 20:
            score -= 22.0 if aggressive else 18.0
            reasons.append("no_punct:-22" if aggressive else "no_punct:-18")

    if mode in {"dictation", "prompt", "markdown"}:
        filler_hits = len(_FILLER_TOKEN_RE.findall(candidate)) + len(_WEAK_FRAGMENT_RE.findall(candidate))
        raw_filler_hits = len(_FILLER_TOKEN_RE.findall(raw_text)) + len(_WEAK_FRAGMENT_RE.findall(raw_text))
        if not aggressive and cand_compact == raw_compact:
            # In conservative mode the normalized transcript is the safety
            # baseline.  Do not make it lose just because it still contains
            # words such as “这个/就是/然后”; several are meaningful in context.
            score += 6.0
            reasons.append("raw_baseline:+6")
        else:
            if filler_hits:
                unit = 12.0 if aggressive else 4.0
                cap = 90.0 if aggressive else 20.0
                penalty = min(cap, unit * filler_hits)
                score -= penalty
                reasons.append(f"fillers:-{penalty:.1f}")
            if raw_filler_hits and filler_hits < raw_filler_hits:
                bonus = min(45.0, 7.0 * (raw_filler_hits - filler_hits)) if aggressive else min(10.0, 2.0 * (raw_filler_hits - filler_hits))
                score += bonus
                reasons.append(f"filler_cleanup:+{bonus:.1f}")
            if cand_compact == raw_compact and len(raw_compact) >= 12:
                penalty = 14.0 if aggressive else 0.0
                if penalty:
                    score -= penalty
                    reasons.append("unchanged_aggressive:-14")
    elif cand_compact == raw_compact:
        score += 1.0
        reasons.append("raw_safe:+1")

    return score, reasons


def _conservative_accept(raw_text: str, candidate: str, *, mode: str) -> tuple[bool, list[str]]:
    """Return whether a non-aggressive LLM candidate is safe enough to commit."""
    if candidate == raw_text or _aggressive_enabled(mode):
        return True, []
    raw_compact = re.sub(r"\s+", "", raw_text)
    cand_compact = re.sub(r"\s+", "", candidate)
    raw_len = max(len(raw_compact), 1)
    ratio = len(cand_compact) / raw_len
    similarity = difflib.SequenceMatcher(None, raw_compact, cand_compact).ratio()
    reasons: list[str] = []
    min_similarity = _env_float("VOICE_IME_LLM_CONSERVATIVE_MIN_SIMILARITY", 0.88 if mode == "dictation" else 0.9)
    min_ratio = _env_float("VOICE_IME_LLM_CONSERVATIVE_MIN_RATIO", 0.78)
    max_ratio = _env_float("VOICE_IME_LLM_CONSERVATIVE_MAX_RATIO", 1.18)
    if len(raw_compact) >= 12 and similarity < min_similarity:
        reasons.append(f"reject_similarity:{similarity:.2f}<{min_similarity:.2f}")
    if len(raw_compact) >= 12 and not (min_ratio <= ratio <= max_ratio):
        reasons.append(f"reject_ratio:{ratio:.2f} not in [{min_ratio:.2f},{max_ratio:.2f}]")
    safety_penalty, safety_reasons = _safety_penalty(raw_text, candidate, mode=mode, aggressive=False)
    if safety_penalty:
        reasons.extend("reject_" + r for r in safety_reasons)
    return not reasons, reasons


def rerank(raw_text: str, candidates: list[str], *, mode: str) -> tuple[str, list[dict[str, Any]]]:
    candidates = _dedupe([raw_text] + [c for c in candidates if c])
    scored: list[dict[str, Any]] = []
    raw_item: dict[str, Any] | None = None
    for cand in candidates:
        cand = _strip_wrappers(cand)
        score, reasons = _score_candidate(raw_text, cand, mode=mode)
        item = {"text": cand, "score": score, "reasons": reasons}
        if cand == raw_text and raw_item is None:
            raw_item = item
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    best_item = scored[0] if scored else {"text": raw_text, "score": 0.0, "reasons": []}

    if raw_item is not None and not _aggressive_enabled(mode):
        margin = _env_float("VOICE_IME_LLM_CONSERVATIVE_MARGIN", 18.0)
        allowed, reject_reasons = _conservative_accept(raw_text, str(best_item["text"]), mode=mode)
        if (not allowed) or (float(best_item["score"]) < float(raw_item["score"]) + margin):
            raw_item.setdefault("reasons", []).append(
                "conservative_fallback:" + (";".join(reject_reasons) if reject_reasons else f"margin<{margin:.1f}")
            )
            best_item = raw_item

    best = str(best_item["text"])
    return best, scored


def _log(raw_text: str, final_text: str, error: str | None = None,
         candidates: list[dict[str, Any]] | None = None) -> None:
    log_path = os.environ.get("VOICE_IME_LLM_LOG", "").strip()
    if not log_path:
        return
    try:
        p = Path(log_path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            item = {
                "ts": time.time(),
                "raw": raw_text,
                "final": final_text,
                "error": error,
            }
            if candidates is not None:
                item["candidates"] = candidates
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def refine(raw_text: str, *, mode: str | None = None) -> str:
    """Return LLM-refined text, or raw_text when disabled/skipped.

    Raises on transport/API/runtime errors so the caller can decide whether to
    fall back.  When reranking is enabled, the raw normalized text is always kept
    as candidate 0 and can win if the LLM output is risky.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text or not enabled():
        return raw_text

    min_chars = _env_int("VOICE_IME_LLM_MIN_CHARS", 8)
    if len(raw_text) < min_chars:
        return raw_text

    mode = (mode or os.environ.get("VOICE_IME_VOICE_MODE", "dictation")).strip().lower()
    base_url = _resolve_base_url()
    model = llm_runtime.model_alias() if llm_runtime.internal_enabled() else os.environ.get("VOICE_IME_LLM_MODEL", DEFAULT_MODEL)
    timeout = _env_float("VOICE_IME_LLM_TIMEOUT", 4.0)
    default_temperature = 0.25 if _aggressive_enabled(mode) else 0.1
    temperature = _env_float("VOICE_IME_LLM_TEMPERATURE", default_temperature)
    max_tokens = _env_int("VOICE_IME_LLM_MAX_TOKENS", 1024)

    if trust_llm_output() or not rerank_enabled():
        final_text = _chat_completion(
            _build_messages(raw_text, mode),
            base_url=base_url,
            model=model,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        final_text = text_postprocess.cleanup_punctuation(_strip_wrappers(final_text)).strip()
        _log(raw_text, final_text)
        return final_text

    if rerank_enabled():
        generated = generate_candidates(
            raw_text,
            mode=mode,
            base_url=base_url,
            model=model,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        final_text, scored = rerank(raw_text, generated, mode=mode)
        final_text = text_postprocess.cleanup_punctuation(final_text).strip()
        _log(raw_text, final_text, candidates=scored)
        return final_text

    return raw_text


def refine_with_fallback(raw_text: str, *, mode: str | None = None) -> str:
    try:
        return refine(raw_text, mode=mode)
    except Exception as exc:
        _log(raw_text, raw_text, error=str(exc))
        if _env_bool("VOICE_IME_LLM_FALLBACK_RAW", True):
            return raw_text
        raise


__all__ = [
    "enabled",
    "generate_candidates",
    "trust_llm_output",
    "refine",
    "refine_with_fallback",
    "rerank",
]
