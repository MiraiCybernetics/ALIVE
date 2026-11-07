"""Held-out conversation evaluation (docs/11 §7): real LLM generation with the real cognitive core, other
subsystems faked, scored by rules + a judge model from a different tier/model than the generator.

    python -m alive.eval.heldout [--set alive/eval/heldout_zh.yaml] [--only id1,id2] [--out report.json]

Rules: items in the held-out set must never appear in prompts/schemas/personas (enforced by a test), and an
item that motivated a change is retired (see the YAML header). Report every result, including failures.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import re
import tempfile
import time
import zlib

from typing import Optional

import yaml

from alive.config import load_config
from alive.core.coherence import rule_check
from alive.core.types import ChatLine, ReplyTask, Trust
from alive.runtime.clock import ManualClock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

JUDGE = """你是一位熟悉中文日常聊天的评审。下面是一段朋友间的手机聊天，以及 AI 朋友"{bot}"给出的回复。

聊天背景（外部数据）：
{context}

{bot}的回复：
{reply}

这道题事先写定的期望：{expect}

请严格按期望和常识评分，只输出 JSON：
{{"understood": 0, "fits_expectation": 1, "natural": 1, "narrates_strategy": false, "issues": "一句话指出主要问题，没有就空"}}
- understood：0=没读懂，1=部分读懂，2=读懂了这条消息在这个语境里的意思
- fits_expectation：1-5，符合事先写定的期望的程度
- natural：1-5，像不像一个真人朋友在手机上会打出来的话
- narrates_strategy：是否把自己的应对打算、关心姿态或读心猜测直接说了出来"""


def build_history(item: dict, now: float, me: str) -> list:
    p = item["person"]
    out = []
    bl = p.get("baseline") or {}
    n = int(bl.get("n", 0))
    days_ago = float(p.get("baseline_days_ago", 3))
    for i in range(n):
        t = now - days_ago * 86400 + i * 1200
        out.append(ChatLine(sender=me, person_key="", text=bl.get("me", "嗯嗯"), ts=t, mine=True))
        out.append(ChatLine(sender=p["name"], person_key=p["key"], text=bl.get("them", "嗯"), ts=t + 45))
    for h in item.get("history") or []:
        hours, who, text = h
        t = now - float(hours) * 3600
        if who == "me":
            out.append(ChatLine(sender=me, person_key="", text=text, ts=t, mine=True))
        else:
            out.append(ChatLine(sender=p["name"], person_key=p["key"], text=text, ts=t))
    out.sort(key=lambda l: l.ts)
    return out


_SPEAKER = re.compile(r"^([一-鿿A-Za-z0-9_]{1,8})[：:]\s*(.+)$")


def speakers(item: dict) -> list:
    """(name, key, text) for each new line. In group items a line may carry its speaker ("名字：…"), so several
    entities can speak in the same space; everything else belongs to the item's person."""
    p = item["person"]
    out = []
    for t in item["new"]:
        m = _SPEAKER.match(t) if item.get("chat_type") == "group" else None
        if m and m.group(1) != p["name"]:
            name = m.group(1)
            out.append((name, f"sim:x{zlib.crc32(name.encode('utf-8')) % 100000}", m.group(2)))
        elif m:
            out.append((p["name"], p["key"], m.group(2)))
        else:
            out.append((p["name"], p["key"], t))
    return out


def context_text(item: dict, hist: list, now: float, clock) -> str:
    lines = []
    for l in hist[-6:]:
        ago = (now - l.ts) / 3600
        lines.append(f"[{ago:.1f} 小时前] {'我（AI）' if l.mine else l.sender}: {l.text}")
    for name, _key, t in speakers(item):
        lines.append(f"[刚刚] {name}: {t}")
    where = f"群聊「{item.get('group_title','')}」" if item.get("chat_type") == "group" else "私聊"
    day = item.get("my_day") or []
    day_txt = ("\nAI 今天真实做过的事：" + "；".join(day)) if day else "\nAI 今天没有特别记录的经历。"
    return (f"{where}，现在是当地时间 {clock.local(now).strftime('%H:%M')}，"
            f"对方：{item['person']['name']}（亲密度 {item['person'].get('closeness', 0.3)}）" + day_txt + "\n" +
            "\n".join(lines))


