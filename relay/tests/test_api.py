from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Door
from app.main import create_app


def test_http_surface(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk

        # Health is open, at the root (for probes) and under /api.
        for path in ("/health", "/api/health"):
            health = client.get(path)
            assert health.status_code == 200
            assert health.json()["apns"] is True  # the test config has a key

        # Everything else needs a paired device, and lives under /api.
        assert client.get("/api/state").status_code == 401

        hmk.store.add_device("dev1", "Claes' iPhone")
        auth = {"Authorization": "Bearer dev1"}

        # Register with a sandbox device (a debug build).
        registered = client.post(
            "/api/register",
            headers=auth,
            json={"apns_token": "abc", "person": "claes", "apns_env": "sandbox", "prefs": {}},
        )
        assert registered.json() == {"ok": True}
        assert hmk.store.device("dev1")["apns_env"] == "development"

        state = client.get("/api/state", headers=auth)
        assert state.status_code == 200
        body = state.json()
        assert body["relay"] == {"online": True, "apns": True}
        assert [d["id"] for d in body["doors"]] == ["front"]

        # Live Activities: push-to-start token, per-activity token, and cleanup.
        assert client.post("/api/live/start-token", headers=auth,
                           json={"apns_token": "start"}).json() == {"ok": True}
        assert hmk.store.device("dev1")["live_start_token"] == "start"
        assert client.post("/api/live/activity", headers=auth,
                           json={"door": "front", "apns_token": "act"}).json() == {"ok": True}
        assert hmk.store.live_activities("front")[0]["token"] == "act"
        assert client.post("/api/live/activity", headers=auth,
                           json={"door": "back", "apns_token": "x"}).status_code == 400
        assert client.delete("/api/live/activity?door=front", headers=auth).json() == {"ok": True}
        assert hmk.store.live_activities("front") == []


def test_pair_with_a_bad_code_is_rejected(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.post("/api/pair", json={"code": "NOPE", "name": "x"}).status_code == 401


def test_pairing_is_rate_limited(cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_PAIR_MAX_ATTEMPTS", 3)
    app = create_app(cfg)
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/api/pair", json={"code": "NOPE", "name": "x"}).status_code == 401
        assert client.post("/api/pair", json={"code": "NOPE", "name": "x"}).status_code == 429


def test_an_owner_can_mint_a_pairing_code(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = client.post("/api/pair",
                            json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]
        user = client.post("/api/pair",
                           json={"code": hmk.pair_code, "name": "User"}).json()["device_token"]

        # Only an owner may mint a code.
        assert client.post("/api/pair-code",
                           headers={"Authorization": f"Bearer {user}"}).status_code == 403

        minted = client.post("/api/pair-code", headers={"Authorization": f"Bearer {owner}"}).json()
        assert minted["expires_in"] == 600

        # …and it really pairs the next device.
        assert client.post("/api/pair",
                           json={"code": minted["code"], "name": "iPad"}).status_code == 200


def test_the_first_device_is_the_owner_and_manages_people(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk

        first = client.post("/api/pair",
                            json={"code": hmk.pair_code, "name": "First"}).json()["device_token"]
        second = client.post("/api/pair",
                             json={"code": hmk.pair_code, "name": "Second"}).json()["device_token"]
        assert hmk.store.device(first)["role"] == "owner"
        assert hmk.store.device(second)["role"] == "user"

        owner = {"Authorization": f"Bearer {first}"}
        user = {"Authorization": f"Bearer {second}"}

        # The caller's role comes with the state, so the app can adapt.
        assert client.get("/api/state", headers=owner).json()["role"] == "owner"
        assert client.get("/api/state", headers=user).json()["role"] == "user"

        # Only an owner may see the family.
        assert client.get("/api/devices", headers=user).status_code == 403
        names = {d["name"] for d in client.get("/api/devices", headers=owner).json()["devices"]}
        assert names == {"First", "Second"}

        # The owner promotes the second device...
        assert client.post(f"/api/devices/{second}/role", headers=owner,
                           json={"role": "owner"}).json() == {"ok": True}
        assert hmk.store.device(second)["role"] == "owner"

        # ...then the first may step down, leaving a single owner...
        assert client.post(f"/api/devices/{first}/role", headers=user,
                           json={"role": "user"}).json() == {"ok": True}

        # ...and the last owner cannot be demoted.
        assert client.post(f"/api/devices/{second}/role", headers=user,
                           json={"role": "user"}).status_code == 409

        # An unknown role is rejected.
        assert client.post(f"/api/devices/{second}/role", headers=user,
                           json={"role": "king"}).status_code == 400


def test_a_person_registers_even_without_a_push_token(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        hmk.store.add_device("dev1", "iPhone")
        auth = {"Authorization": "Bearer dev1"}

        # No push (a simulator): the relay still learns who this device is, so it
        # can attribute app-initiated lock/unlock.
        assert client.post("/api/register", headers=auth,
                           json={"apns_token": "", "person": "claes"}).json() == {"ok": True}

        device = hmk.store.device("dev1")
        assert device["person"] == "claes"
        assert not device["apns_token"]


# -- guests: a role, a window and chosen doors -------------------------------

def two_door_cfg(cfg):
    cfg.doors.append(Door(id="back", name="Källardörren", lock_entity="lock.back"))
    return cfg


def test_a_phone_reports_presence_and_the_owner_sets_home(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}

        # Without a person the report is ignored, not an error.
        assert client.post("/api/presence", headers=owner,
                           json={"state": "home"}).json() == {"ok": True, "ignored": "no person"}

        client.post("/api/register", headers=owner, json={"apns_token": "", "person": "Claes"})
        assert client.post("/api/presence", headers=owner,
                           json={"state": "away"}).json() == {"ok": True}
        assert hmk.store.presence().get("Claes", {}).get("state") == "away"
        assert client.post("/api/presence", headers=owner,
                           json={"state": "maybe"}).status_code == 400

        # The owner sets home; every device gets it with its state.
        assert client.post("/api/settings/home", headers=owner,
                           json={"lat": 59.33, "lon": 18.06, "radius": 150}
                           ).json() == {"ok": True, "radius": 150}
        assert client.get("/api/state", headers=owner).json()["home"] == {
            "lat": 59.33, "lon": 18.06, "radius": 150,
        }


def test_only_the_owner_sets_home(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "O"}).json()["device_token"]}
        user = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "U"}).json()["device_token"]}

        assert client.post("/api/settings/home", headers=user,
                           json={"lat": 1, "lon": 2}).status_code == 403
        assert client.post("/api/settings/home", headers=owner,
                           json={"lat": 999, "lon": 2}).status_code == 400


def test_a_family_member_invitation_makes_a_user(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}

        invite = client.post("/api/invites", headers=owner, json={
            "name": "Elsa", "role": "user", "expires_at": time.time() + 3600,
        }).json()
        assert invite["role"] == "user"

        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "Elsas iPhone"}).json()["device_token"]
        row = hmk.store.device(token)
        assert row["role"] == "user"
        assert row["name"] == "Elsas iPhone"  # the phone names itself
        assert row["person"] == "Elsa"        # the invitation names the person
        assert not row["expires"]             # permanent
        assert client.get("/api/state",
                          headers={"Authorization": f"Bearer {token}"}).json()["role"] == "user"


