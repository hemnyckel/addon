"""The MQTT bridge: the family's people in Home Assistant.

The relay keeps the truth (roles, devices, health) and Home Assistant gets a
projection of it: a ``select`` per person, plus a sensor and a binary_sensor for
the relay itself, all built from MQTT discovery. A role change travels the other
way, on a command topic, and is applied through the *same* store call the app
uses - enforcement stays in the relay, never in the bridge.

The broker comes from Home Assistant, from the first source that has a complete
set of credentials: the historic supervisor-injected ``MQTT_*`` environment, the
broker the Supervisor registers for an app that asked for MQTT (``GET
/services/mqtt``), or explicit add-on options (``mqtt_host`` / ``mqtt_user`` /
``mqtt_password``) as a manual last resort. If none has them the bridge logs once
and stays off: it never falls back to an anonymous connection. See
``docs/mqtt-bridge.md`` for the full design.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import unicodedata
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger("hemnyckel.mqtt")

# Our own namespace; Home Assistant's discovery prefix stays the default.
NAMESPACE = "hemnyckel"
DISCOVERY_PREFIX = "homeassistant"

RELAY_STATE_TOPIC = "hemnyckel/relay/state"
AVAILABILITY_TOPIC = "hemnyckel/relay/availability"

# The only payloads a role command may carry.
ROLES = ("owner", "user", "guest")

# One device for the whole bridge, so the family's device list stays honest:
# the locks are the integration's devices, the relay is this one.
DEVICE = {
    "identifiers": ["hemnyckel_relay"],
    "name": "Hemnyckel",
    "manufacturer": "Hemnyckel",
    "model": "Reläet",
    "configuration_url": "https://ljungen.hall-hogberg.se/hemnyckel/",
}

# A command: hemnyckel/people/<slug>/role/set, and nothing else.
_COMMAND_TOPIC = re.compile(r"^hemnyckel/people/([^/]+)/role/set$")


def slug(name: str) -> str:
    """A person's name as a stable, readable topic segment.

    Accents are folded to their base letter first ("Högberg" -> "hogberg"),
    then anything that is not ``a-z0-9`` becomes a single ``-``. That is what
    the discovery topic and ``unique_id`` use, so a rename in the app updates
    the friendly name instead of creating a second entity.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def _guest_expired(group: dict[str, Any], now: float) -> bool:
    expires = group.get("expires")
    return bool(
        group.get("role") == "guest" and expires is not None and now > float(expires)
    )


def state_document(group: dict[str, Any], *, last_seen: float | None = None,
                   now: float | None = None) -> dict[str, Any]:
    """One person's state, as published (retained) to their state topic.

    ``group`` is a person from ``Store.people()``. ``doors``, ``window`` and
    ``expires`` only mean something for a guest and are omitted otherwise.
    ``last_seen`` is the person's last attributed activity; the store keeps no
    per-device clock, so it is shown on each of their devices.
    """
    moment = time.time() if now is None else now
    role = str(group.get("role") or "user")
    document: dict[str, Any] = {
        "person": group.get("name"),
        "role": role,
        "active": not _guest_expired(group, moment),
        "devices": [
            {
                "id": device.get("id"),
                "name": device.get("name"),
                "model": device.get("device_model"),
                "os": device.get("device_os"),
                "role": device.get("role"),
                "last_seen": last_seen,
            }
            for device in group.get("devices") or []
        ],
    }
    if role == "guest":
        document["doors"] = list(group.get("doors") or [])
        document["window"] = {
            "days": list(group.get("days") or []),
            "from": group.get("from_time"),
            "to": group.get("to_time"),
        }
        document["expires"] = group.get("expires")
    return document


def person_discovery(name: str) -> tuple[str, dict[str, Any]]:
    """The discovery topic and payload for one person's role ``select``."""
    s = slug(name)
    return (
        f"{DISCOVERY_PREFIX}/select/{NAMESPACE}/{s}/config",
        {
            "name": name,
            "unique_id": f"hemnyckel_person_{s}",
            "state_topic": f"{NAMESPACE}/people/{s}/state",
            "command_topic": f"{NAMESPACE}/people/{s}/role/set",
            "value_template": "{{ value_json.role }}",
            "options": list(ROLES),
            "json_attributes_topic": f"{NAMESPACE}/people/{s}/state",
            "availability_topic": AVAILABILITY_TOPIC,
            "icon": "mdi:account-key",
            "device": DEVICE,
        },
    )


def relay_discovery() -> list[tuple[str, dict[str, Any]]]:
    """The discovery topics and payloads for the relay's own entities.

    A sensor (state ``ok``, with ``ha``/``apns``/``doors``/``version`` as
    attributes) and a connectivity binary_sensor for whether push is
    configured - the thing to glance at the day the Apple key lands.
    """
    return [
        (
            f"{DISCOVERY_PREFIX}/sensor/{NAMESPACE}/relaet/config",
            {
                "name": "Reläet",
                "unique_id": "hemnyckel_relay",
                "state_topic": RELAY_STATE_TOPIC,
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": RELAY_STATE_TOPIC,
                "availability_topic": AVAILABILITY_TOPIC,
                "icon": "mdi:door",
                "device": DEVICE,
            },
        ),
        (
            f"{DISCOVERY_PREFIX}/binary_sensor/{NAMESPACE}/apns/config",
            {
                "name": "APNs",
                "unique_id": "hemnyckel_apns",
                "state_topic": RELAY_STATE_TOPIC,
                "value_template": "{{ 'ON' if value_json.apns else 'OFF' }}",
                "device_class": "connectivity",
                "availability_topic": AVAILABILITY_TOPIC,
                "device": DEVICE,
            },
        ),
    ]


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    tls: bool


