"""Model router and providers (docs/10 §4).

Concepts
- Callers never pick a model. They build an ``LLMRequest`` with a ``tier`` (fast/deliberate/deep)
  and a ``purpose`` tag (e.g. "s2.reply", "mem.extract"). The router maps tier -> provider/model
  using config, applies routing hooks (energy/budget downgrades), concurrency limits, rate
  limiting, retries, JSON parsing/repair and cost accounting.
- ``MockProvider`` makes every subsystem unit-testable without network: tests register
  handlers per purpose (prefix match) that return text or dicts.

Performance notes
- One shared aiohttp session with connection pooling (keep-alive) per provider.
- Per-tier semaphores bound concurrency; a token bucket bounds requests/second globally.
- Stable system prompts first: DeepSeek/OpenAI-style providers cache identical prefixes, which
  cuts cost and latency. Put per-call variable content at the *end* of the message list.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("alive.llm")


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------
@dataclass
class LLMRequest:
    purpose: str
    tier: str = "deliberate"
    system: str = ""                       # stable prefix (cache friendly)
    messages: list = field(default_factory=list)   # [{"role": "user"|"assistant", "content": str}]
    json: bool = False
    schema: Optional[dict] = None          # {"field": default_value} -> missing keys filled, types coerced
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    thread_id: Optional[str] = None
    identity_critical: bool = False        # never downgraded below deliberate
    priority: int = 5                      # lower = more important (used when budget is tight)
    timeout: Optional[float] = None        # real seconds
    allow_downgrade: bool = True

    def prompt_hash(self) -> str:
        h = hashlib.sha1()
        h.update(self.system.encode("utf-8"))
        for m in self.messages:
            h.update(str(m.get("content", "")).encode("utf-8"))
        return h.hexdigest()[:16]


@dataclass
class LLMResponse:
    text: str
    data: Optional[dict] = None
    model: str = ""
    tier: str = ""
    usage: dict = field(default_factory=dict)  # prompt, completion, cache_hit, cache_miss, reasoning
    cost_usd: float = 0.0
    latency: float = 0.0
    reasoning: Optional[str] = None
    purpose: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class LLMError(Exception):
    def __init__(self, msg: str, *, retryable: bool = False, status: int = 0):
        super().__init__(msg)
        self.retryable = retryable
        self.status = status


class BudgetDenied(Exception):
    pass


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)


def parse_json_loose(text: str) -> Optional[Any]:
    """Parse JSON from model output: strips code fences, finds the outermost object/array."""
    if text is None:
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    t2 = _FENCE.sub("", t).strip()
    try:
        return json.loads(t2)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t2.find(open_c), t2.rfind(close_c)
        if i != -1 and j > i:
            frag = t2[i:j + 1]
            try:
                return json.loads(frag)
            except Exception:
                # common repairs: trailing commas, python literals
                frag2 = re.sub(r",\s*([}\]])", r"\1", frag)
                frag2 = frag2.replace("True", "true").replace("False", "false").replace("None", "null")
                try:
                    return json.loads(frag2)
                except Exception:
                    continue
    return None


def apply_schema(data: Any, schema: Optional[dict]) -> Any:
    """Fill defaults and coerce simple types. Schema maps field -> default value (type inferred)."""
    if schema is None or not isinstance(data, dict):
        return data
    out = dict(data)
    for k, default in schema.items():
        if k not in out or out[k] is None:
            out[k] = json.loads(json.dumps(default)) if isinstance(default, (list, dict)) else default
            continue
        v = out[k]
        try:
            if isinstance(default, bool) and not isinstance(v, bool):
                out[k] = str(v).lower() in ("1", "true", "yes", "y", "是")
            elif isinstance(default, float) and not isinstance(v, float):
                out[k] = float(v)
            elif isinstance(default, int) and not isinstance(default, bool) and not isinstance(v, int):
                out[k] = int(float(v))
            elif isinstance(default, str) and not isinstance(v, str):
                out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            elif isinstance(default, list) and not isinstance(v, list):
                out[k] = [v] if v not in ("", None) else []
            elif isinstance(default, dict) and not isinstance(v, dict):
                out[k] = dict(default)
        except Exception:
            out[k] = default
    return out


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class Provider:
    name = "provider"

    async def complete(self, req: LLMRequest, tier_cfg: dict) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


class OpenAICompatProvider(Provider):
    """OpenAI-compatible /chat/completions (DeepSeek, OpenAI, vLLM, Ollama, LM Studio, ...).

    tier_cfg keys: model, max_tokens, temperature, reasoning_effort (low|high|max),
    thinking ("disabled"|"enabled"|None), extra (dict merged into payload), timeout.
    """

    def __init__(self, name: str, base_url: str, api_key: str, *, proxy: Optional[str] = None,
                 max_connections: int = 32):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.proxy = proxy
        self.max_connections = max_connections
        self._session = None

    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            conn = aiohttp.TCPConnector(limit=self.max_connections, ttl_dns_cache=300, keepalive_timeout=60)
            self._session = aiohttp.ClientSession(connector=conn, trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def build_payload(self, req: LLMRequest, cfg: dict) -> dict:
        msgs = []
        if req.system:
            msgs.append({"role": "system", "content": req.system})
        msgs.extend(req.messages)
        payload: dict = {"model": cfg["model"], "messages": msgs}
        mt = req.max_tokens or cfg.get("max_tokens")
        if mt:
            mt = int(mt)
            # reasoning models spend completion tokens on hidden reasoning: reserve room so a small
            # max_tokens for the visible answer does not come back empty
            if cfg.get("reasoning_effort") and cfg.get("thinking") != "disabled":
                reserve = int(cfg.get("reasoning_reserve", {"low": 600, "high": 2000, "max": 4000}.get(
                    str(cfg.get("reasoning_effort")), 1000)))
                if req.max_tokens:
                    mt = req.max_tokens + reserve
            payload["max_tokens"] = mt
        temp = req.temperature if req.temperature is not None else cfg.get("temperature")
        if temp is not None:
            payload["temperature"] = float(temp)
        if req.json:
            payload["response_format"] = {"type": "json_object"}
        if cfg.get("thinking") == "disabled":
            payload["thinking"] = {"type": "disabled"}
        elif cfg.get("reasoning_effort"):
            payload["reasoning_effort"] = cfg["reasoning_effort"]
        if cfg.get("extra"):
            payload.update(cfg["extra"])
        return payload

    async def complete(self, req: LLMRequest, cfg: dict) -> LLMResponse:
        import aiohttp
        sess = await self._sess()
        payload = self.build_payload(req, cfg)
        timeout = req.timeout or cfg.get("timeout", 120)
        t0 = time.monotonic()
        try:
            async with sess.post(f"{self.base_url}/chat/completions", json=payload,
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 timeout=aiohttp.ClientTimeout(total=timeout), proxy=self.proxy) as r:
                body = await r.text()
                if r.status != 200:
                    retry = r.status in (408, 409, 425, 429, 500, 502, 503, 504, 529)
                    raise LLMError(f"HTTP {r.status}: {body[:300]}", retryable=retry, status=r.status)
        except asyncio.TimeoutError as e:
            raise LLMError("timeout", retryable=True) from e
        except aiohttp.ClientError as e:
            raise LLMError(f"client error: {e}", retryable=True) from e
        try:
            d = json.loads(body)
            ch = d["choices"][0]["message"]
        except Exception as e:
            raise LLMError(f"bad response: {body[:300]}", retryable=True) from e
        u = d.get("usage") or {}
        usage = {
            "prompt": u.get("prompt_tokens", 0),
            "completion": u.get("completion_tokens", 0),
            "cache_hit": u.get("prompt_cache_hit_tokens", (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0,
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
        }
        usage["cache_miss"] = max(0, usage["prompt"] - usage["cache_hit"])
        finish = d["choices"][0].get("finish_reason")
        if not (ch.get("content") or "").strip() and finish == "length":
            raise LLMError("empty content (length) - reasoning consumed the budget", retryable=True, status=-1)
        return LLMResponse(text=ch.get("content") or "", model=d.get("model", cfg["model"]),
                           usage=usage, latency=time.monotonic() - t0,
                           reasoning=ch.get("reasoning_content"))


MockHandler = Callable[[LLMRequest], Any]


class MockProvider(Provider):
    """Deterministic provider for tests.

    ``on(prefix, handler)`` registers a handler for requests whose purpose starts with prefix
    (longest prefix wins). A handler returns str, dict/list (serialised to JSON) or raises.
    Unmatched requests return ``default`` (JSON "{}" for json requests, "嗯" otherwise).
    All requests are recorded in ``calls``.
    """
    name = "mock"

    def __init__(self, latency: float = 0.0):
        self.handlers: dict = {}
        self.calls: list = []
        self.latency = latency

    def on(self, prefix: str, handler) -> "MockProvider":
        self.handlers[prefix] = handler if callable(handler) else (lambda req, _v=handler: _v)
        return self

    def _find(self, purpose: str):
        best = None
        for p in self.handlers:
            if purpose.startswith(p) and (best is None or len(p) > len(best)):
                best = p
        return self.handlers.get(best) if best is not None else None

    async def complete(self, req: LLMRequest, cfg: dict) -> LLMResponse:
        self.calls.append(req)
        if self.latency:
            await asyncio.sleep(self.latency)
        h = self._find(req.purpose)
        out = h(req) if h else ({} if req.json else "嗯")
        if asyncio.iscoroutine(out):
            out = await out
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        n_in = sum(len(str(m.get("content", ""))) for m in req.messages) + len(req.system)
        return LLMResponse(text=text, model=f"mock-{req.tier}",
                           usage={"prompt": n_in // 2, "completion": len(text) // 2, "cache_hit": 0,
                                  "cache_miss": n_in // 2, "reasoning": 0})

    def calls_for(self, prefix: str) -> list:
        return [c for c in self.calls if c.purpose.startswith(prefix)]


# ---------------------------------------------------------------------------
# Rate limiting & accounting
# ---------------------------------------------------------------------------
class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate = max(0.01, rate)
        self.capacity = max(1.0, burst)
        self.tokens = self.capacity
        self.t = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown: float = 30.0):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.open_until = 0.0

    def allow(self) -> bool:
        return time.monotonic() >= self.open_until

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.monotonic() + self.cooldown
            self.failures = 0


@dataclass
class Pricing:
    input_miss: float = 0.0   # USD per 1M tokens
    input_hit: float = 0.0
    output: float = 0.0

    def cost(self, usage: dict) -> float:
        return (usage.get("cache_miss", 0) * self.input_miss + usage.get("cache_hit", 0) * self.input_hit
                + usage.get("completion", 0) * self.output) / 1_000_000


class CostLedger:
    """In-memory running totals; persisted via rt.llm_call events."""

    def __init__(self, clock):
        self.clock = clock
        self.day = ""
        self.today_usd = 0.0
        self.hour_key = ""
        self.hour_usd = 0.0
        self.total_usd = 0.0
        self.by_purpose: dict = {}
        self.by_tier: dict = {}
        self.calls = 0
        self.tokens = {"prompt": 0, "completion": 0, "cache_hit": 0}

    def _roll(self) -> None:
        d = self.clock.today()
        if d != self.day:
            self.day, self.today_usd = d, 0.0
        hk = self.clock.local().strftime("%Y-%m-%d %H")
        if hk != self.hour_key:
            self.hour_key, self.hour_usd = hk, 0.0

    def add(self, resp: LLMResponse) -> None:
        self._roll()
        self.calls += 1
        self.today_usd += resp.cost_usd
        self.hour_usd += resp.cost_usd
        self.total_usd += resp.cost_usd
        p = resp.purpose.split(":")[0]
        self.by_purpose[p] = self.by_purpose.get(p, 0.0) + resp.cost_usd
        self.by_tier[resp.tier] = self.by_tier.get(resp.tier, 0.0) + resp.cost_usd
        for k in self.tokens:
            self.tokens[k] += resp.usage.get(k, 0)

    def snapshot(self) -> dict:
        self._roll()
        return {"day": self.day, "today_usd": round(self.today_usd, 5), "hour_usd": round(self.hour_usd, 5),
                "total_usd": round(self.total_usd, 5), "calls": self.calls, "tokens": dict(self.tokens),
                "by_tier": {k: round(v, 5) for k, v in self.by_tier.items()},
                "by_purpose": {k: round(v, 5) for k, v in sorted(self.by_purpose.items(), key=lambda x: -x[1])}}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
TIER_ORDER = ["fast", "deliberate", "deep"]

# A routing hook may change the tier (e.g. tired -> shallower), or raise BudgetDenied.
RouteHook = Callable[[LLMRequest, str], str]

# Per-request temperature for composition (docs/10 §4.2, ADR-0007). Coefficients: playfulness (texture value 0..1,
# persona mean ~0.6) moves T by +-0.075 at the extremes; arousal relative to my baseline (-1..1, typically +-0.3)
# by ~+-0.03. Bounds keep replies coherent (min) and well inside the provider's range (max).
STATE_TEMPERATURE = {"purposes": ("s2.",), "playfulness": 0.15, "arousal": 0.10, "min": 0.3, "max": 1.2}


class ModelRouter:
    def __init__(self, tiers: dict, providers: dict, *, clock, pricing: Optional[dict] = None,
                 rps: float = 5.0, burst: float = 10.0, max_retries: int = 3, bus=None):
        """
        tiers: {"fast": {"provider": "deepseek", "model": "...", "concurrency": 8, ...}, ...}
        providers: {"deepseek": Provider}
        pricing: {"model-name": {"input_miss":..,"input_hit":..,"output":..}}
        """
        self.tiers = tiers
        self.providers = providers
        self.clock = clock
        self.pricing = {m: Pricing(**p) for m, p in (pricing or {}).items()}
        self.bucket = TokenBucket(rps, burst)
        self.max_retries = max_retries
        self.sems = {t: asyncio.Semaphore(int(c.get("concurrency", 4))) for t, c in tiers.items()}
        self.breakers = {name: CircuitBreaker() for name in providers}
        self.ledger = CostLedger(clock)
        self.hooks: list = []
        self.bus = bus
        self.inflight = 0
        self.latency_ms: dict = {t: [] for t in tiers}
        self.state_source: Optional[Callable[[], dict]] = None
        self.state_temperature: dict = dict(STATE_TEMPERATURE)

    def add_hook(self, hook: RouteHook) -> None:
        self.hooks.append(hook)

    def set_state_source(self, fn: Optional[Callable[[], dict]], **params) -> None:
        """Register the individual's state for per-request temperatures (the affect system does this at start):
        ``fn() -> {"playfulness": 0..1 (texture), "arousal": deviation from baseline}``. ``params`` override
        STATE_TEMPERATURE (purposes, playfulness, arousal, min, max)."""
        self.state_source = fn
        self.state_temperature.update({k: v for k, v in params.items() if k in STATE_TEMPERATURE})

    def resolve_temperature(self, req: LLMRequest, tier: str) -> Optional[float]:
        """Sampling temperature for this request. An explicit ``req.temperature`` always wins (per-request
        override). For composition purposes the tier's base follows my state:
            T = base + 0.15 (playfulness - 0.5) + 0.10 arousal_dev,  clipped to [0.3, 1.2]
        (a playful, stirred-up day speaks a little more unpredictably; a flat one more plainly). Everything else
        returns None: the provider uses the tier's configured temperature."""
        if req.temperature is not None:
            return float(req.temperature)
        base = (self.tiers.get(tier) or {}).get("temperature")
        st_cfg = self.state_temperature
        if base is None or self.state_source is None or not any(
                req.purpose.startswith(p) for p in (st_cfg.get("purposes") or ())):
            return None
        try:
            st = self.state_source() or {}
        except Exception:
            st = {}
        t = float(base)
        if st.get("playfulness") is not None:
            t += float(st_cfg["playfulness"]) * (float(st["playfulness"]) - 0.5)
        if st.get("arousal") is not None:
            t += float(st_cfg["arousal"]) * float(st["arousal"])
        return max(float(st_cfg["min"]), min(float(st_cfg["max"]), t))

    def resolve_tier(self, req: LLMRequest) -> str:
        tier = req.tier if req.tier in self.tiers else "deliberate"
        for h in self.hooks:
            new = h(req, tier)
            if new and new in self.tiers:
                if not req.allow_downgrade and TIER_ORDER.index(new) < TIER_ORDER.index(tier):
                    continue
                if req.identity_critical and TIER_ORDER.index(new) < TIER_ORDER.index("deliberate"):
                    new = "deliberate"
                tier = new
        return tier

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Route and execute. Raises BudgetDenied or LLMError after retries."""
        tier = self.resolve_tier(req)
        cfg = self.tiers[tier]
        temp = self.resolve_temperature(req, tier)
        prov = self.providers[cfg.get("provider", next(iter(self.providers)))]
        breaker = self.breakers.setdefault(prov.name, CircuitBreaker())
        attempt = 0
        json_retry_done = False
        last_err: Optional[Exception] = None
        async with self.sems[tier]:
            while attempt <= self.max_retries:
                if not breaker.allow():
                    await asyncio.sleep(1.0)
                    attempt += 1
                    continue
                await self.bucket.take()
                self.inflight += 1
                try:
                    # the caller's request is not mutated: retries/rewrites built from it keep "no override"
                    sent = req if (temp is None or req.temperature is not None) else replace(req, temperature=temp)
                    resp = await prov.complete(sent, cfg)
                    breaker.success()
                except LLMError as e:
                    last_err = e
                    if e.status == -1:      # truncated by hidden reasoning: retry with a bigger budget
                        req = LLMRequest(**{**req.__dict__, "max_tokens": int((req.max_tokens or cfg.get("max_tokens") or 1000) * 2)})
                        attempt += 1
                        continue
                    breaker.failure()
                    if not e.retryable:
                        raise
                    attempt += 1
                    await asyncio.sleep(min(20.0, (2 ** attempt) * 0.5 + random.random()))
                    continue
                finally:
                    self.inflight -= 1
                resp.tier, resp.purpose = tier, req.purpose
                price = self.pricing.get(resp.model) or self.pricing.get(cfg.get("model", ""))
                resp.cost_usd = price.cost(resp.usage) if price else 0.0
                self.ledger.add(resp)
                lat = self.latency_ms.setdefault(tier, [])
                lat.append(resp.latency * 1000)
                if len(lat) > 200:
                    del lat[:100]
                if req.json:
                    data = parse_json_loose(resp.text)
                    if not isinstance(data, (dict, list)) and not json_retry_done:
                        json_retry_done = True
                        req = LLMRequest(**{**req.__dict__, "messages": req.messages + [
                            {"role": "assistant", "content": resp.text[:2000]},
                            {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法的 JSON 对象，不要任何其他文字。"}]})
                        continue
                    if isinstance(data, list) and req.schema is not None:
                        data = {"items": data}
                    resp.data = apply_schema(data if isinstance(data, dict) else {}, req.schema) \
                        if (data is not None or req.schema is not None) else None
                    if data is None:
                        resp.error = "json_parse_failed"
                await self._record(req, resp)
                return resp
        raise LLMError(f"exhausted retries: {last_err}", retryable=False)

    async def _record(self, req: LLMRequest, resp: LLMResponse) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit("rt.llm_call", {
                "purpose": req.purpose, "tier": resp.tier, "model": resp.model, "usage": resp.usage,
                "latency_ms": int(resp.latency * 1000), "hash": req.prompt_hash(), "error": resp.error,
            }, source="llm", thread_id=req.thread_id, cost={"usd": round(resp.cost_usd, 6)})
        except Exception:  # never fail a call because accounting failed
            log.debug("failed to record llm call", exc_info=True)

    async def text(self, purpose: str, prompt: str, *, tier: str = "fast", system: str = "", **kw) -> str:
        """Small helper for one-shot text completions."""
        r = await self.complete(LLMRequest(purpose=purpose, tier=tier, system=system,
                                           messages=[{"role": "user", "content": prompt}], **kw))
        return r.text.strip()

    async def json(self, purpose: str, prompt: str, *, tier: str = "fast", system: str = "",
                   schema: Optional[dict] = None, **kw) -> dict:
        """One-shot JSON completion. Always returns a dict (schema defaults on failure)."""
        r = await self.complete(LLMRequest(purpose=purpose, tier=tier, system=system, json=True,
                                           schema=schema, messages=[{"role": "user", "content": prompt}], **kw))
        return r.data if isinstance(r.data, dict) else apply_schema({}, schema) or {}

    def stats(self) -> dict:
        def p50(xs):
            return round(sorted(xs)[len(xs) // 2], 1) if xs else None
        return {"inflight": self.inflight, "ledger": self.ledger.snapshot(),
                "latency_p50_ms": {t: p50(v) for t, v in self.latency_ms.items()}}

    async def close(self) -> None:
        for p in self.providers.values():
            await p.close()


def load_llm_secrets(path: str) -> dict:
    """Read the simple 3-line secrets file: base_url, api_key, model (extra lines ignored)."""
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    out = {}
    if lines:
        out["base_url"] = lines[0]
    if len(lines) > 1:
        out["api_key"] = lines[1]
    if len(lines) > 2:
        out["model"] = lines[2]
    return out
