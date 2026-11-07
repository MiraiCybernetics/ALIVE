"""Inspector HTTP server (docs/10 §6, docs/09 §6): the owner's window into the individual.

GET  /                 tiny auto-refreshing dashboard
GET  /api/status       kernel.status() (all subsystems, llm ledger, db, bus, tasks)
GET  /api/state        "what is it thinking / feeling / planning right now"
GET  /api/events       ?type=prefix&thread=&limit=50
POST /api/freeze       kill switch on   (governance)        POST /api/unfreeze
POST /api/say          {"text": "...", "as": "owner"} inject a message through the http channel (if social supports it)

Binds to 127.0.0.1 by default. If ``http.token`` is set, requests need ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("alive.http")

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>ALIVE</title>
<meta http-equiv="refresh" content="10">
<style>body{font-family:system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 12px;color:#222}
pre{background:#f6f6f6;padding:10px;border-radius:6px;white-space:pre-wrap}h2{margin-top:28px}</style></head>
<body><h1>ALIVE · %(name)s</h1><p>%(now)s · mode <b>%(mode)s</b></p>
<h2>此刻</h2><pre>%(state)s</pre>
<h2>工作空间</h2><pre>%(ws)s</pre>
<h2>最近的想法</h2><pre>%(thoughts)s</pre>
<h2>成本</h2><pre>%(cost)s</pre></body></html>"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Inspector:
    def __init__(self, kernel, host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = None):
        self.k = kernel
        self.host, self.port, self.token = host, port, token
        self._runner = None

    async def start(self) -> None:
        from aiohttp import web

        @web.middleware
        async def auth(request, handler):
            if self.token and request.headers.get("Authorization") != f"Bearer {self.token}":
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[auth])
        app.router.add_get("/", self.page)
        app.router.add_get("/api/status", self.status)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/events", self.events)
        app.router.add_post("/api/freeze", self.freeze)
        app.router.add_post("/api/unfreeze", self.unfreeze)
        app.router.add_post("/api/say", self.say)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("inspector on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @staticmethod
    def _json(data, status: int = 200):
        from aiohttp import web
        return web.Response(text=json.dumps(data, ensure_ascii=False, default=str, indent=1),
                            content_type="application/json", status=status)

    def _state(self) -> dict:
        k = self.k
        out = {"now": k.clock.fmt(), "name": k.selfm.name() if k.selfm else "",
               "mode": k.mind.mode() if k.mind else "", "self": k.selfm.state_text() if k.selfm else ""}
        if k.affect is not None:
            out["affect"] = {**k.affect.labels(), "snapshot": k.affect.snapshot().__dict__}
        if k.motivation is not None:
            out["needs"] = k.motivation.needs()
        if k.mind is not None and hasattr(k.mind, "ws"):
            out["workspace"] = k.mind.ws.snapshot()
        out["cost"] = k.llm.ledger.snapshot() if k.llm else {}
        return out

    async def page(self, request):
        from aiohttp import web
        st = self._state()
        thoughts = await self.k.bus.query(types=["core.thought"], limit=8, desc=True)
        html = _PAGE % {
            "name": _esc(st.get("name", "")), "now": _esc(st["now"]), "mode": _esc(st.get("mode", "")),
            "state": _esc(st.get("self", "")),
            "ws": _esc("\n".join(f"{i['salience']:.2f}  [{i['kind']}] {i['text']}"
                                 for i in st.get("workspace", {}).get("items", []))),
            "thoughts": _esc("\n".join(f"{self.k.clock.local(e.ts).strftime('%m-%d %H:%M')} [{e.payload.get('kind')}] "
                                       f"{e.payload.get('text', '')}" for e in thoughts)),
            "cost": _esc(json.dumps(st.get("cost", {}), ensure_ascii=False, indent=1)),
        }
        return web.Response(text=html, content_type="text/html")

    async def status(self, request):
        return self._json(self.k.status())

    async def state(self, request):
        return self._json(self._state())

    async def events(self, request):
        q = request.query
        limit = min(500, int(q.get("limit", 50)))
        evs = await self.k.bus.query(type_prefix=q.get("type"), thread_id=q.get("thread"), limit=limit, desc=True)
        return self._json([{"id": e.id, "ts": e.ts, "time": self.k.clock.fmt(e.ts), "type": e.type,
                            "thread": e.thread_id, "payload": e.payload} for e in evs])

    async def freeze(self, request):
        g = self.k.gov
        if g is not None and hasattr(g, "freeze"):
            r = g.freeze()
            if hasattr(r, "__await__"):
                await r
            return self._json({"frozen": True})
        return self._json({"error": "governance has no freeze()"}, 501)

    async def unfreeze(self, request):
        g = self.k.gov
        if g is not None and hasattr(g, "unfreeze"):
            r = g.unfreeze()
            if hasattr(r, "__await__"):
                await r
            return self._json({"frozen": False})
        return self._json({"error": "governance has no unfreeze()"}, 501)

    async def say(self, request):
        body = await request.json()
        k = self.k
        from alive.core.types import IncomingMessage, new_id
        msg = IncomingMessage(channel="http", chat_id=str(body.get("chat", "owner")), chat_type="private",
                              sender_id=str(body.get("as", "owner")), sender_name=str(body.get("name", body.get("as", "owner"))),
                              text=str(body.get("text", "")), msg_id=new_id("h"), ts=k.clock.now())
        await k.social.submit_incoming(msg)
        return self._json({"ok": True, "thread": msg.thread_id})
