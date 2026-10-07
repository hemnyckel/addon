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
    # Default environment for devices that don't report one. "development"
    # (a.k.a. sandbox) is used by debug builds, "production" by TestFlight/App
    # Store builds.
    apns_env: str = "production"
    # Optional override; defaults to the bundle id.
    apns_topic: str = ""
    # Live Activities (Lock Screen / Dynamic Island while a door is unlocked).
    live_enabled: bool = True
    live_attributes_type: str = "HemnyckelLockAttributes"
    # Energy: the cheapest hours from a Home Assistant price sensor. Off by
    # default and sensor-agnostic - the public add-on runs outside Sweden too.
    energy_enabled: bool = False
    price_entity: str = "sensor.elpris"
    energy_window_minutes: int = 120
    energy_morning_time: str = "07:00"
    price_divisor: float = 100.0
    energy_currency: str = "SEK"
    energy_attributes_type: str = "HemnyckelEnergyAttributes"
    data_dir: str = "/data"
    port: int = 8099
    doors: list[Door] = field(default_factory=list)

    @property
    def apns_configured(self) -> bool:
        return bool(self.apns_key_path and self.apns_key_id and self.apns_team_id)

    @property
    def topic(self) -> str:
        return self.apns_topic or self.bundle_id

    def apns_key_material(self) -> str:
        """The `.p8` contents: inline PEM, or the file at `apns_key_path`."""
        value = self.apns_key_path
        if "BEGIN" in value and "PRIVATE KEY" in value:
            return value
        with open(value, encoding="utf-8") as fh:
            return fh.read()

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


def normalize_env(value: str | None) -> str:
    """Accept the names people actually use; store the two APNs host names."""
    v = (value or "").strip().lower()
    if v in ("sandbox", "dev", "development"):
        return "development"
    return "production"


def _bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config() -> Config:
    opts = _options_file()
    def pick(env: str, key: str, default: str = "") -> str:
        return os.environ.get(env) or str(opts.get(key) or default)

    # Inside a Home Assistant add-on the supervisor injects a token and proxies
    # core: the relay needs no Home Assistant credentials of its own.
    supervisor = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    ha_url = pick("HEMNYCKEL_HA_URL", "ha_url").rstrip("/")
    ha_token = pick("HEMNYCKEL_HA_TOKEN", "ha_token")
    if not ha_url and supervisor:
        ha_url = "http://supervisor/core"
    if not ha_token and supervisor:
        ha_token = supervisor

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
        ha_url=ha_url,
        ha_token=ha_token,
        apns_key_path=pick("HEMNYCKEL_APNS_KEY", "apns_key"),
        apns_key_id=pick("HEMNYCKEL_APNS_KEY_ID", "apns_key_id"),
        apns_team_id=pick("HEMNYCKEL_APNS_TEAM_ID", "apns_team_id"),
        bundle_id=pick("HEMNYCKEL_BUNDLE_ID", "bundle_id", "se.hemnyckel.app"),
        apns_env=normalize_env(pick("HEMNYCKEL_APNS_ENV", "apns_env", "production")),
        apns_topic=pick("HEMNYCKEL_APNS_TOPIC", "apns_topic"),
        live_enabled=_bool(pick("HEMNYCKEL_LIVE_ENABLED", "live_enabled", "true")),
        live_attributes_type=pick(
            "HEMNYCKEL_LIVE_ATTRIBUTES_TYPE", "live_attributes_type", "HemnyckelLockAttributes"
        ),
        energy_enabled=_bool(pick("HEMNYCKEL_ENERGY_ENABLED", "energy_enabled", "false")),
        price_entity=pick("HEMNYCKEL_PRICE_ENTITY", "price_entity", "sensor.elpris"),
        energy_window_minutes=_int(
            pick("HEMNYCKEL_ENERGY_WINDOW_MINUTES", "energy_window_minutes", "120"), 120
        ),
        energy_morning_time=pick(
            "HEMNYCKEL_ENERGY_MORNING_TIME", "energy_morning_time", "07:00"
        ),
        price_divisor=_float(pick("HEMNYCKEL_PRICE_DIVISOR", "price_divisor", "100"), 100.0),
        energy_currency=pick("HEMNYCKEL_ENERGY_CURRENCY", "energy_currency", "SEK"),
        energy_attributes_type=pick(
            "HEMNYCKEL_ENERGY_ATTRIBUTES_TYPE", "energy_attributes_type", "HemnyckelEnergyAttributes"
        ),
        data_dir=pick("HEMNYCKEL_DATA_DIR", "data_dir", "/data"),
        port=int(pick("HEMNYCKEL_PORT", "port", "8099")),
        doors=doors,
    )
