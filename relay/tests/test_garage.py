from __future__ import annotations

import asyncio

import pytest

from app.apns import PushResult
from app.config import Config, Door
from app.events import from_ha
from app.main import State

from .test_main import FakeApns

CLOSED_SENSOR = "binary_sensor.port_stangd"
OPEN_SENSOR = "binary_sensor.port_oppen"
MOVING_SENSOR = "binary_sensor.port_i_rorelse"
OPEN_BUTTON = "button.port_oppna"
CLOSE_BUTTON = "button.port_stang"
STOP_BUTTON = "button.port_stopp"
PULSE_BUTTON = "button.port_puls"


@pytest.fixture
def garage_cfg(apns_key, tmp_path) -> Config:
    path, _ = apns_key
    return Config(
        apns_key_path=path,
        apns_key_id="ABC123DEFG",
        apns_team_id="TEAM123456",
        data_dir=str(tmp_path / "data"),
        doors=[
            Door(
                id="garage",
                name="Garageport",
                kind="garage",
                closed_sensor=CLOSED_SENSOR,
                open_sensor=OPEN_SENSOR,
                moving_sensor=MOVING_SENSOR,
                open_button=OPEN_BUTTON,
                close_button=CLOSE_BUTTON,
                stop_button=STOP_BUTTON,
                pulse_button=PULSE_BUTTON,
            )
        ],
    )


class FakeHa:
    """The piece of HaClient these tests need: read a sensor, press a button."""

    def __init__(self, states: dict[str, str], on_press=None) -> None:
        self.states = dict(states)
        self.calls: list[tuple[str, str, dict]] = []
        self.connected = True
        self._on_press = on_press

    async def run(self) -> None:  # pragma: no cover - parity with HaClient
        return

    async def call_service(self, domain: str, service: str, data: dict) -> bool:
        self.calls.append((domain, service, dict(data)))
        if self._on_press is not None:
            self._on_press(self, str(data.get("entity_id")))
        return True

    async def entity_state(self, entity_id: str) -> str | None:
        return self.states.get(entity_id)


def make_state(cfg, ha: FakeHa) -> State:
    state = State(cfg)
    state.apns = FakeApns(PushResult(ok=True, status=200))
    state.ha = ha  # type: ignore[assignment]
    return state


def closed() -> dict[str, str]:
    return {CLOSED_SENSOR: "on", OPEN_SENSOR: "off", MOVING_SENSOR: "off"}


# -- state -------------------------------------------------------------------

def test_a_garage_reports_its_kind_and_position(garage_cfg):
    state = make_state(garage_cfg, FakeHa(closed()))

    door = asyncio.run(state.door_state(garage_cfg.doors[0]))

    assert door["kind"] == "garage"
    assert door["open"] is False
    assert door["moving"] is False
    assert door["locked"] is None


def test_a_door_between_the_sensors_is_unknown_not_closed(garage_cfg):
    ha = FakeHa({CLOSED_SENSOR: "off", OPEN_SENSOR: "off", MOVING_SENSOR: "on"})
    state = make_state(garage_cfg, ha)

    door = asyncio.run(state.door_state(garage_cfg.doors[0]))

    assert door["open"] is None      # aldrig "stängd" på en gissning
    assert door["moving"] is True


def test_a_lock_door_still_reports_kind_lock(cfg):
    state = make_state(cfg, FakeHa({"lock.front": "locked"}))

    door = asyncio.run(state.door_state(cfg.doors[0]))

    assert door["kind"] == "lock"
    assert door["locked"] is True


# -- actions -----------------------------------------------------------------

def test_open_presses_the_open_button_and_confirms_the_door_left(garage_cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_GARAGE_CONFIRM_DELAY", 0)

    def opened(ha: FakeHa, entity_id: str) -> None:
        # Porten börjar gå: stängdgivaren släpper och rörelseflaggan går på.
        ha.states = {CLOSED_SENSOR: "off", OPEN_SENSOR: "off", MOVING_SENSOR: "on"}

    ha = FakeHa(closed(), on_press=opened)
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "open", {"person": "Claes"}))

    assert ha.calls == [("button", "press", {"entity_id": OPEN_BUTTON})]
    assert result == {"ok": True, "confirmed": True}


def test_close_presses_the_close_button(garage_cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_GARAGE_CONFIRM_DELAY", 0)
    ha = FakeHa(
        {CLOSED_SENSOR: "off", OPEN_SENSOR: "on", MOVING_SENSOR: "off"},
        on_press=lambda h, _e: h.states.update(
            {OPEN_SENSOR: "off", MOVING_SENSOR: "on"}
        ),
    )
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "close"))

    assert ha.calls == [("button", "press", {"entity_id": CLOSE_BUTTON})]
    assert result["ok"] is True
    assert result["confirmed"] is True


def test_opening_an_open_door_is_refused_with_a_reason(garage_cfg):
    ha = FakeHa({CLOSED_SENSOR: "off", OPEN_SENSOR: "on", MOVING_SENSOR: "off"})
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "open"))

    assert result == {"ok": False, "confirmed": False, "detail": "already open"}
    assert ha.calls == []            # inget klick, ingen impuls


def test_closing_a_closed_door_is_refused(garage_cfg):
    ha = FakeHa(closed())
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "close"))

    assert result["detail"] == "already closed"
    assert ha.calls == []


def test_stop_is_refused_while_the_door_stands_still(garage_cfg):
    ha = FakeHa(closed())
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "stop"))

    assert result["detail"] == "not moving"
    assert ha.calls == []


