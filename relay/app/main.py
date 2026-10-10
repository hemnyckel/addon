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
from datetime import UTC, datetime
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from . import __version__, avatar, energy, live
from .apns import ApnsClient
from .config import Config, Door, load_config, normalize_env
from .events import from_ha
from .ha import HaClient
from .mqtt import MqttBridge
from .store import EVENTS_MAX, Store

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
# A lock with no attribution that follows an unlock within this window is the
# lock's own auto-relock (the lock reports it as "unattributed", like any other).
_AUTO_RELOCK_WINDOW = 30
_AUTO_METHOD = "Automatiskt"
# Pairing is the only unauthenticated endpoint, and the relay may be reachable
# from the internet through a reverse proxy: bound the attempts per client.
_PAIR_MAX_ATTEMPTS = 10
_PAIR_WINDOW = 60
# How long a pairing code stays valid.
_PAIR_TTL = 600
# Roles a device can hold. Guests (a time window and chosen doors) come later.
_ROLES = {"owner", "user"}
# The integration's schedule names its weekdays with lowercase three-letter
# codes, Monday first; the invite stores ISO weekday numbers (Monday = 1).
_DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Upper bound on concurrent pushes, so a busy household can't open thousands
# of connections at once.
_MAX_CONCURRENT_PUSHES = 16
# One clean, human answer when Home Assistant or the lock will not take a call;
# the raw upstream error never reaches the app.
_UPSTREAM_ERROR = "the lock is not reachable right now; try again"
# Credential-kind labels used to derive marks from the individual booleans.
_CREDENTIAL_KINDS = (("pin", "has_pin"), ("fingerprint", "has_fingerprint"), ("rfid", "has_rfid"))
# The read-side pulse compares two reads of the same journal, so an event that
# lands between them may make them differ by a fraction of a second. That is not
# a divergence: the bug this watches for hid whole windows, never a second.
_JOURNAL_READ_EPSILON = 1.0
# The energy pulse: how often the price plan is recomputed (and a window
# started or ended), and the settings that must survive an add-on restart.
_ENERGY_INTERVAL = 60
_ENERGY_ACTIVE_KEY = "energy_activity"
_ENERGY_BRIEFED_KEY = "energy_briefed"
_ENERGY_WINDOW_KEY = "energy_window_minutes"
# The household's chosen window length is clamped to something a machine can
# actually run in: a quarter-hour at the shortest, eight hours at the longest.
_ENERGY_WINDOW_MIN = 15
_ENERGY_WINDOW_MAX = 480


