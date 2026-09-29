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


def test_events_keep_the_newest_and_drop_the_oldest(tmp_path):
    """The regression: a full window must never hide the latest event.

    Once the journal outgrew the default limit, the old ascending query
    returned the *oldest* window and history silently stopped updating.
    """
    store = Store(str(tmp_path))
    for i in range(5):
        store.add_event(event(i))

    window = store.events(limit=3)

    assert [e["id"] for e in window] == ["e2", "e3", "e4"]  # newest kept


def test_events_are_returned_ascending_from_the_newest_end(tmp_path):
    store = Store(str(tmp_path))
    for i in (4, 0, 2, 1, 3):  # inserted out of order on purpose
        store.add_event(event(i))

    window = store.events(limit=3)

    # The newest three (ts 2, 3, 4), handed back oldest first for display.
    assert [e["ts"] for e in window] == [2.0, 3.0, 4.0]


def test_events_limit_one_is_the_newest(tmp_path):
    store = Store(str(tmp_path))
    for i in range(5):
        store.add_event(event(i))

    assert [e["id"] for e in store.events(limit=1)] == ["e4"]


def test_events_since_filters_inclusively_at_the_newest_end(tmp_path):
    store = Store(str(tmp_path))
    for i in range(5):
        store.add_event(event(i))

    assert [e["id"] for e in store.events(since=2.0)] == ["e2", "e3", "e4"]
    # Since matches more rows than the limit: the newest survive.
    assert [e["id"] for e in store.events(since=0.0, limit=2)] == ["e3", "e4"]


def test_events_before_is_the_cursor_for_older_history(tmp_path):
    store = Store(str(tmp_path))
    for i in range(5):
        store.add_event(event(i))

    # Everything strictly older than ts 3, newest first, then ascending.
    assert [e["id"] for e in store.events(before=3.0, limit=2)] == ["e1", "e2"]
    assert [e["id"] for e in store.events(before=2.0)] == ["e0", "e1"]


def test_the_journal_pulse_is_the_newest_event(tmp_path):
    store = Store(str(tmp_path))
    assert store.last_event_at() is None
    assert store.event_count() == 0

    store.add_event(event(7))
    store.add_event(event(9))
    store.add_event(event(8))

    assert store.last_event_at() == 9.0
    assert store.event_count() == 3


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


def test_presence_needs_a_geofence_report(tmp_path):
    store = Store(str(tmp_path))
    # A lock event alone - an unlock, or an automatic relock with no person -
    # is never presence: the board shows only geofence-confirmed people.
    store.add_event(event(1, id="a", door="front", person="Elise", action="unlock"))
    store.add_event(event(2, id="b", door="front", person=None, action="lock"))
    store.add_event(event(3, id="c", door="back", person="Pappa", action="lock"))

    assert store.presence() == {}

    # The phone's own report is what puts someone on the board.
    store.set_presence("Elise", "home")
    assert store.presence()["Elise"]["state"] == "home"
    assert store.presence()["Elise"]["source"] == "geofence"


def test_a_home_nothing_confirms_stops_reading_as_home(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()

    # A fresh report is home; five hours later the same row is not.
    store.set_presence("Elise", "home")
    aged = now - 5 * 3600
    store._db.execute(
        "UPDATE presence SET updated = ?, last_home = ? WHERE person = 'Elise'",
        (aged, aged),
    )
    assert store.presence(now=now)["Elise"] == {
        "state": "away", "source": "geofence", "at": aged,
        "last_home": aged, "stale": True,
    }


def test_a_lapsed_home_is_held_briefly_by_a_recent_unlock(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()
    aged = now - 5 * 3600

    # A lapsed geofence home plus an unlock a few minutes ago: the hint holds it.
    store.set_presence("Elise", "home")
    store._db.execute(
        "UPDATE presence SET updated = ?, last_home = ? WHERE person = 'Elise'",
        (aged, aged),
    )
    store.add_event(event(1, id="a", person="Elise", action="unlock", ts=now - 600))
    assert store.presence(now=now)["Elise"] == {
        "state": "home", "source": "lock", "at": now - 600,
        "last_home": aged, "stale": False,
    }

    # An unlock older than the hint window is history, not corroboration.
    store.set_presence("Isabelle", "home")
    store._db.execute(
        "UPDATE presence SET updated = ?, last_home = ? WHERE person = 'Isabelle'",
        (aged, aged),
    )
    store.add_event(event(2, id="b", person="Isabelle", action="unlock", ts=now - 2 * 3600))
    assert store.presence(now=now)["Isabelle"]["state"] == "away"
    assert store.presence(now=now)["Isabelle"]["stale"] is True


def test_an_unlock_never_turns_an_away_into_home(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()
    store.set_presence("Pappa", "away")
    store.add_event(event(1, id="a", person="Pappa", action="unlock", ts=now - 30))

    # The phone said he left; an unlock is a hint, not a location, so it may
    # not put him back on the board.
    assert store.presence(now=now)["Pappa"]["state"] == "away"
    assert store.presence(now=now)["Pappa"]["source"] == "geofence"


def test_last_home_survives_a_later_away_report(tmp_path):
    store = Store(str(tmp_path))
    store.set_presence("Elise", "home")
    home_at = store._db.execute(
        "SELECT updated FROM presence WHERE person = 'Elise'"
    ).fetchone()["updated"]
    store.set_presence("Elise", "away")

    entry = store.presence()["Elise"]
    assert entry["state"] == "away"
    assert entry["stale"] is False
    assert entry["last_home"] == home_at


def test_guest_slots_round_trip_and_rename(tmp_path):
    store = Store(str(tmp_path))

    store.set_guest_slots("Städ", {"front": 6, "back": 7})
    assert store.guest_slots("Städ") == {"front": 6, "back": 7}
    assert store.guest_slots("Someone else") == {}

    # A person is their name: the registry follows a rename.
    store.rename_guest_slots("Städ", "Städhjälpen")
    assert store.guest_slots("Städ") == {}
    assert store.guest_slots("Städhjälpen") == {"front": 6, "back": 7}

    store.set_guest_slots("Städhjälpen", {})
    assert store.guest_slots("Städhjälpen") == {}


def test_guest_slots_survive_a_malformed_setting(tmp_path):
    store = Store(str(tmp_path))
    store.set_setting("guest_slots", "not json")
    assert store.guest_slots("Städ") == {}


def test_rename_person_moves_devices_presence_and_invites(tmp_path):
    store = Store(str(tmp_path))
    store.add_invited("d1", "Städ", "guest", ["front"], [], None, None, None,
                      person="Städ")
    store.add_invite("CODE1", "Städ", "guest", ["front"], [], None, None, time.time() + 60)
    store.set_presence("Städ", "away")

    store.rename_person("Städ", "Städhjälpen")

    assert store.device("d1")["person"] == "Städhjälpen"
    assert store.presence()["Städhjälpen"]["state"] == "away"
    assert store.invite("CODE1")["name"] == "Städhjälpen"


def test_other_device_count_sees_the_persons_other_phones(tmp_path):
    store = Store(str(tmp_path))
    store.add_invited("a", "Städ", "guest", ["front"], [], None, None, None, person="Städ")
    store.add_invited("b", "Städ", "guest", ["front"], [], None, None, None, person="Städ")

    assert store.other_device_count("Städ", "a") == 1
    assert store.other_device_count("Städ", "b") == 1
    assert store.other_device_count("Städ", "ghost") == 2
