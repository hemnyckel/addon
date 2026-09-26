"""Small SQLite store: paired devices, APNs tokens and a bounded event cache."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any


class Store:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._db = sqlite3.connect(os.path.join(data_dir, "hemnyckel.db"))
        self._db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                person TEXT,
                apns_token TEXT,
                prefs TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL
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
        self._db.commit()

    # -- devices ------------------------------------------------------------
    def add_device(self, device_id: str, name: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO devices (id, name, created) VALUES (?, ?, ?)",
            (device_id, name, time.time()),
        )
        self._db.commit()

    def set_apns(self, device_id: str, apns_token: str, person: str | None, prefs: dict[str, Any]) -> None:
        self._db.execute(
            "UPDATE devices SET apns_token = ?, person = ?, prefs = ? WHERE id = ?",
            (apns_token, person, json.dumps(prefs), device_id),
        )
        self._db.commit()

    def device(self, device_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()

    def devices(self) -> list[sqlite3.Row]:
        return list(self._db.execute("SELECT * FROM devices"))

    def remove_device(self, device_id: str) -> None:
        self._db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
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
        where, args = [], []
        if since is not None:
            where.append("ts >= ?"); args.append(since)
        if door:
            where.append("door = ?"); args.append(door)
        if person:
            where.append("person = ?"); args.append(person)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY ts ASC LIMIT ?"; args.append(limit)
        rows = self._db.execute(q, args).fetchall()
        return [dict(r) | {"door_open": None if r["door_open"] is None else bool(r["door_open"])} for r in rows]

    def last_event(self, door: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM events WHERE door = ? ORDER BY ts DESC LIMIT 1", (door,)
        ).fetchone()
        if row is None:
            return None
        return dict(row) | {"door_open": None if row["door_open"] is None else bool(row["door_open"])}