def test_a_guest_schedule_is_enforced(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}

        today = time.localtime().tm_wday + 1          # ISO weekday
        other = 1 if today != 1 else 2
        invite = client.post("/api/invites", headers=owner, json={
            "name": "Städ", "role": "guest", "doors": ["front"],
            "days": [other], "expires_at": time.time() + 3600,
        }).json()
        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "Städ"}).json()["device_token"]
        assert hmk.store.device(token)["days"] == f"[{other}]"

        # Today is not an allowed day. The guest can read their state (so the app
        # can explain), but acting is refused.
        auth = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/state", headers=auth).status_code == 200
        assert client.get("/api/state", headers=auth).json()["schedule"]["days"] == [other]
        refused = client.post("/api/action", headers=auth,
                              json={"door": "front", "action": "unlock"})
        assert refused.status_code == 403
        assert refused.json()["detail"] == "outside the guest's hours"


def test_a_guest_time_window_is_enforced(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}

        later = datetime.now() + timedelta(hours=3)
        invite = client.post("/api/invites", headers=owner, json={
            "name": "Städ", "role": "guest", "doors": ["front"],
            "from_time": later.strftime("%H:00"), "to_time": later.strftime("%H:01"),
            "expires_at": time.time() + 3600,
        }).json()
        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "Städ"}).json()["device_token"]

        refused = client.post("/api/action", headers={"Authorization": f"Bearer {token}"},
                              json={"door": "front", "action": "unlock"})
        assert refused.status_code == 403
        assert refused.json()["detail"] == "outside the guest's hours"


