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

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)

from . import live
from .apns import ApnsClient
from .config import Config, Door, load_config, normalize_env
from .events import from_ha
from .ha import HaClient
from .store import Store

_LOGGER = logging.getLogger("hemnyckel")

# How long APNs should keep trying to deliver an alert (seconds).
_ALERT_TTL = 3600
# Live Activity pushes: how long they stay deliverable, and how long a
# push-to-start is considered "in flight" before we try again.
_LIVE_TTL = 3600
_LIVE_END_TTL = 300
_LIVE_START_GRACE = 300
# After a door locks, keep the (now "Låst") Live Activity around for a short
# while so its button can flip to "Lås upp" — an undo window — then end it.
_LIVE_LINGER = 60
# How long an app-initiated action stays eligible for attribution.
_ATTRIBUTION_TTL = 20
# Method text shown for an action the app caused.
_APP_METHOD = "App"
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
        # Pending "end" tasks, keyed by (device, door), for the linger window.
        self._live_end_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Recent app-initiated actions, keyed by (door, action), so the lock's
        # own unattributed report can be credited to whoever pressed the button.
        self._pending_attributions: dict[tuple[str, str], dict[str, Any]] = {}

    def cancel_live_ends(self) -> None:
        for task in list(self._live_end_tasks.values()):
            task.cancel()
        self._live_end_tasks.clear()

    # -- incoming events -----------------------------------------------------
    async def on_ha_event(self, event: dict[str, Any]) -> None:
        try:
            mapped = from_ha(self.cfg, event)
            if mapped is None:
                return
            self._attribute(mapped)
            self.store.add_event(mapped)
            await self.broadcast(mapped)
            await self.notify(mapped)
            await self.update_live_activity(mapped)
        except Exception:
            _LOGGER.exception("failed to handle Home Assistant event")

    # -- attribution ----------------------------------------------------------
    def note_app_action(self, door_id: str, action: str, device: dict[str, Any]) -> None:
        """Remember that this device just asked for ``action`` on ``door_id``.

        Recorded *before* Home Assistant is called, because the lock can report
        the operation back before the service call returns.
        """
        self._pending_attributions[(door_id, action)] = {
            "person": device.get("person") or None,
            "device": device.get("name"),
            "expires": time.time() + _ATTRIBUTION_TTL,
        }

    def _attribute(self, ev: dict[str, Any]) -> None:
        """Credit an unattributed lock report to the app action that caused it.

        Only touches reports with no attribution of their own, so a keypad or
        fingerprint entry is never overwritten.
        """
        if ev.get("source") != "unattributed":
            return
        pending = self._pending_attributions.pop((ev["door"], ev["action"]), None)
        if pending is None or pending["expires"] < time.time():
            return
        if pending["person"]:
            ev["person"] = pending["person"]
        ev["source"] = "app"
        ev["method"] = _APP_METHOD

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

    # -- live activities -----------------------------------------------------
    async def update_live_activity(self, ev: dict[str, Any]) -> None:
        """Keep the Lock Screen / Dynamic Island in step with a door."""
        if not self.cfg.live_enabled:
            return
        door = self.cfg.door(ev["door"])
        if door is None:
            return
        locked = ev["action"] != "unlock"
        state = live.content_state(
            locked=locked,
            since=ev["ts"],
            person=ev.get("person"),
            method=ev.get("method"),
        )
        if locked:
            await self._lock_live_activity(door, state)
        else:
            await self._start_or_update_live_activity(
                door, {"doorID": door.id, "doorName": door.name}, state
            )

    async def _start_or_update_live_activity(
        self, door: Door, attributes: dict[str, Any], state: dict[str, Any]
    ) -> None:
        now = time.time()
        existing = {row["device"]: row for row in self.store.live_activities(door.id)}
        sends: list[dict[str, Any]] = []
        for device in self.store.devices():
            # An unlock cancels any pending "lingering locked" end.
            self._cancel_live_end(device["id"], door.id)
            row = existing.get(device["id"])
            if row is not None and row["token"]:
                # The app is up and told us the per-activity token: just update.
                sends.append(
                    self._live_send(
                        device["id"], door, row["token"],
                        live.update_payload(state=state),
                        push_type="update", priority=5, ttl=_LIVE_TTL,
                    )
                )
            elif row is not None and now - row["started"] < _LIVE_START_GRACE:
                continue  # a push-to-start is already in flight; avoid a duplicate
            elif device["live_start_token"]:
                sends.append(
                    self._live_send(
                        device["id"], door, device["live_start_token"],
                        live.start_payload(
                            attributes_type=self.cfg.live_attributes_type,
                            attributes=attributes,
                            state=state,
                        ),
                        push_type="start", priority=10, ttl=_LIVE_TTL,
                    )
                )
                self.store.touch_live_start(device["id"], door.id)
        await self._send_live(sends)

    async def _lock_live_activity(self, door: Door, state: dict[str, Any]) -> None:
        """Show "locked" briefly (so the card can offer Lås upp), then end it."""
        sends: list[dict[str, Any]] = []
        for row in self.store.live_activities(door.id):
            if not row["token"]:
                # A push-to-start we never got a token for: nothing to update.
                self.store.drop_live_activity(row["device"], door.id)
                continue
            sends.append(
                self._live_send(
                    row["device"], door, row["token"],
                    live.update_payload(state=state),
                    push_type="update", priority=10, ttl=_LIVE_TTL,
                )
            )
            self._schedule_live_end(row["device"], door.id, state)
        await self._send_live(sends)

    def _cancel_live_end(self, device_id: str, door_id: str) -> None:
        task = self._live_end_tasks.pop((device_id, door_id), None)
        if task is not None:
            task.cancel()

    def _schedule_live_end(self, device_id: str, door_id: str, state: dict[str, Any]) -> None:
        self._cancel_live_end(device_id, door_id)
        self._live_end_tasks[(device_id, door_id)] = asyncio.create_task(
            self._linger_then_end(device_id, door_id, state)
        )

    async def _linger_then_end(self, device_id: str, door_id: str,
                               state: dict[str, Any]) -> None:
        try:
            await asyncio.sleep(_LIVE_LINGER)
        except asyncio.CancelledError:
            return
        self._live_end_tasks.pop((device_id, door_id), None)
        door = self.cfg.door(door_id)
        if door is not None:
            await self._end_live_for_device(device_id, door, state)

    async def _end_live_for_device(self, device_id: str, door: Door,
                                   state: dict[str, Any]) -> None:
        sends: list[dict[str, Any]] = []
        for row in self.store.live_activities(door.id):
            if row["device"] != device_id:
                continue
            if row["token"]:
                sends.append(
                    self._live_send(
                        row["device"], door, row["token"],
                        live.end_payload(state=state),
                        push_type="end", priority=10, ttl=_LIVE_END_TTL,
                    )
                )
            self.store.drop_live_activity(row["device"], door.id)
        await self._send_live(sends)

    def _live_send(self, device_id: str, door: Door, token: str, payload: dict[str, Any],
                   *, push_type: str, priority: int, ttl: int) -> dict[str, Any]:
        return {
            "device": device_id, "door": door.id, "token": token, "payload": payload,
            "push_type": push_type, "priority": priority,
            "expiration": int(time.time()) + ttl,
        }

    async def _send_live(self, sends: list[dict[str, Any]]) -> None:
        topic = live.topic(self.cfg.bundle_id)

        async def one(send: dict[str, Any]) -> None:
            async with self._send_sem:
                result = await self.apns.send(
                    send["token"], send["payload"], push_type=send["push_type"],
                    priority=send["priority"], topic=topic, expiration=send["expiration"],
                )
            if result.invalidate_token:
                if send["push_type"] == "start":
                    self.store.set_live_start_token(send["device"], "")
                else:
                    self.store.drop_live_activity(send["device"], send["door"])

        await asyncio.gather(*(one(s) for s in sends))

    # -- actions -------------------------------------------------------------
    async def do_action(self, door_id: str, action: str,
                        device: dict[str, Any] | None = None) -> dict[str, Any]:
        door = self.cfg.door(door_id)
        if door is None or action not in ("lock", "unlock"):
            raise HTTPException(400, "unknown door or action")
        if device is not None:
            # Before the service call: the lock may report back first.
            self.note_app_action(door.id, action, device)
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
        state.cancel_live_ends()
        await state.apns.stop()

    app = FastAPI(title="Hemnyckel relay", version="0.1.0", lifespan=lifespan)
    # Expose the runtime state for tests and debugging (app.state.hmk).
    app.state.hmk = state
    # The API lives under /api (as the app and docs expect); /health stays at the
    # root so the add-on and proxies can probe it directly.
    api = APIRouter(prefix="/api")

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "ha": state.ha.connected,
            "apns": state.apns.live,
            "doors": len(cfg.doors),
            "version": app.version,
        }

    @api.post("/pair")
    async def pair(payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code", "")).upper()
        if not code or code != state.pair_code or time.time() > state.pair_expires:
            raise HTTPException(401, "invalid or expired code")
        device_id = uuid.uuid4().hex
        state.store.add_device(device_id, str(payload.get("name") or "Enhet"))
        state.pair_code = secrets.token_hex(3).upper()
        state.pair_expires = time.time() + 600
        return {"device_token": device_id, "relay_id": "hemnyckel"}

    @api.post("/register")
    async def register(payload: dict[str, Any], device: dict = Depends(require_device)) -> dict[str, Any]:
        state.store.set_apns(
            device["id"],
            str(payload.get("apns_token") or ""),
            payload.get("person"),
            payload.get("prefs") or {},
            normalize_env(payload.get("apns_env") or cfg.apns_env),
        )
        return {"ok": True}

    @api.get("/events")
    async def events(_: dict = Depends(require_device), since: float | None = None,
                     door: str | None = None, person: str | None = None,
                     limit: int = 200) -> dict[str, Any]:
        return {"events": state.store.events(since=since, door=door, person=person, limit=limit)}

    @api.get("/state")
    async def door_states(_: dict = Depends(require_device)) -> dict[str, Any]:
        doors = [await state.door_state(d) for d in cfg.doors]
        presence: dict[str, str] = {}
        for d in cfg.doors:
            ev = state.store.last_event(d.id)
            if ev and ev.get("person"):
                presence[ev["person"]] = "home" if ev["action"] == "unlock" else "away"
        return {"doors": doors, "presence": presence, "relay": {"online": True, "apns": state.apns.live}}

    @api.post("/action")
    async def action(payload: dict[str, Any],
                     device: dict = Depends(require_device)) -> dict[str, Any]:
        return await state.do_action(
            str(payload.get("door")), str(payload.get("action")), device
        )

    # -- live activities (Lock Screen / Dynamic Island) ---------------------
    @api.post("/live/start-token")
    async def live_start_token(payload: dict[str, Any],
                               device: dict = Depends(require_device)) -> dict[str, Any]:
        """The device's push-to-start token (lets the relay start an activity)."""
        state.store.set_live_start_token(device["id"], str(payload.get("apns_token") or ""))
        return {"ok": True}

    @api.post("/live/activity")
    async def live_activity(payload: dict[str, Any],
                            device: dict = Depends(require_device)) -> dict[str, Any]:
        """The per-activity update token the app reports once an activity exists."""
        door_id = str(payload.get("door") or "")
        if cfg.door(door_id) is None:
            raise HTTPException(400, "unknown door")
        state.store.set_live_activity(
            device["id"], door_id, str(payload.get("apns_token") or "")
        )
        return {"ok": True}

    @api.delete("/live/activity")
    async def live_activity_end(door: str,
                                device: dict = Depends(require_device)) -> dict[str, Any]:
        state.store.drop_live_activity(device["id"], door)
        return {"ok": True}

    @api.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        state.sockets.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            state.sockets.discard(websocket)

    # The same health handler at the root, for probes.
    app.add_api_route("/health", health)
    app.include_router(api)
    return app
