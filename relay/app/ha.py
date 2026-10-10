"""Home Assistant client: a websocket subscription for events and a REST call
for actions. Reconnects on its own; the relay keeps working from its cache when
Home Assistant is briefly away.
"""
from __future__ import annotations

import asyncio
import inspect
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
                 *, on_connected: Callable[[], Awaitable[None] | None] | None = None) -> None:
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
                # its facts (the MQTT bridge) can stop claiming it is not. The
                # callback may be async (it also learns Home Assistant's origin),
                # so it is awaited when it is.
                result = self._on_connected()
                if inspect.isawaitable(result):
                    await result
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
            # Hemsmart-hubben säger vem som låste. Home Assistants journal vet
            # det inte, för en tjänst bär ingen användare — så utan den här
            # prenumerationen är en familjemedlems upplåsning oattribuerad.
            await ws.send(
                json.dumps(
                    {"id": 3, "type": "subscribe_events", "event_type": "hemsmart_lock"}
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

    async def call_service_status(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> tuple[int, Any]:
        """Call a service that returns data, keeping Home Assistant's answer.

        Returns ``(status, body)``: Home Assistant's HTTP status, or ``0`` when
        the request never reached it. A refusal (4xx) and an outage (5xx) are
        both failures, but only the first is the caller's to explain — the
        integration raises a validation error (Home Assistant answers 400) for
        a request it will not take, and that must not read as an outage. This
        is what ``call_service_result`` throws away.
        """
        if self._http is None:
            _LOGGER.info("[dev] would call %s.%s %s", domain, service, data)
            return 200, None
        try:
            resp = await self._http.post(
                f"/api/services/{domain}/{service}?return_response",
                json=data,
                headers={"Authorization": f"Bearer {self._cfg.ha_token}"},
            )
        except httpx.HTTPError as err:
            _LOGGER.warning("HA %s.%s failed (%s)", domain, service, err)
            return 0, None
        if resp.status_code >= 400:
            _LOGGER.warning("HA %s.%s -> %s %s", domain, service, resp.status_code, resp.text[:200])
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, None
        try:
            body = resp.json()
        except ValueError:
            return 200, None
        # Home Assistant wraps a service response under "service_response".
        if isinstance(body, dict) and "service_response" in body:
            return 200, body.get("service_response")
        return 200, body

    async def call_service_result(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> tuple[bool, Any]:
        """Call a service that returns data (`?return_response`).

        Returns ``(accepted, service_response)``. A transport error or an
        upstream 4xx/5xx is reported as a clean ``False`` so the caller can
        answer with a human message instead of leaking Home Assistant's error.
        """
        status, body = await self.call_service_status(domain, service, data)
        if status == 0 or status >= 400:
            return False, None
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

    async def base_url(self) -> str:
        """The origin a browser reaches Home Assistant at, or "".

        Home Assistant validates an MQTT ``entity_picture`` with ``cv.url``, so a
        relative path is rejected and the picture must be absolute. The origin
        comes from Home Assistant itself (``GET /api/config``): the internal URL
        when one is configured, else the external URL. No URL, no picture - the
        caller then leaves ``entity_picture`` off rather than publish something
        Home Assistant will refuse.
        """
        if self._http is None:
            return ""
        try:
            resp = await self._http.get(
                "/api/config",
                headers={"Authorization": f"Bearer {self._cfg.ha_token}"},
            )
        except httpx.HTTPError as err:
            _LOGGER.warning("HA /api/config failed (%s)", err)
            return ""
        if resp.status_code != 200:
            return ""
        try:
            body = resp.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        for key in ("internal_url", "external_url"):
            value = str(body.get(key) or "").strip().rstrip("/")
            if value:
                return value
        return ""
