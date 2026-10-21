import json
import sys
import anyio
from mcp import Client, StdioServerParameters

EXPECTED = {"search_voice_memories", "get_voice_memory", "get_recent_memories",
            "list_voice_tags", "save_voice_memory"}


def data(r):
    return json.loads(r.content[0].text)


def params(db_path):
    return StdioServerParameters(command=sys.executable, args=["-m", "voice_memory.mcp_server"],
                                 env={"VOICE_MEMORY_DB": str(db_path)})


def test_tools_and_roundtrip(tmp_path):
    async def run():
        async with Client(params(tmp_path / "mcp.db")) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            assert tools == EXPECTED
            r = await c.call_tool("save_voice_memory", {
                "title": "Firebase認証方式の検討", "transcript": "メール認証とGoogleログイン",
                "tags": ["firebase"]})
            assert not r.is_error
            mid = data(r)["saved"]["id"]
            s = await c.call_tool("search_voice_memories", {"query": "認証"})
            assert data(s)["count"] == 1
            g = await c.call_tool("get_voice_memory", {"memory_id": mid})
            assert data(g)["memory"]["id"] == mid
            t = await c.call_tool("list_voice_tags", {})
            assert data(t)["tags"] == [{"name": "firebase", "count": 1}]
            bad = await c.call_tool("save_voice_memory", {"title": "", "transcript": "x"})
            assert bad.is_error
            miss = await c.call_tool("get_voice_memory", {"memory_id": "nope"})
            assert miss.is_error
    anyio.run(run)
