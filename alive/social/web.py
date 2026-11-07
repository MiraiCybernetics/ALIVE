"""Web participation (docs/07 §7): reader-isolated page reading and feed polling.

Isolation (docs/09 §3.2, INV-13/14): the reader LLM call (purpose ``soc.reader``, fast tier) receives ONLY
the extracted page text and a fixed instruction — no identity, no memories, no tools, no chat. Its output is
returned wrapped by ``gov.wrap_untrusted`` and marked trust 0; the page text never reaches a prompt that
also holds private data.

Fetch limits: http(s) only, private/loopback addresses refused unless ``web.allow_private``, total timeout,
byte cap, content-type allowlist. Feeds: RSS 2.0, RSS 1.0 (RDF) and Atom via ElementTree; documents with a
DOCTYPE/ENTITY declaration are rejected (entity-expansion defence).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

from alive.core.types import EV, PermTier, Trust
from alive.social import prompts

log = logging.getLogger("alive.social")

HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = {"text/plain", "text/markdown"}
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "application/rdf+xml",
              "application/feed+json"}
SKIP_TAGS = {"script", "style", "nav", "noscript", "svg", "header", "footer", "aside", "form", "iframe", "template",
             "button", "select", "canvas", "object", "embed"}
BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
              "article", "blockquote", "pre", "dd", "dt", "hr", "main", "figcaption"}
VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "area", "base", "col", "embed", "source", "track", "wbr"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self.title = ""
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in SKIP_TAGS:
            self._skip += 1
        elif t == "title":
            self._in_title = True
        if t in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in SKIP_TAGS and self._skip > 0:
            self._skip -= 1
        elif t == "title":
            self._in_title = False
        if t in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self.parts.append(data)


def extract_text(html_text: str, max_chars: int = 12000) -> tuple:
    """(title, readable text). Strips script/style/nav/header/footer/aside/forms; collapses whitespace."""
    p = _TextExtractor()
    try:
        p.feed(html_text or "")
        p.close()
    except Exception:  # malformed markup: keep what we got
        pass
    raw = "".join(p.parts)
    lines = []
    for line in raw.split("\n"):
        line = re.sub(r"[ \t\r\f\v 　]+", " ", line).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    return " ".join(p.title.split())[:200], text[:max_chars]


def strip_html(s: str, max_chars: int = 600) -> str:
    if not s:
        return ""
    if "<" in s:
        _, t = extract_text(s, max_chars * 2)
        s = t.replace("\n", " ")
    else:
        s = html.unescape(s)
    s = " ".join(s.split())
    return s[:max_chars]


# --------------------------------------------------------------------------- feeds
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"


class FeedError(Exception):
    pass


def _txt(el, path: str) -> str:
    if el is None:
        return ""
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_feed(data) -> tuple:
    """Parse RSS 2.0 / RSS 1.0 / Atom. Returns (feed_title, [{"id","title","url","summary","published"}])."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise FeedError("DTD/ENTITY declarations are not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"bad xml: {e}") from e
    items = []
    tag = root.tag.lower()
    if tag.endswith("rss") or root.find("channel") is not None and not tag.startswith(ATOM.lower()):
        ch = root.find("channel")
        title = _txt(ch, "title")
        for it in (ch.findall("item") if ch is not None else []):
            link = _txt(it, "link")
            guid = _txt(it, "guid") or link or _txt(it, "title")
            summ = _txt(it, CONTENT + "encoded") or _txt(it, "description")
            items.append({"id": guid, "title": strip_html(_txt(it, "title"), 200), "url": link,
                          "summary": strip_html(summ, 600), "published": _txt(it, "pubDate") or _txt(it, DC + "date")})
        return title, items
    if root.tag == ATOM + "feed":
        title = _txt(root, ATOM + "title")
        for e in root.findall(ATOM + "entry"):
            url = ""
            for ln in e.findall(ATOM + "link"):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" and ln.get("href"):
                    url = ln.get("href")
                    break
                if not url and ln.get("href"):
                    url = ln.get("href")
            summ = _txt(e, ATOM + "summary") or _txt(e, ATOM + "content")
            items.append({"id": _txt(e, ATOM + "id") or url, "title": strip_html(_txt(e, ATOM + "title"), 200),
                          "url": url, "summary": strip_html(summ, 600),
                          "published": _txt(e, ATOM + "updated") or _txt(e, ATOM + "published")})
        return title, items
    if tag.endswith("rdf"):
        ch = root.find(RSS1 + "channel")
        title = _txt(ch, RSS1 + "title")
        for it in root.findall(RSS1 + "item"):
            link = _txt(it, RSS1 + "link")
            items.append({"id": it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or link,
                          "title": strip_html(_txt(it, RSS1 + "title"), 200), "url": link,
                          "summary": strip_html(_txt(it, RSS1 + "description"), 600),
                          "published": _txt(it, DC + "date")})
        return title, items
    raise FeedError(f"unknown feed root {root.tag}")


