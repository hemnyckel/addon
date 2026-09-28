from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.config import Door
from app.main import create_app

# The face of the integration's slots sensor: the lock's human name, the live
# entry id a service call must use, and the slot table itself.
SLOT_STATES = [
    {
        "entity_id": "sensor.ytterdorren_slots",
        "state": "2 occupied",
        "attributes": {
            "lock": "Ytterdörren",
            "entry_id": "ent-front",
            "slots": [
                {"slot": 6, "name": "Elise", "has_pin": True,
                 "has_fingerprint": False, "has_rfid": False, "credentials": ["pin"]},
                {"slot": 4, "name": "", "has_pin": False,
                 "has_fingerprint": True, "has_rfid": False, "credentials": ["fingerprint"],
                 "finger_used": False, "finger_state": "claimed",
                 "fingers": [{"label": "left index", "enrolled": "2026-09-28T10:00:00+00:00"}]},
            ],
        },
    },
    {
        "entity_id": "sensor.ytterdorren_lock_facts",
        "state": "50 PIN / 50 RFID / 100 total",
        "attributes": {
            "lock": "Ytterdörren", "entry_id": "ent-front",
            "total_users": 100, "pin_users": 50, "rfid_users": 50,
        },
    },
]


class FakeHa:
    """A stand-in for HaClient: canned states and recorded service calls."""

    def __init__(self, states: list[dict] | None = None) -> None:
        self._states = list(states or [])
        self.calls: list[tuple[str, str, dict]] = []
        self.ok = True
        self.response = None
        # A specific refusal status (e.g. 400 for a request Home Assistant will
        # not take); None means success/failure follows ``ok``.
        self.status: int | None = None
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

    async def call_service_status(self, domain: str, service: str, data: dict):
        self.calls.append((domain, service, data))
        if self.status is not None:
            return self.status, self.response
        return (200 if self.ok else 502), self.response


def make_client(cfg, fake: FakeHa, *, two_doors: bool = False):
    """An app with a fake Home Assistant, for the TestClient context manager."""
    if two_doors:
        cfg.doors.append(Door(id="back", name="Källardörren", lock_entity="lock.back"))
    app = create_app(cfg)
    app.state.hmk.ha = fake
    return app


def pair_owner(client: TestClient, hmk) -> dict:
    token = client.post("/api/pair",
                        json={"code": hmk.pair_code, "name": "Owner"}).json()["device_token"]
    return {"Authorization": f"Bearer {token}"}


def test_the_slot_list_resolves_a_door_from_its_slots_sensor(cfg):
    app = make_client(cfg, FakeHa(SLOT_STATES))
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        body = client.get("/api/slots?door=front", headers=owner).json()

        assert body["door"] == "front"
        assert body["name"] == "Ytterdörren"
        assert body["capacity"] == {"pin": 50, "rfid": 50, "total": 100}
        # Sorted by number, both slots present.
        assert [slot["slot"] for slot in body["slots"]] == [4, 6]
        elise = body["slots"][1]
        assert elise["name"] == "Elise"
        assert elise["occupied"] is True
        assert elise["credentials"] == ["pin"]
        assert elise["door"] == "front"
        # A slot with a credential but no name is still occupied.
        assert body["slots"][0]["occupied"] is True
        assert body["slots"][0]["has_fingerprint"] is True
        # The finger labels and their state travel through from the slot table.
        assert body["slots"][0]["fingers"] == [
            {"label": "left index", "enrolled": "2026-09-28T10:00:00+00:00"}
        ]
        assert body["slots"][0]["finger_state"] == "claimed"
        assert body["slots"][0]["finger_used"] is False
        # A slot without a label is empty here, not invented.
        assert body["slots"][1]["fingers"] == []
        assert body["slots"][1]["finger_state"] == ""


def test_a_door_with_no_slots_sensor_is_simply_empty(cfg):
    app = make_client(cfg, FakeHa(SLOT_STATES), two_doors=True)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        body = client.get("/api/slots?door=back", headers=owner).json()

        assert body["slots"] == []
        assert body["capacity"] is None
        # An unknown door is a clean refusal, not a traceback.
        assert client.get("/api/slots?door=nope", headers=owner).status_code == 400


