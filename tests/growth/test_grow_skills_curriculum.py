"""Skills registry (SKILL.md) and the learning curriculum."""
import pytest

from alive.growth.skills import parse_skill_md
from tests.growth.growhelp import MONDAY, kernel


def write_skill(base, name, fm: str, body="正文：怎么做这件事。", extra=None):
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n{body}\n", encoding="utf-8")
    for fn, txt in (extra or {}).items():
        (d / fn).write_text(txt, encoding="utf-8")


def test_parse_skill_md():
    fm, body = parse_skill_md("---\nname: x\ndescription: 做 X\n---\n# 标题\n内容")
    assert fm == {"name": "x", "description": "做 X"} and body.startswith("# 标题")
    assert parse_skill_md("没有 frontmatter") == ({}, "没有 frontmatter")


async def test_skills_registry(tmp_path):
    persona = tmp_path / "persona"
    data = tmp_path / "data"
    write_skill(persona / "skills", "paper-notes", "name: paper-notes\ndescription: 整理论文笔记")
    write_skill(data / "skills", "find-recipes", "name: find-recipes\ndescription: 找菜谱的最佳路径",
                extra={"verify.py": "assert True\n"})
    write_skill(data / "skills", "no-verifier", "name: no-verifier\ndescription: 自己写的但没有验证器")
    write_skill(data / "skills", "user-skill", "name: user-skill\ndescription: 用户装的\nauthor: user")
    from alive.runtime.clock import ManualClock
    from alive.testing import make_test_kernel
    k = await make_test_kernel(data, real=["growth"], clock=ManualClock(start=MONDAY),
                               cfg_overrides={"persona": str(persona)})
    try:
        sk = {s["name"]: s for s in await k.growth.skills()}
        assert sk["paper-notes"]["author"] == "author" and sk["paper-notes"]["status"] == "active"
        assert sk["user-skill"]["status"] == "active"
        assert sk["find-recipes"]["author"] == "self" and sk["find-recipes"]["status"] == "candidate"
        assert sk["find-recipes"]["has_verifier"] and not sk["no-verifier"]["has_verifier"]
        assert "怎么做" in k.growth.skill_body("paper-notes")
        assert not (await k.growth.verify_skill("no-verifier", True))["ok"]       # needs a verifier
        assert (await k.growth.verify_skill("find-recipes", True, 1.0))["status"] == "active"
        await k.growth.reload_skills()
        assert {s["name"]: s for s in await k.growth.skills()}["find-recipes"]["status"] == "active"
        # editing a self-written skill sends it back to candidate until re-verified
        (data / "skills" / "find-recipes" / "SKILL.md").write_text(
            "---\nname: find-recipes\ndescription: 改过了\n---\n新正文\n", encoding="utf-8")
        await k.growth.reload_skills()
        assert {s["name"]: s for s in await k.growth.skills()}["find-recipes"]["status"] == "candidate"
        hist = await k.db.fetchall("SELECT change, snapshot FROM grow_versions WHERE artifact_id='skill:find-recipes'")
        assert len(hist) == 2
    finally:
        await k.stop()


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


async def test_curriculum_scoring_and_intentions(k):
    mot = k.motivation
    await mot.add_intention({"goal": "成为一个懂天文的朋友", "level": "life", "kind": "general"})
    mot.needs = lambda: {"curiosity": {"level": 0.3, "setpoint": 0.75, "pressure": 0.4}}
    for _ in range(2):
        await k.growth.record_evidence(level="S2", polarity="harmful", about="explain_too_long")
    k.mock.on("grow.curriculum", {"items": [
        {"topic": "学会用一句话讲清楚黑洞", "why": "朋友常问", "success_criterion": "能用两句话向阿杰解释黑洞",
         "learnable": 0.8, "interesting": 0.9, "goal_fit": 0.9},
        {"topic": "学微积分", "why": "", "success_criterion": "", "learnable": 0.9, "interesting": 0.9, "goal_fit": 0.9},
        {"topic": "了解天文望远镜", "why": "好奇", "success_criterion": "能说出三种望远镜的区别",
         "learnable": 0.6, "interesting": 0.5, "goal_fit": 0.6},
    ]})
    rep = await k.growth.run_sleep(k.clock.now(), stages=["plan"])
    assert rep["stages"][0]["report"]["agenda"] == 2
    prompt = k.mock.calls_for("grow.curriculum")[0].messages[0]["content"]
    assert "好奇心偏低" in prompt and "反复出现的失误：explain_too_long" in prompt and "长期目标" in prompt
    learn = [i for i in mot.intentions.values() if i.kind == "learn"]
    assert [i.goal for i in learn] == ["学习：学会用一句话讲清楚黑洞", "学习：了解天文望远镜"]
    agenda = await k.db.kv_get("grow.curriculum", rep["date"])
    assert agenda[0]["score"] > agenda[1]["score"]
    assert all(a["success_criterion"] for a in agenda)
    a0 = agenda[0]
    assert a0["score"] == pytest.approx(a0["learnable"] * a0["interesting"] * a0["novel"] * a0["goal_fit"], rel=1e-2)
