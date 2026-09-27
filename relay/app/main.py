"""Hemnyckel relay: a small HTTPS service that turns local lock events into
Apple push notifications, and forwards app actions back to Home Assistant.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect

from .apns import ApnsClient
from .config import Config, Door, load_config, normalize_env
from .events import from_ha
from .ha import HaClient
from .store import Store

_LOGGER = logging.getLogger("hemnyckel")

# How long APNs should keep trying to deliver an alert (seconds).
_ALERT_TTL = 3600
# Upper bound on concurrent pushes, so a busy household can't open thousands
# of connections at once.
_MAX_CONCURRENT_PUSHES = 16


def _prefs(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}



class State:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.data_dir)
        self.apns = ApnsClient(cfg)
        self.ha = HaClient(cfg, self.on_ha_event)
        self.pair_code = secrets.token_hex(3).upper()
        self.pair_expires = time.time() + 600
        self.sockets: set[WebSocket] = set()
        self._send_sem = asyncio.Semaphore(_MAX_CONCURRENT_PUSHES)

    # -- incoming events -----------------------------------------------------
    async def on_ha_event(self, event: dict[str, Any]) -> None:
        try:
            mapped = from_ha(self.cfg, event)
            if mapped is None:
                return
            self.store.add_event(mapped)
            await self.broadcast(mapped)
            await self.notify(mapped)
        except Exception:
            _LOGGER.exception("failed to handle Home Assistant event")

    # -- push ----------------------------------------------------------------
    def _payload(self, ev: dict[str, Any], door: Door) -> dict[str, Any]:
        person = ev.get("person") or "Någon"
        verb = "Låstes upp" if ev["action"] == "unlock" else "Låstes"
        when = time.strftime("%H:%M", time.localtime(ev["ts"]))
        return {
            "aps": {
                "alert": {
                    "title": door.name,
                    "subtitle": f"{person} · {ev.get('method') or 'Okänt'}",
                    "body": f"{verb} {when}",
                },
                "sound": "default",
                "category": "DOOR_EVENT",
                "interruption-level": "time-sensitive",
                "thread-id": f"door-{door.id}",
                "mutable-content": 1,
                "relevance-score": 1.0,
            },
            "event": ev,
        }

    async def notify(self, ev: dict[str, Any]) -> None:
        door = self.cfg.door(ev["door"])
        if door is None:
            return
        # System events (auto-relock) are mirrored but never interrupt anyone.
        if ev.get("source") == "auto":
            return
        payload = self._payload(ev, door)
        expiration = int(time.time()) + _ALERT_TTL
        # Collapse a burst on the same door and action into one notification.
        collapse_id = f"door-{door.id}-{ev['action']}"[:64]

        targets = []
        for device in self.store.devices():
            if not device["apns_token"]:
                continue
            prefs = _prefs(device["prefs"])
            if prefs.get("doors") and door.id not in prefs["doors"]:
                continue
            if ev.get("person") and prefs.get("skip_self") and device["person"] == ev["person"]:
                continue
            targets.append(device)

        async def deliver(device: Any) -> None:
            async with self._send_sem:
                result = await self.apns.send(
                    device["apns_token"],
                    payload,
                    env=normalize_env(device["apns_env"] or self.cfg.apns_env),
                    expiration=expiration,
                    collapse_id=collapse_id,
                )
            if result.invalidate_token:
                self.store.disable_apns(device["id"])

        await asyncio.gather(*(deliver(d) for d in targets))

    async def broadcast(self, ev: dict[str, Any]) -> None:
        dead = []
        for ws in self.sockets:
            try:
                await ws.send_json(ev)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    # -- actions -------------------------------------------------------------
    async def do_action(self, door_id: str, action: str) -> dict[str, Any]:
        door = self.cfg.door(door_id)
        if door is None or action not in ("lock", "unlock"):
            raise HTTPException(400, "unknown door or action")
        ok = await self.ha.call_service("lock", action, {"entity_id": door.lock_entity})
        if not ok:
            return {"ok": False, "confirmed": False}
        await asyncio.sleep(1.0)
        state = await self.ha.entity_state(door.lock_entity)
        confirmed = state == ("unlocked" if action == "unlock" else "locked")
        return {"ok": True, "confirmed": confirmed}

    async def door_state(self, door: Door) -> dict[str, Any]:
        locked = None
        state = await self.ha.entity_state(door.lock_entity)
        if state == "locked":
            locked = True
        elif state == "unlocked":
            locked = False
        opened = None
        if door.door_sensor:
            s = await self.ha.entity_state(door.door_sensor)
            if s in ("on", "off"):
                opened = s == "on"
        return {
            "id": door.id, "name": door.name, "locked": locked, "open": opened,
            "last_event": self.store.last_event(door.id),
        }


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    state = State(cfg)

    async def require_device(authorization: str = Header(default="")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        device = state.store.device(token) if token else None
        if device is None:
            raise HTTPException(401, "invalid device token")
        return dict(device)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logging.basicConfig(level=logging.INFO)
        _LOGGER.info("Pairing code: %s (expires in 10 min)", state.pair_code)
        await state.apns.start()
        task = asyncio.create_task(state.ha.run())
        yield
        task.cancel()
        await state.apns.stop()

    app = FastAPI(title="Hemnyckel relay", version="0.1.0", lifespan=lifespan)
    # Expose the runtime state for tests and debugging (app.state.hmk).
    app.state.hmk = state

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "ha": state.ha.connected,
            "apns": state.apns.live,
            "doors": len(cfg.doors),
            "version": app.version,
        }

    @app.post("/pair")
    async def pair(payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code", "")).upper()
        if not code or code != state.pair_code or time.time() > state.pair_expires:
            raise HTTPException(401, "invalid or expired code")
        device_id = uuid.uuid4().hex
        state.store.add_device(device_id, str(payload.get("name") or "Enhet"))
        state.pair_code = secrets.token_hex(3).upper()
        state.pair_expires = time.time() + 600
        return {"device_token": device_id, "relay_id": "hemnyckel"}

    @app.post("/register")
    async def register(payload: dict[str, Any], device: dict = Depends(require_device)) -> dict[str, Any]:
        state.store.set_apns(
            device["id"],
            str(payload.get("apns_token") or ""),
            payload.get("person"),
            payload.get("prefs") or {},
            normalize_env(payload.get("apns_env") or cfg.apns_env),
        )
        return {"ok": True}

    @app.get("/events")
    async def events(_: dict = Depends(require_device), since: float | None = None,
                     door: str | None = None, person: str | None = None,
                     limit: int = 200) -> dict[str, Any]:
        return {"events": state.store.events(since=since, door=door, person=person, limit=limit)}

    @app.get("/state")
    async def door_states(_: dict = Depends(require_device)) -> dict[str, Any]:
        doors = [await state.door_state(d) for d in cfg.doors]
        presence: dict[str, str] = {}
        for d in cfg.doors:
            ev = state.store.last_event(d.id)
            if ev and ev.get("person"):
                presence[ev["person"]] = "home" if ev["action"] == "unlock" else "away"
        return {"doors": doors, "presence": presence, "relay": {"online": True, "apns": state.apns.live}}

    @app.post("/action")
    async def action(payload: dict[str, Any], _: dict = Depends(require_device)) -> dict[str, Any]:
        return await state.do_action(str(payload.get("door")), str(payload.get("action")))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        state.sockets.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            state.sockets.discard(websocket)

    return app
