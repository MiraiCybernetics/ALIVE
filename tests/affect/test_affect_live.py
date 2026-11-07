"""Live check (1 real LLM call): LLM-assisted reappraisal returns a usable first-person reappraisal."""
import pytest

from tests.affect import affhelp


@pytest.mark.live
async def test_live_reappraisal(tmp_path):
    k = await affhelp.live_kernel(tmp_path, real=("affect",))
    try:
        a = k.affect
        a.apply_deltas([{"type": "sadness", "intensity": 0.95, "cause": "好朋友两天没回我消息"}], "ev-live",
                       target_weight=1.0)
        out = await a.regulate()
        assert out is not None and 0.3 <= out["factor"] <= 1.0
        assert out["reappraisal"] and out["reappraisal"] != "也许没我想的那么糟，先缓一缓。"
    finally:
        await k.stop()
