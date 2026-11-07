"""CLI adapter: commands, sender/chat switching, pairing approval, non-blocking stdin reader."""
import asyncio
import io

from tests.social.soc_helpers import make_kernel


async def _cli_kernel(tmp_path, **cfg):
    k = await make_kernel(tmp_path, channels=[{"kind": "cli", "name": "cli", "read_stdin": False, **cfg}])
    cli = k.social.adapter("cli")
    cli._out = io.StringIO()
    return k, cli


async def test_cli_private_group_switching_and_output(tmp_path):
    k, cli = await _cli_kernel(tmp_path)
    try:
        await cli.handle_line("你好呀")
        await k.clock.advance(30)
        out = cli._out.getvalue()
        assert "[私聊·owner] 小屿：好的" in out
        await cli.handle_line("/as 阿明")
        await cli.handle_line("/group 摸鱼群")
        m = await cli.handle_line("@小屿 出来玩")
        assert m.mentions_me and m.chat_type == "group" and m.chat_id == "g:摸鱼群" and m.person_key == "cli:阿明"
        await k.clock.advance(30)
        assert "[群·摸鱼群] 小屿" in cli._out.getvalue()
        await cli.handle_line("/private")
        m = await cli.handle_line("私聊一下")
        assert m.chat_id == "阿明" and m.chat_type == "private"
        await cli.handle_line("/bot")
        assert cli.is_bot
        await cli.handle_line("/status")
        assert "线程" in cli._out.getvalue()
        await cli.handle_line("/quit")
        assert cli.quit_event.is_set()
    finally:
        await k.stop()


async def test_cli_pairing_approval(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "cli", "name": "cli", "read_stdin": False},
                                              {"kind": "sim", "name": "sim", "dm_policy": "pairing"}])
    try:
        cli = k.social.adapter("cli")
        cli._out = io.StringIO()
        await k.social.adapter("sim").inject("你好", sender="s1", sender_name="路人")
        await k.clock.advance(30)
        await cli.handle_line("/pairings")
        code = k.social.list_pairings()[0]["code"]
        assert code in cli._out.getvalue()
        await cli.handle_line(f"/approve {code}")
        assert "已批准 路人" in cli._out.getvalue() and k.social.is_paired("sim:s1")
        await cli.handle_line("/approve NOPE1234")
        assert "无效" in cli._out.getvalue()
    finally:
        await k.stop()


async def test_cli_stdin_reader_thread_does_not_block_loop(tmp_path):
    stdin = io.StringIO("第一句\n/as 小红\n第二句\n")
    k = await make_kernel(tmp_path, channels=[])
    try:
        from alive.social.channels.cli import CLIAdapter
        cli = CLIAdapter(k, "cli", {}, stdin=stdin, stdout=io.StringIO())
        k.social.adapters["cli"] = cli
        await cli.start()
        for _ in range(200):
            if cli.quit_event.is_set():
                break
            await asyncio.sleep(0.01)
        assert cli.quit_event.is_set()          # EOF -> quit
        await k.clock.advance(30)
        hist = await k.social.history("cli:owner")
        assert hist[0].text == "第一句"
        assert (await k.social.history("cli:小红"))[0].text == "第二句"
        await cli.stop()
    finally:
        await k.stop()
