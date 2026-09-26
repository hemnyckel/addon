"""Development tool: print raw nimly_journal_entry and lock state_changed events.

Usage (from the relay directory, with the relay venv):

    HEMNYCKEL_HA_URL=... HEMNYCKEL_HA_TOKEN=... \
      .venv/bin/python ../tools/listen_journal.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets


async def main() -> None:
    url = os.environ["HEMNYCKEL_HA_URL"].replace("https://", "wss://").replace("http://", "ws://")
    token = os.environ["HEMNYCKEL_HA_TOKEN"]
    async with websockets.connect(f"{url}/api/websocket", max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        print("auth:", json.loads(await ws.recv()).get("type"), flush=True)
        await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "nimly_journal_entry"}))
        await ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "event":
                continue
            ev = msg["event"]
            if ev.get("event_type") == "nimly_journal_entry":
                print("JOURNAL:", json.dumps(ev.get("data"), ensure_ascii=False), flush=True)
            else:
                data = ev.get("data", {})
                if str(data.get("entity_id", "")).startswith("lock."):
                    state = (data.get("new_state") or {}).get("state")
                    print("STATE:", data.get("entity_id"), "->", state, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
