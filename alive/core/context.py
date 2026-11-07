"""Context assembly (INV-1, docs/01 §5).

Block order is fixed and callers cannot reorder it:
  system:  [constitution] [identity digest] [voice] [global habits] [output contract]   <- stable, cached
  user:    [self state] [relationship] [memories] [playbook (scoped)] [workspace] [task] [external chat]

All dynamic sources are fetched concurrently and each has a timeout + graceful fallback, so a slow
memory query never stalls a reply.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Sequence

from alive.core import prompts
from alive.core.types import ChatLine, Trust
from alive.world.audience import origins_of

log = logging.getLogger("alive.core.context")


async def _safe(coro, default, timeout: float = 3.0, what: str = ""):
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception as e:
        log.debug("context source %s failed: %r", what, e)
        return default


class ContextAssembler:
    def __init__(self, kernel):
        self.k = kernel
        self._system_cache: dict = {}
        self.global_habits = ""   # refreshed after sleep (stable during the day)
        self._habits_day = ""

    # ------------------------------------------------------------------ stable prefix
    def system_prompt(self, contract: str = prompts.REPLY_CONTRACT) -> str:
        k = self.k
        constitution = k.gov.constitution_text() if k.gov else ""
        identity = k.selfm.identity_digest() if k.selfm else ""
        voice = "\n".join(f"- {v}" for v in (k.selfm.voice_samples(6) if k.selfm else []))
        habits = f"【我长期形成的习惯】\n{self.global_habits}\n\n" if self.global_habits else ""
        key = (hash(constitution), hash(identity), hash(voice), hash(habits), hash(contract))
        cached = self._system_cache.get(key)
        if cached is None:
            cached = prompts.SYSTEM_CORE.format(constitution=constitution, identity=identity, voice=voice,
                                                habits=habits) + contract
            self._system_cache = {key: cached}
        return cached

    async def refresh_global_habits(self) -> None:
        """Global playbook entries go into the stable prefix; refresh at most once a day."""
        day = self.k.clock.today()
        if day == self._habits_day or self.k.growth is None:
            return
        self._habits_day = day
        try:
            entries = await self.k.growth.playbook_for("", ["global"], limit=10)
            self.global_habits = self.k.growth.format_playbook(entries, []) if entries else ""
        except Exception:
            log.debug("habit refresh failed", exc_info=True)

    # ------------------------------------------------------------------ dynamic blocks
    def render_chat(self, history: Sequence[ChatLine], new_lines: Sequence[ChatLine], my_name: str,
                    max_chars: int = 2600) -> str:
        now = self.k.clock.now()

        def fmt(l: ChatLine, mark: str = "") -> str:
            who = f"{my_name}（我）" if l.mine else l.sender
            ago = now - l.ts
            t = self.k.clock.local(l.ts).strftime("%H:%M") if ago < 86400 else self.k.clock.local(l.ts).strftime("%m-%d %H:%M")
            return f"[{t}] {who}: {l.text}{mark}"
        new_ids = {id(x) for x in new_lines}
        old = [l for l in history if id(l) not in new_ids and not any(l.msg_id and l.msg_id == n.msg_id for n in new_lines)]
        lines_old = [fmt(l) for l in old]
        lines_new = [fmt(l, "  ← 新") for l in new_lines]
        body_new = "\n".join(lines_new)
        budget = max(400, max_chars - len(body_new))
        kept: list = []
        total = 0
        for s in reversed(lines_old):
            if total + len(s) > budget:
                break
            kept.append(s)
            total += len(s) + 1
        kept.reverse()
        gap = "\n…（更早的省略）\n" if len(kept) < len(lines_old) else ""
        return (gap + "\n".join(kept) + ("\n" if kept else "") + body_new).strip()

    async def gather(self, *, person_key: Optional[str], thread_id: Optional[str], cue: str,
                     scopes: Sequence[str], workspace_text: str, deep_memory: bool = False,
                     budget_tokens: int = 1100, present: Optional[Sequence[str]] = None) -> dict:
        """Dynamic blocks for speaking in ``thread_id``. Privacy follows the information (INV-34): memories and
        remembered facts are only those whose audience covers everyone who would hear me there (that space's
        people + ``present``). ``blocks["_withheld"]``: contents left out (for the outbound leak check);
        ``blocks["_origins"]``: where what was used came from (rule of two)."""
        k = self.k
        snap = k.affect.snapshot() if k.affect else None
        mood = (snap.P, snap.A, snap.D) if snap else None
        withheld: list = []
        origins: list = []
        pres = list(present or ())

        async def rel():
            if not (k.social and person_key):
                return ""
            return await k.social.relationship_context(person_key, space=thread_id, present=pres, withheld=withheld,
                                                       origins=origins)

        async def mem():
            if not k.memory:
                return ""
            hits = await k.memory.retrieve(cue, person_key=person_key, thread_id=thread_id,
                                           budget_tokens=budget_tokens, tier="deep" if deep_memory else "standard",
                                           mood=mood, present=pres, withheld=withheld)
            for h in hits:
                origins.extend(origins_of(h))
            return k.memory.format_hits(hits, k.clock.now()) if hits else ""

        async def pb():
            if not k.growth:
                return ""
            entries, exprs = await asyncio.gather(k.growth.playbook_for(cue, scopes, limit=6),
                                                  k.growth.expressions_for(cue, scopes, limit=4))
            return k.growth.format_playbook(entries, exprs)

        t_mem = 12.0 if deep_memory else 4.0
        relationship, memories, playbook = await asyncio.gather(
            _safe(rel(), "", 3.0, "relationship"), _safe(mem(), "", t_mem, "memory"), _safe(pb(), "", 3.0, "playbook"))
        return {"state": k.selfm.state_text() if k.selfm else "", "relationship": relationship or "（不太了解）",
                "memories": memories or "（想不起什么特别相关的）", "playbook": playbook or "（无）",
                "workspace": workspace_text or "（没什么特别挂心的）", "_withheld": withheld, "_origins": origins}

    def build_reply_messages(self, blocks: dict, instruction: str, chat: str, chat_kind: str) -> list:
        content = prompts.REPLY_TASK.format(state=blocks["state"],
                                            present=blocks.get("present") or prompts.PRESENT_NONE,
                                            focus=blocks.get("focus") or prompts.FOCUS_NONE,
                                            relationship=blocks["relationship"],
                                            memories=blocks["memories"], playbook=blocks["playbook"],
                                            workspace=blocks["workspace"],
                                            understanding=blocks.get("understanding") or "（没什么特别的）",
                                            dialogue=blocks.get("dialogue") or "（顺其自然）",
                                            instruction=instruction,
                                            chat_kind=chat_kind, chat=chat)
        return [{"role": "user", "content": content}]
