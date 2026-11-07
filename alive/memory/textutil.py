"""Small text helpers shared by memory (and reused by growth): token estimates, FTS5 query building,
cheap lexical similarity. Pure functions, no I/O."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]+")
_LATIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'\-]*")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_]+|[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]+")

# frequent function words that make terrible FTS terms on their own
_CJK_STOP = set("的了是我你他她它们在和就也都很还吗呢吧啊呀嘛哦哈有没不这那个一要会说去来到对被把给让")


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip().lower()


def estimate_tokens(text: str) -> int:
    """≈ CJK chars / 1.5 + latin words (+1 per ~4 punctuation/other chars)."""
    if not text:
        return 0
    cjk = len(_CJK_CHAR.findall(text))
    words = len(_LATIN_WORD.findall(text))
    return int(cjk / 1.5 + words + 0.5) + 1


def fts_terms(cue: str, max_terms: int = 24) -> tuple:
    """Split a cue into (trigram_terms, short_terms) for an FTS5 trigram index.

    - CJK runs of length >= 3 contribute overlapping 3-grams (trigram index needs >= 3 chars).
    - Latin words of length >= 3 are kept whole (matched as substrings).
    - CJK runs of length 1-2 (and 2-char latin words) are returned as ``short_terms`` for the
      LIKE fallback.
    """
    t = norm(cue)
    tri: list = []
    short: list = []
    seen: set = set()
    for m in _TOKEN_RUN.finditer(t):
        w = m.group(0)
        if _CJK_CHAR.match(w):
            if len(w) >= 3:
                added = 0
                for i in range(len(w) - 2):
                    g = w[i:i + 3]
                    if g in seen:
                        continue
                    if sum(1 for ch in g if ch in _CJK_STOP) >= 2:
                        continue
                    seen.add(g)
                    tri.append(g)
                    added += 1
                if not added:  # e.g. "我的猫": every trigram is mostly function words
                    for i in range(len(w) - 1):
                        b = w[i:i + 2]
                        if b not in seen and not all(ch in _CJK_STOP for ch in b):
                            seen.add(b)
                            short.append(b)
            else:
                if w not in seen and not all(ch in _CJK_STOP for ch in w):
                    seen.add(w)
                    short.append(w)
        else:
            if len(w) >= 3:
                if w not in seen:
                    seen.add(w)
                    tri.append(w)
            elif len(w) == 2 and w not in seen and not w.isdigit():
                seen.add(w)
                short.append(w)
    if len(tri) > max_terms:  # keep an even spread across the cue
        step = len(tri) / max_terms
        tri = [tri[int(i * step)] for i in range(max_terms)]
    return tri, short[:6]


def fts_match_expr(terms: Iterable[str]) -> str:
    """OR-query of quoted phrases (quotes escaped) for FTS5 MATCH."""
    parts = ['"' + t.replace('"', '""') + '"' for t in terms if t]
    return " OR ".join(parts)


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def bigrams(text: str) -> set:
    t = re.sub(r"\s+", "", norm(text))
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def jaccard(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def containment(a: str, b: str) -> float:
    """Share of a's bigrams found in b (asymmetric)."""
    A, B = bigrams(a), bigrams(b)
    if not A:
        return 1.0
    return len(A & B) / len(A)


def split_items(text: str) -> list:
    """Split a summary into items (sentences / bullets) for the max-loss rewrite rule."""
    parts = []
    for line in re.split(r"\n+", text or ""):
        line = line.strip().lstrip("-•*·").strip()
        if not line:
            continue
        for p in re.split(r"[。；;！!？?]+", line):
            p = p.strip(" ，,、")
            if len(p) >= 2:
                parts.append(p)
    return parts


def item_loss(old: str, new: str, keep_threshold: float = 0.5) -> float:
    """Fraction of old summary items not retained in the new summary (0..1)."""
    old_items = split_items(old)
    if not old_items:
        return 0.0
    new_items = split_items(new) or [new]
    lost = 0
    nn = norm(new)
    for it in old_items:
        if norm(it) in nn:
            continue
        best = max(containment(it, ni) for ni in new_items)
        if best < keep_threshold and containment(it, new) < keep_threshold:
            lost += 1
    return lost / len(old_items)


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def contains_any(text: str, words: Iterable[str]) -> Optional[str]:
    t = norm(text)
    for w in words:
        w2 = norm(w)
        if w2 and w2 in t:
            return w
    return None