def test_people_group_devices_and_set_a_role_once(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Pappas iPhone"}).json()["device_token"]}
        client.post("/api/register", headers=owner, json={"apns_token": "", "person": "Pappa"})

        # Two devices for Elsa, from two nameless invitations.
        for name in ("Elsas iPhone", "Elsas iPad"):
            invite = client.post("/api/invites", headers=owner,
                                 json={"role": "user", "expires_at": time.time() + 3600}).json()
            token = client.post("/api/pair",
                                json={"code": invite["code"], "name": name}).json()["device_token"]
            client.post("/api/register", headers={"Authorization": f"Bearer {token}"},
                        json={"apns_token": "", "person": "Elsa"})

        people = client.get("/api/people", headers=owner).json()["people"]
        elsa = next(p for p in people if p["name"] == "Elsa")
        assert [d["name"] for d in elsa["devices"]] == ["Elsas iPhone", "Elsas iPad"]
        assert elsa["role"] == "user"

        # The role is set once for the person, and the last owner is protected.
        assert client.post("/api/people/Pappa/role", headers=owner,
                           json={"role": "user"}).status_code == 409

        assert client.post("/api/people/Elsa/role", headers=owner,
                           json={"role": "owner"}).json() == {"ok": True}
        assert all(hmk.store.device(d["id"])["role"] == "owner" for d in elsa["devices"])

        # With two owners, one may step down.
        assert client.post("/api/people/Pappa/role", headers=owner,
                           json={"role": "user"}).json() == {"ok": True}


def test_re_pairing_the_same_phone_replaces_its_old_row(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Pappas iPhone"}).json()["device_token"]}
        device = {"model": "iPhone", "os": "iOS 26.0"}
        client.post("/api/register", headers=owner,
                    json={"apns_token": "", "person": "Pappa", "device": device})

        # The same phone pairs again (a reinstall, a re-connect).
        invite = client.post("/api/invites", headers=owner,
                             json={"role": "user", "expires_at": time.time() + 3600}).json()
        again = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": invite["code"], "name": "Pappas iPhone"}).json()["device_token"]}
        client.post("/api/register", headers=again,
                    json={"apns_token": "", "person": "Pappa", "device": device})

        people = client.get("/api/people", headers=again).json()["people"]
        pappa = next(p for p in people if p["name"] == "Pappa")
        assert len(pappa["devices"]) == 1                      # replaced, not added
        assert pappa["role"] == "owner"                        # the role survived
        assert pappa["devices"][0]["id"] == again["Authorization"].removeprefix("Bearer ")
        # …and the replaced device's token is gone with it.
        assert client.get("/api/state", headers=owner).status_code == 401


def test_a_nameless_family_invitation_is_fine(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}

        invite = client.post("/api/invites", headers=owner, json={
            "role": "user", "expires_at": time.time() + 3600,
        }).json()

        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "Elsas iPhone"}).json()["device_token"]
        row = hmk.store.device(token)
        assert row["role"] == "user"
        assert row["name"] == "Elsas iPhone"   # the phone names itself
        assert not row["person"]               # she sets her own name later


