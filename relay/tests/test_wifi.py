from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store
from app.wifi import HomeWifi


def router(entity: str, state: str, name: str | None = None) -> tuple[str, str, dict]:
    return entity, state, {"source_type": "router", "friendly_name": name or entity}


def test_a_router_tracker_that_sees_the_phone_is_home():
    wifi = HomeWifi(["Claes"])
    reading = wifi.reading(*router("device_tracker.claes_iphone_unifi", "home"))
    assert reading is not None
    assert reading.person == "Claes"
    assert reading.evidence == "router"


def test_a_router_that_does_not_see_the_phone_says_nothing():
    wifi = HomeWifi(["Claes"])
    assert wifi.reading(*router("device_tracker.claes_iphone_unifi", "not_home")) is None
    assert wifi.reading(*router("device_tracker.claes_iphone_unifi", "unavailable")) is None


def test_a_gps_tracker_is_not_a_network_reading():
    """Only a router's view counts: a GPS fix is what the geofence already has."""
    wifi = HomeWifi(["Claes"])
    assert wifi.reading("device_tracker.claes_iphone", "home",
                        {"source_type": "gps"}) is None


def test_the_ssid_sensor_needs_a_home_network_to_compare_with():
    """Without a name, any network a phone found would look like the house."""
    unconfigured = HomeWifi(["Claes"])
    assert unconfigured.reading("sensor.claes_iphone_ssid", "Slottet", {}) is None

    configured = HomeWifi(["Claes"], home_ssids=["Slottet"])
    reading = configured.reading("sensor.claes_iphone_ssid", "Slottet", {})
    assert reading is not None and reading.evidence == "wifi"
    # Someone else's network is not ours, and "Not Connected" is no claim.
    assert configured.reading("sensor.claes_iphone_ssid", "Cafe", {}) is None
    assert configured.reading("sensor.claes_iphone_ssid", "Not Connected", {}) is None


def test_the_bssid_sensor_matches_the_house_access_points():
    wifi = HomeWifi(["Claes"], home_bssids=["9A:ED:E1:AE:2B:93"])
    assert wifi.reading("sensor.claes_iphone_bssid", "9a:ed:e1:ae:2b:93", {}) is not None
    assert wifi.reading("sensor.claes_iphone_bssid", "aa:bb:cc:dd:ee:ff", {}) is None


def test_the_owner_can_point_a_person_at_any_entity():
    """A phone named nothing like its owner still counts, when the owner says so."""
    wifi = HomeWifi(["Claes"], entity_map={"Claes": ["device_tracker.the_phone"]})
    reading = wifi.reading("device_tracker.the_phone", "home", {"source_type": "router"})
    assert reading is not None and reading.person == "Claes"
    # The override adds a name; it does not take away the one that already fits.
    assert wifi.reading("device_tracker.claes_iphone_unifi", "home",
                        {"source_type": "router"}).person == "Claes"


def test_the_longer_name_wins_but_a_tie_is_nobodys():
    """A wrong attribution is worse than none: the relay does not guess."""
    wifi = HomeWifi(["Anna", "Anna-Karin"])
    assert wifi.reading("device_tracker.anna_iphone", "home",
                        {"source_type": "router"}).person == "Anna"
    assert wifi.reading("device_tracker.anna_karin_iphone", "home",
                        {"source_type": "router"}).person == "Anna-Karin"

    # Two people whose names match equally well: neither can claim the phone.
    same = HomeWifi(["Claes Högberg", "Claes Berg"])
    assert same.reading("device_tracker.claes_iphone", "home",
                        {"source_type": "router"}) is None


def test_a_wifi_reading_confirms_home_even_after_an_away_report(tmp_path):
    """The house saw the phone, so the GPS jitter at the edge of the zone loses."""
    store = Store(str(tmp_path))
    store.set_presence("Claes", "away")
    store.note_wifi("Claes", evidence="router")

    entry = store.presence()["Claes"]
    assert entry["state"] == "home"
    assert entry["source"] == "wifi"
    assert entry["evidence"] == "router"
    assert entry["stale"] is False


def test_a_lapsed_wifi_reading_is_silence_not_away(tmp_path):
    store = Store(str(tmp_path), wifi_window=60)
    store.note_wifi("Claes", at=1000.0)

    assert store.presence(now=1059.0)["Claes"]["state"] == "home"
    # The geofence never spoke, so once the reading is old there is nothing to
    # say - and nothing on the board.
    assert store.presence(now=1061.0) == {}


