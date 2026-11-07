"""Kernel: owns runtime services and wires subsystems together.

    kernel = Kernel(cfg)            # or Kernel(cfg, clock=ManualClock(), llm_provider=MockProvider())
    await kernel.start()
    ...
    await kernel.stop()

Subsystems are built from factories ``module:function`` (each package exposes ``build(kernel)``)
and can be overridden per kernel (``overrides={"affect": instance_or_factory}``) for tests.
Start order respects dependencies; stop order is the reverse.

Identity merges (docs/22 §3.3, INV-27): every ``world.merged`` event re-keys all data stored under the absorbed
entity id (alive/world/rekey.py). Merges that have no ``world.rekeyed`` follow-up yet (declared in config at this
start, or interrupted by a crash) are migrated at start, before any subsystem loads its state.
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Callable, Optional

from alive.config import Cfg, load_config, resolve_path
from alive.core.types import EV
from alive.runtime.bus import EventBus
from alive.runtime.clock import Clock, make_clock
from alive.runtime.embed import Embedder, make_embedder
from alive.runtime.llm import ModelRouter, OpenAICompatProvider, Provider, load_llm_secrets
from alive.runtime.scheduler import Scheduler
from alive.runtime.store import Database
from alive.runtime.texture import LifeTexture, means_from_big_five
from alive.world import World
from alive.world import rekey as world_rekey

log = logging.getLogger("alive.kernel")

# name -> default factory; order = start order
FACTORIES: dict = {
    "gov": "alive.governance:build",
    "affect": "alive.affect:build",
    "motivation": "alive.motivation:build",
    "memory": "alive.memory:build",
    "growth": "alive.growth:build",
    "selfm": "alive.self_model:build",
    "social": "alive.social:build",
    "mind": "alive.core.mind:build",
}


def _load_factory(spec: str) -> Callable:
    mod, _, attr = spec.partition(":")
    return getattr(importlib.import_module(mod), attr or "build")


class Kernel:
    gov: Any
    affect: Any
    motivation: Any
    memory: Any
    growth: Any
    selfm: Any
    social: Any
    mind: Any

    def __init__(self, cfg: Optional[Cfg] = None, *, clock: Optional[Clock] = None,
                 llm_provider: Optional[Provider] = None, embedder: Optional[Embedder] = None,
                 overrides: Optional[dict] = None, only: Optional[list] = None, db_path: Optional[str] = None):
        """
        only: build just these subsystems (others set to None) - for isolated tests.
        overrides: {name: instance | factory(kernel) | "module:attr"}.
        """
        self.cfg = cfg or load_config()
        self.clock = clock or make_clock(self.cfg.get("clock.kind", "real"), scale=self.cfg.get("clock.scale", 60.0),
                                         start=self.cfg.get("clock.start"), tz=self.cfg.get("timezone"))
        data_dir = self.cfg.get("data_dir")
        self.data_dir = data_dir
        self.db = Database(db_path or os.path.join(data_dir, "alive.db"), readers=self.cfg.get("db.readers", 4))
        if hasattr(self.clock, "add_settle_hook"):  # ManualClock: make advance() wait for DB threads
            self.clock.add_settle_hook(self.db.wait_idle)
        self.bus = EventBus(self.db, self.clock)
        self.scheduler = Scheduler(self.clock, self.bus)
        self.embedder = embedder or make_embedder(self.cfg.get("embedder", {}))
        self._llm_provider = llm_provider
        self.llm: ModelRouter = None  # type: ignore
        self.texture: LifeTexture = None  # type: ignore
        self.world: World = World(self)
        self._overrides = overrides or {}
        self._only = only
        self._order: list = []
        self.started = False
        self._entity_hooks: list = []     # alive.world.rekey.register_entity_hook
        self._rekey_sub = None
        self.persona_dir = self.cfg.get("persona_dir")
        for name in FACTORIES:
            setattr(self, name, None)

    # ------------------------------------------------------------------ build
    def _build_llm(self) -> ModelRouter:
        lcfg = self.cfg.section("llm")
        tiers = lcfg.get("tiers", {})
        providers: dict = {}
        if self._llm_provider is not None:
            providers = {"main": self._llm_provider}
            for t in tiers.values():
                t["provider"] = "main"
        else:
            secrets = {}
            sf = lcfg.get("secrets_file")
            if sf and os.path.exists(resolve_path(self.cfg, sf)):
                secrets = load_llm_secrets(resolve_path(self.cfg, sf))
            for name, p in (lcfg.get("providers") or {}).items():
                base = p.get("base_url") or os.environ.get("ALIVE_LLM_BASE_URL") or secrets.get("base_url")
                key = p.get("api_key") or os.environ.get("ALIVE_LLM_API_KEY") or secrets.get("api_key")
                if not base or not key:
                    log.warning("LLM provider %s has no base_url/api_key; calls will fail", name)
                providers[name] = OpenAICompatProvider(name, base or "http://localhost", key or "")
        return ModelRouter(tiers, providers, clock=self.clock, pricing=lcfg.get("pricing"),
                           rps=lcfg.get("rps", 5.0), burst=lcfg.get("burst", 10.0),
                           max_retries=lcfg.get("max_retries", 3), bus=self.bus)

    def _build_texture(self) -> LifeTexture:
        """Life texture (docs/21 §4): trait means from the persona, seed from config or the persona name."""
        import yaml
        bf = {}
        try:
            with open(os.path.join(self.persona_dir or "", "temperament.yaml"), encoding="utf-8") as f:
                bf = (yaml.safe_load(f) or {}).get("big_five") or {}
        except Exception:
            pass
        tcfg = self.cfg.get("texture", {}) or {}
        seed = tcfg.get("seed")
        if seed is None:
            import zlib
            seed = zlib.crc32(str(self.cfg.get("persona_name", "alive")).encode("utf-8"))
        return LifeTexture(self, seed=int(seed), means=means_from_big_five(bf), config=tcfg)

    def _build_subsystem(self, name: str) -> Any:
        ov = self._overrides.get(name)
        if ov is not None:
            if isinstance(ov, str):
                return _load_factory(ov)(self)
            if isinstance(ov, type) or (callable(ov) and not hasattr(ov, "start")):
                return ov(self)  # class or factory function
            if hasattr(ov, "k"):
                ov.k = self      # pre-built instance
            return ov
        spec = self.cfg.get(f"subsystems.{name}") or FACTORIES[name]
        return _load_factory(spec)(self)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.started:
            return
        logging.getLogger("alive").setLevel(self.cfg.get("log_level", "INFO"))
        await self.db.start()
        self.llm = self._build_llm()
        self.texture = self._build_texture()
        await self.texture.start()
        await self.world.start()   # entities before subsystems: governance reads resolved owner ids
        for name in FACTORIES:
            if self._only is not None and name not in self._only and name not in self._overrides:
                continue
            inst = self._build_subsystem(name)
            setattr(self, name, inst)
            self._order.append(name)
        self.db.ensure_schema()  # schemas registered by subsystem modules on import
        await self._rekey_pending()   # all tables exist now; subsystems load already-migrated state
        self._rekey_sub = self.bus.subscribe(world_rekey.MERGED, self._on_world_merged, name="kernel.rekey")
        if self.gov is not None and hasattr(self.gov, "route_hook"):
            self.llm.add_hook(self.gov.route_hook)
        for name in self._order:
            inst = getattr(self, name)
            if inst is not None:
                await inst.start()
        self.started = True
        await self.bus.emit(EV.RT_START, {"persona": self.cfg.get("persona_name"), "subsystems": self._order})

    async def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        try:
            await self.bus.emit(EV.RT_STOP, {})
        except Exception:
            pass
        if self._rekey_sub is not None:   # let an in-flight merge migration finish while subsystems still run
            import asyncio
            try:
                await asyncio.wait_for(self._rekey_sub.queue.join(), 10.0)
            except Exception:
                log.warning("entity re-keying still running at stop")
        for name in reversed(self._order):
            inst = getattr(self, name)
            if inst is not None:
                try:
                    await inst.stop()
                except Exception:
                    log.exception("stop %s failed", name)
        try:
            await self.texture.save()
            await self.world.flush()
        except Exception:
            pass
        await self.scheduler.close()
        await self.bus.drain(2.0)
        await self.bus.close()
        if self.llm is not None:
            await self.llm.close()
        await self.db.close()

    # ------------------------------------------------------------------ identity merges (INV-27)
    async def _rekey_pending(self) -> None:
        for event_id, old, new in await world_rekey.pending_merges(self):
            try:
                await world_rekey.rekey(self, old, new, cause=event_id, hooks=False)
            except Exception:
                log.exception("migrating merged entity %s -> %s failed (retried at next start)", old, new)

    async def _on_world_merged(self, ev) -> None:
        p = ev.payload or {}
        if p.get("from") and p.get("into"):
            await world_rekey.rekey(self, p["from"], p["into"], cause=ev.id)

    async def rekey_entity(self, old: str, new: str) -> dict:
        """Move everything stored under entity ``old`` to ``new`` now (normally driven by ``world.merged``)."""
        return await world_rekey.rekey(self, old, new)

    def status(self) -> dict:
        out = {"now": self.clock.fmt(), "llm": self.llm.stats() if self.llm else {}, "db": self.db.stats,
               "texture": self.texture.status() if self.texture else {},
               "world": self.world.status() if self.world else {},
               "bus": self.bus.stats(), "tasks": self.scheduler.running()}
        for name in self._order:
            inst = getattr(self, name)
            try:
                out[name] = inst.status() if inst is not None else None
            except Exception as e:  # pragma: no cover
                out[name] = {"error": repr(e)}
        return out
