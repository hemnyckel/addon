"""Home Assistant client: a websocket subscription for events and a REST call
for actions. Reconnects on its own; the relay keeps working from its cache when
Home Assistant is briefly away.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

from .config import Config

_LOGGER = logging.getLogger("hemnyckel.ha")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class HaClient:
    def __init__(self, cfg: Config, on_event: EventHandler) -> None:
        self._cfg = cfg
        self._on_event = on_event
        self._connected = False
        self._http: httpx.AsyncClient | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        if not self._cfg.ha_configured:
            _LOGGER.info("Home Assistant not configured - running without events")
            return
        self._http = httpx.AsyncClient(base_url=self._cfg.ha_url, timeout=15)
        backoff = 2
        while True:
            try:
                await self._session()
                backoff = 2
            except Exception as err:  # noqa: BLE001
                self._connected = False
                _LOGGER.warning("Home Assistant connection lost (%s); retry in %ss", err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self) -> None:
        ws_url = self._cfg.ha_url.replace("https://", "wss://").replace("http://", "ws://")
        async with websockets.connect(f"{ws_url}/api/websocket", max_size=None) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": self._cfg.ha_token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant auth failed: {auth.get('type')}")
            self._connected = True
            _LOGGER.info("Connected to Home Assistant")
            await ws.send(
                json.dumps(
                    {"id": 1, "type": "subscribe_events",
                     "event_type": "hemnyckel_door_event"}
                )
            )
            await ws.send(
                json.dumps(
                    {"id": 2, "type": "subscribe_events", "event_type": "state_changed"}
                )
            )
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                event = msg["event"]
                await self._on_event(event)

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        """Calls a Home Assistant service. Returns True when accepted (200)."""
        if self._http is None:
            _LOGGER.info("[dev] would call %s.%s %s", domain, service, data)
            return True
        resp = await self._http.post(
            f"/api/services/{domain}/{service}",
            json=data,
            headers={"Authorization": f"Bearer {self._cfg.ha_token}"},
        )
        if resp.status_code >= 400:
            _LOGGER.warning("HA %s.%s -> %s %s", domain, service, resp.status_code, resp.text[:200])
            return False
        return True

    async def entity_state(self, entity_id: str) -> str | None:
        if self._http is None:
            return None
        resp = await self._http.get(
            f"/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {self._cfg.ha_token}"},
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("state")
