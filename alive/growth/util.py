"""Small pure helpers for growth (kept local so growth never imports another subsystem's internals)."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip().lower()


def bigrams(text: str) -> set:
    t = re.sub(r"\s+", "", norm(text))
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def containment(a: str, b: str) -> float:
    """Share of a's character bigrams found in b."""
    A, B = bigrams(a), bigrams(b)
    if not A:
        return 1.0
    return len(A & B) / len(A)


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dataclass-like object or a dict (fakes return dicts)."""
    if isinstance(obj, dict):
        if name == "observed_at" and "observed_at" not in obj:
            return obj.get("ts", default)
        return obj.get(name, default)
    v = getattr(obj, name, default)
    return default if v is None and default is not None else v


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return lo
    return lo if x < lo else hi if x > hi else x
