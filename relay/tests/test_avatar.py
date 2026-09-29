"""Person icons: the pure rules, the store's identity, and the HTTP surface."""
from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.avatar import (
    MAX_PHOTO_BYTES,
    SYMBOLS,
    avatar_etag,
    is_jpeg,
    matches_etag,
    normalize_color,
    valid_color,
    valid_symbol,
)
from app.main import create_app
from app.mqtt import MqttBridge, MqttSettings
from app.store import Store

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 4 + b"\xff\xd9"


# -- the pure vocabulary and rules -------------------------------------------

def test_the_symbol_vocabulary_is_exactly_the_twenty_shared_tokens():
    assert {
        "pawprint", "star", "heart", "bolt", "leaf", "moon", "sun", "house",
        "key", "car", "bike", "music", "book", "game", "flower", "tree",
        "wave", "camera", "plane", "cup",
    } == SYMBOLS


@pytest.mark.parametrize("token", sorted(SYMBOLS))
def test_every_shared_token_is_valid(token):
    assert valid_symbol(token)


@pytest.mark.parametrize("token", ["Star", "STAR", "", None, "rocket", "  star", 3])
def test_anything_outside_the_vocabulary_is_refused(token):
    assert not valid_symbol(token)


@pytest.mark.parametrize("value", ["#FF9500", "#ff9500", "#000000", "#ABCDEF", None])
def test_a_hex_colour_or_nothing_is_valid(value):
    assert valid_color(value)


@pytest.mark.parametrize("value", ["#FFF", "FF9500", "#GGGGGG", "red", "#12345", 5])
def test_anything_that_is_not_rrggbb_is_refused(value):
    assert not valid_color(value)


def test_a_colour_is_normalised_to_upper_case():
    assert normalize_color("#ff9500") == "#FF9500"
    assert normalize_color(None) is None
    assert normalize_color("") is None


def test_a_bad_colour_raises_rather_than_being_stored():
    with pytest.raises(ValueError):
        normalize_color("red")


def test_the_etag_is_the_quoted_version():
    assert avatar_etag(0) == '"0"'
    assert avatar_etag(7) == '"7"'
    assert matches_etag('"7"', 7)
    assert matches_etag('"6", "7"', 7)
    assert matches_etag("*", 7)
    assert not matches_etag('"6"', 7)
    assert not matches_etag(None, 7)
    assert not matches_etag('W/"7"', 7)


def test_only_a_real_jpeg_header_passes():
    assert is_jpeg(JPEG)
    assert not is_jpeg(b"GIF89a")
    assert not is_jpeg(b"")
    assert not is_jpeg(b"\xff\xd8")


# -- the store's identity -----------------------------------------------------

def person_store(tmp_path) -> Store:
    store = Store(str(tmp_path))
    store.add_invited("d1", "iPhone", "user", [], [], None, None, None, person="Claes")
    return store


def test_an_avatar_starts_as_a_monogram_at_version_zero(tmp_path):
    store = person_store(tmp_path)
    person = store.person_by_name("Claes")

    assert person["avatar_kind"] == "monogram"
    assert person["avatar_version"] == 0
    assert store.avatar_descriptor(person) == {
        "kind": "monogram", "symbol": None, "color": None, "version": 0,
    }
    assert store.avatar_descriptor(None)["kind"] == "monogram"


def test_every_avatar_change_bumps_the_version(tmp_path):
    store = person_store(tmp_path)

    assert store.set_avatar("Claes", kind="symbol", symbol="star", color="#FF9500") == 1
    assert store.set_avatar("Claes", kind="symbol", symbol="heart", color=None) == 2
    assert store.set_avatar_photo("Claes", JPEG) == 3
    assert store.clear_avatar("Claes") == 4

    descriptor = store.avatar_descriptor(store.person_by_name("Claes"))
    assert descriptor == {"kind": "monogram", "symbol": None, "color": None, "version": 4}


