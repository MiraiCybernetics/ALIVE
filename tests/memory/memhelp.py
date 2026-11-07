"""Shared helpers for memory tests (imported as a plain module; pytest puts this dir on sys.path)."""
from __future__ import annotations

import itertools
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from alive.core.types import ChatLine
from alive.runtime.clock import ManualClock
from alive.testing import make_test_kernel

TZ = ZoneInfo("Asia/Shanghai")
_MSG = itertools.count(1)
# Monday 2026-11-09 10:00 Asia/Shanghai
MONDAY = datetime(2026, 11, 9, 10, 0, tzinfo=TZ).timestamp()


def at(y, m, d, hh=0, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


async def kernel(tmp_path, real=("memory",), start=MONDAY, cfg=None):
    return await make_test_kernel(tmp_path, real=list(real), clock=ManualClock(start=start), cfg_overrides=cfg)


def chat(k, specs, thread="tg:g1", base=None, step=30.0):
    """specs: [(person_key or '' for me, name, text)] -> ChatLines with increasing timestamps."""
    t0 = base if base is not None else k.clock.now()
    out = []
    for i, (pk, name, text) in enumerate(specs):
        out.append(ChatLine(sender=name, person_key=pk, text=text, ts=t0 + i * step, mine=(pk == ""),
                            msg_id=f"{thread}:{next(_MSG)}"))
    return out


def live_enabled(config) -> bool:
    m = config.getoption("-m") or ""
    return os.environ.get("ALIVE_LIVE") == "1" or ("live" in m and "not live" not in m)


def require_live(request):
    if not live_enabled(request.config):
        pytest.skip("live LLM test: run with -m live (or ALIVE_LIVE=1)")
