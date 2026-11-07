"""Contamination guard (docs/11 §7): held-out evaluation items must never leak into what the model sees.

Checks every message/history text (>= 4 chars) of the held-out set against prompt templates, the schema and
genre libraries, persona files, the dialogue catalog, and the life simulator's person prompts and scenarios
(what the simulated contacts say reaches the individual too).
"""
import glob
import os

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SURFACES = [
    "alive/core/prompts.py", "alive/core/schemas/*.yaml", "alive/core/dialogue/catalog.py",
    "alive/self_model/prompts.py", "alive/*/prompts.py", "personas/**/*.md", "personas/**/*.yaml",
    "alive/eval/simulator.py", "alive/eval/scenarios.py",
]


def _surface_text() -> str:
    seen, parts = set(), []
    for pat in SURFACES:
        for p in glob.glob(os.path.join(REPO, pat), recursive=True):
            if p not in seen:
                seen.add(p)
                with open(p, encoding="utf-8") as f:
                    parts.append(f.read())
    return "\n".join(parts)


def _heldout_strings() -> list:
    out = []
    for path in glob.glob(os.path.join(REPO, "alive/eval/heldout_*.yaml")):
        data = yaml.safe_load(open(path, encoding="utf-8"))
        for item in data.get("items", []) + data.get("retired", []):
            for t in item.get("new", []):
                out.append((item["id"], t))
            for h in item.get("history") or []:
                out.append((item["id"], h[2]))
    return [(i, t) for i, t in out if len(t.strip()) >= 4]


def test_heldout_items_do_not_leak_into_model_inputs():
    surface = _surface_text()
    leaks = [(i, t) for i, t in _heldout_strings() if t in surface]
    assert not leaks, f"held-out text found in prompts/schemas/personas: {leaks}"


def test_heldout_set_is_nonempty():
    assert len(_heldout_strings()) >= 10
