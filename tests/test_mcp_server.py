import json
import sys
import anyio
from mcp import Client, StdioServerParameters

EXPECTED = {"search_voice_memories", "get_voice_memory", "get_recent_memories",
            "list_voice_tags", "save_voice_memory",
            "get_pending_action_memories", "record_action_registration",
            "list_todos", "get_todo", "add_todo", "update_todo", "set_todo_status"}


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


def test_todo_tools(tmp_path):
    async def run():
        async with Client(params(tmp_path / "mcp.db")) as c:
            m = data(await c.call_tool("save_voice_memory", {"title": "歯医者", "transcript": "来月どこかで"}))
            mid = m["saved"]["id"]
            r = await c.call_tool("add_todo", {"title": "歯医者を予約", "due_text": "来月どこか", "memory_ids": [mid]})
            todo = data(r)["todo"]
            assert not r.is_error and todo["due_date"] is None and todo["memory_ids"] == [mid]
            again = data(await c.call_tool("add_todo", {"title": "歯医者を 予約！"}))["todo"]
            assert again["duplicate"] and again["id"] == todo["id"]
            assert data(await c.call_tool("list_todos", {}))["count"] == 1
            u = data(await c.call_tool("update_todo", {"todo_id": todo["id"], "due_date": "2099-01-05"}))
            assert u["todo"]["due_date"] == "2099-01-05" and u["todo"]["due_text"] == "来月どこか"
            done = data(await c.call_tool("set_todo_status", {"todo_id": todo["id"], "status": "completed"}))
            assert done["todo"]["completed_at"]
            assert data(await c.call_tool("list_todos", {}))["count"] == 0
            g = data(await c.call_tool("get_todo", {"todo_id": todo["id"]}))
            assert g["todo"]["memories"][0]["id"] == mid
            for name, args in [("add_todo", {"title": ""}), ("add_todo", {"title": "x", "due_date": "2026-13-40"}),
                               ("add_todo", {"title": "x", "memory_ids": ["nope"]}),
                               ("add_todo", {"title": "x", "priority": "urgent"}),
                               ("set_todo_status", {"todo_id": todo["id"], "status": "done"}),
                               ("update_todo", {"todo_id": "nope", "title": "y"}),
                               ("get_todo", {"todo_id": "nope"}), ("list_todos", {"status": "bogus"})]:
                assert (await c.call_tool(name, args)).is_error, (name, args)
    anyio.run(run)
