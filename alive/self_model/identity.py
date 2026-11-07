"""Identity files (docs/08 §2): core.md (immutable zone), narrative.md / voice.md (slow zone), boundaries.md.

The persona directory is read-only at runtime. Slow-zone updates are written as new versions into
the data directory (``<data_dir>/identity/narrative.md`` + version table) and take precedence over
the persona copy; the persona pack is never modified (INV-8 / docs/12 §5).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _sections(md: str) -> dict:
    """Split markdown into {heading: body} by '## ' headings."""
    out: dict = {}
    cur = "_"
    buf: list = []
    for line in md.splitlines():
        if line.startswith("## "):
            out[cur] = "\n".join(buf).strip()
            cur, buf = line[3:].strip(), []
        elif line.startswith("# "):
            continue
        else:
            buf.append(line)
    out[cur] = "\n".join(buf).strip()
    return {k: v for k, v in out.items() if v}


_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def _bullets(text: str) -> list:
    return [m.group(1).strip() for l in text.splitlines() if (m := _BULLET.match(l))]


@dataclass
class Identity:
    name: str = "小屿"
    core_md: str = ""
    narrative_md: str = ""
    voice_md: str = ""
    boundaries_md: str = ""
    voice_samples: list = field(default_factory=list)
    interests: list = field(default_factory=list)

    @classmethod
    def load(cls, persona_dir: str, override_dir: Optional[str] = None) -> "Identity":
        def pick(fn: str) -> str:
            if override_dir:
                p = os.path.join(override_dir, fn)
                if os.path.exists(p):
                    return _read(p)
            return _read(os.path.join(persona_dir, "identity", fn))

        core = pick("core.md")
        narrative = pick("narrative.md")
        voice = pick("voice.md")
        boundaries = pick("boundaries.md")
        name = "小屿"
        m = re.search(r"名字[:：]\s*([^\s（(，,。]+)", core)
        if m:
            name = m.group(1)
        vs = []
        vsec = _sections(voice)
        for k, v in vsec.items():
            if "样例" in k or "sample" in k.lower():
                vs = _bullets(v)
        interests = []
        for line in narrative.splitlines():
            if "兴趣" in line or "喜欢" in line:
                interests.append(line.strip("- ").strip())
        return cls(name=name, core_md=core, narrative_md=narrative, voice_md=voice,
                   boundaries_md=boundaries, voice_samples=vs, interests=interests[:5])

    def digest(self, max_chars: int = 650) -> str:
        """Deterministic, stable identity summary for the cached prompt prefix (~<=400 tokens)."""
        core = _sections(self.core_md)
        parts = [f"我是{self.name}。"]
        for key in ("名字与来历", "核心价值（不超过十条）", "核心价值", "人格（Big Five，0–1）", "不可妥协的边界"):
            if key in core:
                body = core[key]
                lines = _bullets(body) or [l.strip() for l in body.splitlines() if l.strip()]
                if key.startswith("人格"):
                    lines = [l for l in lines if "文字描述" in l] or lines[-1:]
                parts.append(" ".join(lines))
        narr = [l.strip() for l in self.narrative_md.splitlines() if l.strip() and not l.startswith("#")]
        if narr:
            parts.append("关于我：" + " ".join(narr[:3]))
        voice = _sections(self.voice_md)
        vlines = _bullets(voice.get("_", "")) if voice else []
        if vlines:
            parts.append("我说话：" + "；".join(vlines[:4]))
        text = "\n".join(p for p in parts if p)
        return text[:max_chars]

    def full_text(self) -> str:
        return "\n\n".join(x for x in (self.core_md, self.narrative_md, self.voice_md, self.boundaries_md) if x)