def test_the_device_identifies_itself_without_wiping_a_person(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}
        invite = client.post("/api/invites", headers=owner, json={
            "name": "Elsa", "role": "user", "expires_at": time.time() + 3600,
        }).json()
        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "x"}).json()["device_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # An empty person on register must not wipe the one the invitation set…
        client.post("/api/register", headers=auth, json={
            "apns_token": "", "person": "",
            "device": {"model": "iPhone", "os": "iOS 26.0"},
        })
        row = hmk.store.device(token)
        assert row["person"] == "Elsa"
        # …and the model was stored (nobody typed it).
        assert row["device_model"] == "iPhone"
        assert row["device_os"] == "iOS 26.0"

        mine = next(d for d in client.get("/api/devices", headers=owner).json()["devices"]
                    if d["id"] == token)
        assert mine["device_model"] == "iPhone"

        # The relay lists everyone it knows, for the app's pickers.
        assert "Elsa" in client.get("/api/state", headers=auth).json()["people"]


def test_a_guest_invitation_restricts_the_device(cfg):
    app = create_app(two_door_cfg(cfg))
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}

        invite = client.post("/api/invites", headers=owner, json={
            "name": "Städning", "role": "guest", "doors": ["front"],
            "expires_at": time.time() + 3600,
        }).json()
        assert invite["doors"] == ["front"]

        guest = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": invite["code"], "name": "Städ"}).json()["device_token"]}
        row = hmk.store.device(guest["Authorization"].removeprefix("Bearer "))
        assert row["role"] == "guest"
        assert row["doors"] == '["front"]'
        assert row["expires"] > time.time()

        # No history, no presence, and only their door.
        assert client.get("/api/events", headers=guest).json() == {"events": []}
        state = client.get("/api/state", headers=guest).json()
        assert [d["id"] for d in state["doors"]] == ["front"]
        assert state["presence"] == {}
        assert state["role"] == "guest"

        # They may act on their door, never on another.
        assert client.post("/api/action", headers=guest,
                           json={"door": "front", "action": "unlock"}).status_code == 200
        assert client.post("/api/action", headers=guest,
                           json={"door": "back", "action": "unlock"}).status_code == 403

        # The invitation is single use.
        assert client.post("/api/pair", json={"code": invite["code"], "name": "igen"}).status_code == 401


def test_an_invitation_needs_a_door_and_an_owner(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}
        user = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "User"}).json()["device_token"]}

        assert client.post("/api/invites", headers=user,
                           json={"name": "x", "doors": ["front"]}).status_code == 403
        assert client.post("/api/invites", headers=owner,
                           json={"name": "x", "doors": []}).status_code == 400