def test_a_slots_sensor_without_a_lock_attribute_is_found_by_name(cfg):
    """The integration names the entity after the lock ("Ytterdörren Slots").

    Even when the sensor carries no explicit ``lock``/``entry_id`` attribute,
    the door resolves by that name and the config's entry id is the fallback.
    """
    cfg.doors[0].entry_id = "ent-front"
    fake = FakeHa([
        {
            "entity_id": "sensor.ytterdorren_slots",
            "state": "1 occupied",
            "attributes": {
                "friendly_name": "Ytterdörren Slots",
                "slots": [
                    {"slot": 5, "name": "Pappa", "has_pin": True,
                     "has_fingerprint": False, "has_rfid": False, "credentials": ["pin"]},
                ],
            },
        },
    ])
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        body = client.get("/api/slots?door=front", headers=owner).json()
        assert [slot["slot"] for slot in body["slots"]] == [5]

        assert client.post("/api/slots/5/name", headers=owner,
                           json={"door": "front", "name": "Pappa"}).json()["ok"] is True
        assert fake.calls[-1][2]["entry_id"] == "ent-front"


def test_a_slot_can_be_named_with_the_live_entry_id(cfg):
    fake = FakeHa(SLOT_STATES)
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        assert client.post("/api/slots/6/name", headers=owner,
                           json={"door": "front", "name": "Elise"}).json() == {
                               "ok": True, "slot": 6, "name": "Elise"}
        # The live entry id comes from the sensor, not the (stale) config hint.
        assert fake.calls[-1] == ("hemnyckel", "set_slot_name",
                                  {"slot": 6, "name": "Elise", "entry_id": "ent-front"})

        # An empty name is refused before Home Assistant is ever called.
        assert client.post("/api/slots/6/name", headers=owner,
                           json={"door": "front", "name": "  "}).status_code == 400


def test_creating_a_code_returns_it_once_and_never_stores_it(cfg):
    fake = FakeHa(SLOT_STATES)
    fake.response = {"ent-front": {"slot": 6, "code": "4821",
                                   "name": "Elise", "until": None}}
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        body = client.post("/api/slots/6/code", headers=owner,
                           json={"door": "front", "name": "Elise"}).json()

        assert body == {"ok": True, "slot": 6, "name": "Elise",
                        "until": None, "code": "4821"}
        # The code lives only in this response: it is not in what we sent, nor
        # anywhere in the relay's own store.
        assert "4821" not in json.dumps(fake.calls[-1][2])
        assert "4821" not in json.dumps(client.get("/api/slots?door=front",
                                                   headers=owner).json())


def test_a_slot_can_be_cleared(cfg):
    fake = FakeHa(SLOT_STATES)
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        assert client.delete("/api/slots/6?door=front", headers=owner).json() == {
            "ok": True, "slot": 6}
        assert fake.calls[-1] == ("hemnyckel", "clear_slot",
                                  {"slot": 6, "entry_id": "ent-front"})


def test_a_finger_enrolment_lights_the_reader(cfg):
    fake = FakeHa(SLOT_STATES)
    fake.response = {"ent-front": {"slot": 6, "name": "Elise", "via": "local"}}
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        assert client.post("/api/slots/6/finger", headers=owner,
                           json={"door": "front"}).json() == {
                               "ok": True, "slot": 6, "finger": None}
        assert fake.calls[-1] == ("hemnyckel", "enroll_fingerprint",
                                  {"slot": 6, "entry_id": "ent-front"})


def test_a_finger_enrolment_carries_the_owners_label(cfg):
    fake = FakeHa(SLOT_STATES)
    fake.response = {"ent-front": {"slot": 6, "name": "Elise", "via": "local"}}
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        body = client.post("/api/slots/6/finger", headers=owner,
                           json={"door": "front", "finger": "left index"}).json()

        assert body == {"ok": True, "slot": 6, "finger": "left index"}
        assert fake.calls[-1] == (
            "hemnyckel", "enroll_fingerprint",
            {"slot": 6, "entry_id": "ent-front", "finger": "left index"},
        )


