from __future__ import annotations

import sqlite3

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


def test_events_are_bounded_and_queryable(tmp_path):
    store = Store(str(tmp_path))
    for i in range(5):
        store.add_event(event(i))

    assert len(store.events()) == 5
    assert store.last_event("front")["id"] == "e4"
    assert [e["id"] for e in store.events(since=2.0)] == ["e2", "e3", "e4"]
    assert store.last_event("back") is None
