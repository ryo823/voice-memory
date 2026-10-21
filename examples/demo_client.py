"""Demo: talk to the MCP server over stdio with a throwaway DB and print each tool call."""
import json
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters


async def call(c, name, args):
    r = await c.call_tool(name, args)
    print(f"\n>>> {name} {json.dumps(args, ensure_ascii=False)}")
    print(json.dumps(json.loads(r.content[0].text), ensure_ascii=False, indent=2))
    return json.loads(r.content[0].text)


async def main():
    with tempfile.TemporaryDirectory() as d:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "voice_memory.mcp_server"],
            env={"VOICE_MEMORY_DB": str(Path(d) / "demo.db")},
        )
        async with Client(params) as c:
            print("tools:", [t.name for t in (await c.list_tools()).tools])
            await call(c, "save_voice_memory", {
                "title": "Firebase認証方式の検討",
                "transcript": "メール認証とGoogleログインを比較。まずGoogleとメールを実装し、Appleは後追い。",
                "summary": "Firebase Authの方式比較", "tags": ["開発", "firebase", "認証"]})
            await call(c, "search_voice_memories", {"query": "Googleログイン"})
            await call(c, "list_voice_tags", {})


anyio.run(main)