def test_a_fingerprint_can_be_cleared_for_one_slot(cfg):
    fake = FakeHa(SLOT_STATES)
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        assert client.delete("/api/slots/4/finger?door=front", headers=owner).json() == {
            "ok": True, "slot": 4}
        assert fake.calls[-1] == (
            "hemnyckel", "clear_fingerprint",
            {"slot": 4, "entry_id": "ent-front"},
        )


def test_an_unlabelled_fingerprint_can_be_named_in_place(cfg):
    fake = FakeHa(SLOT_STATES)
    fake.response = {"ent-front": {"slot": 4, "finger": "left index"}}
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        body = client.post("/api/slots/4/label", headers=owner,
                           json={"door": "front", "finger": "Left Index"}).json()

        # The integration names the label (normalised); the relay carries it.
        assert body == {"ok": True, "slot": 4, "finger": "left index"}
        assert fake.calls[-1] == (
            "hemnyckel", "relabel_fingerprint",
            {"slot": 4, "finger": "Left Index", "entry_id": "ent-front"},
        )

        # A nameless label is refused before Home Assistant is ever called.
        assert client.post("/api/slots/4/label", headers=owner,
                           json={"door": "front", "finger": "  "}).status_code == 400


def test_a_refused_label_is_a_bad_request_not_an_outage(cfg):
    """A slot with no fingerprint is the caller's error, not a broken lock."""
    fake = FakeHa(SLOT_STATES)
    fake.status = 400
    fake.response = {"message": "no fingerprint is recorded in slot 6"}
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        response = client.post("/api/slots/6/label", headers=owner,
                               json={"door": "front", "finger": "left index"})

        assert response.status_code == 400
        assert response.json()["detail"] == "no fingerprint is recorded in slot 6"


def test_only_an_owner_may_manage_codes(cfg):
    fake = FakeHa(SLOT_STATES)
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        hmk = app.state.hmk
        hmk.store.add_device("user1", "User")  # a later device is not an owner
        user = {"Authorization": "Bearer user1"}

        assert client.get("/api/slots?door=front", headers=user).status_code == 403
        assert client.post("/api/slots/6/name", headers=user,
                           json={"door": "front", "name": "Elise"}).status_code == 403
        assert client.post("/api/slots/6/code", headers=user,
                           json={"door": "front", "name": "Elise"}).status_code == 403
        assert client.post("/api/slots/6/finger", headers=user,
                           json={"door": "front"}).status_code == 403
        assert client.post("/api/slots/6/label", headers=user,
                           json={"door": "front", "finger": "left index"}).status_code == 403
        assert client.delete("/api/slots/6/finger?door=front",
                             headers=user).status_code == 403
        assert client.delete("/api/slots/6?door=front", headers=user).status_code == 403
        # Nothing ever reached Home Assistant.
        assert fake.calls == []


def test_an_upstream_failure_becomes_a_clean_error(cfg):
    fake = FakeHa(SLOT_STATES)
    fake.ok = False
    app = make_client(cfg, fake)
    with TestClient(app) as client:
        owner = pair_owner(client, app.state.hmk)

        response = client.post("/api/slots/6/name", headers=owner,
                               json={"door": "front", "name": "Elise"})

        assert response.status_code == 502
        assert response.json()["detail"] == "the lock is not reachable right now; try again"

        # The same clean answer when a code could not be written.
        assert client.post("/api/slots/6/code", headers=owner,
                           json={"door": "front", "name": "Elise"}).status_code == 502

        # And when Home Assistant itself fails behind a label, it is an outage
        # (502), not the bad request a refusal would be.
        response = client.post("/api/slots/4/label", headers=owner,
                               json={"door": "front", "finger": "left index"})
        assert response.status_code == 502
        assert response.json()["detail"] == "the lock is not reachable right now; try again"
