from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.apns import PushResult
from app.config import Config, Door
from app.main import create_app

from .test_main import FakeApns, make_state

CLOSED = "binary_sensor.port_stangd"
OPEN_SENSOR = "binary_sensor.port_oppen"
MOVING = "binary_sensor.port_i_rorelse"
CAMERA = "camera.garaget"
JPEG = b"\xff\xd8\xff\xe0not-a-real-jpeg\xff\xd9"


def garage_config(apns_key, tmp_path, **door) -> Config:
    path, _ = apns_key
    return Config(
        apns_key_path=path,
        apns_key_id="ABC123DEFG",
        apns_team_id="TEAM123456",
        data_dir=str(tmp_path / "data"),
        open_alert_minutes=10,
        doors=[
            Door(
                id="garage",
                name="Garageport",
                kind="garage",
                closed_sensor=CLOSED,
                open_sensor=OPEN_SENSOR,
                moving_sensor=MOVING,
                open_button="button.oppna",
                snapshot_entity=CAMERA,
                **door,
            )
        ],
    )


@pytest.fixture
def garage_cfg(apns_key, tmp_path) -> Config:
    return garage_config(apns_key, tmp_path)


class FakeHa:
    """The two reads this feature needs: a sensor, and the camera's still."""

    def __init__(self, *, open_state: str = "on", image: bytes | None = JPEG) -> None:
        self.open_state = open_state
        self.image = image
        self.lookups = 0

    async def run(self) -> None:  # pragma: no cover - parity with HaClient
        return

    async def entity_state(self, entity_id: str) -> str | None:
        if entity_id == OPEN_SENSOR:
            self.lookups += 1
            return self.open_state
        if entity_id == CLOSED:
            return "off" if self.open_state == "on" else "on"
        if entity_id == MOVING:
            return "off"
        return None

    async def camera_image(self, entity_id: str) -> bytes | None:
        return self.image if entity_id == CAMERA else None

    async def call_service(self, *_args, **_kwargs) -> bool:
        return True


def make(cfg, ha: FakeHa):
    """A relay with one owner's phone paired for push."""
    state = make_state(cfg)
    state.ha = ha  # type: ignore[assignment]
    state.apns = FakeApns(PushResult(ok=True, status=200))
    state.store.add_device("d1", "Claes' iPhone", role="owner")
    state.store.set_apns("d1", "aabbccdd" * 8, "Claes", {}, "development")
    return state, state.store.device("d1")


def tick(state, at: float):
    asyncio.run(state._open_alert_tick(at))


def test_no_reminder_before_the_limit(garage_cfg):
    state, _ = make(garage_cfg, FakeHa())

    tick(state, 1_000.0)          # porten är öppen — klockan startar
    tick(state, 1_000.0 + 9 * 60)

    assert state.apns.sent == []


def test_a_reminder_after_ten_minutes(garage_cfg):
    state, _ = make(garage_cfg, FakeHa())

    tick(state, 1_000.0)
    tick(state, 1_000.0 + 10 * 60)

    assert len(state.apns.sent) == 1
    payload = state.apns.sent[0]["payload"]
    assert payload["aps"]["alert"]["title"] == "Garageport"
    assert "10 min" in payload["aps"]["alert"]["subtitle"]
    assert payload["door"] == "garage"
    assert payload["garage"] == {"door": "garage", "door_name": "Garageport",
                                 "minutes": 10}
    # Bilden hämtas av appens notistjänst, inte av reläet
    assert payload["snapshot"] == "/api/snapshot/garage"
    assert payload["aps"]["mutable-content"] == 1


def test_only_one_reminder_per_opening(garage_cfg):
    state, _ = make(garage_cfg, FakeHa())

    tick(state, 1_000.0)
    tick(state, 1_000.0 + 10 * 60)
    tick(state, 1_000.0 + 40 * 60)

    assert len(state.apns.sent) == 1


def test_closing_arms_the_next_opening(garage_cfg):
    ha = FakeHa()
    state, _ = make(garage_cfg, ha)

    tick(state, 1_000.0)
    tick(state, 1_000.0 + 10 * 60)
    assert len(state.apns.sent) == 1

    ha.open_state = "off"          # porten stängs
    tick(state, 1_000.0 + 11 * 60)
    ha.open_state = "on"           # och öppnas igen
    tick(state, 1_000.0 + 12 * 60)
    tick(state, 1_000.0 + 22 * 60)

    assert len(state.apns.sent) == 2


def test_the_door_can_have_its_own_limit(apns_key, tmp_path):
    cfg = garage_config(apns_key, tmp_path, open_alert_minutes=2)
    state, _ = make(cfg, FakeHa())

    tick(state, 1_000.0)
    tick(state, 1_000.0 + 2 * 60)

    assert len(state.apns.sent) == 1


def test_zero_turns_the_reminder_off(apns_key, tmp_path):
    cfg = garage_config(apns_key, tmp_path, open_alert_minutes=0)
    state, _ = make(cfg, FakeHa())

    tick(state, 1_000.0)
    tick(state, 1_000.0 + 600 * 60)

    assert state.apns.sent == []


def test_a_lock_is_never_reminded_about(cfg):
    state = make_state(cfg)
    state.apns = FakeApns(PushResult(ok=True, status=200))

    asyncio.run(state._open_alert_tick(1_000.0))
    asyncio.run(state._open_alert_tick(1_000.0 + 600 * 60))

    assert state.apns.sent == []


def test_a_garage_without_a_camera_gets_a_reminder_without_a_picture(garage_cfg):
    garage_cfg.doors[0].snapshot_entity = None
    state, _ = make(garage_cfg, FakeHa())

    payload = state.open_alert_payload(garage_cfg.doors[0], 12)

    assert "snapshot" not in payload
    assert payload["aps"]["alert"]["subtitle"] == "Öppen i 12 min"


# -- the snapshot the notification shows -------------------------------------

def test_the_snapshot_is_the_camera_still(garage_cfg):
    app = create_app(garage_cfg)
    hmk = app.state.hmk
    hmk.ha = FakeHa()  # type: ignore[assignment]
    hmk.store.add_device("d1", "iPhone")

    with TestClient(app) as client:
        auth = {"Authorization": "Bearer d1"}
        response = client.get("/api/snapshot/garage", headers=auth)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG
    assert response.headers["cache-control"] == "no-store"


def test_the_snapshot_needs_a_device(garage_cfg):
    app = create_app(garage_cfg)
    app.state.hmk.ha = FakeHa()  # type: ignore[assignment]

    with TestClient(app) as client:
        assert client.get("/api/snapshot/garage").status_code == 401


def test_a_door_without_a_camera_has_no_snapshot(garage_cfg):
    garage_cfg.doors[0].snapshot_entity = None
    app = create_app(garage_cfg)
    app.state.hmk.ha = FakeHa()  # type: ignore[assignment]
    app.state.hmk.store.add_device("d1", "iPhone")

    with TestClient(app) as client:
        response = client.get("/api/snapshot/garage", headers={"Authorization": "Bearer d1"})

    assert response.status_code == 404


def test_a_silent_camera_is_a_bad_gateway(garage_cfg):
    app = create_app(garage_cfg)
    app.state.hmk.ha = FakeHa(image=None)  # type: ignore[assignment]
    app.state.hmk.store.add_device("d1", "iPhone")

    with TestClient(app) as client:
        response = client.get("/api/snapshot/garage", headers={"Authorization": "Bearer d1"})

    assert response.status_code == 502
