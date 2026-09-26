"""Map Home Assistant events to Hemnyckel events (who / when / how / which door).

Two sources:

* ``nimly_journal_entry`` - fired by the lock integration; already attributed
  (person, slot, method). This is the rich source.
* ``state_changed`` on a lock entity - a fallback for lock/unlock with no
  attribution ("unattributed").
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .config import Config

_SOURCE = {
    "keypad": "keypad",
    "code": "keypad",
    "finger": "finger",
    "fingerprint": "finger",
    "tag": "tag",
    "rfid": "tag",
    "auto": "auto",
}


def _method_text(source: str) -> str:
    return {
        "keypad": "Kod",
        "finger": "Fingeravtryck",
        "tag": "Bricka",
        "auto": "Automatiskt",
        "unattributed": "Oattribuerad",
    }.get(source, source)


def from_journal(cfg: Config, event: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data") or {}
    entry = data.get("entry") or data
    action = str(entry.get("action") or "").lower()
    if action not in ("lock", "unlock"):
        return None
    entry_id = str(entry.get("entry_id") or data.get("entry_id") or "")
    door = cfg.door_by_entry(entry_id) if entry_id else None
    if door is None:
        door = cfg.doors[0] if len(cfg.doors) == 1 else None
    if door is None:
        return None
    slot = entry.get("slot")
    try:
        slot = int(slot) if slot is not None else None
    except (TypeError, ValueError):
        slot = None
    source = _SOURCE.get(str(entry.get("source") or "").lower(), "unattributed")
    person = entry.get("name") or entry.get("person")
    if not person and slot is not None:
        person = door.persons.get(str(slot))
    return {
        "id": uuid.uuid4().hex,
        "ts": float(entry.get("time") or event.get("time_fired_ts") or time.time()),
        "door": door.id,
        "person": person,
        "slot": slot,
        "action": action,
        "source": source,
        "method": _method_text(source),
        "door_open": None,
    }


def from_state(cfg: Config, event: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data") or {}
    entity_id = data.get("entity_id", "")
    door = cfg.door_by_lock_entity(entity_id)
    if door is None:
        return None
    new_state = (data.get("new_state") or {}).get("state")
    if new_state not in ("locked", "unlocked"):
        return None
    return {
        "id": uuid.uuid4().hex,
        "ts": float(event.get("time_fired_ts") or time.time()),
        "door": door.id,
        "person": None,
        "slot": None,
        "action": "unlock" if new_state == "unlocked" else "lock",
        "source": "unattributed",
        "method": _method_text("unattributed"),
        "door_open": None,
    }


def from_ha(cfg: Config, event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event_type") == "nimly_journal_entry":
        return from_journal(cfg, event)
    if event.get("event_type") == "state_changed":
        return from_state(cfg, event)
    return None