def test_stop_is_allowed_while_the_door_moves(garage_cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_GARAGE_CONFIRM_DELAY", 0)
    ha = FakeHa(
        {CLOSED_SENSOR: "off", OPEN_SENSOR: "off", MOVING_SENSOR: "on"},
        on_press=lambda h, _e: h.states.update({MOVING_SENSOR: "off"}),
    )
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "stop"))

    assert ha.calls == [("button", "press", {"entity_id": STOP_BUTTON})]
    assert result == {"ok": True, "confirmed": True}


def test_pulse_is_refused_at_an_end_position(garage_cfg):
    for states in (
        closed(),
        {CLOSED_SENSOR: "off", OPEN_SENSOR: "on", MOVING_SENSOR: "off"},
    ):
        ha = FakeHa(states)
        state = make_state(garage_cfg, ha)

        result = asyncio.run(state.do_action("garage", "pulse"))

        assert result["detail"] == "not between"
        assert ha.calls == []


def test_pulse_continues_a_door_stuck_between_the_sensors(garage_cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_GARAGE_CONFIRM_DELAY", 0)
    ha = FakeHa(
        {CLOSED_SENSOR: "off", OPEN_SENSOR: "off", MOVING_SENSOR: "off"},
        on_press=lambda h, _e: h.states.update({MOVING_SENSOR: "on"}),
    )
    state = make_state(garage_cfg, ha)

    result = asyncio.run(state.do_action("garage", "pulse"))

    assert ha.calls == [("button", "press", {"entity_id": PULSE_BUTTON})]
    assert result == {"ok": True, "confirmed": True}


def test_a_garage_without_end_sensors_cannot_confirm(garage_cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_GARAGE_CONFIRM_DELAY", 0)
    cfg = Config(
        apns_key_path=garage_cfg.apns_key_path,
        apns_key_id=garage_cfg.apns_key_id,
        apns_team_id=garage_cfg.apns_team_id,
        data_dir=garage_cfg.data_dir,
        doors=[Door(id="garage", name="Garageport", kind="garage", open_button=OPEN_BUTTON)],
    )
    state = make_state(cfg, FakeHa({}))

    result = asyncio.run(state.do_action("garage", "open"))

    assert result == {"ok": True, "confirmed": None}


def test_a_lock_action_on_a_garage_is_rejected(garage_cfg):
    from fastapi import HTTPException

    state = make_state(garage_cfg, FakeHa(closed()))

    with pytest.raises(HTTPException) as error:
        asyncio.run(state.do_action("garage", "unlock"))

    assert error.value.status_code == 400


# -- events ------------------------------------------------------------------

def sensor_event(entity_id: str, state: str) -> dict:
    return {
        "event_type": "state_changed",
        "time_fired_ts": 1795534493.9,
        "data": {"entity_id": entity_id, "new_state": {"state": state}},
    }


def test_arriving_at_the_open_position_is_an_event(garage_cfg):
    mapped = from_ha(garage_cfg, sensor_event(OPEN_SENSOR, "on"))

    assert mapped is not None
    assert mapped["door"] == "garage"
    assert mapped["action"] == "unlock"      # "upplåst" = öppnad
    assert mapped["door_open"] is True


def test_arriving_at_the_closed_position_is_an_event(garage_cfg):
    mapped = from_ha(garage_cfg, sensor_event(CLOSED_SENSOR, "on"))

    assert mapped["action"] == "lock"
    assert mapped["door_open"] is False


def test_leaving_a_position_is_not_an_event(garage_cfg):
    """Bara den positiva flanken blir en händelse — annars dubblas varje resa."""
    assert from_ha(garage_cfg, sensor_event(OPEN_SENSOR, "off")) is None
    assert from_ha(garage_cfg, sensor_event(CLOSED_SENSOR, "off")) is None


def test_an_unrelated_sensor_is_ignored(garage_cfg):
    assert from_ha(garage_cfg, sensor_event("binary_sensor.kallare", "on")) is None


def test_a_garage_event_is_credited_to_the_app_that_pressed(garage_cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_GARAGE_CONFIRM_DELAY", 0)
    ha = FakeHa(closed())
    state = make_state(garage_cfg, ha)

    asyncio.run(state.do_action("garage", "open", {"person": "Claes", "name": "iPhone"}))
    asyncio.run(state.on_ha_event(sensor_event(OPEN_SENSOR, "on")))

    stored = state.store.last_event("garage")
    assert stored["person"] == "Claes"
    assert stored["source"] == "app"
    assert stored["method"] == "App"


def test_closing_right_after_opening_is_not_an_auto_relock(garage_cfg):
    """En port som stängs 10 s efter att den öppnats är bara stängd."""
    state = make_state(garage_cfg, FakeHa(closed()))
    opened = from_ha(garage_cfg, sensor_event(OPEN_SENSOR, "on"))
    closed_event = from_ha(garage_cfg, sensor_event(CLOSED_SENSOR, "on"))
    closed_event["ts"] = opened["ts"] + 10

    state._classify_auto_relock(opened)
    state._track_unlock(opened)
    state._classify_auto_relock(closed_event)

    assert closed_event["source"] == "unattributed"


def test_the_notification_says_opened_not_unlocked(garage_cfg):
    state = make_state(garage_cfg, FakeHa(closed()))
    event = from_ha(garage_cfg, sensor_event(OPEN_SENSOR, "on"))

    payload = state._payload(event, garage_cfg.doors[0])

    assert payload["aps"]["alert"]["title"] == "Garageport"
    assert payload["aps"]["alert"]["body"].startswith("Öppnades")