def test_a_symbol_keeps_its_token_and_colour(tmp_path):
    store = person_store(tmp_path)
    store.set_avatar("Claes", kind="symbol", symbol="leaf", color="#00FF00")

    assert store.avatar_descriptor(store.person_by_name("Claes")) == {
        "kind": "symbol", "symbol": "leaf", "color": "#00FF00", "version": 1,
    }


def test_the_person_id_survives_a_rename(tmp_path):
    store = person_store(tmp_path)
    before = store.person_by_name("Claes")["id"]

    store.rename_person("Claes", "John Appleseed")

    after = store.person_by_name("John Appleseed")
    assert after is not None
    assert after["id"] == before
    assert store.person_by_name("Claes") is None


def test_a_rename_does_not_detach_the_photo(tmp_path):
    store = person_store(tmp_path)
    store.set_avatar_photo("Claes", JPEG)
    original = store.avatar_file(store.person_by_name("Claes"))

    store.rename_person("Claes", "John Appleseed")

    moved = store.person_by_name("John Appleseed")
    assert store.avatar_file(moved) == original
    assert store.avatar_descriptor(moved)["kind"] == "photo"


def test_re_registering_a_phone_with_a_new_name_moves_the_identity(tmp_path):
    """A rename is the same device row carrying a new name (the bridge's rule)."""
    store = Store(str(tmp_path))
    store.add_device("d1", "iPhone")
    store.set_apns("d1", "", "Claes", {})
    before = store.person_by_name("Claes")["id"]

    store.set_apns("d1", "", "John Appleseed", {})

    assert store.person_by_name("Claes") is None
    assert store.person_by_name("John Appleseed")["id"] == before
    assert store.device("d1")["person"] == "John Appleseed"


def test_a_phone_leaving_a_multi_device_person_gets_a_fresh_identity(tmp_path):
    store = Store(str(tmp_path))
    store.add_invited("a", "iPhone", "user", [], [], None, None, None, person="Elsa")
    store.add_invited("b", "iPad", "user", [], [], None, None, None, person="Elsa")
    elsa = store.person_by_name("Elsa")["id"]

    # One of Elsa's phones registers under a different name: Elsa keeps her
    # identity, and the phone that left is a new person.
    store.set_apns("b", "", "Bo", {})

    assert store.person_by_name("Elsa")["id"] == elsa
    assert store.person_by_name("Bo")["id"] != elsa
    assert store.device("a")["person"] == "Elsa"
    assert store.device("b")["person"] == "Bo"


def test_the_people_list_carries_the_id_and_avatar(tmp_path):
    store = person_store(tmp_path)
    store.set_avatar("Claes", kind="symbol", symbol="cup", color="#123ABC")

    group = store.people()[0]
    assert group["name"] == "Claes"
    assert group["id"] == store.person_by_name("Claes")["id"]
    assert group["avatar"] == {
        "kind": "symbol", "symbol": "cup", "color": "#123ABC", "version": 1,
    }


def test_the_photo_is_named_by_id_and_deleted_with_the_person(tmp_path):
    store = person_store(tmp_path)
    store.set_avatar_photo("Claes", JPEG)
    path = store.avatar_file(store.person_by_name("Claes"))

    assert path.endswith(f"{store.person_by_name('Claes')['id']}.jpg")
    assert path.startswith(str(tmp_path / "avatars"))
    with open(path, "rb") as fh:
        assert fh.read() == JPEG

    store.remove_device("d1")

    assert store.person_by_name("Claes") is None
    assert not os.path.exists(path)


def test_a_monogram_or_symbol_removes_the_photo_file(tmp_path):
    store = person_store(tmp_path)
    store.set_avatar_photo("Claes", JPEG)
    path = store.avatar_file(store.person_by_name("Claes"))
    assert os.path.exists(path)

    store.set_avatar("Claes", kind="monogram")

    assert not os.path.exists(path)


