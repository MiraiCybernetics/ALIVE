"""Local command-line chat (stdin/stdout) for trying an individual out.

Lines are read by a daemon thread and handed to the event loop (never blocks the loop). Commands:
    /as <name>        speak as another simulated human (person key cli:<name>)
    /group <name>     switch to group chat <name>;   /private  back to a private chat with the current sender
    /bot              toggle "the current sender is a bot"
    /pairings         list pending pairing codes;    /approve <CODE> | /reject <CODE>
    /status           short social status;           /help;   /quit
Plain text is a message; "@<name>" mentions the individual.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Optional, TextIO

from alive.core.types import IncomingMessage
from alive.social.channels.base import ChannelAdapter

log = logging.getLogger("alive.social")

HELP = ("命令：/as <名字> 切换说话人 · /group <群名> 进群聊 · /private 回私聊 · /bot 切换机器人身份 · "
        "/pairings 待批配对 · /approve <码> · /reject <码> · /status · /quit")


class CLIAdapter(ChannelAdapter):
    kind = "cli"

    def __init__(self, kernel, name: str, cfg: dict, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None):
        super().__init__(kernel, name, cfg)
        self.caps.typing = bool(self.cfg.get("show_typing", False))
        self.caps.reactions = True
        self.caps.max_text = 100_000
        self.identity.user_id = "__self__"
        self.sender = str(self.cfg.get("sender", "owner"))
        self.sender_name = str(self.cfg.get("sender_name", self.sender))
        self.group: Optional[str] = None
        self.is_bot = False
        self._in = stdin
        self._out = stdout
        self._q: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._task = None
        self._n = 0
        self.quit_event: Optional[asyncio.Event] = None
        self.read_stdin = bool(self.cfg.get("read_stdin", True))

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.running = True
        self.connected = True
        self.quit_event = asyncio.Event()
        self._q = asyncio.Queue(maxsize=1000)
        if self.read_stdin:
            loop = asyncio.get_running_loop()
            src = self._in or sys.stdin
            self._thread = threading.Thread(target=self._reader, args=(loop, src), name=f"cli-reader-{self.name}",
                                            daemon=True)
            self._thread.start()
            self._task = self.k.scheduler.spawn(self._consume, f"soc.cli:{self.name}")
        self._write(f"（{self.all_names()[0] if self.all_names() else 'ALIVE'} 已上线。{HELP}）")

    async def stop(self) -> None:
        self.running = False
        self.connected = False
        if self._task is not None:
            self._task.cancel()

    def _reader(self, loop: asyncio.AbstractEventLoop, src) -> None:
        try:
            for line in iter(src.readline, ""):
                if not self.running:
                    break
                try:
                    loop.call_soon_threadsafe(self._q.put_nowait, line.rstrip("\r\n"))
                except Exception:  # loop closed or queue full
                    break
        finally:
            try:
                loop.call_soon_threadsafe(self._q.put_nowait, None)
            except Exception:
                pass

    async def _consume(self) -> None:
        while True:
            line = await self._q.get()
            if line is None:
                if self.quit_event is not None:
                    self.quit_event.set()
                return
            try:
                await self.handle_line(line)
            except Exception:
                log.exception("cli: failed to handle line")

    # ------------------------------------------------------------------ input
    @property
    def chat_id(self) -> str:
        return f"g:{self.group}" if self.group else self.sender

    async def handle_line(self, line: str) -> Optional[IncomingMessage]:
        """Process one input line (commands or a message). Returns the delivered message, if any."""
        s = (line or "").strip()
        if not s:
            return None
        if s.startswith("/"):
            await self._command(s)
            return None
        self._n += 1
        msg = IncomingMessage(channel=self.name, chat_id=self.chat_id, chat_type="group" if self.group else "private",
                              sender_id=self.sender, sender_name=self.sender_name, text=s, msg_id=f"cli{self._n}",
                              ts=self.k.clock.now(), mentions_me=self.at_mentions_me(s), is_bot=self.is_bot,
                              raw={"chat_title": self.group} if self.group else None)
        await self.deliver(msg)
        return msg

    async def _command(self, s: str) -> None:
        cmd, _, arg = s.partition(" ")
        arg = arg.strip()
        social = self.k.social
        if cmd == "/as" and arg:
            self.sender = self.sender_name = arg
            self._write(f"（现在你是 {arg}）")
        elif cmd == "/group" and arg:
            self.group = arg
            self._write(f"（进入群聊「{arg}」）")
        elif cmd == "/private":
            self.group = None
            self._write("（回到私聊）")
        elif cmd == "/bot":
            self.is_bot = not self.is_bot
            self._write(f"（机器人身份：{'是' if self.is_bot else '否'}）")
        elif cmd == "/pairings" and hasattr(social, "list_pairings"):
            ps = social.list_pairings()
            self._write("\n".join(f"  {p['code']}  {p['channel']}  {p['sender_name']} ({p['person_key']})" for p in ps)
                        or "（没有待批的配对请求）")
        elif cmd in ("/approve", "/reject") and arg and hasattr(social, "approve_pairing"):
            fn = social.approve_pairing if cmd == "/approve" else social.reject_pairing
            d = await fn(arg, f"cli:{self.sender}")
            self._write(f"（{'已批准' if cmd == '/approve' else '已拒绝'} {d['sender_name']}）" if d else "（配对码无效或已过期）")
        elif cmd == "/status" and social is not None:
            st = social.status()
            self._write(f"（线程 {st.get('threads')} · 活跃 {st.get('actors')} · 计数 {st.get('counters')}）")
        elif cmd == "/quit":
            if self.quit_event is not None:
                self.quit_event.set()
            self._write("（再见）")
        else:
            self._write(HELP)

    # ------------------------------------------------------------------ output
    def _write(self, s: str) -> None:
        out = self._out or sys.stdout
        try:
            out.write(s + "\n")
            out.flush()
        except Exception:
            pass

    def _label(self, chat_id: str) -> str:
        return f"群·{chat_id[2:]}" if str(chat_id).startswith("g:") else f"私聊·{chat_id}"

    def chat_type_of(self, chat_id: str) -> str:
        return "group" if str(chat_id).startswith("g:") else "private"

    async def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None, **kw) -> str:
        self._n += 1
        name = self.all_names()[0] if self.all_names() else "ALIVE"
        quote = f"（回复 {reply_to}）" if reply_to else ""
        self._write(f"[{self._label(chat_id)}] {name}{quote}：{text}")
        self.stats["out"] += 1
        return f"cliout{self._n}"

    async def send_typing(self, chat_id: str) -> None:
        if self.caps.typing:
            self._write(f"[{self._label(chat_id)}] …正在输入")

    async def react(self, chat_id: str, msg_id: str, emoji: str) -> bool:
        self._write(f"[{self._label(chat_id)}] （对 {msg_id} 回了 {emoji}）")
        return True
