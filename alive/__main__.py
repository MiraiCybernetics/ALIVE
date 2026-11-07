"""Command line entry point.

    python -m alive run   [-c config/alive.yaml]          # run the individual 24/7 (channels from config)
    python -m alive chat  [-c config/alive.yaml]          # run with an interactive terminal channel
    python -m alive sim   [-c ...] --days 2 --scale 600   # accelerated life simulation + report
    python -m alive check [-c ...]                        # verify LLM connectivity and storage
    python -m alive status [--url http://127.0.0.1:8765]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys

from alive.config import load_config


def _setup_logging(cfg, console_level: str) -> str:
    logdir = os.path.join(cfg.get("data_dir"), "logs")
    os.makedirs(logdir, exist_ok=True)
    path = os.path.join(logdir, "alive.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(cfg.get("log_level", "INFO"))
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.handlers[:] = [fh, ch]
    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


async def _run(cfg, *, forever: bool = True) -> None:
    from alive.kernel import Kernel
    from alive.runtime.http import Inspector
    k = Kernel(cfg)
    await k.start()
    insp = None
    if cfg.get("http.enabled", True):
        insp = Inspector(k, cfg.get("http.host", "127.0.0.1"), int(cfg.get("http.port", 8765)), cfg.get("http.token"))
        try:
            await insp.start()
        except OSError as e:
            logging.getLogger("alive").warning("inspector not started: %s", e)
            insp = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - windows
            pass
    if k.social is not None and hasattr(k.social, "wait_closed"):
        # e.g. the CLI channel ends when the user types /quit
        asyncio.ensure_future(_wait_then(k.social.wait_closed(), stop))
    await stop.wait()
    if insp:
        await insp.stop()
    await k.stop()


async def _wait_then(aw, ev: asyncio.Event) -> None:
    try:
        await aw
    finally:
        ev.set()


async def _check(cfg) -> int:
    from alive.kernel import Kernel
    k = Kernel(cfg, only=[])
    await k.start()
    ok = True
    for tier in ("fast", "deliberate", "deep"):
        try:
            r = await k.llm.text(f"check.{tier}", "回复两个字：在的", tier=tier, max_tokens=50)
            print(f"[ok] {tier:10s} {k.llm.tiers[tier]['model']:18s} -> {r[:20]!r}")
        except Exception as e:
            ok = False
            print(f"[fail] {tier}: {e}")
    print("ledger:", json.dumps(k.llm.ledger.snapshot(), ensure_ascii=False))
    print("data dir:", k.data_dir)
    await k.stop()
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "chat", "sim", "check"):
        p = sub.add_parser(name)
        p.add_argument("-c", "--config", default=os.environ.get("ALIVE_CONFIG"))
        p.add_argument("--data-dir")
        p.add_argument("--persona")
        if name == "sim":
            p.add_argument("--days", type=float, default=1.0)
            p.add_argument("--scale", type=float, default=600.0)
            p.add_argument("--scenario", default="default")
            p.add_argument("--start", default=None, help="virtual start time, e.g. '2026-11-09 08:00'")
            p.add_argument("--out", default=None)
    sp = sub.add_parser("status")
    sp.add_argument("--url", default="http://127.0.0.1:8765")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        import urllib.request
        with urllib.request.urlopen(args.url + "/api/state", timeout=5) as r:
            print(r.read().decode("utf-8"))
        return 0

    ov: dict = {}
    if args.data_dir:
        ov["data_dir"] = args.data_dir
    if args.persona:
        ov["persona"] = args.persona
    if args.cmd == "chat":
        ov["channels"] = [{"kind": "cli", "name": "cli"}]
    cfg = load_config(args.config, ov, base_dir=os.getcwd())
    if args.cmd == "sim":
        from alive.eval.simulator import run_simulation
        _setup_logging(cfg, "WARNING")
        return asyncio.run(run_simulation(cfg, days=args.days, scale=args.scale, scenario=args.scenario,
                                          start=args.start, out=args.out))
    logpath = _setup_logging(cfg, "WARNING" if args.cmd == "chat" else "INFO")
    if args.cmd == "check":
        return asyncio.run(_check(cfg))
    print(f"ALIVE · persona={cfg.get('persona_name')} · data={cfg.get('data_dir')} · log={logpath}", file=sys.stderr)
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