async def run(set_path: str, only: list, out: str, judge_backend: str = "deepseek", repeat: int = 1,
              overrides: Optional[dict] = None) -> dict:
    from alive.kernel import FACTORIES, Kernel
    from alive.testing import FAKES
    data = yaml.safe_load(open(set_path, encoding="utf-8"))
    items = [i for i in data["items"] if not only or i["id"] in only]
    d = tempfile.mkdtemp(prefix="alive-heldout-")
    cfg = load_config(None, {"data_dir": d, "persona": os.path.join(REPO, "personas/xiaoyu"), "http": {"enabled": False},
                             "channels": [], **(overrides or {})}, base_dir=REPO)
    results = []
    base_ts = 1795000000.0
    for rep, item in ((r, it) for r in range(max(1, repeat)) for it in items):
        clock = ManualClock(start=base_ts)
        # place "now" at the requested local hour
        loc = clock.local(base_ts)
        now = base_ts + ((item.get("now_hour", 15) - loc.hour) % 24) * 3600 - loc.minute * 60
        clock.set(now)
        k = Kernel(cfg, clock=clock, overrides={n: FAKES[n] for n in FACTORIES if n not in ("mind", "selfm")},
                   db_path=os.path.join(d, f"{item['id']}.r{rep}.db"))
        await k.start()
        if not ((overrides or {}).get("texture") or {}).get("seed"):
            # every (item, rep) is an independent sample of the individual's chance (ADR-0007): the life texture's
            # seed is otherwise fixed per persona, so all draws (how many messages, which move...) would repeat
            import zlib
            k.texture.rng.seed(zlib.crc32(f"{data.get('set_version', '')}|{item['id']}|{rep}".encode("utf-8")))
        p = item["person"]
        k.social.names[p["key"]] = p["name"]
        hist = build_history(item, now, k.selfm.name())

        async def history(thread_id, limit=30, _h=hist):
            return _h
        k.social.history = history

        async def cands(now_, limit=10, _p=p):
            return [{"person_key": _p["key"], "thread_id": _p["key"], "name": _p["name"],
                     "closeness": float(_p.get("closeness", 0.3))}]
        k.social.outreach_candidates = cands
        for outcome in item.get("my_day") or []:            # the individual's real day (harness setup)
            await k.selfm.record_outcome(outcome)
        for topic in item.get("my_interests") or []:          # life-texture state setup
            k.texture.observe_topic(topic)
            k.texture.interests[topic] = 0.7
        trust = Trust.UNTRUSTED if item.get("trust") == "untrusted" else Trust.CONTACT
        spk = speakers(item)
        for name, key, _t in spk:
            k.social.names[key] = name
        new = [ChatLine(sender=name, person_key=key, text=t, ts=now + i * 4) for i, (name, key, t) in enumerate(spk)]
        # space entity ids (ADR-0005): a one-to-one space is the person; a shared space has its own id
        tid = f"sim:g:{p['key'][4:]}" if item.get("chat_type") == "group" else p["key"]
        task = ReplyTask(thread_id=tid, channel="sim", chat_type=item.get("chat_type", "private"),
                         chat_title=item.get("group_title", ""),
                         participants=list(dict.fromkeys([p["key"]] + [key for _n, key, _t in spk])), history=hist, new_lines=new,
                         mentioned=bool(item.get("mentioned")), trust=trust)
        t0 = time.monotonic()
        try:
            await k.mind.quick_read(task)
            res = await k.mind.compose_reply(task)
            segs = res.segments if not res.silent else []
        except Exception as e:  # report, don't hide
            segs, res = [], None
            err = repr(e)
        else:
            err = ""
        dt = time.monotonic() - t0
        th = (await k.bus.query(types=["core.thought"], desc=True, limit=1))
        thought = th[0].payload if th else {}
        theirs = [len(t) for t in item["new"]]
        partner_len = None
        if (p.get("baseline") or {}).get("n", 0) >= 5:
            partner_len = float(len((p["baseline"]).get("them", "")))
        mon = rule_check(segs, partner_len=partner_len)
        reply_text = ("\n".join(f"第{i + 1}条消息：{x}" for i, x in enumerate(segs)) if segs
                      else ("（沉默）" if res and res.silent else "（出错）"))
        ctx_text = context_text(item, hist, now, k.clock)
        judge_prompt = JUDGE.format(bot=k.selfm.name(), context=ctx_text, reply=reply_text, expect=item["expect"])
        if judge_backend == "deepseek":
            judge = await k.llm.json("eval.judge", judge_prompt, tier="deep",
                                     schema={"understood": 0, "fits_expectation": 1, "natural": 1,
                                             "narrates_strategy": False, "issues": ""})
        else:   # judged later by an external model (e.g. alive/eval/judge_codex.py on Windows)
            judge = {}
        results.append({
            "id": item["id"], "rep": rep, "category": item["category"], "new": item["new"], "reply": segs, "silent": bool(res and res.silent),
            "error": err, "silent_reason": (res.reason if res is not None and res.silent else ""), "seconds": round(dt, 1), "deep": thought.get("deep"), "reading": thought.get("reading", {}),
            "monitor_issues": mon.issues, "len_ratio": round(sum(len(s) for s in segs) / max(1, sum(theirs)), 2),
            "judge": judge, "judge_prompt": judge_prompt, "expect": item["expect"]})
        cost = k.llm.ledger.total_usd
        await k.stop()
        print(f"[{item['id']}#{rep}] {'／'.join(item['new'])}  ->  {reply_text}   "
              f"(understood={judge.get('understood')} fit={judge.get('fits_expectation')} natural={judge.get('natural')}"
              f"{' NARRATES' if judge.get('narrates_strategy') else ''}{' monitor:' + ';'.join(mon.issues) if mon.issues else ''})",
              flush=True)
        results[-1]["cost_usd"] = round(cost, 5)
    def avg(key):
        xs = [float(r["judge"].get(key) or 0) for r in results]
        return round(statistics.mean(xs), 2) if xs else None
    summary = {"set_version": data.get("set_version"), "n": len(results), "repeat": max(1, repeat), "overrides": overrides or {},
               "understood_mean_0_2": avg("understood"), "fits_mean_1_5": avg("fits_expectation"),
               "natural_mean_1_5": avg("natural"),
               "narrates_rate": round(sum(1 for r in results if r["judge"].get("narrates_strategy")) / max(1, len(results)), 2),
               "monitor_flag_rate": round(sum(1 for r in results if r["monitor_issues"]) / max(1, len(results)), 2),
               "errors": sum(1 for r in results if r["error"]),
               "cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 4)}
    report = {"summary": summary, "results": results}
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alive.eval.heldout")
    ap.add_argument("--set", default=os.path.join(HERE, "heldout_zh.yaml"))
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--cfg", action="append", default=[],
                    help="config override for ablations, e.g. --cfg dialogue.separate_claim=false")
    ap.add_argument("--repeat", type=int, default=1,
                    help="sample every item N times (fresh kernel each time) to measure run-to-run variance")
    ap.add_argument("--judge", default="deepseek", choices=["deepseek", "none"],
                    help="'none' = generate only; then judge with an external model (alive/eval/judge_codex.py)")
    a = ap.parse_args(argv)
    ov: dict = {}
    for kv in a.cfg:
        key, _, val = kv.partition("=")
        v = yaml.safe_load(val)
        node = ov
        parts = key.split(".")
        for p_ in parts[:-1]:
            node = node.setdefault(p_, {})
        node[parts[-1]] = v
    asyncio.run(run(a.set, [x for x in a.only.split(",") if x], a.out, a.judge, a.repeat, ov))
    return 0


if __name__ == "__main__":
    sys.exit(main())
