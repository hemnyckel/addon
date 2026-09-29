"""Small SQLite store: paired devices, APNs tokens and a bounded event cache."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

_LOGGER = logging.getLogger("hemnyckel.store")

# Presence has a shelf life. A phone's geofence is the truth, and a `home` it
# reported is trusted for this long; after that the person stops reading as
# home. Hours, not days: long enough to cover an outing or a school run that the
# phone never explicitly left, short enough that one geofence blip cannot pin
# someone "home" for a whole day.
_PRESENCE_TTL = 4 * 3600
# An unlock is a hint, never a source. It can corroborate a home that has just
# lapsed, for this long - but it never sets presence by itself, and never for
# the hours a geofence does.
_UNLOCK_HINT = 30 * 60


def _row(row: sqlite3.Row) -> dict[str, Any]:
    """A row as a dict, with `door_open` normalised to a real bool or None."""
    data = dict(row)
    if data.get("door_open") is not None:
        data["door_open"] = bool(data["door_open"])
    return data


def _json_list(raw: Any) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _json_map(raw: Any) -> dict[str, int]:
    """A JSON object of door -> slot, or an empty map when malformed."""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, slot in value.items():
        try:
            result[str(key)] = int(slot)
        except (TypeError, ValueError):
            continue
    return result


# The person-level map of the lock slots a guest's codes live in, keyed by
# person name. It exists because a guest is a *person* - their codes may have
# been created by several invitations, or edited after the fact - and the
# invitation is a per-device artifact that may not even exist (a role set from
# Home Assistant before guests stopped being a bridge role). It is a registry,
# not history.
_GUEST_SLOTS_KEY = "guest_slots"


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
                device_model TEXT,
                device_os TEXT,
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
                used_by TEXT,
                slots TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presence (
                person TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated REAL NOT NULL,
                last_home REAL
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
        if "device_model" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN device_model TEXT")
        if "device_os" not in columns:
            self._db.execute("ALTER TABLE devices ADD COLUMN device_os TEXT")
        # Presence gained the last confirmed home, so a lapsed `home` can still
        # say when it was last true ("senast hemma 09:17"). A row that says home
        # right now is its own last home; older rows have no earlier time to
        # recover, and an absent value stays honestly unknown.
        presence_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(presence)")}
        if "last_home" not in presence_columns:
            self._db.execute("ALTER TABLE presence ADD COLUMN last_home REAL")
        self._db.execute(
            "UPDATE presence SET last_home = updated WHERE state = 'home' AND last_home IS NULL"
        )
        invite_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(invites)")}
        for column, ddl in (
            ("role", "ALTER TABLE invites ADD COLUMN role TEXT NOT NULL DEFAULT 'guest'"),
            ("days", "ALTER TABLE invites ADD COLUMN days TEXT"),
            ("from_time", "ALTER TABLE invites ADD COLUMN from_time TEXT"),
            ("to_time", "ALTER TABLE invites ADD COLUMN to_time TEXT"),
            # The lock slot(s) this invitation created, keyed by door id. The
            # slot is the handle a later revocation needs; the code never lands
            # here.
            ("slots", "ALTER TABLE invites ADD COLUMN slots TEXT"),
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

    def set_device_info(self, device_id: str, model: str | None, os: str | None) -> None:
        """What the phone says it is (it identifies itself; nobody types it)."""
        if not model and not os:
            return
        self._db.execute(
            "UPDATE devices SET device_model = COALESCE(?, device_model), "
            "device_os = COALESCE(?, device_os) WHERE id = ?",
            (model or None, os or None, device_id),
        )
        self._db.commit()

    def persons(self) -> list[str]:
        """The people the relay knows, i.e. those who have a device.

        Deliberately devices-only: the app's pickers must match the Personer
        screen exactly, or they drift.
        """
        return [
            str(row["person"])
            for row in self._db.execute(
                "SELECT DISTINCT person FROM devices "
                "WHERE person IS NOT NULL AND person != '' ORDER BY person"
            )
        ]

    def set_role(self, device_id: str, role: str) -> None:
        self._db.execute("UPDATE devices SET role = ? WHERE id = ?", (role, device_id))
        self._db.commit()

    def set_role_for_person(self, person: str, role: str) -> None:
        """A role belongs to the person, not to each of their phones."""
        self._db.execute("UPDATE devices SET role = ? WHERE person = ?", (role, person))
        self._db.commit()

    def replace_duplicates(self, device_id: str) -> None:
        """Drop older rows that are the same phone re-paired.

        Matched on name, model and person together, so two family members whose
        phones share a default name are never confused. An owner role is carried
        over to the surviving device.
        """
        row = self.device(device_id)
        if row is None or not row["device_model"] or not row["person"]:
            return
        duplicates = self._db.execute(
            "SELECT id, role FROM devices "
            "WHERE id != ? AND name = ? AND device_model = ? AND person = ?",
            (device_id, row["name"], row["device_model"], row["person"]),
        ).fetchall()
        if not duplicates:
            return
        if any(duplicate["role"] == "owner" for duplicate in duplicates):
            self.set_role(device_id, "owner")
        for duplicate in duplicates:
            self.remove_device(duplicate["id"])
        _LOGGER.info("replaced %d older row(s) for %s", len(duplicates), row["name"])

    def person_exists(self, person: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM devices WHERE person = ? LIMIT 1", (person,)
        ).fetchone()
        return row is not None

    def owner_devices(self, person: str | None = None) -> int:
        if person is None:
            row = self._db.execute(
                "SELECT COUNT(*) AS c FROM devices WHERE role = 'owner'"
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) AS c FROM devices WHERE role = 'owner' AND person = ?",
                (person,),
            ).fetchone()
        return int(row["c"])

    def people(self) -> list[dict[str, Any]]:
        """People, each with their devices — the shape the Personer screen needs."""
        groups: dict[str, dict[str, Any]] = {}
        for row in self._db.execute("SELECT * FROM devices ORDER BY created"):
            person = str(row["person"] or "").strip()
            group = groups.get(person) if person else None
            if group is None:
                group = {
                    "name": person,
                    "role": row["role"],
                    "doors": _json_list(row["doors"]) or None,
                    "days": _json_list(row["days"]) or None,
                    "from_time": row["from_time"],
                    "to_time": row["to_time"],
                    "expires": row["expires"],
                    "devices": [],
                }
                groups[person or f"#{row['id']}"] = group
            elif row["role"] == "owner" or (row["role"] == "guest"
                                            and group["role"] == "user"):
                # A person's role is the strongest of their devices.
                group["role"] = row["role"]
            group["devices"].append({
                "id": row["id"],
                "name": row["name"],
                "person": row["person"],
                "role": row["role"],
                "device_model": row["device_model"],
                "device_os": row["device_os"],
                "created": row["created"],
            })
        return sorted(
            groups.values(),
            key=lambda group: (not group["name"], group["name"] or group["devices"][0]["name"]),
        )

    # -- guest life: the person's own code slots -----------------------------
    def _all_guest_slots(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for person, raw in self._guest_slots_raw().items():
            slots = _json_map(raw)
            if slots:
                result[str(person)] = slots
        return result

    def _guest_slots_raw(self) -> dict[str, Any]:
        raw = self.setting(_GUEST_SLOTS_KEY)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _save_guest_slots(self, slots: dict[str, dict[str, int]]) -> None:
        self.set_setting(_GUEST_SLOTS_KEY, json.dumps(slots))

    def guest_slots(self, person: str) -> dict[str, int]:
        """The lock slots this person's guest codes live in, by door."""
        return dict(self._all_guest_slots().get(person, {}))

    def set_guest_slots(self, person: str, slots: dict[str, int]) -> None:
        if not person:
            return
        all_slots = self._all_guest_slots()
        clean = {str(door): int(slot) for door, slot in slots.items() if str(door)}
        if clean:
            all_slots[person] = clean
        else:
            all_slots.pop(person, None)
        self._save_guest_slots(all_slots)

    def rename_guest_slots(self, old: str, new: str) -> None:
        if not old or not new or old == new:
            return
        all_slots = self._all_guest_slots()
        moved = all_slots.pop(old, None)
        if moved:
            all_slots[new] = moved
            self._save_guest_slots(all_slots)

    def set_guest_fields_for_person(
        self, person: str, doors: list[str], days: list[int],
        from_time: str | None, to_time: str | None, expires: float | None,
    ) -> None:
        """Move a whole guest life at once - every one of the person's devices."""
        self._db.execute(
            "UPDATE devices SET doors = ?, days = ?, from_time = ?, to_time = ?, "
            "expires = ? WHERE person = ?",
            (json.dumps(doors), json.dumps(days), from_time, to_time, expires, person),
        )
        self._db.commit()

    def rename_person(self, old: str, new: str) -> None:
        """A person is their name: every row that carried it carries the new one."""
        self._db.execute("UPDATE devices SET person = ? WHERE person = ?", (new, old))
        self._db.execute("UPDATE presence SET person = ? WHERE person = ?", (new, old))
        self._db.execute("UPDATE invites SET name = ? WHERE name = ?", (new, old))
        self._db.commit()

    def other_device_count(self, person: str, device_id: str) -> int:
        """How many of this person's devices are not ``device_id``."""
        row = self._db.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE person = ? AND id != ?",
            (person, device_id),
        ).fetchone()
        return int(row["c"])

    def add_guest(self, device_id: str, name: str, doors: list[str],
                  expires: float) -> None:
        self.add_invited(device_id, name, "guest", doors, [], None, None, expires,
                         person=name)

    def add_invited(self, device_id: str, name: str, role: str, doors: list[str],
                    days: list[int], from_time: str | None, to_time: str | None,
                    expires: float | None, person: str | None = None) -> None:
        """A device created from an owner's invitation (a family member or guest).

        The person may be unknown: a family member sets their own name once their
        phone is paired.
        """
        self._db.execute(
            "INSERT OR REPLACE INTO devices "
            "(id, name, person, role, doors, days, from_time, to_time, expires, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, name, person, role, json.dumps(doors), json.dumps(days),
             from_time, to_time, expires, time.time()),
        )
        self._db.commit()

    # -- invitations (a device is created by the owner in advance) -----------
    def add_invite(self, code: str, name: str, role: str, doors: list[str],
                   days: list[int], from_time: str | None, to_time: str | None,
                   expires: float, slots: dict[str, int] | None = None) -> None:
        """Record an owner's invitation.

        ``slots`` maps each door to the lock slot a matching guest code was
        written to, so the codes can be revoked with the guest. The code itself
        is never stored — only the slot number.
        """
        self._db.execute(
            "INSERT OR REPLACE INTO invites "
            "(code, name, role, doors, days, from_time, to_time, expires, created, slots) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, name, role, json.dumps(doors), json.dumps(days),
             from_time, to_time, expires, time.time(),
             json.dumps(slots) if slots else None),
        )
        self._db.commit()

    def invite(self, code: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()

    def invite_for_device(self, device_id: str) -> sqlite3.Row | None:
        """The invitation a device was created from, if any."""
        return self._db.execute(
            "SELECT * FROM invites WHERE used_by = ?", (device_id,)
        ).fetchone()

    def clear_invite_slots(self, code: str) -> None:
        """Forget an invitation's lock slots once their codes are revoked.

        Cleared after the attempt, not before: this is what keeps a guest's
        revocation (or a refusal at expiry) from being retried on every request.
        """
        self._db.execute("UPDATE invites SET slots = NULL WHERE code = ?", (code,))
        self._db.commit()

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
        # An empty person never wipes a known one (an invitation may have named
        # this person already).
        self._db.execute(
            "UPDATE devices SET apns_token = ?, "
            "person = COALESCE(NULLIF(?, ''), person), prefs = ?, apns_env = ? WHERE id = ?",
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

    def last_seen(self, person: str) -> float | None:
        """When this person was last behind an attributed lock event.

        The relay keeps no per-device clock, so this is the person's own last
        activity; the bridge shows it on each of their devices. None means the
        relay has never attributed anything to them.
        """
        row = self._db.execute(
            "SELECT MAX(ts) AS ts FROM events WHERE person = ?", (person,)
        ).fetchone()
        return None if row is None or row["ts"] is None else float(row["ts"])

    def event_persons(self) -> list[str]:
        """Every person name the event history has attributed something to.

        The bridge uses this once, to seed its published-slug registry on an
        install that predates it: a person renamed in the app left the old name
        only in this history, and the old retained discovery must be withdrawn.
        It is a read of history, never a rewrite of it.
        """
        return [
            str(row["person"])
            for row in self._db.execute(
                "SELECT DISTINCT person FROM events "
                "WHERE person IS NOT NULL AND person != '' ORDER BY person"
            )
        ]

    def presence(self, *, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Each person's presence, from their phone's geofence, with a shelf life.

        The geofence is the truth: a phone's own enter/exit report is the only
        thing that sets presence, and a ``home`` is trusted for ``_PRESENCE_TTL``.
        Once that window has passed the person reads as away again, with the time
        of the last confirmed home kept in ``last_home`` (what the app can show as
        "senast hemma"). An ``away`` never expires - it stays until a later
        report - because away is not a claim that needs a clock.

        An unlock is a hint that travels with ``source: "lock"``: it can keep a
        home that has just lapsed for ``_UNLOCK_HINT``, but it never sets presence
        on its own, never flips an explicit away back to home, and never lasts the
        hours a geofence does. A person the relay has never seen a geofence report
        from has no presence at all - the board never guesses.
        """
        moment = time.time() if now is None else now
        latest_unlock: dict[str, float] = {}
        for row in self._db.execute(
            "SELECT person, MAX(ts) AS ts FROM events "
            "WHERE person IS NOT NULL AND action = 'unlock' GROUP BY person"
        ):
            latest_unlock[str(row["person"])] = float(row["ts"])

        result: dict[str, dict[str, Any]] = {}
        for row in self._db.execute(
            "SELECT person, state, updated, last_home FROM presence"
        ):
            person = str(row["person"])
            state = str(row["state"])
            updated = float(row["updated"])
            recorded = row["last_home"]
            last_home = float(recorded) if recorded is not None else (
                updated if state == "home" else None
            )
            if state == "home" and moment - updated > _PRESENCE_TTL:
                unlock_at = latest_unlock.get(person)
                if unlock_at is not None and moment - unlock_at <= _UNLOCK_HINT:
                    # The geofence home has lapsed; a recent unlock is all that
                    # still holds it, so it is tagged as the hint it is.
                    result[person] = {
                        "state": "home", "source": "lock", "at": unlock_at,
                        "last_home": last_home, "stale": False,
                    }
                    continue
                # Nothing has confirmed this home within its shelf life: it is
                # no longer home, but the last known state and its time remain.
                result[person] = {
                    "state": "away", "source": "geofence", "at": updated,
                    "last_home": last_home, "stale": True,
                }
                continue
            result[person] = {
                "state": state, "source": "geofence", "at": updated,
                "last_home": last_home, "stale": False,
            }
        return result

    def set_presence(self, person: str, state: str) -> None:
        """Record a phone's geofence report, keeping the last confirmed home.

        ``last_home`` only ever moves forward on a home report, so a later away
        does not erase when the person was last actually at home.
        """
        now = time.time()
        self._db.execute(
            "INSERT INTO presence (person, state, updated, last_home) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(person) DO UPDATE SET state = excluded.state, "
            "updated = excluded.updated, "
            "last_home = CASE WHEN excluded.state = 'home' THEN excluded.updated "
            "ELSE presence.last_home END",
            (person, state, now, now if state == "home" else None),
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
