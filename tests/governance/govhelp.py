"""Shared helpers for governance tests (unique module name: tests dirs are not packages)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from alive.runtime.clock import ManualClock
from alive.testing import make_test_kernel

TZ = ZoneInfo("Asia/Shanghai")


def ts(s: str) -> float:
    """Local Asia/Shanghai 'YYYY-MM-DD HH:MM' -> epoch seconds. 2026-11-09 is a Monday."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


async def kernel(tmp_path, start: str = "2026-11-09 10:00", real=("gov",), cfg=None, clock=None):
    clock = clock or ManualClock(start=ts(start))
    return await make_test_kernel(str(tmp_path), real=list(real), clock=clock, cfg_overrides=cfg)


def emo(aff, etype):
    """Current active emotion dict of a type (strongest) or None."""
    found = [e for e in aff.snapshot().emotions if e["type"] == etype]
    return found[0] if found else None


async def events(k, etype):
    await k.clock.settle()
    return [e.payload for e in await k.bus.query(types=[etype])]


async def live_kernel(tmp_path, real, start: str = "2026-11-09 10:00"):
    """Kernel with the REAL model provider (API config from .secrets/LLM.txt via the kernel) and fakes for
    every subsystem not in ``real``. Only for @pytest.mark.live tests."""
    import os

    from alive.config import load_config
    from alive.kernel import FACTORIES, Kernel
    from alive.testing import FAKES, REPO
    cfg = load_config(None, {"data_dir": str(tmp_path), "persona": os.path.join(REPO, "personas/xiaoyu"),
                             "http": {"enabled": False}, "channels": []}, base_dir=REPO)
    k = Kernel(cfg, clock=ManualClock(start=ts(start)), overrides={n: FAKES[n] for n in FACTORIES if n not in real})
    await k.start()
    return k
