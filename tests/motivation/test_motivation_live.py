"""Live check (1 real LLM call): an abstract social urge becomes a concrete suggestion."""
import pytest

from tests.motivation import mothelp
from alive.interfaces import Urge
from alive.motivation.prompts import FALLBACK_SUGGESTION


@pytest.mark.live
async def test_live_concretize(tmp_path):
    k = await mothelp.live_kernel(tmp_path, real=("motivation",))
    try:
        now = k.clock.now()

        async def cands(now_, limit=10):
            return [{"person_key": "tg:M", "thread_id": "tg:M", "name": "小美", "closeness": 0.8,
                     "last_contact": now - 3 * 86400, "proactive_ok": True}]
        k.social.outreach_candidates = cands
        await k.memory.note(kind="episodic", content="小美说她下周三有个很重要的面试，有点紧张")
        u = await k.motivation.concretize(Urge(need="social", pressure=0.15, created_at=now))
        assert u.suggestion and u.suggestion != FALLBACK_SUGGESTION["social"] and len(u.suggestion) <= 80
        assert u.target_person in (None, "tg:M")
    finally:
        await k.stop()