def _prefs(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _device_doors(device: dict[str, Any]) -> list[str] | None:
    """The doors a guest may use, or None meaning "all"."""
    raw = device.get("doors")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return [str(v) for v in value] if isinstance(value, list) else None


def _guest_expired(device: dict[str, Any]) -> bool:
    expires = device.get("expires")
    return bool(device.get("role") == "guest" and expires is not None
                and time.time() > float(expires))


def _json_list(raw: Any) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _json_map(raw: Any) -> dict[str, int]:
    """A JSON object of door -> slot, or an empty map when malformed."""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, slot in value.items():
        try:
            result[str(key)] = int(slot)
        except (TypeError, ValueError):
            continue
    return result


def _schedule_window(days: list[int], from_time: str | None,
                     to_time: str | None) -> dict[str, Any]:
    """Map an invite's window to the integration's schedule shape.

    The invite stores ISO weekday numbers (Monday = 1) and "HH:MM"; the
    integration wants lowercase three-letter day codes and ``start``/``end``.
    An absent or unusable time becomes the integration's full-day window
    ("00:00"-"00:00", an end at the start crossing midnight).
    """
    start = from_time if _parse_hhmm(from_time or "") is not None else "00:00"
    end = to_time if _parse_hhmm(to_time or "") is not None else "00:00"
    return {
        "days": [_DAY_CODES[d - 1] for d in days if 1 <= d <= 7],
        "start": start,
        "end": end,
    }


def _iso_until(expires: float) -> str:
    """An invite's expiry (epoch seconds) as the ISO timestamp the lock wants."""
    return datetime.fromtimestamp(expires, tz=UTC).isoformat()


def _schedule_ok(device: dict[str, Any], when: float) -> bool:
    """A guest's access window: chosen weekdays and times (empty means any)."""
    if device.get("role") != "guest":
        return True
    local = time.localtime(when)
    days = [int(d) for d in _json_list(device.get("days")) if str(d).isdigit()]
    if days and (local.tm_wday + 1) not in days:  # ISO weekday, Monday = 1
        return False
    start = _parse_hhmm(str(device.get("from_time") or ""))
    end = _parse_hhmm(str(device.get("to_time") or ""))
    if start is None or end is None or start == end:
        return True
    now = local.tm_hour * 60 + local.tm_min
    return start <= now < end if start < end else (now >= start or now < end)


def _parse_hhmm(value: str) -> int | None:
    try:
        hour, minute = value.split(":")
        hours, minutes = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    if 0 <= hours < 24 and 0 <= minutes < 60:
        return hours * 60 + minutes
    return None


def _in_quiet(prefs: dict[str, Any], when: float) -> bool:
    """Is this moment inside the device's quiet hours?"""
    quiet = prefs.get("quiet") or {}
    start = _parse_hhmm(str(quiet.get("from") or ""))
    end = _parse_hhmm(str(quiet.get("to") or ""))
    if start is None or end is None or start == end:
        return False
    local = time.localtime(when)
    now = local.tm_hour * 60 + local.tm_min
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps midnight


def _wants(prefs: dict[str, Any], ev: dict[str, Any]) -> bool:
    """The device's notification preferences for this event.

    Order matters: a switch first, then who it cares about, then quiet hours —
    except for the people it always wants to hear about (the kids).
    """
    if prefs.get("enabled") is False:
        return False
    person = ev.get("person")
    people = prefs.get("people") or []
    if people and (not person or person not in people):
        return False
    if person and person in (prefs.get("watch") or []):
        return True  # always, even in quiet hours
    return not _in_quiet(prefs, ev["ts"])


def _minutes_now(when: float) -> int:
    local = time.localtime(when)
    return local.tm_hour * 60 + local.tm_min


def _json_obj(raw: Any) -> dict[str, Any] | None:
    """A JSON object, or None when the value is absent or malformed."""
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _price_attributes(states: list[dict[str, Any]], entity: str) -> dict[str, Any] | None:
    """One entity's attributes, or None when Home Assistant does not carry it."""
    for state in states:
        if state.get("entity_id") == entity:
            attributes = state.get("attributes")
            return attributes if isinstance(attributes, dict) else {}
    return None


def _energy_brief_wants(device: dict[str, Any], when: float) -> bool:
    """May this device get the daily price briefing right now?"""
    if device["role"] == "guest":
        return False
    if not device["apns_token"]:
        return False
    prefs = _prefs(device["prefs"])
    if prefs.get("enabled") is False:
        return False
    energy_prefs = prefs.get("energy") or {}
    if energy_prefs.get("morning", True) is False:
        return False
    return not _in_quiet(prefs, when)


def _energy_live_wants(device: dict[str, Any]) -> bool:
    """May this device get the cheap-window Live Activity (always silent)?"""
    if device["role"] == "guest":
        return False
    prefs = _prefs(device["prefs"])
    if prefs.get("enabled") is False:
        return False
    energy_prefs = prefs.get("energy") or {}
    return energy_prefs.get("live", True) is not False


# -- slots: the lock's code table, resolved from Home Assistant --------------

def _attributes(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


# The entity name each slot sensor carries, for the fallback below.
_SENSOR_LABEL = {"_slots": "Slots", "_lock_facts": "Lock facts"}


def _slot_sensor(states: list[dict[str, Any]], door: Door,
                 suffix: str) -> dict[str, Any] | None:
    """Find a door's sensor by the lock name it carries, then by entry id.

    The slots and lock-facts sensors both carry the lock's human name (the same
    one a household sees, e.g. "Ytterdörren"). The live entry id — not the one
    in the relay's config, which is a stale hint — is what a service call needs,
    so it is read from the sensor itself.
    """
    matches = [
        state for state in states
        if str(state.get("entity_id") or "").startswith("sensor.")
        and str(state.get("entity_id") or "").endswith(suffix)
    ]
    for state in matches:
        if _attributes(state).get("lock") == door.name:
            return state
    # The integration names the entity after the lock ("Ytterdörren Slots"), so
    # the friendly name identifies it even without an explicit lock attribute.
    label = _SENSOR_LABEL.get(suffix)
    if label:
        wanted = f"{door.name} {label}"
        for state in matches:
            if _attributes(state).get("friendly_name") == wanted:
                return state
    if door.entry_id:
        for state in matches:
            if _attributes(state).get("entry_id") == door.entry_id:
                return state
    return None


def _lock_facts(states: list[dict[str, Any]], door: Door,
                slots_state: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
    """The matching lock-facts attributes, if the lock has reported them."""
    entity_id = str(slots_state.get("entity_id") or "")
    if entity_id.endswith("_slots"):
        sibling = entity_id[: -len("_slots")] + "_lock_facts"
        for state in states:
            if state.get("entity_id") == sibling:
                return _attributes(state)
    facts_state = _slot_sensor(states, door, "_lock_facts")
    if facts_state is None:
        # On an older sensor that carries no name: match by the entry id.
        facts_state = next(
            (state for state in states
             if str(state.get("entity_id") or "").startswith("sensor.")
             and str(state.get("entity_id") or "").endswith("_lock_facts")
             and entry_id
             and _attributes(state).get("entry_id") == entry_id),
            None,
        )
    return _attributes(facts_state) if facts_state is not None else None


def _capacity(facts: dict[str, Any] | None) -> dict[str, Any] | None:
    if not facts:
        return None
    return {
        "pin": facts.get("pin_users"),
        "rfid": facts.get("rfid_users"),
        "total": facts.get("total_users"),
    }


def _slot_rows(attrs: dict[str, Any], door_id: str) -> list[dict[str, Any]]:
    """The lock's slot table, sorted by number, with names and credential marks."""
    rows: list[dict[str, Any]] = []
    for raw in attrs.get("slots") or []:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw["slot"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(raw.get("name") or "")
        marks = {
            "has_pin": bool(raw.get("has_pin")),
            "has_fingerprint": bool(raw.get("has_fingerprint")),
            "has_rfid": bool(raw.get("has_rfid")),
        }
        credentials = raw.get("credentials")
        if not isinstance(credentials, list):
            credentials = [kind for kind, key in _CREDENTIAL_KINDS if marks[key]]
        fingers = raw.get("fingers")
        rows.append({
            "slot": number,
            "door": door_id,
            "name": name,
            "occupied": bool(name) or bool(credentials),
            **marks,
            "finger_used": bool(raw.get("finger_used")),
            # The finger labels travel through unchanged: the integration owns
            # them, and the relay only carries them to the app. ``finger_state``
            # is the integration's own display state (none/claimed/confirmed).
            "finger_state": str(raw.get("finger_state") or ""),
            "fingers": [
                dict(item) for item in (fingers if isinstance(fingers, list) else [])
                if isinstance(item, dict)
            ],
            "credentials": [str(kind) for kind in credentials],
        })
    rows.sort(key=lambda row: row["slot"])
    return rows


def _service_row(response: Any, entry_id: str) -> dict[str, Any] | None:
    """Pick one entry's record out of a Home Assistant service response."""
    if not isinstance(response, dict):
        return None
    row = response.get(entry_id)
    if isinstance(row, dict):
        return row
    return next((value for value in response.values() if isinstance(value, dict)), None)


def _guest_rows(response: Any, entry_id: str) -> list[dict[str, Any]]:
    """One entry's guest rows out of a ``list_guests`` service response."""
    if not isinstance(response, dict):
        return []
    rows = response.get(entry_id)
    if not isinstance(rows, list):
        rows = next((value for value in response.values() if isinstance(value, list)), None)
    return [row for row in (rows or []) if isinstance(row, dict)]


def _refusal_reason(response: Any) -> str:
    """Home Assistant's own words for a refusal, when it gave any.

    The integration raises a validation error for a request it will not take,
    and Home Assistant answers 400 with the message; that reason is the honest
    thing to hand the app. Anything without one falls back to a plain phrase.
    """
    if isinstance(response, dict):
        for key in ("message", "error"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class State:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.data_dir, share_dir=avatar.SHARE_AVATAR_DIR)
        self.store.ensure_owner()
        self.apns = ApnsClient(cfg)
        # The MQTT bridge is a projection of what this store holds, published to
        # Home Assistant; it stays off unless the supervisor injected a broker.
        self.mqtt = MqttBridge(cfg, self.store, version=__version__, facts=self.health)
        # The bridge connects to the broker before Home Assistant is up, so its
        # first retained document says "ha": false. The HA connection coming up
        # is the moment that becomes untrue, and this republishes the facts.
        self.ha = HaClient(cfg, self.on_ha_event, on_connected=self.on_ha_connected)
        self.pair_code = ""
        self.pair_expires = 0.0
        self.new_pair_code()
        self.sockets: set[WebSocket] = set()
        self._send_sem = asyncio.Semaphore(_MAX_CONCURRENT_PUSHES)
        # Pending "end" tasks, keyed by (device, door), for the linger window.
        self._live_end_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Recent app-initiated actions, keyed by (door, action), so the lock's
        # own unattributed report can be credited to whoever pressed the button.
        self._pending_attributions: dict[tuple[str, str], dict[str, Any]] = {}
        # When each door was last unlocked, to recognise its automatic relock.
        self._last_unlock: dict[str, float] = {}
        # Pairing attempts per client, to bound brute force.
        self._pair_attempts: dict[str, list[float]] = {}
        # Whether the journal's read path was already reported as diverging, so
        # the warning is logged once per divergence and not on every poll.
        self._journal_diverged = False
        # The newest computed price plan, served by /api/energy between ticks.
        self._energy_plan: dict[str, Any] | None = None

    def _journal_status(self) -> tuple[float | None, float | None, bool]:
        """The journal's read side: newest ingested, newest readable, and whether
        the read path is keeping up.

        ``last_event_at()`` is the ingest truth; the readable timestamp is taken
        through the *same* ``Store.events()`` the ``/api/events`` handler serves
        from, one row deep - not a second hand-rolled query. The read-side bug
        this guards against returned the oldest window, so once the journal
        outgrew it the newest events were ingested but never readable. Comparing
        the two reads catches exactly that (and would catch any future window
        mistake), while a tiny slack absorbs an event arriving in between.

        A divergence is logged once, when it appears, not on every poll.
        """
        ingested = self.store.last_event_at()
        newest = self.store.events(limit=1)
        read_at = float(newest[-1]["ts"]) if newest else None
        if ingested is None or read_at is None:
            # An empty journal is healthy; a readable event with no ingest
            # truth (or the reverse) is not.
            ok = ingested is None and read_at is None
        else:
            ok = read_at >= ingested - _JOURNAL_READ_EPSILON
        if not ok and not self._journal_diverged:
            _LOGGER.warning(
                "journal read path is behind: newest readable event %s, "
                "newest ingested %s",
                read_at, ingested,
            )
        self._journal_diverged = not ok
        return ingested, read_at, ok

    def health(self) -> dict[str, Any]:
        """The relay's facts, shared by ``/health`` and the MQTT bridge.

        ``last_event_at`` is the journal's *ingest* pulse - the newest event's
        timestamp (None on an empty journal) - and ``events`` is how much it
        holds. ``journal_read_at`` is the newest timestamp the read path
        actually hands back (built through the same ``Store.events()`` the app
        calls), and ``journal_ok`` says whether the read side shows the newest
        ingested event. Ingest and read are watched separately on purpose: the
        first bug was invisible to an ingest-only pulse because the events were
        stored and simply never readable.
        """
        ingested, read_at, ok = self._journal_status()
        return {
            "status": "ok",
            "ha": self.ha.connected,
            "apns": self.apns.live,
            "doors": len(self.cfg.doors),
            "energy": self.cfg.energy_enabled,
            "version": __version__,
            "last_event_at": ingested,
            "journal_read_at": read_at,
            "journal_ok": ok,
            "events": self.store.event_count(),
        }

    async def on_ha_connected(self) -> None:
        """Home Assistant is up: refresh the facts, then learn its origin.

        Awaitable from the websocket session, so the base URL is fetched and the
        projection republished before the session settles.
        """
        self.mqtt.publish_state()
        await self.refresh_base_url()

    async def refresh_base_url(self) -> None:
        """Learn Home Assistant's origin, then republish the projection.

        A photo's ``entity_picture`` must be absolute (Home Assistant rejects a
        relative one), so the projection is only complete once Home Assistant
        answers with its own URL. On a change the whole projection is
        republished, so the retained discovery converges.
        """
        if self.mqtt.set_base_url(await self.ha.base_url()):
            self.mqtt.publish_all_now()

    def cancel_live_ends(self) -> None:
        for task in list(self._live_end_tasks.values()):
            task.cancel()
        self._live_end_tasks.clear()

    def new_pair_code(self) -> str:
        """Mint a fresh, short-lived pairing code (at startup and on demand)."""
        self.pair_code = secrets.token_hex(3).upper()
        self.pair_expires = time.time() + _PAIR_TTL
        return self.pair_code

    def pairing_allowed(self, client: str) -> bool:
        """Record a pairing attempt; False once the client is over the limit."""
        now = time.time()
        recent = [t for t in self._pair_attempts.get(client, []) if now - t < _PAIR_WINDOW]
        if len(recent) >= _PAIR_MAX_ATTEMPTS:
            self._pair_attempts[client] = recent
            return False
        recent.append(now)
        self._pair_attempts[client] = recent
        return True

    # -- incoming events -----------------------------------------------------
    async def on_ha_event(self, event: dict[str, Any]) -> None:
        try:
            mapped = from_ha(self.cfg, event)
            if mapped is None:
                return
            if mapped["source"] == "hemsmart":
                # The family's home app asked for this. Its identity is not a
                # door event of its own — it is the missing attribution for the
                # report the lock is already on its way to send, unattributed.
                # Recorded before that report lands, exactly like this app's own
                # action; the report then carries the name instead of a second
                # notification.
                self.note_app_action(
                    mapped["door"], mapped["action"],
                    {"name": "Hemsmart", "person": mapped.get("person")},
                    method="Hemsmart",
                )
                return
            self._attribute(mapped)
            self._classify_auto_relock(mapped)
            self._track_unlock(mapped)
            previous = self.store.last_event(mapped["door"])
            self.store.add_event(mapped)
            # The journal's pulse moved: publish it now, so Home Assistant sees
            # the journal is receiving without waiting for the quiet timer.
            self.mqtt.publish_state()
            # A new last_seen is a change to the person's state (rule 6).
            if mapped.get("person"):
                self.mqtt.refresh(mapped["person"])
            await self.broadcast(mapped)
            await self.notify(mapped, previous=previous)
            await self.update_live_activity(mapped)
        except Exception:
            _LOGGER.exception("failed to handle Home Assistant event")

    # -- attribution ----------------------------------------------------------
    def note_app_action(self, door_id: str, action: str, device: dict[str, Any],
                        method: str = _APP_METHOD) -> None:
        """Remember that this device just asked for ``action`` on ``door_id``.

        Recorded *before* Home Assistant is called, because the lock can report
        the operation back before the service call returns.

        ``method`` is how the credit will read: "App" for this app's own
        screens, "Hemsmart" when the family's home app asked instead. Both are
        the same kind of fact — whose phone it was — so both take the same path.
        """
        self._pending_attributions[(door_id, action)] = {
            "person": device.get("person") or None,
            "device": device.get("name"),
            "method": method,
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
        ev["method"] = pending.get("method") or _APP_METHOD

    def _classify_auto_relock(self, ev: dict[str, Any]) -> None:
        """Recognise a door's own auto-relock.

        The lock reports it as "unattributed", exactly like a manual lock, so it
        is inferred: a lock with no attribution of its own, on a door that was
        unlocked a moment ago, is the lock closing itself. Labelled "auto" so it
        reads "Automatiskt" in history and never notifies.
        """
        if ev.get("action") != "lock" or ev.get("source") != "unattributed":
            return
        unlocked_at = self._last_unlock.get(ev["door"])
        if unlocked_at is None or ev["ts"] - unlocked_at > _AUTO_RELOCK_WINDOW:
            return
        ev["source"] = "auto"
        ev["method"] = _AUTO_METHOD

    def _track_unlock(self, ev: dict[str, Any]) -> None:
        if ev.get("action") == "unlock":
            self._last_unlock[ev["door"]] = ev["ts"]

    # -- push ----------------------------------------------------------------
    def _payload(self, ev: dict[str, Any], door: Door) -> dict[str, Any]:
        # `aps.alert` is the fallback for a phone whose Notification Service
        # Extension does not run: Swedish, chosen here. The `event` alongside it
        # is the facts, including the door's human name, so the phone can write
        # the visible words itself, in its own language.
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
            "event": {**ev, "door_name": door.name},
        }

    async def notify(self, ev: dict[str, Any], *,
                     previous: dict[str, Any] | None = None) -> None:
        door = self.cfg.door(ev["door"])
        if door is None:
            return
        # System events (auto-relock) are mirrored but never interrupt anyone.
        if ev.get("source") == "auto":
            return
        # A report that repeats the door's last action changes nothing — some
        # locks emit a redundant "lock" every hour. Never notify for noise.
        if previous is not None and previous.get("action") == ev["action"]:
            return
        payload = self._payload(ev, door)
        expiration = int(time.time()) + _ALERT_TTL
        # Collapse a burst on the same door and action into one notification.
        collapse_id = f"door-{door.id}-{ev['action']}"[:64]

        targets = []
        for device in self.store.devices():
            if device["role"] == "guest":
                continue  # a guest is not notified
            if not device["apns_token"]:
                continue
            prefs = _prefs(device["prefs"])
            if prefs.get("doors") and door.id not in prefs["doors"]:
                continue
            if ev.get("person") and prefs.get("skip_self") and device["person"] == ev["person"]:
                continue
            if not _wants(prefs, ev):
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
            source=ev.get("source"),
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
            if device["role"] == "guest":
                continue  # no push-driven Live Activity for guests
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

    def _live_send(self, device_id: str, door: Door | None, token: str, payload: dict[str, Any],
                   *, push_type: str, priority: int, ttl: int,
                   kind: str = "door") -> dict[str, Any]:
        return {
            "device": device_id, "door": door.id if door is not None else "",
            "token": token, "payload": payload,
            "push_type": push_type, "priority": priority, "kind": kind,
            "expiration": int(time.time()) + ttl,
        }

    async def _send_live(self, sends: list[dict[str, Any]]) -> None:
        topic = live.topic(self.cfg.bundle_id)

        async def one(send: dict[str, Any]) -> None:
            async with self._send_sem:
                result = await self.apns.send(
                    # The HTTP push type of every Live Activity push is
                    # "liveactivity"; the start/update/end distinction lives in
                    # the payload's aps.event. Sending "update" as the header
                    # makes APNs answer 400 InvalidPushType.
                    send["token"], send["payload"], push_type="liveactivity",
                    priority=send["priority"], topic=topic, expiration=send["expiration"],
                )
            if result.invalidate_token:
                if send.get("kind") == "energy":
                    if send["push_type"] == "start":
                        self.store.set_live_energy_start_token(send["device"], "")
                    else:
                        self.store.drop_energy_activity(send["device"])
                elif send["push_type"] == "start":
                    self.store.set_live_start_token(send["device"], "")
                else:
                    self.store.drop_live_activity(send["device"], send["door"])

        await asyncio.gather(*(one(s) for s in sends))

    # -- energy: the cheapest hours of the day ------------------------------
    def energy_window_minutes(self) -> int:
        """The household's chosen window length, clamped to something sane.

        The length lives in the store (set from the app by an owner), so every
        phone and the scheduler agree; the add-on option is only the default.
        """
        stored = self.store.setting(_ENERGY_WINDOW_KEY)
        try:
            minutes = int(stored) if stored else self.cfg.energy_window_minutes
        except (TypeError, ValueError):
            minutes = self.cfg.energy_window_minutes
        return max(_ENERGY_WINDOW_MIN, min(minutes, _ENERGY_WINDOW_MAX))

    async def compute_energy_plan(self) -> dict[str, Any] | None:
        """Read the price sensor from Home Assistant and compute the plan.

        None means the module is off, or Home Assistant carries no usable price
        sensor - there is nothing honest to show, so the caller says so.
        """
        if not self.cfg.energy_enabled:
            return None
        states = await self.ha.states()
        attributes = _price_attributes(states, self.cfg.price_entity)
        if attributes is None:
            return None
        now = time.time()
        currency = str(attributes.get("currency") or "").strip() or self.cfg.energy_currency
        return energy.plan(
            attributes,
            now=now,
            window_minutes=self.energy_window_minutes(),
            divisor=self.cfg.price_divisor,
            currency=currency,
            entity=self.cfg.price_entity,
        )

    async def run_energy(self) -> None:
        """The price pulse: brief in the morning, run the cheap window."""
        while True:
            try:
                await self._energy_tick()
            except Exception:
                _LOGGER.exception("energy tick failed")
            await asyncio.sleep(_ENERGY_INTERVAL)

    async def _energy_tick(self) -> None:
        if not self.cfg.energy_enabled or not self.cfg.ha_configured:
            return
        plan = await self.compute_energy_plan()
        if plan is None or not plan.get("available"):
            return
        self._energy_plan = plan
        now = time.time()
        await self._maybe_brief_energy(plan, now)
        await self._sync_energy_activity(plan, now)

    async def _maybe_brief_energy(self, plan: dict[str, Any], now: float) -> None:
        """One briefing a day, once the morning time has passed.

        The day is recorded *before* sending, in the store rather than in
        memory, so an add-on restart at 09:00 never sends a second briefing.
        """
        window = plan.get("ahead")
        if not window:
            return
        today = datetime.fromtimestamp(now).date().isoformat()
        if self.store.setting(_ENERGY_BRIEFED_KEY) == today:
            return
        target = _parse_hhmm(self.cfg.energy_morning_time)
        if target is None or _minutes_now(now) < target:
            return
        self.store.set_setting(_ENERGY_BRIEFED_KEY, today)
        title, body = energy.briefing_text(window, now)
        payload = energy.notification_payload(
            title=title, body=body, window=window, currency=plan["currency"]
        )
        expiration = int(now) + _ALERT_TTL
        targets = [
            device for device in self.store.devices()
            if _energy_brief_wants(device, now)
        ]

        async def deliver(device: Any) -> None:
            async with self._send_sem:
                result = await self.apns.send(
                    device["apns_token"], payload,
                    env=normalize_env(device["apns_env"] or self.cfg.apns_env),
                    expiration=expiration, collapse_id="energy-briefing",
                )
            if result.invalidate_token:
                self.store.disable_apns(device["id"])

        await asyncio.gather(*(deliver(device) for device in targets))

    async def _sync_energy_activity(self, plan: dict[str, Any], now: float) -> None:
        """Run one Live Activity for the day's cheapest window, on time.

        The record of the running window lives in the store, so a restart in the
        middle of a window resumes it instead of starting a second one - and it
        is always ended (or its token pruned) once the window has closed.
        """
        active = _json_obj(self.store.setting(_ENERGY_ACTIVE_KEY))
        if active and now >= float(active.get("end", 0)):
            await self._end_energy_activity(active, plan)
            self.store.set_setting(_ENERGY_ACTIVE_KEY, "")
            active = None
        if active is not None:
            return
        for day in plan.get("days") or []:
            window = day.get("cheapest")
            if not window:
                continue
            if float(window["start"]) <= now < float(window["end"]):
                record = {
                    "date": day["date"],
                    "start": window["start"],
                    "end": window["end"],
                    "average": window["average"],
                    "lowest": window["lowest"],
                }
                self.store.set_setting(_ENERGY_ACTIVE_KEY, json.dumps(record))
                await self._start_energy_activity(record, plan)
                return

    async def _start_energy_activity(self, record: dict[str, Any],
                                     plan: dict[str, Any]) -> None:
        state = energy.live_state(
            start=record["start"], end=record["end"],
            average=record["average"], lowest=record["lowest"],
            currency=plan["currency"],
        )
        attributes = {"day": record["date"]}
        sends: list[dict[str, Any]] = []
        for device in self.store.devices():
            if not _energy_live_wants(device):
                continue
            token = device["live_energy_start_token"]
            if not token:
                continue
            sends.append(self._live_send(
                device["id"], None, token,
                energy.start_payload(
                    attributes_type=self.cfg.energy_attributes_type,
                    attributes=attributes, state=state,
                ),
                push_type="start", priority=10, ttl=_LIVE_TTL, kind="energy",
            ))
            self.store.touch_energy_start(device["id"])
        await self._send_live(sends)

    async def _end_energy_activity(self, record: dict[str, Any],
                                   plan: dict[str, Any]) -> None:
        state = energy.live_state(
            start=record["start"], end=record["end"],
            average=float(record.get("average") or 0.0),
            lowest=float(record.get("lowest") or 0.0),
            currency=plan["currency"],
        )
        rows = self.store.energy_activities()
        sends = [
            self._live_send(
                row["device"], None, row["token"],
                energy.end_payload(state=state),
                push_type="end", priority=10, ttl=_LIVE_END_TTL, kind="energy",
            )
            for row in rows if row["token"]
        ]
        await self._send_live(sends)
        for row in rows:
            self.store.drop_energy_activity(row["device"])

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

    # -- slots: the lock's code table ----------------------------------------
    async def slots_snapshot(self, door_id: str) -> dict[str, Any]:
        """One door's slot table, resolved live from Home Assistant's states."""
        door = self.cfg.door(door_id)
        if door is None:
            raise HTTPException(400, "unknown door")
        states = await self.ha.states()
        slots_state = _slot_sensor(states, door, "_slots")
        if slots_state is None:
            # The door exists, the lock has not reported a slot table (yet).
            return {"door": door.id, "name": door.name, "capacity": None, "slots": []}
        attrs = _attributes(slots_state)
        entry_id = str(attrs.get("entry_id") or door.entry_id or "")
        facts = _lock_facts(states, door, slots_state, entry_id)
        return {
            "door": door.id,
            "name": door.name,
            "capacity": _capacity(facts),
            "slots": _slot_rows(attrs, door.id),
        }

    async def slot_entry_id(self, door_id: str) -> str:
        """The live config entry id for a door, read from Home Assistant.

        The integration re-mints an entry id whenever its entry is re-created,
        so the config's value is only a fallback; the slots sensor carries the
        one a service call must use today.
        """
        door = self.cfg.door(door_id)
        if door is None:
            raise HTTPException(400, "unknown door")
        states = await self.ha.states()
        slots_state = _slot_sensor(states, door, "_slots")
        entry_id = ""
        if slots_state is not None:
            entry_id = str(_attributes(slots_state).get("entry_id") or "")
        entry_id = entry_id or door.entry_id or ""
        if not entry_id:
            raise HTTPException(409, "this door's slot table is not known to Home Assistant yet")
        return entry_id

    # -- guest identity: one invitation, matching codes on each door ----------
    async def create_lock_guest(self, door_id: str, name: str, days: list[int],
                                from_time: str | None, to_time: str | None,
                                expires: float) -> dict[str, Any] | None:
        """Write a matching guest code on one door, best-effort.

        Weekdays mean a recurring guest (the integration enforces the window);
        without them, a simple guest that expires with the invitation. The
        name rides on the slot, so a later keypad event attributes to the person.

        Returns ``{"slot", "code", "until"}`` or None when this door's lock
        cannot take it — the invitation still stands, so an unreachable lock
        never blocks it. The code is returned to the caller once and is never
        logged or stored.
        """
        try:
            entry_id = await self.slot_entry_id(door_id)
        except HTTPException as err:
            _LOGGER.warning("invite %s: no slot table for door %s (%s)",
                            name, door_id, err.detail)
            return None
        if days:
            service = "create_recurring_guest"
            data: dict[str, Any] = {
                "name": name,
                "schedule": [_schedule_window(days, from_time, to_time)],
                # The end date travels with the code whatever its kind: a weekly
                # window must not let a cleaner's code outlive the arrangement.
                "until": _iso_until(expires),
                "entry_id": entry_id,
            }
        else:
            service = "create_guest_code"
            data = {"name": name, "entry_id": entry_id, "until": _iso_until(expires)}
        ok, response = await self.ha.call_service_result("hemnyckel", service, data)
        if not ok:
            _LOGGER.warning("invite %s: %s refused on door %s", name, service, door_id)
            return None
        row = _service_row(response, entry_id)
        code = str((row or {}).get("code") or "")
        slot = (row or {}).get("slot")
        if not code or slot is None:
            _LOGGER.warning("invite %s: %s returned no code for door %s",
                            name, service, door_id)
            return None
        return {"slot": int(slot), "code": code, "until": (row or {}).get("until")}

    async def revoke_invite_codes(self, device_id: str) -> None:
        """Remove the lock codes a guest holds, best-effort.

        Called when a guest device is revoked and when an expired guest is
        refused. A guest is a *person*: once they have been edited, their codes
        live in the person-level registry rather than one invitation, so a
        person with another device still in place keeps the shared codes and the
        registry is only cleared when their last device goes. Before any edit,
        the invitation's own slots are used exactly as before. The stored slots
        are cleared after the attempt, so a refusal revokes once and is not
        retried; a door that is unreachable never fails the revocation.
        """
        device = self.store.device(device_id)
        person = str(device["person"]) if device is not None and device["person"] else None
        invite = self.store.invite_for_device(device_id)
        registry = self.store.guest_slots(person) if person else {}
        if registry:
            if invite is not None:
                self.store.clear_invite_slots(str(invite["code"]))
            if person and self.store.other_device_count(person, device_id) > 0:
                return  # another of this person's devices still uses the codes
            if person:
                self.store.set_guest_slots(person, {})
            slots = registry
        else:
            if invite is None:
                return
            slots = _json_map(invite["slots"])
            if not slots:
                return
            self.store.clear_invite_slots(str(invite["code"]))
        for door_id, slot in slots.items():
            try:
                entry_id = await self.slot_entry_id(door_id)
            except HTTPException:
                _LOGGER.warning("revoke: no slot table for door %s", door_id)
                continue
            ok = await self.ha.call_service(
                "hemnyckel", "revoke_guest_code",
                {"slot": int(slot), "entry_id": entry_id},
            )
            if not ok:
                _LOGGER.warning("revoke: door %s slot %s not reached", door_id, slot)

    # -- editing a guest: the person, and the codes on every lock -------------
    async def guest_kind(self, door_id: str, slot: int) -> str | None:
        """The integration's kind for a guest slot, read live, or None.

        ``list_guests`` is the integration's own view of what the lock holds
        ("recurring" or "simple"); using it instead of a remembered copy means
        an edit does what is true now. None (an unreachable lock, an older
        integration) is treated as "cannot be changed in place".
        """
        try:
            entry_id = await self.slot_entry_id(door_id)
        except HTTPException:
            return None
        ok, response = await self.ha.call_service_result(
            "hemnyckel", "list_guests", {"entry_id": entry_id}
        )
        if not ok:
            return None
        for row in _guest_rows(response, entry_id):
            try:
                if int(row.get("slot", -1)) == int(slot):
                    return str(row.get("kind") or "") or None
            except (TypeError, ValueError):
                continue
        return None

    async def update_lock_guest(self, door_id: str, slot: int, name: str,
                                days: list[int], from_time: str | None,
                                to_time: str | None) -> bool:
        """Change a recurring guest's name and window in place - the code lives."""
        try:
            entry_id = await self.slot_entry_id(door_id)
        except HTTPException:
            return False
        data: dict[str, Any] = {"slot": int(slot), "name": name, "entry_id": entry_id}
        if days:
            data["schedule"] = [_schedule_window(days, from_time, to_time)]
        ok, _response = await self.ha.call_service_result("hemnyckel", "update_guest", data)
        if not ok:
            _LOGGER.warning("edit: %s could not be updated on door %s", name, door_id)
        return ok

    async def revoke_lock_guest(self, door_id: str, slot: int) -> bool:
        """Clear one guest code on one door, best-effort."""
        try:
            entry_id = await self.slot_entry_id(door_id)
        except HTTPException:
            _LOGGER.warning("edit: no slot table for door %s", door_id)
            return False
        ok = await self.ha.call_service(
            "hemnyckel", "revoke_guest_code",
            {"slot": int(slot), "entry_id": entry_id},
        )
        if not ok:
            _LOGGER.warning("edit: door %s slot %s not reached", door_id, slot)
        return ok

    def _code_row(self, door_id: str, created: dict[str, Any]) -> dict[str, Any]:
        door = self.cfg.door(door_id)
        return {
            "door": door_id,
            "door_name": door.name if door is not None else door_id,
            "slot": int(created["slot"]),
            "code": str(created["code"]),
            "until": created.get("until"),
        }

    async def edit_guest_life(self, person: str, *, name: str, doors: list[str],
                              days: list[int], from_time: str | None,
                              to_time: str | None, expires: float) -> dict[str, Any]:
        """A guest's whole life, moved as one: the relay's rows and every lock.

        The person is the unit - the same way ``set_role_for_person`` moves a
        role - so every device row gets the new doors, weekdays, window and end
        date, and the codes are reconciled per door. A recurring guest whose end
        date did not change is updated in place, so the code the guest already
        knows keeps working; anything else (a door added or removed, a simple
        guest turned recurring or back, an end date that moved) is revoked and
        recreated, because the integration cannot change that in place. A fresh
        code is returned once, exactly like an invitation's.
        """
        group = next((g for g in self.store.people() if g.get("name") == person), None)
        if group is None:
            raise HTTPException(404, "unknown person")
        old_doors = sorted(str(d) for d in (group.get("doors") or []))
        old_days = sorted(int(d) for d in (group.get("days") or []))
        old_from = group.get("from_time")
        old_to = group.get("to_time")
        old_expires = group.get("expires")

        # Every code this person holds: the invitations that created them, and
        # the person-level registry an earlier edit wrote.
        existing: dict[str, int] = {}
        for device in self.store.devices():
            if str(device["person"] or "") != person:
                continue
            invite = self.store.invite_for_device(device["id"])
            if invite is not None:
                existing.update(_json_map(invite["slots"]))
        existing.update(self.store.guest_slots(person))

        schedule_changed = days != old_days or (
            bool(days) and (from_time, to_time) != (old_from, old_to)
        )
        until_changed = (
            old_expires is None or abs(float(expires) - float(old_expires)) > 0.5
        )
        desired = set(doors)

        new_slots: dict[str, int] = {}
        codes: list[dict[str, Any]] = []
        failed: list[str] = []
        for door_id in dict.fromkeys([*existing, *doors]):
            if self.cfg.door(door_id) is None:
                continue  # a door that is no longer configured
            slot = existing.get(door_id)
            if door_id not in desired:
                # A code we cannot clear stays on the lock; keep knowing about it
                # so a later revocation (or expiry) can try again, and say so.
                if slot is not None and not await self.revoke_lock_guest(door_id, slot):
                    new_slots[door_id] = slot
                    failed.append(door_id)
                continue
            if slot is None:
                created = await self.create_lock_guest(
                    door_id, name, days, from_time, to_time, expires
                )
                if created is None:
                    failed.append(door_id)
                    continue
                new_slots[door_id] = int(created["slot"])
                codes.append(self._code_row(door_id, created))
                continue
            target_kind = "recurring" if days else "simple"
            kind = await self.guest_kind(door_id, slot)
            if (not until_changed and kind == target_kind
                    and await self.update_lock_guest(door_id, slot, name, days,
                                                     from_time, to_time)):
                new_slots[door_id] = slot
                continue
            # The code has to change but the old one must go first: if it will
            # not clear, keep it and say so rather than leaving two codes.
            if not await self.revoke_lock_guest(door_id, slot):
                new_slots[door_id] = slot
                failed.append(door_id)
                continue
            created = await self.create_lock_guest(
                door_id, name, days, from_time, to_time, expires
            )
            if created is None:
                failed.append(door_id)
                continue
            new_slots[door_id] = int(created["slot"])
            codes.append(self._code_row(door_id, created))

        changed: list[str] = []
        if name != person:
            changed.append("name")
        if doors != old_doors:
            changed.append("doors")
        if days != old_days:
            changed.append("days")
        elif schedule_changed:
            changed.append("window")
        if until_changed:
            changed.append("expires")

        self.store.set_guest_fields_for_person(
            person, doors, days, from_time, to_time, expires
        )
        self.store.set_guest_slots(person, new_slots)
        if name != person:
            self.store.rename_person(person, name)
            self.store.rename_guest_slots(person, name)
            self.mqtt.refresh(person)  # withdraw the old slug
        self.mqtt.refresh(name)
        return {
            "ok": True,
            "person": name,
            "doors": doors,
            "days": days,
            "from_time": from_time,
            "to_time": to_time,
            "expires_at": expires,
            "guest_codes": codes,
            "failed": failed,
            "changed": changed,
        }


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    state = State(cfg)

    async def require_device(authorization: str = Header(default="")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        device = state.store.device(token) if token else None
        if device is None:
            raise HTTPException(401, "invalid device token")
        found = dict(device)
        if _guest_expired(found):
            # Refuse the guest and take the lock codes with them — once, when
            # the refusal first happens, not on every request that follows.
            await state.revoke_invite_codes(found["id"])
            raise HTTPException(403, "guest access has expired")
        return found

    async def require_owner(device: dict = Depends(require_device)) -> dict:
        if device.get("role") != "owner":
            raise HTTPException(403, "owner only")
        return device

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logging.basicConfig(level=logging.INFO)
        _LOGGER.info("Pairing code: %s (expires in 10 min)", state.pair_code)
        await state.apns.start()
        await state.mqtt.start()
        task = asyncio.create_task(state.ha.run())
        energy_task = asyncio.create_task(state.run_energy()) if cfg.energy_enabled else None
        yield
        task.cancel()
        if energy_task is not None:
            energy_task.cancel()
        state.cancel_live_ends()
        await state.mqtt.stop()
        await state.apns.stop()

    app = FastAPI(title="Hemnyckel relay", version=__version__, lifespan=lifespan)
    # Expose the runtime state for tests and debugging (app.state.hmk).
    app.state.hmk = state
    # The API lives under /api (as the app and docs expect); /health stays at the
    # root so the add-on and proxies can probe it directly.
    api = APIRouter(prefix="/api")

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return state.health()

    @api.post("/pair")
    async def pair(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        if not state.pairing_allowed(client):
            raise HTTPException(429, "too many pairing attempts; try again shortly")
        code = str(payload.get("code", "")).upper()
        if not code:
            raise HTTPException(401, "invalid or expired code")

        # An invitation, created by the owner, carries the guest's name, doors
        # and window — it is what the guest redeems.
        invite = state.store.invite(code)
        if invite is not None and not invite["used_by"] and time.time() <= invite["expires"]:
            device_id = uuid.uuid4().hex
            role = str(invite["role"] or "guest")
            invited_name = str(invite["name"] or "").strip()
            state.store.add_invited(
                device_id,
                # The phone names itself; the invitation names the person.
                str(payload.get("name") or invited_name or "Enhet"),
                role,
                [str(d) for d in _json_list(invite["doors"])],
                [int(d) for d in _json_list(invite["days"]) if str(d).isdigit()],
                invite["from_time"],
                invite["to_time"],
                # A family member is permanent; the code's expiry is not theirs.
                None if role == "user" else float(invite["expires"]),
                person=invited_name or None,
            )
            state.store.use_invite(code, device_id)
            if invited_name:
                state.mqtt.refresh(invited_name)
            _LOGGER.info("%s '%s' paired", role, invited_name or payload.get("name"))
            return {"device_token": device_id, "relay_id": "hemnyckel"}

        if code != state.pair_code or time.time() > state.pair_expires:
            raise HTTPException(401, "invalid or expired code")
        device_id = uuid.uuid4().hex
        # The first device to pair owns the install; everyone after is a user.
        role = "owner" if not state.store.devices() else "user"
        state.store.add_device(device_id, str(payload.get("name") or "Enhet"), role)
        # Rotate, and say so, so the log always shows the current code.
        state.new_pair_code()
        _LOGGER.info("Pairing code: %s (expires in 10 min)", state.pair_code)
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
        # The phone identifies itself; nobody types its model.
        info = payload.get("device") or {}
        state.store.set_device_info(
            device["id"],
            str(info.get("model") or "") or None,
            str(info.get("os") or "") or None,
        )
        # A re-paired phone replaces its older row rather than adding one.
        state.store.replace_duplicates(device["id"])
        person = state.store.device(device["id"])["person"]
        if person:
            state.mqtt.refresh(str(person))
        return {"ok": True}

    @api.get("/events")
    async def events(device: dict = Depends(require_device), since: float | None = None,
                     before: float | None = None, door: str | None = None,
                     person: str | None = None, limit: int = EVENTS_MAX) -> dict[str, Any]:
        # A guest sees no history at all.
        if device.get("role") == "guest":
            return {"events": []}
        return {"events": state.store.events(
            since=since, before=before, door=door, person=person, limit=limit
        )}

    @api.get("/state")
    async def door_states(device: dict = Depends(require_device)) -> dict[str, Any]:
        doors = [await state.door_state(d) for d in cfg.doors]
        role = device.get("role", "user")
        presence: dict[str, Any] = {}
        schedule = None
        if role == "guest":
            # Only their doors, and never who is home.
            allowed = _device_doors(device) or []
            doors = [d for d in doors if d["id"] in allowed]
            schedule = {
                "days": [int(d) for d in _json_list(device.get("days")) if str(d).isdigit()],
                "from": device.get("from_time"),
                "to": device.get("to_time"),
                "until": device.get("expires"),
            }
        else:
            presence = state.store.presence()
        home = state.store.setting("home")
        return {
            "doors": doors,
            "presence": presence,
            # The caller's role, so the app can show only what it may.
            "role": role,
            "device_id": device["id"],
            "person": device.get("person"),
            # Everyone the relay knows, so the app's people pickers stay in sync.
            "people": [] if role == "guest" else state.store.persons(),
            "expires": device.get("expires"),
            "schedule": schedule,
            "home": json.loads(home) if home else None,
            # The journal's ingest truth, so the app can tell "no events" from
            # "the read path is behind" the same way /health can.
            "last_event_at": state.store.last_event_at(),
            "relay": {"online": True, "apns": state.apns.live},
        }

    # -- energy: the cheapest hours (opt-in module) -------------------------
    @api.get("/energy")
    async def energy_plan(device: dict = Depends(require_device)) -> dict[str, Any]:
        """Today's curve and the cheapest window, plus the next one ahead.

        The plan the pulse already computed is served as-is; a device that
        arrives before the first tick gets one computed on the spot. ``enabled``
        tells the app whether the module is on at all.
        """
        if not cfg.energy_enabled:
            return {"enabled": False, "available": False}
        plan = state._energy_plan or await state.compute_energy_plan()
        state._energy_plan = plan
        if plan is None:
            return {"enabled": True, "available": False}
        return {"enabled": True, **plan}

    @api.post("/energy/window")
    async def set_energy_window(payload: dict[str, Any],
                                _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Set the household's cheap-window length (owner only).

        The length is a household fact, not a per-phone one, so it lives in the
        relay and the scheduler, the briefing and every phone all read the same
        number. The next tick recomputes the plan with it.
        """
        try:
            minutes = int(payload["minutes"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "minutes is required") from None
        minutes = max(_ENERGY_WINDOW_MIN, min(minutes, _ENERGY_WINDOW_MAX))
        state.store.set_setting(_ENERGY_WINDOW_KEY, str(minutes))
        state._energy_plan = None  # recompute with the new length
        return {"ok": True, "minutes": minutes}

    # -- people (owner only) -------------------------------------------------
    @api.post("/presence")
    async def report_presence(payload: dict[str, Any],
                              device: dict = Depends(require_device)) -> dict[str, Any]:
        """A phone reports whether it is at home (from the home geofence)."""
        value = str(payload.get("state") or "")
        if value not in ("home", "away"):
            raise HTTPException(400, "state must be home or away")
        person = device.get("person")
        if device.get("role") == "guest" or not person:
            return {"ok": True, "ignored": "no person"}
        state.store.set_presence(str(person), value)
        return {"ok": True}

    @api.post("/settings/home")
    async def set_home(payload: dict[str, Any],
                       _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Where home is, for the phones' geofence (owner only)."""
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "lat and lon are required") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(400, "lat or lon out of range")
        radius = max(100, min(int(payload.get("radius") or 150), 1000))
        state.store.set_setting(
            "home", json.dumps({"lat": lat, "lon": lon, "radius": radius})
        )
        _LOGGER.info("Home set to %.5f, %.5f (r=%d m)", lat, lon, radius)
        return {"ok": True, "radius": radius}

    @api.post("/pair-code")
    async def create_pair_code(_: dict = Depends(require_owner)) -> dict[str, Any]:
        """Mint a fresh pairing code (owner only), for inviting another device."""
        code = state.new_pair_code()
        _LOGGER.info("Pairing code: %s (expires in 10 min)", code)
        return {"code": code, "expires_in": _PAIR_TTL}

    # -- people and their icons ---------------------------------------------
    def person_or_404(ref: str) -> Any:
        """Resolve a person by opaque id, or by name for compatibility."""
        person = state.store.person(ref)
        if person is None:
            raise HTTPException(404, "unknown person")
        return person

    def may_edit_avatar(device: dict, person: Any) -> bool:
        """A device may change its own person's icon; an owner, anyone's."""
        if device.get("role") == "owner":
            return True
        return bool(device.get("person")) and str(device["person"]) == str(person["name"])

    def avatar_body(person: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "id": person["id"],
            "name": person["name"],
            "avatar": state.store.avatar_descriptor(person),
        }

    @api.get("/people")
    async def list_people(device: dict = Depends(require_device)) -> dict[str, Any]:
        """People with their devices and icons — the Personer screen.

        Any paired device may read the icons; a guest still sees no family, the
        same way ``/state`` shows them none. The full row (devices included) is
        for an owner; everyone else gets just the identity and the icon.
        """
        if device.get("role") == "guest":
            return {"people": []}
        people = state.store.people()
        if device.get("role") != "owner":
            people = [
                {"id": p["id"], "name": p["name"], "role": p["role"], "avatar": p["avatar"]}
                for p in people
            ]
        return {"people": people}

    @api.get("/people/{person}/avatar")
    async def get_person_avatar(person: str, request: Request,
                                _: dict = Depends(require_device)) -> Response:
        """A person's photo, with the avatar version as its entity tag.

        Only a photo has bytes; a monogram or a symbol is drawn by the client,
        so anything but a photo is a 404. ``If-None-Match`` is honoured, so a
        client that already has this version gets a 304 and no body.
        """
        row = person_or_404(person)
        if str(row["avatar_kind"]) != "photo":
            raise HTTPException(404, "this person has no photo")
        try:
            with open(state.store.avatar_file(row), "rb") as fh:
                data = fh.read()
        except OSError:
            raise HTTPException(404, "this person has no photo") from None
        version = int(row["avatar_version"])
        etag = avatar.avatar_etag(version)
        if avatar.matches_etag(request.headers.get("if-none-match"), version):
            return Response(status_code=304, headers={"ETag": etag})
        return Response(content=data, media_type="image/jpeg",
                        headers={"ETag": etag, "Cache-Control": "no-cache"})

    @api.put("/people/{person}/avatar")
    async def set_person_avatar(person: str, payload: dict[str, Any],
                                device: dict = Depends(require_device)) -> dict[str, Any]:
        """Choose a monogram or a symbol (a photo has its own upload call)."""
        row = person_or_404(person)
        if not may_edit_avatar(device, row):
            raise HTTPException(403, "you may only change your own icon")
        kind = str(payload.get("kind") or "")
        name = str(row["name"])
        if kind == "monogram":
            state.store.set_avatar(name, kind="monogram")
        elif kind == "symbol":
            symbol = payload.get("symbol")
            if not avatar.valid_symbol(symbol):
                raise HTTPException(400, "unknown symbol")
            try:
                color = avatar.normalize_color(payload.get("color"))
            except ValueError as err:
                raise HTTPException(400, str(err)) from None
            state.store.set_avatar(name, kind="symbol", symbol=str(symbol), color=color)
        elif kind == "photo":
            raise HTTPException(400, "upload a photo with POST .../avatar/photo")
        else:
            raise HTTPException(400, "kind must be monogram, symbol or photo")
        state.mqtt.refresh(name)
        return avatar_body(state.store.person_by_name(name))

    @api.post("/people/{person}/avatar/photo")
    async def set_person_avatar_photo(person: str, request: Request,
                                      device: dict = Depends(require_device)) -> dict[str, Any]:
        """Store a person's JPEG photo (never larger than 512 KB)."""
        row = person_or_404(person)
        if not may_edit_avatar(device, row):
            raise HTTPException(403, "you may only change your own icon")
        data = await request.body()
        if len(data) > avatar.MAX_PHOTO_BYTES:
            raise HTTPException(413, "the photo is larger than 512 KB")
        if not avatar.is_jpeg(data):
            raise HTTPException(415, "the photo must be a JPEG")
        name = str(row["name"])
        state.store.set_avatar_photo(name, data)
        state.mqtt.refresh(name)
        return avatar_body(state.store.person_by_name(name))

    @api.delete("/people/{person}/avatar")
    async def clear_person_avatar(person: str,
                                  device: dict = Depends(require_device)) -> dict[str, Any]:
        """Back to the monogram, and delete the photo."""
        row = person_or_404(person)
        if not may_edit_avatar(device, row):
            raise HTTPException(403, "you may only change your own icon")
        name = str(row["name"])
        state.store.clear_avatar(name)
        state.mqtt.refresh(name)
        return avatar_body(state.store.person_by_name(name))

    @api.post("/people/{person}/role")
    async def set_person_role(person: str, payload: dict[str, Any],
                              _: dict = Depends(require_owner)) -> dict[str, Any]:
        role = str(payload.get("role") or "").lower()
        if role not in _ROLES:
            raise HTTPException(400, f"role must be one of {sorted(_ROLES)}")
        if not state.store.person_exists(person):
            raise HTTPException(404, "unknown person")
        if role != "owner" and state.store.owner_devices() - state.store.owner_devices(person) < 1:
            raise HTTPException(409, "the last owner cannot be demoted")
        state.store.set_role_for_person(person, role)
        state.mqtt.refresh(person)
        return {"ok": True}

    @api.post("/people/{person}/guest")
    async def edit_person_guest(person: str, payload: dict[str, Any],
                                _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Edit a guest's life: name, doors, weekdays, window and end date.

        Owner only. A person is the unit and the lock codes follow, so the
        guest the app edits and the code on the lock stay the same person. A
        code that can be changed in place is; otherwise it is revoked and
        recreated with the new window and returned once.
        """
        group = next((g for g in state.store.people() if g.get("name") == person), None)
        if group is None:
            raise HTTPException(404, "unknown person")
        if group.get("role") != "guest":
            raise HTTPException(409, "only a guest has a guest life to edit")
        name = str(payload.get("name") or "").strip() or person
        if name != person and state.store.person_exists(name):
            raise HTTPException(409, "a person with that name already exists")
        doors = sorted({str(d) for d in (payload.get("doors") or [])
                        if cfg.door(str(d)) is not None})
        if not doors:
            raise HTTPException(400, "choose at least one door")
        days = sorted({int(d) for d in (payload.get("days") or [])
                       if str(d).isdigit() and 1 <= int(d) <= 7})
        from_time = str(payload.get("from_time") or "") or None
        to_time = str(payload.get("to_time") or "") or None
        if bool(from_time) != bool(to_time):
            raise HTTPException(400, "give both a start and an end time, or neither")
        if (from_time and _parse_hhmm(from_time) is None) or \
                (to_time and _parse_hhmm(to_time) is None):
            raise HTTPException(400, "times must be HH:MM")
        try:
            expires = float(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "expires_at is required") from None
        now = time.time()
        if not now < expires <= now + 366 * 86400:
            raise HTTPException(400, "expires_at must be within the next year")
        return await state.edit_guest_life(
            person, name=name, doors=doors, days=days,
            from_time=from_time, to_time=to_time, expires=expires,
        )

    @api.get("/devices")
    async def list_devices(_: dict = Depends(require_owner)) -> dict[str, Any]:
        return {
            "devices": [
                {
                    "id": d["id"],
                    "name": d["name"],
                    "person": d["person"],
                    "role": d["role"],
                    "doors": _json_list(d["doors"]) or None,
                    "days": _json_list(d["days"]) or None,
                    "from_time": d["from_time"],
                    "to_time": d["to_time"],
                    "device_model": d["device_model"],
                    "device_os": d["device_os"],
                    "expires": d["expires"],
                    "created": d["created"],
                }
                for d in state.store.devices()
            ]
        }

    @api.post("/invites")
    async def create_invite(payload: dict[str, Any],
                            _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Invite a family member (permanent) or a guest (doors, days, times)."""
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or "guest").lower()
        if role not in ("user", "guest"):
            raise HTTPException(400, "role must be user or guest")
        # A family member sets their own name on their device; a guest needs one.
        if role == "guest" and not name:
            raise HTTPException(400, "a name is required")

        doors: list[str] = []
        days: list[int] = []
        from_time = to_time = None
        if role == "guest":
            doors = [str(d) for d in (payload.get("doors") or [])
                     if cfg.door(str(d)) is not None]
            if not doors:
                raise HTTPException(400, "choose at least one door")
            days = sorted({int(d) for d in (payload.get("days") or [])
                           if str(d).isdigit() and 1 <= int(d) <= 7})
            from_time = str(payload.get("from_time") or "") or None
            to_time = str(payload.get("to_time") or "") or None
            if (from_time and _parse_hhmm(from_time) is None) or \
                    (to_time and _parse_hhmm(to_time) is None):
                raise HTTPException(400, "times must be HH:MM")

        try:
            expires = float(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "expires_at is required") from None
        now = time.time()
        if not now < expires <= now + 366 * 86400:
            raise HTTPException(400, "expires_at must be within the next year")

        code = secrets.token_hex(3).upper()
        # A guest is one identity: write a matching code on each chosen door
        # (all doors when the invitation named none), so the same person can
        # type a code at the door and redeem the invitation in the app.
        guest_codes: list[dict[str, Any]] = []
        slots: dict[str, int] = {}
        if role == "guest":
            for door_id in doors or [d.id for d in cfg.doors]:
                try:
                    created = await state.create_lock_guest(
                        door_id, name, days, from_time, to_time, expires
                    )
                except Exception as err:  # noqa: BLE001 - a lock must not break the invite
                    _LOGGER.warning("invite %s: door %s failed: %s", name, door_id, err)
                    continue
                if created is None:
                    continue
                door = cfg.door(door_id)
                slots[door_id] = int(created["slot"])
                guest_codes.append({
                    "door": door_id,
                    "door_name": door.name if door is not None else door_id,
                    "slot": int(created["slot"]),
                    "code": str(created["code"]),
                    "until": created.get("until"),
                })
        state.store.add_invite(code, name, role, doors, days, from_time, to_time,
                               expires, slots=slots)
        _LOGGER.info("Invite for %s (%s): %s", name, role, code)
        return {"code": code, "role": role, "expires_at": expires,
                "doors": doors, "days": days, "guest_codes": guest_codes}

    @api.delete("/devices/{device_id}")
    async def revoke_device(device_id: str,
                            device: dict = Depends(require_owner)) -> dict[str, Any]:
        """Revoke a device (a guest that has left, an old phone)."""
        target = state.store.device(device_id)
        if target is None:
            raise HTTPException(404, "unknown device")
        if target["id"] == device["id"]:
            raise HTTPException(409, "you cannot remove your own device")
        if target["role"] == "owner" and state.store.owner_count() <= 1:
            raise HTTPException(409, "the last owner cannot be removed")
        # The guest's lock codes go with their device, best-effort.
        person = str(target["person"]) if target["person"] else None
        await state.revoke_invite_codes(device_id)
        state.store.remove_device(device_id)
        if person:
            state.mqtt.refresh(person)
        return {"ok": True}

    @api.post("/devices/{device_id}/role")
    async def set_device_role(device_id: str, payload: dict[str, Any],
                              _: dict = Depends(require_owner)) -> dict[str, Any]:
        role = str(payload.get("role") or "").lower()
        if role not in _ROLES:
            raise HTTPException(400, f"role must be one of {sorted(_ROLES)}")
        target = state.store.device(device_id)
        if target is None:
            raise HTTPException(404, "unknown device")
        if (target["role"] == "owner" and role != "owner"
                and state.store.owner_count() <= 1):
            raise HTTPException(409, "the last owner cannot be demoted")
        state.store.set_role(device_id, role)
        if target["person"]:
            state.mqtt.refresh(str(target["person"]))
        return {"ok": True}

    # -- slots & codes (owner only) ------------------------------------------
    # The lock's slots are where the journal gets its attribution: a named slot
    # is what turns "slot 6" into "Elise". Only an owner manages them.
    @api.get("/slots")
    async def list_slots(door: str, _: dict = Depends(require_owner)) -> dict[str, Any]:
        return await state.slots_snapshot(door)

    @api.post("/slots/{slot}/name")
    async def name_slot(slot: int, payload: dict[str, Any],
                        _: dict = Depends(require_owner)) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "a name is required")
        entry_id = await state.slot_entry_id(str(payload.get("door") or ""))
        ok = await state.ha.call_service(
            "hemnyckel", "set_slot_name",
            {"slot": slot, "name": name, "entry_id": entry_id},
        )
        if not ok:
            raise HTTPException(502, _UPSTREAM_ERROR)
        return {"ok": True, "slot": slot, "name": name}

    @api.post("/slots/{slot}/code")
    async def create_slot_code(slot: int, payload: dict[str, Any],
                               _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Write a code to a slot and return it exactly once.

        The code is write-only: it is never read back, logged or stored. When
        the caller supplies none, the lock's own generator makes one and the
        service response carries it — this response is the only place it lives.
        """
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "a name is required")
        entry_id = await state.slot_entry_id(str(payload.get("door") or ""))
        data: dict[str, Any] = {"slot": slot, "name": name, "entry_id": entry_id}
        if payload.get("code"):
            data["code"] = str(payload["code"])
        if payload.get("until"):
            data["until"] = str(payload["until"])
        ok, response = await state.ha.call_service_result(
            "hemnyckel", "create_guest_code", data
        )
        if not ok:
            raise HTTPException(502, _UPSTREAM_ERROR)
        created = _service_row(response, entry_id)
        code = str((created or {}).get("code") or "")
        if not code:
            raise HTTPException(502, _UPSTREAM_ERROR)
        return {
            "ok": True,
            "slot": int((created or {}).get("slot") or slot),
            "name": str((created or {}).get("name") or name),
            "until": (created or {}).get("until"),
            "code": code,
        }

    @api.post("/slots/{slot}/finger")
    async def enroll_finger(slot: int, payload: dict[str, Any],
                            _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Light the lock's reader so the person can touch it at the door.

        ``finger`` is the owner's label for the finger being enrolled (one of
        the fixed vocabulary, or a free word). The integration stores it as a
        claim; the lock reports nothing while enrolling.
        """
        entry_id = await state.slot_entry_id(str(payload.get("door") or ""))
        finger = str(payload.get("finger") or "").strip()
        data: dict[str, Any] = {"slot": slot, "entry_id": entry_id}
        if finger:
            data["finger"] = finger
        ok, _response = await state.ha.call_service_result(
            "hemnyckel", "enroll_fingerprint", data,
        )
        if not ok:
            raise HTTPException(502, _UPSTREAM_ERROR)
        return {"ok": True, "slot": slot, "finger": finger or None}

    @api.post("/slots/{slot}/label")
    async def label_finger(slot: int, payload: dict[str, Any],
                           _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Name a fingerprint the slot already holds — no reader, no template.

        The label is our bookkeeping, not a credential, so a confirmed
        fingerprint that predates labels is named in place instead of being
        enrolled a second time. The integration owns the policy and refuses a
        slot with no fingerprint; that refusal is carried back as a bad request
        rather than hidden behind the outage answer.
        """
        finger = str(payload.get("finger") or "").strip()
        if not finger:
            raise HTTPException(400, "a finger label is required")
        entry_id = await state.slot_entry_id(str(payload.get("door") or ""))
        status, response = await state.ha.call_service_status(
            "hemnyckel", "relabel_fingerprint",
            {"slot": slot, "finger": finger, "entry_id": entry_id},
        )
        if 400 <= status < 500:
            raise HTTPException(
                400, _refusal_reason(response) or "that slot's fingerprint cannot be labelled"
            )
        if status != 200:
            raise HTTPException(502, _UPSTREAM_ERROR)
        row = _service_row(response, entry_id)
        return {
            "ok": True,
            "slot": int((row or {}).get("slot") or slot),
            "finger": str((row or {}).get("finger") or finger),
        }

    @api.delete("/slots/{slot}/finger")
    async def clear_finger(slot: int, door: str,
                           _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Clear the fingerprint template in one slot, and forget its label."""
        entry_id = await state.slot_entry_id(door)
        ok = await state.ha.call_service(
            "hemnyckel", "clear_fingerprint", {"slot": slot, "entry_id": entry_id}
        )
        if not ok:
            raise HTTPException(502, _UPSTREAM_ERROR)
        return {"ok": True, "slot": slot}

    @api.delete("/slots/{slot}")
    async def clear_slot(slot: int, door: str,
                         _: dict = Depends(require_owner)) -> dict[str, Any]:
        """Clear a slot's credential on the lock, and forget its name."""
        entry_id = await state.slot_entry_id(door)
        ok = await state.ha.call_service(
            "hemnyckel", "clear_slot", {"slot": slot, "entry_id": entry_id}
        )
        if not ok:
            raise HTTPException(502, _UPSTREAM_ERROR)
        return {"ok": True, "slot": slot}

    @api.post("/action")
    async def action(payload: dict[str, Any],
                     device: dict = Depends(require_device)) -> dict[str, Any]:
        door_id = str(payload.get("door"))
        if device.get("role") == "guest":
            allowed = _device_doors(device) or []
            if door_id not in allowed:
                raise HTTPException(403, "not your door")
            if not _schedule_ok(device, time.time()):
                raise HTTPException(403, "outside the guest's hours")
        return await state.do_action(door_id, str(payload.get("action")), device)

    # -- live activities (Lock Screen / Dynamic Island) ---------------------
    @api.post("/live/start-token")
    async def live_start_token(payload: dict[str, Any],
                               device: dict = Depends(require_device)) -> dict[str, Any]:
        """The device's push-to-start token (lets the relay start an activity).

        ``kind`` picks the activity type: a door (the default) or the energy
        module's cheap window. Each type has its own push-to-start token, so the
        two never collide.
        """
        if device.get("role") == "guest":
            return {"ok": True}  # guests receive no pushes; ignore the token
        token = str(payload.get("apns_token") or "")
        if str(payload.get("kind") or "door") == "energy":
            state.store.set_live_energy_start_token(device["id"], token)
        else:
            state.store.set_live_start_token(device["id"], token)
        return {"ok": True}

    @api.post("/live/activity")
    async def live_activity(payload: dict[str, Any],
                            device: dict = Depends(require_device)) -> dict[str, Any]:
        """The per-activity update token the app reports once an activity exists."""
        if device.get("role") == "guest":
            return {"ok": True}
        if str(payload.get("kind") or "door") == "energy":
            state.store.set_energy_activity(device["id"], str(payload.get("apns_token") or ""))
            return {"ok": True}
        door_id = str(payload.get("door") or "")
        if cfg.door(door_id) is None:
            raise HTTPException(400, "unknown door")
        state.store.set_live_activity(
            device["id"], door_id, str(payload.get("apns_token") or "")
        )
        return {"ok": True}

    @api.delete("/live/activity")
    async def live_activity_end(door: str | None = None, kind: str = "door",
                                device: dict = Depends(require_device)) -> dict[str, Any]:
        if kind == "energy":
            state.store.drop_energy_activity(device["id"])
            return {"ok": True}
        if not door:
            raise HTTPException(400, "a door is required")
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
