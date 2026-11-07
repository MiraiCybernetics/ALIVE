"""Motivation, intentions and initiative (docs/05): homeostatic needs -> urges -> intentions -> outreach.

    kernel.motivation = build(kernel)   # MotivationEngine, implements alive.interfaces.MotivationSystem
"""
from __future__ import annotations

from alive.motivation.engine import IntentionRecord, MotivationEngine

__all__ = ["IntentionRecord", "MotivationEngine", "build"]


def build(kernel) -> MotivationEngine:
    return MotivationEngine(kernel)
