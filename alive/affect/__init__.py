"""Affect system (docs/04): temperament, mood (PAD, second-order dynamics), OCC emotion events,
physiology (two-process sleep model, energy, social battery, stress, boredom), appraisal, regulation,
expression mapping.

    kernel.affect = build(kernel)   # AffectEngine, implements alive.interfaces.AffectSystem
"""
from __future__ import annotations

from alive.affect.engine import AffectEngine, Emotion

__all__ = ["AffectEngine", "Emotion", "build"]


def build(kernel) -> AffectEngine:
    return AffectEngine(kernel)
