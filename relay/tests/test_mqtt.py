from __future__ import annotations

import asyncio
import json

import pytest

from app.mqtt import (
    AVAILABILITY_TOPIC,
    MqttBridge,
    MqttSettings,
    load_settings,
    person_discovery,
    relay_discovery,
    slug,
    state_document,
    supervisor_settings,
)
from app.store import Store


def make_bridge(cfg, tmp_path, published: list) -> tuple[MqttBridge, Store]:
    """A bridge wired to a real store and a recording publisher."""
    store = Store(str(tmp_path / "data"))
    bridge = MqttBridge(
        cfg,
        store,
        version="0.3.0",
        facts=lambda: {"status": "ok", "ha": True, "apns": False,
                       "doors": 1, "version": "0.3.0"},
        settings=MqttSettings("broker.local", 1883, "hemnyckel", "secret", False),
        publish=lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    return bridge, store


def add_person(store: Store, device_id: str, name: str, *, role: str = "user", **extra) -> None:
    store.add_invited(
        device_id, name, role,
        extra.get("doors") or [], extra.get("days") or [],
        extra.get("from_time"), extra.get("to_time"), extra.get("expires"),
        person=name,
    )
    if role != "user":
        store.set_role(device_id, role)


def state_payloads(published: list) -> list[dict]:
    return [json.loads(p) for topic, p, _ in published if topic.endswith("/state")]


# -- pure parts ---------------------------------------------------------------

@pytest.mark.parametrize("name, want", [
    ("Elise Högberg", "elise-hogberg"),
    ("Claes' iPhone", "claes-iphone"),
    ("Åsa Ärlig Öberg", "asa-arlig-oberg"),
    ("  Foo   Bar  ", "foo-bar"),
    ("Pappa", "pappa"),
])
def test_slug_folds_accents_and_punctuation(name, want):
    assert slug(name) == want


def test_the_person_state_document_matches_the_design():
    group = {
        "name": "Elise Högberg", "role": "user",
        # A guest's fields are present in the store but must not leak into a
        # family member's document.
        "doors": ["front"], "days": [1, 3], "from_time": "08:00",
        "to_time": "17:00", "expires": 1791000000.0,
        "devices": [{
            "id": "8f2c", "name": "iPhone 13", "device_model": "iPhone 13",
            "device_os": "18.7", "role": "user",
        }],
    }

    document = state_document(group, last_seen=1790618400)

    assert document == {
        "person": "Elise Högberg",
        "role": "user",
        "active": True,
        "devices": [{
            "id": "8f2c", "name": "iPhone 13", "model": "iPhone 13",
            "os": "18.7", "role": "user", "last_seen": 1790618400,
        }],
    }
    assert "doors" not in document and "window" not in document and "expires" not in document


def test_a_guest_state_carries_the_window_and_expiry():
    group = {
        "name": "Städning", "role": "guest", "doors": ["front"],
        "days": [1, 3], "from_time": "08:00", "to_time": "17:00",
        "expires": 2_000_000_000.0, "devices": [],
    }

    document = state_document(group, now=1_000_000_000.0)

    assert document["doors"] == ["front"]
    assert document["window"] == {"days": [1, 3], "from": "08:00", "to": "17:00"}
    assert document["expires"] == 2_000_000_000.0
    assert document["active"] is True


def test_an_expired_guest_is_not_active():
    group = {
        "name": "Städning", "role": "guest", "doors": ["front"], "days": [],
        "from_time": None, "to_time": None, "expires": 1.0, "devices": [],
    }

    assert state_document(group, now=2.0)["active"] is False


def test_person_discovery_matches_the_design():
    topic, payload = person_discovery("Elise Högberg")

    assert topic == "homeassistant/select/hemnyckel/elise-hogberg/config"
    assert payload["name"] == "Elise Högberg"
    assert payload["unique_id"] == "hemnyckel_person_elise-hogberg"
    assert payload["state_topic"] == "hemnyckel/people/elise-hogberg/state"
    assert payload["command_topic"] == "hemnyckel/people/elise-hogberg/role/set"
    assert payload["value_template"] == "{{ value_json.role }}"
    assert payload["options"] == ["owner", "user", "guest"]
    assert payload["json_attributes_topic"] == "hemnyckel/people/elise-hogberg/state"
    assert payload["availability_topic"] == AVAILABILITY_TOPIC
    assert payload["icon"] == "mdi:account-key"
    assert payload["device"]["identifiers"] == ["hemnyckel_relay"]


def test_relay_discovery_matches_the_design():
    topics = dict(relay_discovery())

    assert set(topics) == {
        "homeassistant/sensor/hemnyckel/relaet/config",
        "homeassistant/binary_sensor/hemnyckel/apns/config",
    }
    sensor = topics["homeassistant/sensor/hemnyckel/relaet/config"]
    assert sensor["state_topic"] == "hemnyckel/relay/state"
    assert sensor["value_template"] == "{{ value_json.status }}"
    assert sensor["json_attributes_topic"] == "hemnyckel/relay/state"
    assert sensor["device"]["name"] == "Hemnyckel"
    binary = topics["homeassistant/binary_sensor/hemnyckel/apns/config"]
    assert binary["device_class"] == "connectivity"
    assert binary["value_template"] == "{{ 'ON' if value_json.apns else 'OFF' }}"


def test_broker_settings_need_host_user_and_password():
    # Pure environment lookup: the other sources are stubbed out.
    no_service = {"service": lambda: None, "options": {}}
    assert load_settings({}, **no_service) is None
    assert load_settings({"MQTT_HOST": "broker"}, **no_service) is None
    assert load_settings({"MQTT_HOST": "broker", "MQTT_USERNAME": "u"}, **no_service) is None
    assert load_settings(
        {"MQTT_HOST": "broker", "MQTT_USERNAME": "u", "MQTT_PASSWORD": ""}, **no_service
    ) is None

    settings = load_settings({
        "MQTT_HOST": "broker", "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p",
        "MQTT_PORT": "8883", "MQTT_SSL": "true",
    }, **no_service)
    assert settings == MqttSettings("broker", 8883, "u", "p", True)


def test_the_environment_is_preferred_over_the_other_sources():
    settings = load_settings(
        {"MQTT_HOST": "env", "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p"},
        service=lambda: MqttSettings("svc", 1883, "u", "p", False),
        options={"mqtt_host": "opt", "mqtt_user": "u", "mqtt_password": "p"},
    )
    assert settings == MqttSettings("env", 1883, "u", "p", False)


def test_the_supervisor_registered_broker_is_used_when_the_environment_is_empty():
    settings = load_settings(
        {},
        service=lambda: MqttSettings("core-mosquitto", 1883, "addons", "p", False),
        options={"mqtt_host": "opt", "mqtt_user": "u", "mqtt_password": "p"},
    )
    assert settings == MqttSettings("core-mosquitto", 1883, "addons", "p", False)


def test_the_options_are_the_last_resort():
    settings = load_settings(
        {},
        service=lambda: None,
        options={"mqtt_host": "core-mosquitto", "mqtt_port": 1883,
                 "mqtt_user": "hemnyckel", "mqtt_password": "p"},
    )
    assert settings == MqttSettings("core-mosquitto", 1883, "hemnyckel", "p", False)


def test_an_incomplete_option_set_is_treated_as_absent():
    assert load_settings({}, service=lambda: None,
                         options={"mqtt_host": "core-mosquitto"}) is None
    assert load_settings({}, service=lambda: None,
                         options={"mqtt_host": "core-mosquitto", "mqtt_user": "u"}) is None
    assert load_settings({}, service=lambda: None,
                         options={"mqtt_host": "core-mosquitto", "mqtt_user": "u",
                                  "mqtt_password": ""}) is None


def test_supervisor_settings_are_none_without_a_token(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert supervisor_settings() is None


def test_supervisor_settings_read_the_registered_broker():
    def fake_fetch(url, token, timeout):
        assert url == "http://supervisor/services/mqtt"
        assert token == "tok"
        return {"result": "ok", "data": {
            "host": "core-mosquitto", "port": 1883, "username": "addons",
            "password": "p", "ssl": False, "addon": "core_mosquitto"}}

    settings = supervisor_settings(token="tok", fetch=fake_fetch)
    assert settings == MqttSettings("core-mosquitto", 1883, "addons", "p", False)


def test_supervisor_settings_survive_a_failed_lookup():
    def broken_fetch(url, token, timeout):
        raise OSError("no supervisor")

    assert supervisor_settings(token="tok", fetch=broken_fetch) is None


def test_supervisor_settings_read_a_partial_payload_as_absent():
    def partial_fetch(url, token, timeout):
        return {"host": "core-mosquitto"}

    assert supervisor_settings(token="tok", fetch=partial_fetch) is None


# -- commands -----------------------------------------------------------------

def test_a_role_change_is_applied_and_republished(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "owner-dev", "Claes", role="owner")
    add_person(store, "elise-dev", "Elise Högberg")
    published.clear()

    asyncio.run(bridge.command("hemnyckel/people/elise-hogberg/role/set", "owner"))

    assert store.device("elise-dev")["role"] == "owner"
    assert [topic for topic, _, _ in published] == ["hemnyckel/people/elise-hogberg/state"]
    assert state_payloads(published)[0]["role"] == "owner"


def test_an_invalid_role_payload_is_ignored(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "elise-dev", "Elise")
    published.clear()

    asyncio.run(bridge.command("hemnyckel/people/elise/role/set", "admin"))

    assert store.device("elise-dev")["role"] == "user"
    assert published == []


def test_an_unknown_slug_is_ignored(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "owner-dev", "Claes", role="owner")
    published.clear()

    asyncio.run(bridge.command("hemnyckel/people/nobody/role/set", "owner"))

    assert published == []
    assert store.persons() == ["Claes"]


def test_a_non_command_topic_is_ignored(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "owner-dev", "Claes", role="owner")
    published.clear()

    asyncio.run(bridge.command("hemnyckel/relay/state", "owner"))
    asyncio.run(bridge.command("hemnyckel/people/claes/role", "user"))

    assert published == []


def test_the_last_owner_cannot_be_demoted_and_the_truth_is_republished(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "owner-dev", "Claes", role="owner")
    published.clear()

    asyncio.run(bridge.command("hemnyckel/people/claes/role/set", "user"))

    assert store.device("owner-dev")["role"] == "owner"
    # The control snaps back: the refusal republishes what is still true.
    assert [topic for topic, _, _ in published] == ["hemnyckel/people/claes/state"]
    assert state_payloads(published)[0]["role"] == "owner"


def test_an_owner_may_step_down_when_another_owner_remains(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "owner-dev", "Claes", role="owner")
    add_person(store, "elise-dev", "Elise", role="owner")

    asyncio.run(bridge.command("hemnyckel/people/claes/role/set", "user"))

    assert store.device("owner-dev")["role"] == "user"
    assert state_payloads(published)[0]["role"] == "user"


# -- the projection -----------------------------------------------------------

def test_refresh_publishes_discovery_then_state(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "elise-dev", "Elise Högberg")
    published.clear()

    bridge.refresh("Elise Högberg")

    assert [topic for topic, _, _ in published] == [
        "homeassistant/select/hemnyckel/elise-hogberg/config",
        "hemnyckel/people/elise-hogberg/state",
    ]
    assert state_payloads(published)[0]["person"] == "Elise Högberg"


def test_refresh_withdraws_an_entity_when_the_person_is_gone(cfg, tmp_path):
    published: list = []
    bridge, store = make_bridge(cfg, tmp_path, published)
    add_person(store, "elise-dev", "Elise")
    store.remove_device("elise-dev")
    published.clear()

    bridge.refresh("Elise")

    assert ("homeassistant/select/hemnyckel/elise/config", "", True) in published
    assert ("hemnyckel/people/elise/state", "", True) in published
