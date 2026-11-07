"""Live prompt sanity check for memory extraction + reconciliation (2 real LLM calls).

Deselected by default (pyproject addopts: -m 'not live'). Run explicitly:
    scripts/wsl-run.sh python -m pytest -q -s -m live tests/memory/test_mem_live.py
"""
import os

import pytest

from alive.core.types import Trust
from tests.memory.memhelp import MONDAY, chat, day_of, require_live

pytestmark = pytest.mark.live


async def live_kernel(tmp_path, real):
    from alive.config import load_config
    from alive.kernel import FACTORIES, Kernel
    from alive.runtime.clock import ManualClock
    from alive.testing import FAKES, REPO
    cfg = load_config(None, {"data_dir": str(tmp_path), "persona": os.path.join(REPO, "personas/xiaoyu"),
                             "http": {"enabled": False}, "channels": []}, base_dir=REPO)
    k = Kernel(cfg, clock=ManualClock(start=MONDAY), overrides={n: FAKES[n] for n in FACTORIES if n not in real})
    await k.start()
    return k


async def test_live_extract_and_reconcile(tmp_path, request):
    require_live(request)
    k = await live_kernel(tmp_path, ["memory"])
    try:
        m = k.memory
        old = await m.note(kind="semantic", content="阿杰在阿里巴巴上班", source="told_by", told_by="tg:jie",
                           trust=Trust.CONTACT, person_keys=["tg:jie"], observed_at=MONDAY - 60 * 86400)
        lines = chat(k, [("tg:jie", "阿杰", "跟你说个事，我下周三有个面试，字节的"),
                         ("", "小屿", "哇！加油！是什么岗位呀"),
                         ("tg:jie", "阿杰", "后端开发。我已经从阿里离职了，现在在腾讯，不过又打算跳槽了"),
                         ("tg:jie", "阿杰", "对了你上次答应过要帮我改简历的哦"),
                         ("", "小屿", "哈哈好呀，发我吧")], thread="tg:jie", base=MONDAY - 300)
        await m.observe_conversation("tg:jie", lines, ["tg:jie"])
        await m.flush_all()
        await m.drain()
        rows = await k.db.fetchall("SELECT kind, status, source, content, ref_start, ref_raw FROM mem_records")
        for r in rows:
            print(dict(r))
        n_extract = len(k.llm.ledger.by_purpose)
        print("purposes:", k.llm.ledger.by_purpose, "calls:", k.llm.ledger.calls)
        assert k.llm.ledger.calls <= 4
        eps = [r for r in rows if r["kind"] == "episodic"]
        facts = [r for r in rows if r["kind"] in ("semantic", "relational") and r["content"] != "阿杰在阿里巴巴上班"]
        assert eps, "no episode extracted"
        assert facts, "no candidate fact extracted"
        dated = [r for r in rows if r["ref_start"] is not None and "面试" in r["content"]]
        if dated:
            assert day_of(dated[0]["ref_start"]) == "2026-11-18", dated[0]
        st = (await m.get(old)).status
        print("old fact status:", st, "| self claims:", [r["content"] for r in rows if r["kind"] == "self"])
        assert st in ("invalid", "superseded", "active")
        assert n_extract >= 1
    finally:
        await k.stop()
