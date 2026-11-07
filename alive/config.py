"""Configuration loading (docs/10 §9).

Layers, later wins:  built-in DEFAULTS  <  config file (YAML)  <  persona overrides  <  env vars.
Config is a nested dict wrapped in ``Cfg`` for attribute access with defaults:
    cfg.get("social.debounce.quiet_s", 2.0)
Subsystems should read their own section (e.g. ``cfg.section("affect")``) and keep defaults
next to the code that uses them, so the defaults table here only covers cross-cutting values.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Optional

import yaml

DEFAULTS: dict = {
    "persona": "personas/xiaoyu",
    "data_dir": "~/.local/share/alive/{persona_name}",
    "timezone": "Asia/Shanghai",
    "clock": {"kind": "real", "scale": 60.0, "start": None},
    "log_level": "INFO",
    "llm": {
        "secrets_file": ".secrets/LLM.txt",
        "rps": 5.0,
        "burst": 10.0,
        "max_retries": 3,
        "providers": {
            # filled from secrets_file when base_url/api_key are absent
            "main": {"kind": "openai", "base_url": None, "api_key": None},
        },
        "tiers": {
            "fast": {"provider": "main", "model": "deepseek-flash", "thinking": "disabled",
                     "max_tokens": 800, "temperature": 0.7, "timeout": 40, "concurrency": 8},
            "deliberate": {"provider": "main", "model": "deepseek-flash", "reasoning_effort": "low",
                           "max_tokens": 2000, "temperature": 0.9, "timeout": 90, "concurrency": 4},
            "deep": {"provider": "main", "model": "deepseek-v4-pro", "reasoning_effort": "high",
                     "max_tokens": 6000, "temperature": 0.8, "timeout": 300, "concurrency": 2},
        },
        # USD per 1M tokens. PLACEHOLDER VALUES: set these to your provider's real prices.
        "pricing": {
            "deepseek-flash": {"input_miss": 0.14, "input_hit": 0.028, "output": 0.28},
            "deepseek-v4-pro": {"input_miss": 0.55, "input_hit": 0.14, "output": 2.2},
        },
    },
    "embedder": {"kind": "hashing", "dim": 512},
    "db": {"readers": 4},
    "http": {"enabled": True, "host": "127.0.0.1", "port": 8765, "token": None},
    "channels": [{"kind": "cli", "name": "cli"}],
    "owner": {"person_keys": ["cli:owner"]},
}


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Cfg:
    """Read-only-ish view over a nested dict with dotted-path access."""

    def __init__(self, data: Optional[dict] = None, root: Optional["Cfg"] = None):
        self.data = data if data is not None else {}
        self.root = root or self

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def section(self, path: str) -> "Cfg":
        v = self.get(path, {})
        return Cfg(v if isinstance(v, dict) else {}, self.root)

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def __contains__(self, path: str) -> bool:
        return self.get(path, _MISSING) is not _MISSING

    def as_dict(self) -> dict:
        return copy.deepcopy(self.data)


_MISSING = object()


def _env_overrides() -> dict:
    """ALIVE__A__B=value  ->  {"a": {"b": value}} (value parsed as YAML scalar)."""
    out: dict = {}
    for k, v in os.environ.items():
        if not k.startswith("ALIVE__"):
            continue
        parts = [p.lower() for p in k[len("ALIVE__"):].split("__") if p]
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        try:
            cur[parts[-1]] = yaml.safe_load(v)
        except Exception:
            cur[parts[-1]] = v
    return out


def load_config(path: Optional[str] = None, overrides: Optional[dict] = None,
                base_dir: Optional[str] = None) -> Cfg:
    data = copy.deepcopy(DEFAULTS)
    base_dir = base_dir or os.getcwd()
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = deep_merge(data, yaml.safe_load(f) or {})
        base_dir = os.path.dirname(os.path.abspath(path))
        # config files live in config/, resolve relative paths from the repo root
        if os.path.basename(base_dir) == "config":
            base_dir = os.path.dirname(base_dir)
    data = deep_merge(data, _env_overrides())
    if overrides:
        data = deep_merge(data, overrides)
    data["_base_dir"] = base_dir
    persona = data["persona"]
    if not os.path.isabs(persona):
        persona = os.path.join(base_dir, persona)
    data["persona_dir"] = persona
    data["persona_name"] = os.path.basename(os.path.normpath(persona))
    data["data_dir"] = os.path.expanduser(str(data["data_dir"]).format(persona_name=data["persona_name"]))
    return Cfg(data)


def resolve_path(cfg: Cfg, p: str) -> str:
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(cfg.get("_base_dir", os.getcwd()), p)
