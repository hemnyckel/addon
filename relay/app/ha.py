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
    def __init__(self, cfg: Config, on_event: EventHandler,
                 *, on_connected: Callable[[], None] | None = None) -> None:
        self._cfg = cfg
        self._on_event = on_event
        self._on_connected = on_connected
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
            if self._on_connected is not None:
                # The relay now knows Home Assistant is up; anything projecting
                # its facts (the MQTT bridge) can stop claiming it is not.
                self._on_connected()
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

    async def call_service_result(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> tuple[bool, Any]:
        """Call a service that returns data (`?return_response`).

        Returns ``(accepted, service_response)``. A transport error or an
        upstream 4xx/5xx is reported as a clean ``False`` so the caller can
        answer with a human message instead of leaking Home Assistant's error.
        """
        if self._http is None:
            _LOGGER.info("[dev] would call %s.%s %s", domain, service, data)
            return True, None
        try:
            resp = await self._http.post(
                f"/api/services/{domain}/{service}?return_response",
                json=data,
                headers={"Authorization": f"Bearer {self._cfg.ha_token}"},
            )
        except httpx.HTTPError as err:
            _LOGGER.warning("HA %s.%s failed (%s)", domain, service, err)
            return False, None
        if resp.status_code >= 400:
            _LOGGER.warning("HA %s.%s -> %s %s", domain, service, resp.status_code, resp.text[:200])
            return False, None
        try:
            body = resp.json()
        except ValueError:
            return True, None
        # Home Assistant wraps a service response under "service_response".
        if isinstance(body, dict) and "service_response" in body:
            return True, body.get("service_response")
        return True, body

    async def states(self) -> list[dict[str, Any]]:
        """Every entity state, for resolving a door's sensors by attribute."""
        if self._http is None:
            return []
        resp = await self._http.get(
            "/api/states",
            headers={"Authorization": f"Bearer {self._cfg.ha_token}"},
        )
        if resp.status_code != 200:
            return []
        try:
            body = resp.json()
        except ValueError:
            return []
        return body if isinstance(body, list) else []

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
