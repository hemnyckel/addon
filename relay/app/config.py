"""Configuration for the Hemnyckel relay.

Read from environment variables (the Home Assistant add-on passes its options
through as environment). Every value has a safe default so the relay can run in
development with no Home Assistant and no APNs key.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class Door:
    id: str
    name: str
    lock_entity: str
    door_sensor: str | None = None
    entry_id: str | None = None
    # optional fallback: lock slot -> person id, when the event carries no name
    persons: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    ha_url: str = ""
    ha_token: str = ""
    apns_key_path: str = ""
    apns_key_id: str = ""
    apns_team_id: str = ""
    bundle_id: str = "se.hemnyckel.app"
    data_dir: str = "/data"
    port: int = 8099
    doors: list[Door] = field(default_factory=list)

    @property
    def apns_configured(self) -> bool:
        return bool(self.apns_key_path and self.apns_key_id and self.apns_team_id)

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_url and self.ha_token)

    def door(self, door_id: str) -> Door | None:
        return next((d for d in self.doors if d.id == door_id), None)

    def door_by_lock_entity(self, entity_id: str) -> Door | None:
        return next((d for d in self.doors if d.lock_entity == entity_id), None)

    def door_by_entry(self, entry_id: str) -> Door | None:
        return next((d for d in self.doors if d.entry_id == entry_id), None)


def _options_file() -> dict:
    """Home Assistant add-ons pass their options in /data/options.json."""
    try:
        with open("/data/options.json", encoding="utf-8") as fh:
            import json as _json
            return _json.load(fh)
    except (OSError, ValueError):
        return {}


def load_config() -> Config:
    opts = _options_file()
    def pick(env: str, key: str, default: str = "") -> str:
        return os.environ.get(env) or str(opts.get(key) or default)

    doors: list[Door] = []
    raw = os.environ.get("HEMNYCKEL_DOORS") or opts.get("doors") or "[]"
    if not isinstance(raw, str):
        import json as _json
        raw = _json.dumps(raw)
    try:
        for item in json.loads(raw):
            doors.append(Door(**item))
    except (ValueError, TypeError):
        doors = []
    return Config(
        ha_url=pick("HEMNYCKEL_HA_URL", "ha_url").rstrip("/"),
        ha_token=pick("HEMNYCKEL_HA_TOKEN", "ha_token"),
        apns_key_path=pick("HEMNYCKEL_APNS_KEY", "apns_key"),
        apns_key_id=pick("HEMNYCKEL_APNS_KEY_ID", "apns_key_id"),
        apns_team_id=pick("HEMNYCKEL_APNS_TEAM_ID", "apns_team_id"),
        bundle_id=pick("HEMNYCKEL_BUNDLE_ID", "bundle_id", "se.hemnyckel.app"),
        data_dir=pick("HEMNYCKEL_DATA_DIR", "data_dir", "/data"),
        port=int(pick("HEMNYCKEL_PORT", "port", "8099")),
        doors=doors,
    )
