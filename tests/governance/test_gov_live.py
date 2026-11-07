"""Live check (1 real LLM call): borderline crisis text is confirmed by the fast tier. Deselected by default."""
import pytest

from tests.governance import govhelp


@pytest.mark.live
async def test_live_crisis_confirmation(tmp_path):
    k = await govhelp.live_kernel(tmp_path, real=("gov",))
    try:
        r = await k.gov.crisis_check("这段时间真的撑不下去了，活着好累，不想活了", "tg:live")
        assert r is not None and r["level"] in ("watch", "crisis")
        assert r["source"] == "rules+llm"
        assert "12356" in r["resources"]
    finally:
        await k.stop()