# --------------------------------------------------------------------------- fetching
class FetchError(Exception):
    pass


def _host_is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".local")
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


class WebReader:
    def __init__(self, kernel, cfg):
        self.k = kernel
        self.cfg = cfg
        self.timeout = float(cfg.get("timeout_s", 15.0))
        self.max_bytes = int(cfg.get("max_bytes", 2_000_000))
        self.max_chars = int(cfg.get("reader_max_chars", 12000))
        self.allow_private = bool(cfg.get("allow_private", False))
        self.user_agent = str(cfg.get("user_agent", "Mozilla/5.0 (compatible; ALIVE-reader/0.1)"))
        self.cache_ttl = float(cfg.get("cache_ttl_s", 3600.0))
        self._cache: "OrderedDict[str, tuple]" = OrderedDict()
        self._sem = asyncio.Semaphore(int(cfg.get("concurrency", 3)))
        self._session = None
        self._feeds_seen_loaded: set = set()
        self._seen: dict = {}    # feed -> set(item ids)

    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _check_url(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise FetchError("only http(s) URLs are allowed")
        if self.allow_private:
            return
        if _host_is_private(u.hostname):
            raise FetchError("private address refused")
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        except OSError as e:
            raise FetchError(f"dns: {e}") from e
        for info in infos:
            if _host_is_private(info[4][0]):
                raise FetchError("host resolves to a private address")

    async def fetch(self, url: str, accept: set) -> tuple:
        """(content_type, text, final_url). Raises FetchError."""
        import aiohttp
        await self._check_url(url)
        sess = await self._sess()
        timeout = aiohttp.ClientTimeout(total=self.timeout, sock_connect=min(10.0, self.timeout))
        async with self._sem:
            try:
                async with sess.get(url, timeout=timeout, allow_redirects=True, max_redirects=5,
                                    headers={"User-Agent": self.user_agent,
                                             "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5"}
                                    ) as r:
                    if r.status >= 400:
                        raise FetchError(f"HTTP {r.status}")
                    final = str(r.url)
                    if not self.allow_private and r.url.host and _host_is_private(r.url.host):
                        raise FetchError("redirected to a private address")
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype not in accept:
                        raise FetchError(f"content-type {ctype or '?'} not allowed")
                    if r.content_length and r.content_length > self.max_bytes:
                        raise FetchError("too large")
                    buf = bytearray()
                    async for chunk in r.content.iter_chunked(65536):
                        buf += chunk
                        if len(buf) > self.max_bytes:
                            buf = buf[: self.max_bytes]
                            break
                    charset = r.charset
            except asyncio.TimeoutError as e:
                raise FetchError("timeout") from e
            except aiohttp.ClientError as e:
                raise FetchError(f"client error: {e}") from e
        if not charset:
            m = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", bytes(buf[:4096]), re.I)
            charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
        try:
            text = bytes(buf).decode(charset, errors="replace")
        except LookupError:
            text = bytes(buf).decode("utf-8", errors="replace")
        return ctype, text, final

    # ------------------------------------------------------------------ read_url
    def _empty(self, url: str, error: str) -> dict:
        return {"title": "", "summary": "", "points": [], "url": url, "trust": 0, "error": error}

    async def read_url(self, url: str, purpose: str = "curiosity") -> dict:
        k = self.k
        now = k.clock.now()
        hit = self._cache.get(url)
        if hit and now - hit[0] < self.cache_ttl:
            return dict(hit[1])
        if k.gov is not None:
            from alive.interfaces import ActRequest
            d = await k.gov.check_action(ActRequest(kind="fetch_url", perm=int(PermTier.T0_READ), text=url,
                                                    session_caps={"untrusted"}, initiated_by="internal",
                                                    meta={"url": url, "purpose": purpose}))
            if not d.allow:
                return self._empty(url, f"denied:{d.reason}")
        try:
            ctype, body, final = await self.fetch(url, HTML_TYPES | TEXT_TYPES | FEED_TYPES)
        except FetchError as e:
            return self._empty(url, str(e))
        if ctype in HTML_TYPES:
            title, text = extract_text(body, self.max_chars)
        elif ctype in FEED_TYPES:
            try:
                ftitle, items = parse_feed(body)
            except FeedError as e:
                return self._empty(url, str(e))
            title = ftitle
            text = "\n".join(f"{it['title']}：{it['summary']}" for it in items[:20])[: self.max_chars]
        else:
            title, text = "", body[: self.max_chars]
        if not text.strip():
            return self._empty(url, "empty")
        data = await self.summarize(text, thread_hint=None)
        summary = str(data.get("summary") or "").strip()
        points = [str(p)[:120] for p in (data.get("points") or []) if str(p).strip()][:6]
        rtitle = str(data.get("title") or title or "").strip()[:200]
        wrapped = k.gov.wrap_untrusted(summary, url) if k.gov is not None else summary
        out = {"title": rtitle, "summary": wrapped, "points": points, "url": final or url, "trust": 0,
               "author": str(data.get("author") or "")[:80], "suspicious": bool(data.get("suspicious"))}
        try:
            await k.bus.emit(EV.WEB_READ, {"url": final or url, "title": rtitle, "summary": summary, "points": points,
                                           "purpose": purpose, "source": "reader",
                                           "suspicious": out["suspicious"]}, source="social.reader",
                             trust=Trust.UNTRUSTED)
        except Exception:
            log.debug("percept.web emit failed", exc_info=True)
        self._cache[url] = (now, out)
        while len(self._cache) > 128:
            self._cache.popitem(last=False)
        return dict(out)

    async def summarize(self, text: str, thread_hint: Optional[str] = None) -> dict:
        """The isolated reader call: fixed system instruction + page text only."""
        schema = {"title": "", "summary": "", "points": [], "author": "", "suspicious": False}
        try:
            return await self.k.llm.json("soc.reader", prompts.READER_USER.format(text=text[: self.max_chars]),
                                         tier="fast", system=prompts.READER_SYSTEM, schema=schema,
                                         max_tokens=600, priority=7)
        except Exception as e:  # budget denied / provider down: degrade to an extractive summary
            log.info("reader call failed (%r); using extractive summary", e)
            first = " ".join(text.split())[:150]
            return {**schema, "summary": first, "points": []}

    # ------------------------------------------------------------------ feeds
    def feed_list(self) -> list:
        feeds = []
        pdir = self.k.persona_dir or ""
        path = os.path.join(pdir, "feeds.yaml") if pdir else ""
        data = {}
        if path and os.path.exists(path):
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                log.warning("bad feeds.yaml", exc_info=True)
        extra = list(self.cfg.get("feeds") or []) + list(self.k.cfg.get("social.feeds") or [])
        for f in list(data.get("feeds") or []) + extra:
            if isinstance(f, str):
                feeds.append({"url": f, "name": urlparse(f).hostname or f})
            elif isinstance(f, dict) and f.get("url"):
                feeds.append({"url": f["url"], "name": f.get("name") or urlparse(f["url"]).hostname or f["url"]})
        seen = set()
        out = []
        for f in feeds:
            if f["url"] not in seen:
                seen.add(f["url"])
                out.append(f)
        return out

    async def _load_seen(self, feed_url: str) -> set:
        if feed_url not in self._seen:
            rows = await self.k.db.fetchall("SELECT item_id FROM soc_feed_seen WHERE feed=?", (feed_url,))
            self._seen[feed_url] = {r["item_id"] for r in rows}
        return self._seen[feed_url]

    async def poll_feeds(self) -> list:
        loop = asyncio.get_running_loop()
        feeds = await loop.run_in_executor(None, self.feed_list)
        if not feeds:
            return []
        max_new = int(self.cfg.get("max_new_per_feed", 10))

        async def one(feed: dict) -> list:
            try:
                ctype, body, _ = await self.fetch(feed["url"], FEED_TYPES | HTML_TYPES | TEXT_TYPES)
                ftitle, items = parse_feed(body)
            except (FetchError, FeedError) as e:
                log.info("feed %s failed: %s", feed["url"], e)
                return []
            seen = await self._load_seen(feed["url"])
            new = []
            for it in items:
                iid = hashlib.sha1((it.get("id") or it.get("url") or it.get("title", "")).encode("utf-8")).hexdigest()[:20]
                if iid in seen:
                    continue
                seen.add(iid)
                new.append((iid, it))
            if not new:
                return []
            now = self.k.clock.now()
            await self.k.db.executemany("INSERT OR IGNORE INTO soc_feed_seen (feed,item_id,ts) VALUES (?,?,?)",
                                        [(feed["url"], iid, now) for iid, _ in new])
            out = []
            for iid, it in new[:max_new]:
                url = urljoin(feed["url"], it.get("url") or "") if it.get("url") else ""
                wrapped = self.k.gov.wrap_untrusted(it.get("summary", ""), url or feed["url"]) if self.k.gov else it.get("summary", "")
                out.append({"title": it.get("title", ""), "url": url, "summary": wrapped,
                            "feed": feed.get("name") or ftitle or feed["url"], "published": it.get("published", ""),
                            "trust": 0})
            return out

        results = await asyncio.gather(*(one(f) for f in feeds))
        items = [it for r in results for it in r]
        for it in items:
            try:
                await self.k.bus.emit(EV.WEB_READ, {"url": it["url"], "title": it["title"], "feed": it["feed"],
                                                    "source": "feed"}, source="social.feed", trust=Trust.UNTRUSTED)
            except Exception:
                pass
        return items
