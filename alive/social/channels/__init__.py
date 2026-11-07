"""Channel adapters. ``build_adapter(kernel, spec)`` builds one from a config dict ``{"kind", "name", ...}``.

Built-in kinds: cli, telegram, onebot, sim. ``kind`` may also be "package.module:ClassName" for third-party
adapters (subclasses of ``ChannelAdapter``).
"""
from __future__ import annotations

import importlib

from alive.social.channels.base import Backoff, Capabilities, ChannelAdapter, Quotas, SelfIdentity, split_text

KINDS = {
    "cli": "alive.social.channels.cli:CLIAdapter",
    "telegram": "alive.social.channels.telegram:TelegramAdapter",
    "onebot": "alive.social.channels.onebot:OneBotAdapter",
    "sim": "alive.social.channels.sim:SimAdapter",
}


def adapter_class(kind: str):
    spec = KINDS.get(kind, kind)
    if ":" not in spec:
        raise ValueError(f"unknown channel kind {kind!r}")
    mod, _, cls = spec.partition(":")
    return getattr(importlib.import_module(mod), cls)


def build_adapter(kernel, spec: dict) -> ChannelAdapter:
    kind = str(spec.get("kind") or "")
    name = str(spec.get("name") or kind)
    if ":" in name:
        raise ValueError("channel names must not contain ':' (thread ids are '<channel>:<chat_id>')")
    return adapter_class(kind)(kernel, name, spec)


__all__ = ["Backoff", "Capabilities", "ChannelAdapter", "Quotas", "SelfIdentity", "build_adapter", "adapter_class",
           "split_text", "KINDS"]