def test_migration_backfills_an_id_for_an_existing_name(tmp_path):
    db = tmp_path / "hemnyckel.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "person TEXT, created REAL NOT NULL)"
    )
    con.execute("INSERT INTO devices (id, name, person, created) "
                "VALUES ('old', 'Old phone', 'Claes', 1.0)")
    con.commit()
    con.close()

    store = Store(str(tmp_path))

    person = store.person_by_name("Claes")
    assert person is not None
    assert person["id"]
    assert store.people()[0]["id"] == person["id"]


# -- the HTTP surface ---------------------------------------------------------

def pair(client: TestClient, hmk, name: str) -> dict:
    token = client.post("/api/pair",
                        json={"code": hmk.pair_code, "name": name}).json()["device_token"]
    return {"Authorization": f"Bearer {token}"}


def register(client: TestClient, auth: dict, person: str) -> None:
    client.post("/api/register", headers=auth, json={"apns_token": "", "person": person})


def test_the_people_endpoint_is_readable_by_any_paired_device(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        register(client, owner, "Elise")

        people = client.get("/api/people", headers=owner).json()["people"]
        elise = next(p for p in people if p["name"] == "Elise")
        assert elise["id"]
        assert elise["avatar"] == {
            "kind": "monogram", "symbol": None, "color": None, "version": 0,
        }
        assert [d["name"] for d in elise["devices"]] == ["Owner"]


def test_a_person_may_set_their_own_symbol_by_id_or_by_name(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "iPhone")
        register(client, owner, "Elise")
        elise = next(p for p in client.get("/api/people", headers=owner).json()["people"]
                     if p["name"] == "Elise")

        body = client.put(f"/api/people/{elise['id']}/avatar", headers=owner,
                          json={"kind": "symbol", "symbol": "star", "color": "#ff9500"}).json()
        assert body["id"] == elise["id"]
        assert body["avatar"] == {
            "kind": "symbol", "symbol": "star", "color": "#FF9500", "version": 1,
        }

        # The name still works in place of the id.
        again = client.put("/api/people/Elise/avatar", headers=owner,
                           json={"kind": "monogram"}).json()
        assert again["avatar"] == {
            "kind": "monogram", "symbol": None, "color": None, "version": 2,
        }


def test_only_an_owner_may_change_someone_elses_icon(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Elise iPhone")
        user = pair(client, hmk, "Bo iPhone")
        register(client, owner, "Elise")
        register(client, user, "Bo")

        # Bo (a user) may change his own...
        assert client.put("/api/people/Bo/avatar", headers=user,
                          json={"kind": "symbol", "symbol": "car"}).status_code == 200
        # ...but not Elise's.
        assert client.put("/api/people/Elise/avatar", headers=user,
                          json={"kind": "monogram"}).status_code == 403
        # The owner may change anyone's.
        assert client.put("/api/people/Bo/avatar", headers=owner,
                          json={"kind": "symbol", "symbol": "cup"}).status_code == 200


def test_a_guest_sees_no_family(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        invite = client.post("/api/invites", headers=owner, json={
            "name": "Städ", "role": "guest", "doors": ["front"],
            "expires_at": time.time() + 3600,
        }).json()
        guest = {"Authorization": "Bearer " + client.post(
            "/api/pair", json={"code": invite["code"], "name": "Städ"}).json()["device_token"]}

        assert client.get("/api/people", headers=guest).json() == {"people": []}
        # A guest may still set their own icon.
        assert client.put("/api/people/Städ/avatar", headers=guest,
                          json={"kind": "symbol", "symbol": "leaf"}).status_code == 200


def test_the_avatar_request_validation(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        register(client, owner, "Elise")

        assert client.put("/api/people/Elise/avatar", headers=owner,
                          json={"kind": "symbol", "symbol": "rocket"}).status_code == 400
        assert client.put("/api/people/Elise/avatar", headers=owner,
                          json={"kind": "symbol", "symbol": "star",
                                "color": "orange"}).status_code == 400
        assert client.put("/api/people/Elise/avatar", headers=owner,
                          json={"kind": "photo"}).status_code == 400
        assert client.put("/api/people/Elise/avatar", headers=owner,
                          json={"kind": "nonsense"}).status_code == 400
        assert client.put("/api/people/Nobody/avatar", headers=owner,
                          json={"kind": "monogram"}).status_code == 404


def test_a_photo_round_trips_with_an_etag_and_a_304(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        register(client, owner, "Elise")
        person = next(p for p in client.get("/api/people", headers=owner).json()["people"]
                      if p["name"] == "Elise")

        # A symbol has no bytes to serve.
        client.put("/api/people/Elise/avatar", headers=owner,
                   json={"kind": "symbol", "symbol": "star"})
        assert client.get(f"/api/people/{person['id']}/avatar",
                          headers=owner).status_code == 404

        posted = client.post("/api/people/Elise/avatar/photo",
                             headers={**owner, "Content-Type": "image/jpeg"}, content=JPEG)
        assert posted.status_code == 200
        assert posted.json()["avatar"]["kind"] == "photo"

        got = client.get(f"/api/people/{person['id']}/avatar", headers=owner)
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/jpeg"
        assert got.headers["etag"] == '"2"'
        assert got.content == JPEG

        cached = client.get(f"/api/people/{person['id']}/avatar",
                            headers={**owner, "If-None-Match": got.headers["etag"]})
        assert cached.status_code == 304
        assert cached.content == b""

        # The name resolves too.
        assert client.get("/api/people/Elise/avatar", headers=owner).status_code == 200


def test_a_photo_over_512_kb_is_rejected(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        register(client, owner, "Elise")

        too_big = b"\xff\xd8\xff" + b"a" * MAX_PHOTO_BYTES
        assert len(too_big) > MAX_PHOTO_BYTES
        response = client.post("/api/people/Elise/avatar/photo",
                               headers={**owner, "Content-Type": "image/jpeg"},
                               content=too_big)
        assert response.status_code == 413
        # Nothing was stored: the kind is untouched and no file exists.
        assert app.state.hmk.store.person_by_name("Elise")["avatar_kind"] == "monogram"


def test_a_non_jpeg_body_is_rejected(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        register(client, owner, "Elise")

        response = client.post("/api/people/Elise/avatar/photo",
                               headers={**owner, "Content-Type": "image/jpeg"},
                               content=b"not a jpeg at all")
        assert response.status_code == 415


def test_delete_returns_to_the_monogram_and_removes_the_photo(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "Owner")
        register(client, owner, "Elise")
        client.post("/api/people/Elise/avatar/photo",
                    headers={**owner, "Content-Type": "image/jpeg"}, content=JPEG)

        body = client.delete("/api/people/Elise/avatar", headers=owner).json()
        assert body["avatar"] == {
            "kind": "monogram", "symbol": None, "color": None, "version": 2,
        }
        assert client.get("/api/people/Elise/avatar", headers=owner).status_code == 404


def test_a_rename_keeps_the_id_over_the_api(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        owner = pair(client, hmk, "iPhone")
        register(client, owner, "Claes")
        before = next(p for p in client.get("/api/people", headers=owner).json()["people"]
                      if p["name"] == "Claes")["id"]

        register(client, owner, "John Appleseed")

        people = client.get("/api/people", headers=owner).json()["people"]
        assert not any(p["name"] == "Claes" for p in people)
        assert next(p for p in people if p["name"] == "John Appleseed")["id"] == before


def test_the_projection_carries_the_identity_and_avatar(cfg, tmp_path):
    store = Store(str(tmp_path / "data"))
    published: list = []
    bridge = MqttBridge(
        cfg, store, version="test",
        settings=MqttSettings("broker.local", 1883, "u", "p", False),
        publish=lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    store.add_invited("d1", "iPhone", "user", [], [], None, None, None, person="Elise")
    store.set_avatar("Elise", kind="symbol", symbol="heart", color="#FF0000")

    bridge.refresh("Elise")

    document = json.loads(next(p for t, p, _ in published
                               if t == "hemnyckel/people/elise/state"))
    assert document["id"] == store.person_by_name("Elise")["id"]
    assert document["avatar_kind"] == "symbol"
    assert document["avatar_version"] == 1
