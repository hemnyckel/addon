from __future__ import annotations

from fastapi.testclient import TestClient

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