def test_an_expired_guest_is_refused(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]}
        invite = client.post("/api/invites", headers=owner, json={
            "name": "Gäst", "role": "guest", "doors": ["front"],
            "expires_at": time.time() + 600,
        }).json()
        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "G"}).json()["device_token"]

        hmk.store._db.execute("UPDATE devices SET expires = ? WHERE id = ?",
                              (time.time() - 1, token))
        hmk.store._db.commit()

        assert client.get("/api/state",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_the_owner_can_revoke_a_device(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner_token = client.post("/api/pair",
                                  json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]
        user_token = client.post("/api/pair",
                                 json={"code": hmk.pair_code, "name": "User"}).json()["device_token"]
        owner = {"Authorization": f"Bearer {owner_token}"}
        user = {"Authorization": f"Bearer {user_token}"}

        assert client.delete(f"/api/devices/{user_token}", headers=owner).json() == {"ok": True}
        assert client.get("/api/state", headers=user).status_code == 401
        # An owner cannot remove their own device, nor the last owner.
        assert client.delete(f"/api/devices/{owner_token}", headers=owner).status_code == 409


# -- one guest identity: the invitation and the lock code together -----------

class FakeHa:
    """A stand-in for HaClient: canned states and recorded service calls."""

    def __init__(self, states: list[dict] | None = None, response=None,
                 ok: bool = True) -> None:
        self._states = list(states or [])
        self.calls: list[tuple[str, str, dict]] = []
        self.ok = ok
        self.response = response
        self.connected = False

    async def run(self) -> None:  # pragma: no cover - parity with HaClient
        pass

    async def states(self) -> list[dict]:
        return self._states

    async def entity_state(self, entity_id: str) -> str | None:
        return None

    async def call_service(self, domain: str, service: str, data: dict) -> bool:
        self.calls.append((domain, service, data))
        return self.ok

    async def call_service_result(self, domain: str, service: str, data: dict):
        self.calls.append((domain, service, data))
        return self.ok, self.response


def guest_sensor_states(*doors: tuple[str, str, str]) -> list[dict]:
    """A slots sensor per door: the lock's name plus the live entry id."""
    return [
        {
            "entity_id": f"sensor.{door_id}_slots",
            "state": "0 occupied",
            "attributes": {"lock": name, "entry_id": entry, "slots": []},
        }
        for door_id, name, entry in doors
    ]


def guest_client(cfg, fake: FakeHa, *, two_doors: bool = False):
    if two_doors:
        cfg.doors.append(Door(id="back", name="Källardörren", lock_entity="lock.back"))
    app = create_app(cfg)
    app.state.hmk.ha = fake
    return app


def pair_owner(client: TestClient, hmk) -> dict:
    token = client.post("/api/pair",
                        json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]
    return {"Authorization": f"Bearer {token}"}


def guest_invite(client: TestClient, owner: dict, **overrides) -> dict:
    body = {"name": "Städ", "role": "guest", "doors": ["front"],
            "expires_at": time.time() + 7 * 86400}
    body.update(overrides)
    return client.post("/api/invites", headers=owner, json=body).json()


def test_a_guest_invitation_writes_a_code_on_each_chosen_door(cfg):
    fake = FakeHa(
        states=guest_sensor_states(("front", "Ytterdörren", "ent-front"),
                                   ("back", "Källardörren", "ent-back")),
        response={
            "ent-front": {"slot": 6, "code": "111111", "name": "Städ"},
            "ent-back": {"slot": 7, "code": "222222", "name": "Städ"},
        },
    )
    app = guest_client(cfg, fake, two_doors=True)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)
        invite = guest_invite(client, owner, doors=["front", "back"],
                              days=[1, 3], from_time="08:00", to_time="17:00")

        # Every chosen door gets a recurring guest, with the invite's window
        # mapped to the integration's schedule shape (ISO weekday -> day code).
        assert [(c[1], c[2]["entry_id"]) for c in fake.calls] == [
            ("create_recurring_guest", "ent-front"),
            ("create_recurring_guest", "ent-back"),
        ]
        assert fake.calls[0][2]["name"] == "Städ"
        assert fake.calls[0][2]["schedule"] == [
            {"days": ["mon", "wed"], "start": "08:00", "end": "17:00"}
        ]

        # The codes come back once, each with its door…
        assert invite["guest_codes"] == [
            {"door": "front", "door_name": "Ytterdörren", "slot": 6,
             "code": "111111", "until": None},
            {"door": "back", "door_name": "Källardörren", "slot": 7,
             "code": "222222", "until": None},
        ]
        # …and only the slots are stored, never a code.
        stored = dict(app.state.hmk.store.invite(invite["code"]))
        assert json.loads(stored["slots"]) == {"front": 6, "back": 7}
        assert "111111" not in json.dumps(stored)
        assert "222222" not in json.dumps(stored)


def test_a_guest_invitation_without_weekdays_writes_a_simple_code(cfg):
    fake = FakeHa(
        states=guest_sensor_states(("front", "Ytterdörren", "ent-front")),
        response={"ent-front": {"slot": 5, "code": "123456", "name": "Städ",
                                "until": "2026-10-01T12:00:00+00:00"}},
    )
    app = guest_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)
        expires = time.time() + 86400
        invite = guest_invite(client, owner, expires_at=expires)

        service, data = fake.calls[-1][1], fake.calls[-1][2]
        assert service == "create_guest_code"
        assert data["until"] == datetime.fromtimestamp(expires, tz=UTC).isoformat()
        assert data["entry_id"] == "ent-front"
        assert invite["guest_codes"][0]["code"] == "123456"