# Where a modern Supervisor hands an app the broker the Mosquitto app
# registered (services: mqtt:want). See docs/mqtt-bridge.md.
SUPERVISOR_SERVICES_URL = "http://supervisor/services/mqtt"


def _settings_from(host: Any, port: Any, username: Any, password: Any,
                   tls: Any) -> MqttSettings | None:
    """A complete set of credentials, or None when anything is missing.

    All three of host, username and password are required: a partial set is
    treated as absent, so the bridge never connects without authentication.
    """
    host = str(host or "").strip()
    username = str(username or "").strip()
    password = str(password or "")
    if not host or not username or not password:
        return None
    try:
        port_number = int(str(port or "1883"))
    except (TypeError, ValueError):
        port_number = 1883
    tls_on = (
        tls if isinstance(tls, bool)
        else str(tls or "").strip().lower() in ("1", "true", "yes", "on")
    )
    return MqttSettings(host=host, port=port_number, username=username,
                        password=password, tls=tls_on)


def _options_file() -> dict[str, Any]:
    """Home Assistant add-ons pass their options in /data/options.json."""
    try:
        with open("/data/options.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_service(url: str, token: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def supervisor_settings(
    *, token: str | None = None,
    fetch: Callable[[str, str, float], Any] | None = None,
    timeout: float = 5.0,
) -> MqttSettings | None:
    """The broker the Supervisor registers for an app that asked for MQTT.

    A modern Supervisor no longer injects ``MQTT_*`` into the container; the
    Mosquitto app registers the broker as MQTT service data and this endpoint
    hands it to an app whose ``services`` list includes ``mqtt``. The bridge is
    optional, so any failure here is just "no credentials" - never an outage.
    ``fetch`` is a test seam for the HTTP call.
    """
    token = (token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")).strip()
    if not token:
        return None
    fetch = fetch or _fetch_service
    try:
        payload = fetch(SUPERVISOR_SERVICES_URL, token, timeout)
    except Exception:  # noqa: BLE001 - the bridge is a convenience, never a dependency
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    return _settings_from(data.get("host"), data.get("port"), data.get("username"),
                          data.get("password"), data.get("ssl"))


def load_settings(
    environ: dict[str, str] | None = None,
    *,
    service: Callable[[], MqttSettings | None] | None = None,
    options: dict[str, Any] | None = None,
) -> MqttSettings | None:
    """The broker, from the first source that has complete credentials.

    In order: the environment Home Assistant injects (``MQTT_HOST`` /
    ``MQTT_USERNAME`` / ``MQTT_PASSWORD``), then the broker the Supervisor
    registers (``GET /services/mqtt``), then explicit add-on options
    (``mqtt_host`` / ``mqtt_port`` / ``mqtt_user`` / ``mqtt_password``). The
    environment stays preferred for compatibility; the options are the honest
    last resort for a Supervisor that hands the app nothing at all.
    ``service`` and ``options`` are test seams for the real Supervisor lookup
    and ``/data/options.json``.
    """
    env = os.environ if environ is None else environ
    settings = _settings_from(env.get("MQTT_HOST"), env.get("MQTT_PORT"),
                              env.get("MQTT_USERNAME"), env.get("MQTT_PASSWORD"),
                              env.get("MQTT_SSL"))
    if settings is not None:
        return settings
    service = service or supervisor_settings
    settings = service()
    if settings is not None:
        return settings
    opts = _options_file() if options is None else options
    return _settings_from(opts.get("mqtt_host"), opts.get("mqtt_port"),
                          opts.get("mqtt_user"), opts.get("mqtt_password"),
                          opts.get("mqtt_ssl"))


class MqttBridge:
    """Publishes the family to Home Assistant and applies role commands.

    ``store`` is the relay's store and ``facts`` a callable returning the same
    health document as ``/health``. ``publish`` is a test seam: when given, it
    is called with ``(topic, payload, retain)`` instead of a real broker.
    """

    def __init__(self, cfg: Any, store: Any, *, version: str,
                 facts: Callable[[], dict[str, Any]] | None = None,
                 settings: MqttSettings | None = None,
                 publish: Callable[[str, str, bool], None] | None = None) -> None:
        self._store = store
        self._facts = facts or (
            lambda: {"status": "ok", "ha": False, "apns": False,
                     "doors": len(getattr(cfg, "doors", []) or []), "version": version}
        )
        self._settings = settings if settings is not None else load_settings()
        self._publish = publish
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def enabled(self) -> bool:
        return self._settings is not None

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._settings is None:
            _LOGGER.info(
                "MQTT bridge off: Home Assistant did not provide broker credentials"
            )
            return
        self._loop = asyncio.get_running_loop()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hemnyckel-relay")
        client.username_pw_set(self._settings.username, self._settings.password)
        if self._settings.tls:
            client.tls_set()
        # A dead relay says so: the last will greys the entities out.
        client.will_set(AVAILABILITY_TOPIC, "offline", retain=True, qos=1)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        client.connect_async(self._settings.host, self._settings.port)
        client.loop_start()
        self._client = client
        _LOGGER.info("MQTT bridge on: %s:%s as %s",
                     self._settings.host, self._settings.port, self._settings.username)

    async def stop(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        published = client.publish(AVAILABILITY_TOPIC, "offline", retain=True, qos=1)
        with contextlib.suppress(ValueError, RuntimeError):
            published.wait_for_publish(timeout=2)
        client.disconnect()
        client.loop_stop()
        self._loop = None

    # -- paho callbacks (its own thread) ------------------------------------
    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        if getattr(reason_code, "is_failure", False):
            _LOGGER.warning("MQTT connect refused: %s", reason_code)
            return
        client.subscribe(f"{NAMESPACE}/people/+/role/set", qos=1)
        client.publish(AVAILABILITY_TOPIC, "online", retain=True, qos=1)
        # The truth is published from the event loop, where the store lives.
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.publish_all(), self._loop)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        if self._loop is None:
            return
        payload = message.payload.decode("utf-8", "replace")
        asyncio.run_coroutine_threadsafe(
            self.command(message.topic, payload), self._loop
        )

    # -- publishing ---------------------------------------------------------
    def publish(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        if self._publish is not None:
            self._publish(topic, payload, retain)
        elif self._client is not None:
            self._client.publish(topic, payload, retain=retain, qos=1)

    def _people(self) -> list[dict[str, Any]]:
        return [group for group in self._store.people() if slug(str(group.get("name") or ""))]

    def _group(self, person: str) -> dict[str, Any] | None:
        return next(
            (group for group in self._store.people() if group.get("name") == person),
            None,
        )

    def _person_state_topic(self, name: str) -> str:
        return f"{NAMESPACE}/people/{slug(name)}/state"

    def publish_person(self, group: dict[str, Any]) -> None:
        name = str(group.get("name") or "")
        if not slug(name):
            return
        self.publish(
            self._person_state_topic(name),
            state_document(group, last_seen=self._store.last_seen(name)),
        )

    async def publish_all(self) -> None:
        """The full projection, from the event loop (a paho connect callback)."""
        self.publish_all_now()

    def refresh(self, person: str | None = None) -> None:
        """Republish a person after a change, or withdraw them when gone.

        Called when the app changes a role, when a device is added, renamed,
        revoked or re-registered, and after every role command. ``None``
        refreshes the whole projection.
        """
        if not self.enabled:
            return
        if person is None:
            self.publish_all_now()
            return
        s = slug(person)
        group = self._group(person)
        if group is None:
            if s:
                # Withdraw the entity and its state: nothing retained lies.
                self.publish(f"{DISCOVERY_PREFIX}/select/{NAMESPACE}/{s}/config", "")
                self.publish(self._person_state_topic(person), "")
            return
        topic, payload = person_discovery(person)
        self.publish(topic, payload)
        self.publish_person(group)

    def publish_all_now(self) -> None:
        """publish_all for synchronous callers (endpoints, callbacks)."""
        self.publish(RELAY_STATE_TOPIC, self._facts())
        self.publish(AVAILABILITY_TOPIC, "online")
        for topic, payload in relay_discovery():
            self.publish(topic, payload)
        for group in self._people():
            topic, payload = person_discovery(str(group["name"]))
            self.publish(topic, payload)
            self.publish_person(group)

    # -- commands -----------------------------------------------------------
    async def command(self, topic: str, payload: str) -> None:
        """Apply one ``.../role/set`` command, then republish the truth.

        The six rules of the design live here: the payload must be a role, the
        slug must be a known person, the last owner can never be demoted, an
        accepted change goes through the same store call the app uses, and the
        person's state is republished after every attempt - accepted or refused.
        """
        match = _COMMAND_TOPIC.match(topic)
        if match is None:
            return
        s = match.group(1)
        role = (payload or "").strip().lower()
        if role not in ROLES:
            _LOGGER.warning("MQTT: ignoring role %r on %s", payload, topic)
            return
        group = next(
            (g for g in self._people() if slug(str(g["name"])) == s),
            None,
        )
        if group is None:
            _LOGGER.warning("MQTT: unknown person slug %r", s)
            return
        person = str(group["name"])
        if role != "owner" and self._store.owner_devices() - self._store.owner_devices(person) < 1:
            _LOGGER.warning("MQTT: refusing to demote the last owner (%s)", person)
            self.publish_person(group)
            return
        self._store.set_role_for_person(person, role)
        _LOGGER.info("MQTT: %s is now %s", person, role)
        self.publish_person(self._group(person) or group)
