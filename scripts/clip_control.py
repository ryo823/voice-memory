#!/usr/bin/env python3
"""Tiny BLE helper run with the clip-sdk venv: `clip_control.py MAC state|stop|start`.

Prints one JSON line. `state` reports {"state", "charging", "battery", "duration"} (duration: seconds recorded so far); `stop`/`start` only change
the recording state (they never delete anything on the Clip).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "clip-sdk" / "src"))

from clip import ClipCommands, ClipDevice


async def main(mac: str, action: str) -> dict:
    async with ClipDevice(address=mac) as device:
        cmds = ClipCommands(device)
        if action == "stop":
            await cmds.stop_recording()
        elif action == "start":
            await cmds.start_recording()
        s = await cmds.get_state()
        return {"state": s.state, "charging": s.charging, "battery": s.battery, "duration": s.duration}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(asyncio.wait_for(main(sys.argv[1], sys.argv[2]), 40))))
