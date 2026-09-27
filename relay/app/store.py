"""Small SQLite store: paired devices, APNs tokens and a bounded event cache."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any


def _row(row: sqlite3.Row) -> dict[str, Any]:
    """A row as a dict, with `door_open` normalised to a real bool or None."""
    data = dict(row)
    if data.get("door_open") is not None:
        data["door_open"] = bool(data["door_open"])
    return data


class Store:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        # All access happens on the event-loop thread (the API is fully async),
        # but allow other threads so the relay can be driven from tests/tools.
        self._db = sqlite3.connect(
            os.path.join(data_dir, "hemnyckel.db"), check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                person TEXT,
                apns_token TEXT,
                apns_env TEXT NOT NULL DEFAULT 'production',
                live_start_token TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                doors TEXT,
                days TEXT,
                from_time TEXT,
                to_time TEXT,
                expires REAL,
                prefs TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites (
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presence (
                person TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_activities (
                device TEXT NOT NULL,
                door TEXT NOT NULL,
                token TEXT,
                started REAL NOT NULL,
                PRIMARY KEY (device, door)
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                door TEXT NOT NULL,
                person TEXT,
                slot INTEGER,
                action TEXT NOT NULL,
                source TEXT NOT NULL,
                method TEXT,
                door_open INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            """
        )
        # Migration for databases created before apns_env existed.
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(devices)")}
        if "apns_env" not in columns:
            self._db.execute(
                "ALTER TABLE devices ADD COLUMN apns_env TEXT NOT NULL DEFAULT 'production'"
            )
        if "live_start_token" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN live_start_token TEXT")
        if "role" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "doors" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN doors TEXT")
        if "expires" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN expires REAL")
        if "days" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN days TEXT")
        if "from_time" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN from_time TEXT")
        if "to_time" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN to_time TEXT")
        invite_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(invites)")}
        for column, ddl in (
            ("role", "ALTER TABLE invites ADD COLUMN role TEXT NOT NULL DEFAULT 'guest'"),
            ("days", "ALTER TABLE invites ADD COLUMN days TEXT"),
            ("from_time", "ALTER TABLE invites ADD COLUMN from_time TEXT"),
            ("to_time", "ALTER TABLE invites ADD COLUMN to_time TEXT"),
        ):
            if column not in invite_columns:
                self._db.execute(ddl)
        self._db.commit()

    # -- devices ------------------------------------------------------------
    def add_device(self, device_id: str, name: str, role: str = "user") -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO devices (id, name, role, created) VALUES (?, ?, ?, ?)",
            (device_id, name, role, time.time()),
        )
        self._db.commit()

    def set_role(self, device_id: str, role: str) -> None:
        self._db.execute("UPDATE devices SET role = ? WHERE id = ?", (role, device_id))
        self._db.commit()

    def add_guest(self, device_id: str, name: str, doors: list[str],
                  expires: float) -> None:
        self.add_invited(device_id, name, "guest", doors, [], None, None, expires)

    def add_invited(self, device_id: str, name: str, role: str, doors: list[str],
                    days: list[int], from_time: str | None, to_time: str | None,
                    expires: float | None) -> None:
        """A device created from an owner's invitation (a family member or guest)."""
        self._db.execute(
            "INSERT OR REPLACE INTO devices "
            "(id, name, role, doors, days, from_time, to_time, expires, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, name, role, json.dumps(doors), json.dumps(days),
             from_time, to_time, expires, time.time()),
        )
        self._db.commit()

    # -- invitations (a device is created by the owner in advance) -----------
    def add_invite(self, code: str, name: str, role: str, doors: list[str],
                   days: list[int], from_time: str | None, to_time: str | None,
                   expires: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO invites "
            "(code, name, role, doors, days, from_time, to_time, expires, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, name, role, json.dumps(doors), json.dumps(days),
             from_time, to_time, expires, time.time()),
        )
        self._db.commit()

    def invite(self, code: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()

    def use_invite(self, code: str, device_id: str) -> None:
        self._db.execute("UPDATE invites SET used_by = ? WHERE code = ?", (device_id, code))
        self._db.commit()

    def owner_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE role = 'owner'"
        ).fetchone()
        return int(row["c"])

    def ensure_owner(self) -> None:
        """Every install needs an owner: promote the oldest device if none is."""
        if self.owner_count() > 0:
            return
        row = self._db.execute(
            "SELECT id FROM devices ORDER BY created LIMIT 1"
        ).fetchone()
        if row is not None:
            self.set_role(row["id"], "owner")

    def set_apns(self, device_id: str, apns_token: str, person: str | None,
                 prefs: dict[str, Any], env: str = "production") -> None:
        self._db.execute(
            "UPDATE devices SET apns_token = ?, person = ?, prefs = ?, apns_env = ? WHERE id = ?",
            (apns_token, person, json.dumps(prefs), env, device_id),
        )
        self._db.commit()

    def disable_apns(self, device_id: str) -> None:
        """Drop a device's push token after Apple says it is dead.

        The device keeps its pairing token, so the app can re-register a fresh
        APNs token on its next launch — it just gets no pushes until then.
        """
        self._db.execute(
            "UPDATE devices SET apns_token = NULL WHERE id = ?", (device_id,)
        )
        self._db.commit()

    def device(self, device_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()

    def devices(self) -> list[sqlite3.Row]:
        return list(self._db.execute("SELECT * FROM devices"))

    def remove_device(self, device_id: str) -> None:
        self._db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self._db.commit()

    # -- live activities ----------------------------------------------------
    def set_live_start_token(self, device_id: str, token: str) -> None:
        """A device's push-to-start token, used to start an activity headlessly."""
        self._db.execute(
            "UPDATE devices SET live_start_token = ? WHERE id = ?", (token, device_id)
        )
        self._db.commit()

    def live_activities(self, door: str) -> list[sqlite3.Row]:
        return list(
            self._db.execute(
                "SELECT * FROM live_activities WHERE door = ? ORDER BY started DESC", (door,)
            )
        )

    def set_live_activity(self, device_id: str, door: str, token: str) -> None:
        """Record the per-activity update token the app reported."""
        self._db.execute(
            "INSERT INTO live_activities (device, door, token, started) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(device, door) DO UPDATE SET token = excluded.token",
            (device_id, door, token, time.time()),
        )
        self._db.commit()

    def touch_live_start(self, device_id: str, door: str) -> None:
        """Note that a push-to-start was sent, before the app reports a token."""
        self._db.execute(
            "INSERT OR IGNORE INTO live_activities (device, door, token, started) "
            "VALUES (?, ?, NULL, ?)",
            (device_id, door, time.time()),
        )
        self._db.commit()

    def drop_live_activity(self, device_id: str, door: str) -> None:
        self._db.execute(
            "DELETE FROM live_activities WHERE device = ? AND door = ?", (device_id, door)
        )
        self._db.commit()

    # -- events -------------------------------------------------------------
    def add_event(self, event: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO events "
            "(id, ts, door, person, slot, action, source, method, door_open) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["id"], event["ts"], event["door"], event.get("person"),
                event.get("slot"), event["action"], event["source"],
                event.get("method"), None if event.get("door_open") is None else int(event["door_open"]),
            ),
        )
        # keep the cache bounded
        self._db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY ts DESC LIMIT 2000)"
        )
        self._db.commit()

    def events(self, *, since: float | None = None, door: str | None = None,
               person: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = "SELECT * FROM events"
        where: list[str] = []
        args: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if door:
            where.append("door = ?")
            args.append(door)
        if person:
            where.append("person = ?")
            args.append(person)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY ts ASC LIMIT ?"
        args.append(limit)
        rows = self._db.execute(q, args).fetchall()
        return [_row(row) for row in rows]

    def last_event(self, door: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM events WHERE door = ? ORDER BY ts DESC LIMIT 1", (door,)
        ).fetchone()
        return None if row is None else _row(row)

    def presence(self) -> dict[str, str]:
        """Each person's last known state.

        Two signals, latest wins: a lock event attributed to them (an unlock is
        an arrival, a lock a departure) and an explicit report from their phone
        (the home geofence). Per person, so an automatic relock — which carries
        no person — can never make someone disappear.
        """
        latest: dict[str, tuple[float, str]] = {}
        for row in self._db.execute(
            "SELECT person, action, ts FROM events WHERE person IS NOT NULL ORDER BY ts"
        ):
            latest[str(row["person"])] = (
                float(row["ts"]),
                "home" if row["action"] == "unlock" else "away",
            )
        for row in self._db.execute("SELECT person, state, updated FROM presence"):
            person = str(row["person"])
            ts = float(row["updated"])
            if person not in latest or ts > latest[person][0]:
                latest[person] = (ts, str(row["state"]))
        return {person: state for person, (_, state) in latest.items()}

    def set_presence(self, person: str, state: str) -> None:
        self._db.execute(
            "INSERT INTO presence (person, state, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(person) DO UPDATE SET state = excluded.state, updated = excluded.updated",
            (person, state, time.time()),
        )
        self._db.commit()

    # -- settings -----------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        self._db.commit()

    def setting(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])
