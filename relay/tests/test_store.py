from __future__ import annotations

import json
import sqlite3
import time

from app.store import Store


def event(i: int, **overrides) -> dict:
    base = {
        "id": f"e{i}",
        "ts": float(i),
        "door": "front",
        "person": "claes",
        "slot": 1,
        "action": "unlock",
        "source": "keypad",
        "method": "Kod",
        "door_open": None,
    }
    return base | overrides


def test_device_apns_roundtrip(tmp_path):
    store = Store(str(tmp_path))
    store.add_device("d1", "Claes' iPhone")
    store.set_apns("d1", "abc", "claes", {"doors": ["front"]}, "development")

    row = store.device("d1")
    assert row["apns_token"] == "abc"
    assert row["person"] == "claes"
    assert row["apns_env"] == "development"


def test_disable_apns_keeps_the_paired_device(tmp_path):
    store = Store(str(tmp_path))
    store.add_device("d1", "Claes' iPhone")
    store.set_apns("d1", "abc", None, {}, "production")

    store.disable_apns("d1")

    row = store.device("d1")
    assert row["apns_token"] is None
    assert row["name"] == "Claes' iPhone"  # pairing survives; only push is dropped
    assert store.devices()


def test_migrates_a_database_without_apns_env(tmp_path):
    db = tmp_path / "hemnyckel.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            person TEXT,
            apns_token TEXT,
            prefs TEXT NOT NULL DEFAULT '{}',
            created REAL NOT NULL
        );
        """
    )
    con.execute("INSERT INTO devices (id, name, created) VALUES ('old', 'Old phone', 1.0)")
    con.commit()
    con.close()

    store = Store(str(tmp_path))
    assert store.device("old")["apns_env"] == "production"
    assert store.device("old")["live_start_token"] is None


def test_invite_slots_round_trip_and_are_cleared(tmp_path):
    """An invitation remembers where its lock codes live, never the codes."""
    store = Store(str(tmp_path))
    store.add_invite("A1B2C3", "Städ", "guest", ["front", "back"], [1], "08:00", "17:00",
                     time.time() + 3600, slots={"front": 6, "back": 7})

    assert json.loads(store.invite("A1B2C3")["slots"]) == {"front": 6, "back": 7}

    store.use_invite("A1B2C3", "dev1")
    assert store.invite_for_device("dev1")["code"] == "A1B2C3"
    assert store.invite_for_device("nobody") is None

    store.clear_invite_slots("A1B2C3")
    assert store.invite("A1B2C3")["slots"] is None


def test_migrates_an_invites_table_without_slots(tmp_path):
    db = tmp_path / "hemnyckel.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE invites (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'guest',
            doors TEXT,
            days TEXT,
            from_time TEXT,
            to_time TEXT,
            expires REAL NOT NULL,
            created REAL NOT NULL,
            used_by TEXT
        );
        """
    )
    con.commit()
    con.close()

    store = Store(str(tmp_path))
    store.add_invite("X", "N", "guest", [], [], None, None, time.time() + 1,
                     slots={"front": 6})
    assert json.loads(store.invite("X")["slots"]) == {"front": 6}


def test_events_are_bounded_and_queryable(tmp_path):
    store = Store(str(tmp_path))
    for i in range(5):
        store.add_event(event(i))

    assert len(store.events()) == 5
    assert store.last_event("front")["id"] == "e4"
    assert [e["id"] for e in store.events(since=2.0)] == ["e2", "e3", "e4"]
    assert store.last_event("back") is None


def test_live_activity_tracking(tmp_path):
    store = Store(str(tmp_path))
    store.add_device("d1", "iPhone")

    store.set_live_start_token("d1", "start-1")
    assert store.device("d1")["live_start_token"] == "start-1"

    # A push-to-start is remembered before the app reports a per-activity token.
    store.touch_live_start("d1", "front")
    assert store.live_activities("front")[0]["token"] is None

    store.set_live_activity("d1", "front", "act-1")
    assert store.live_activities("front")[0]["token"] == "act-1"

    # Touching again must not wipe the token we already have.
    store.touch_live_start("d1", "front")
    assert store.live_activities("front")[0]["token"] == "act-1"

    store.drop_live_activity("d1", "front")
    assert store.live_activities("front") == []


def test_role_defaults_and_owner_bootstrap(tmp_path):
    store = Store(str(tmp_path))
    store.add_device("d1", "First")
    store.add_device("d2", "Second")

    assert store.device("d1")["role"] == "user"
    assert store.owner_count() == 0

    store.ensure_owner()
    assert store.device("d1")["role"] == "owner"  # the oldest device owns
    assert store.device("d2")["role"] == "user"

    store.ensure_owner()  # idempotent
    assert store.owner_count() == 1


def test_presence_follows_each_person_not_each_door(tmp_path):
    store = Store(str(tmp_path))
    store.add_event(event(1, id="a", door="front", person="Elise", action="unlock"))
    store.add_event(event(2, id="b", door="front", person=None, action="lock"))  # auto
    store.add_event(event(3, id="c", door="back", person="Pappa", action="unlock"))
    store.add_event(event(4, id="d", door="back", person="Pappa", action="lock"))

    # Elise is still home: the automatic relock carries no person.
    now = store.presence()
    assert now["Elise"]["state"] == "home" and now["Elise"]["source"] == "lock"
    assert now["Pappa"]["state"] == "away"


def test_presence_merges_lock_events_and_geofence_reports(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()

    # An unlock a while ago, then a geofence report just now: she has left.
    store.add_event(event(1, id="a", person="Elise", action="unlock", ts=now - 600))
    store.set_presence("Elise", "away")
    assert store.presence()["Elise"]["state"] == "away"
    assert store.presence()["Elise"]["source"] == "geofence"

    # The other way round: a report, then a fresh unlock: he is home again.
    store.add_event(event(2, id="b", person="Pappa", action="lock", ts=now - 600))
    store.set_presence("Pappa", "away")
    store._db.execute("UPDATE presence SET updated = ? WHERE person = 'Pappa'", (now - 300,))
    store.add_event(event(3, id="c", person="Pappa", action="unlock", ts=now - 10))
    assert store.presence()["Pappa"] == {"state": "home", "source": "lock", "at": now - 10}
