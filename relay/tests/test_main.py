from __future__ import annotations

import asyncio

from app.apns import PushResult
from app.main import State

from .conftest import DEVICE_TOKEN

UNLOCK = {
    "id": "e1",
    "ts": 1700000000,
    "door": "front",
    "person": "Elise",
    "slot": 6,
    "action": "unlock",
    "source": "keypad",
    "method": "Kod",
    "door_open": None,
}


class FakeApns:
    def __init__(self, result: PushResult) -> None:
        self.result = result
        self.sent: list[dict] = []

    async def start(self) -> None:  # pragma: no cover - parity with ApnsClient
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    async def send(self, token: str, payload: dict, **kwargs) -> PushResult:
        self.sent.append({"token": token, "payload": payload, **kwargs})
        return self.result


def make_state(cfg) -> State:
    state = State(cfg)
    state.apns = FakeApns(PushResult(ok=True, status=200))
    return state


def test_payload_answers_who_when_how(cfg):
    payload = make_state(cfg)._payload(UNLOCK, cfg.doors[0])
    aps = payload["aps"]

    assert aps["alert"]["title"] == "Ytterdörren"
    assert "Elise" in aps["alert"]["subtitle"]
    assert "Kod" in aps["alert"]["subtitle"]
    assert aps["alert"]["body"].startswith("Låstes upp")
    assert aps["category"] == "DOOR_EVENT"
    assert aps["thread-id"] == "door-front"
    assert aps["mutable-content"] == 1
    assert payload["event"]["id"] == "e1"


def test_notify_sends_with_device_environment_and_collapse(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "claes", {}, "development")

    asyncio.run(state.notify(UNLOCK))

    assert len(state.apns.sent) == 1
    call = state.apns.sent[0]
    assert call["token"] == DEVICE_TOKEN
    assert call["env"] == "development"
    assert call["collapse_id"] == "door-front-unlock"
    assert call["expiration"] > 0


def test_notify_prunes_a_dead_token(cfg):
    state = make_state(cfg)
    state.apns = FakeApns(
        PushResult(ok=False, status=410, reason="Unregistered", invalidate_token=True)
    )
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, None, {}, "production")

    asyncio.run(state.notify(UNLOCK))

    assert state.store.device("d1")["apns_token"] is None


def test_notify_skips_auto_events_and_unknown_doors(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, None, {}, "production")

    asyncio.run(state.notify({**UNLOCK, "source": "auto"}))
    asyncio.run(state.notify({**UNLOCK, "door": "back"}))

    assert state.apns.sent == []


def test_notify_respects_door_preferences(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, None, {"doors": ["back"]}, "production")

    asyncio.run(state.notify(UNLOCK))

    assert state.apns.sent == []


def test_notify_can_skip_the_person_who_acted(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "Elise", {"skip_self": True}, "production")

    asyncio.run(state.notify(UNLOCK))

    assert state.apns.sent == []