def test_wifi_moves_the_last_confirmed_home_forward(tmp_path):
    store = Store(str(tmp_path))
    store.set_presence("Claes", "home")
    later = time.time() + 100
    store.note_wifi("Claes", at=later)

    entry = store.presence(now=later + 1)["Claes"]
    assert entry["state"] == "home"
    assert entry["last_home"] >= later


def test_an_older_wifi_reading_keeps_the_newer_one(tmp_path):
    store = Store(str(tmp_path))
    store.note_wifi("Claes", evidence="router", at=2000.0)
    store.note_wifi("Claes", evidence="wifi", at=1000.0)

    assert store.presence(now=2001.0)["Claes"]["evidence"] == "router"


def test_a_router_sighting_outranks_an_ssid_in_the_same_instant(tmp_path):
    store = Store(str(tmp_path))
    store.note_wifi("Claes", evidence="wifi", at=5000.0)
    store.note_wifi("Claes", evidence="router", at=5000.0)

    assert store.presence(now=5000.0)["Claes"]["evidence"] == "router"


def test_a_rename_moves_the_wifi_reading(tmp_path):
    store = Store(str(tmp_path))
    store.add_device("d1", "Claes' iPhone")
    store.set_apns("d1", "abc", "Claes", {}, "production")
    store.note_wifi("Claes", evidence="router")

    store.rename_person("Claes", "Claes Högberg")

    presence = store.presence()
    assert "Claes" not in presence
    assert presence["Claes Högberg"]["source"] == "wifi"


def test_a_merge_keeps_the_more_recent_reading(tmp_path):
    store = Store(str(tmp_path))
    store.note_wifi("Claes", evidence="old", at=1000.0)
    store.note_wifi("Klas", evidence="new", at=2000.0)

    store.rename_person("Klas", "Claes")

    entry = store.presence(now=2001.0)["Claes"]
    assert entry["evidence"] == "new"


def test_the_last_device_takes_the_wifi_reading_with_it(tmp_path):
    store = Store(str(tmp_path))
    store.add_device("d1", "Claes' iPhone")
    store.set_apns("d1", "abc", "Claes", {}, "production")
    store.note_wifi("Claes")

    store.remove_device("d1")

    assert store.presence() == {}


def state_change(entity: str, state: str, **attributes) -> dict:
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity,
            "new_state": {"state": state, "attributes": attributes},
        },
    }


def test_a_router_state_change_becomes_presence(cfg):
    """The live path: Home Assistant's own stream, not a poll of ours."""
    app = create_app(cfg)
    with TestClient(app):
        hmk = app.state.hmk
        hmk.store.add_device("d1", "Claes' iPhone")
        hmk.store.set_apns("d1", "abc", "Claes", {}, "production")

        hmk.note_wifi_state(
            state_change("device_tracker.claes_iphone_unifi", "home",
                         source_type="router")
        )

        entry = hmk.store.presence()["Claes"]
        assert entry["state"] == "home"
        assert entry["source"] == "wifi"


def test_a_lock_state_change_is_still_not_presence(cfg):
    app = create_app(cfg)
    with TestClient(app):
        hmk = app.state.hmk
        hmk.store.add_device("d1", "Claes' iPhone")
        hmk.store.set_apns("d1", "abc", "Claes", {}, "production")

        hmk.note_wifi_state(state_change("lock.front", "unlocked"))

        assert hmk.store.presence() == {}


def test_wifi_can_be_switched_off(cfg):
    cfg.wifi_enabled = False
    app = create_app(cfg)
    with TestClient(app):
        hmk = app.state.hmk
        hmk.store.add_device("d1", "Claes' iPhone")
        hmk.store.set_apns("d1", "abc", "Claes", {}, "production")

        hmk.note_wifi_state(
            state_change("device_tracker.claes_iphone_unifi", "home",
                         source_type="router")
        )

        assert hmk.store.presence() == {}


def test_the_add_on_options_carry_the_wifi_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HEMNYCKEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HEMNYCKEL_HOME_SSIDS", "Slottet, Slottet 5G")
    monkeypatch.setenv("HEMNYCKEL_WIFI_WINDOW", "600")
    monkeypatch.setenv(
        "HEMNYCKEL_WIFI_PEOPLE", '{"Claes": ["device_tracker.the_phone"]}'
    )

    from app.config import load_config

    cfg = load_config()
    assert cfg.wifi_enabled is True
    assert cfg.home_ssids == ["Slottet", "Slottet 5G"]
    assert cfg.wifi_window == 600
    assert cfg.wifi_people == {"Claes": ["device_tracker.the_phone"]}