def test_a_door_whose_lock_is_unreachable_does_not_break_the_invitation(cfg):
    fake = FakeHa(states=guest_sensor_states(("front", "Ytterdörren", "ent-front")), ok=False)
    app = guest_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)
        response = client.post("/api/invites", headers=owner, json={
            "name": "Städ", "role": "guest", "doors": ["front"],
            "expires_at": time.time() + 3600,
        })

        assert response.status_code == 200
        assert response.json()["guest_codes"] == []
        assert app.state.hmk.store.invite(response.json()["code"])["slots"] is None


def test_only_an_owner_creates_a_guest_and_nothing_reaches_the_lock(cfg):
    fake = FakeHa(states=guest_sensor_states(("front", "Ytterdörren", "ent-front")))
    app = guest_client(cfg, fake)
    with TestClient(app) as client:
        hmk = app.state.hmk
        pair_owner(client, hmk)
        user = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": hmk.pair_code, "name": "User"}).json()["device_token"]}

        assert client.post("/api/invites", headers=user, json={
            "name": "Städ", "role": "guest", "doors": ["front"],
            "expires_at": time.time() + 3600,
        }).status_code == 403
        assert fake.calls == []


def test_revoking_a_guest_revokes_the_lock_codes(cfg):
    fake = FakeHa(
        states=guest_sensor_states(("front", "Ytterdörren", "ent-front")),
        response={"ent-front": {"slot": 6, "code": "111111", "name": "Städ"}},
    )
    app = guest_client(cfg, fake)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair_owner(client, hmk)
        invite = guest_invite(client, owner)
        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "Städ"}).json()["device_token"]

        fake.calls.clear()
        assert client.delete(f"/api/devices/{token}", headers=owner).json() == {"ok": True}

        assert ("hemnyckel", "revoke_guest_code",
                {"slot": 6, "entry_id": "ent-front"}) in fake.calls
        # The slots are forgotten with the codes, so nothing is retried.
        assert hmk.store.invite(invite["code"])["slots"] is None
        assert client.get("/api/state",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_an_expired_guest_revokes_its_code_once(cfg):
    fake = FakeHa(
        states=guest_sensor_states(("front", "Ytterdörren", "ent-front")),
        response={"ent-front": {"slot": 6, "code": "111111", "name": "Städ"}},
    )
    app = guest_client(cfg, fake)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair_owner(client, hmk)
        invite = guest_invite(client, owner)
        token = client.post("/api/pair",
                            json={"code": invite["code"], "name": "Städ"}).json()["device_token"]
        hmk.store._db.execute("UPDATE devices SET expires = ? WHERE id = ?",
                              (time.time() - 1, token))
        hmk.store._db.commit()

        auth = {"Authorization": f"Bearer {token}"}
        fake.calls.clear()
        assert client.get("/api/state", headers=auth).status_code == 403
        revokes = [c for c in fake.calls if c[1] == "revoke_guest_code"]
        assert revokes == [("hemnyckel", "revoke_guest_code",
                            {"slot": 6, "entry_id": "ent-front"})]

        # The refusal is honest: it revokes when it refuses, not on every request.
        fake.calls.clear()
        assert client.get("/api/state", headers=auth).status_code == 403
        assert [c for c in fake.calls if c[1] == "revoke_guest_code"] == []
