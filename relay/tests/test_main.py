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


# -- live activities ---------------------------------------------------------

UNLOCK_EVENT = {**UNLOCK, "source": "keypad"}


def live_topic(cfg):
    return f"{cfg.bundle_id}.push-type.liveactivity"


def test_unlock_starts_a_live_activity_via_push_to_start(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_live_start_token("d1", "start-token")

    asyncio.run(state.update_live_activity(UNLOCK_EVENT))

    assert len(state.apns.sent) == 1
    send = state.apns.sent[0]
    assert send["push_type"] == "start"
    assert send["priority"] == 10
    assert send["topic"] == live_topic(cfg)
    assert send["token"] == "start-token"
    assert send["payload"]["aps"]["event"] == "start"
    assert send["payload"]["aps"]["attributes"] == {"doorID": "front", "doorName": "Ytterdörren"}
    # A pending row now exists, so we don't start a second activity.
    assert state.store.live_activities("front")[0]["token"] is None


def test_unlock_updates_an_existing_activity(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_live_activity("d1", "front", "act-token")

    asyncio.run(state.update_live_activity(UNLOCK_EVENT))

    send = state.apns.sent[0]
    assert send["push_type"] == "update"
    assert send["priority"] == 5
    assert send["token"] == "act-token"
    assert send["payload"]["aps"]["event"] == "update"


def test_a_pending_start_is_not_restarted_immediately(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_live_start_token("d1", "start-token")

    asyncio.run(state.update_live_activity(UNLOCK_EVENT))
    state.apns.sent.clear()
    asyncio.run(state.update_live_activity(UNLOCK_EVENT))

    assert state.apns.sent == []


def test_lock_lingers_then_ends(cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_LIVE_LINGER", 0.02)
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_live_activity("d1", "front", "act-token")

    async def go():
        # Auto-relock still updates the card (to "Låst"), even though it never
        # notifies, and schedules the end after the shorten linger.
        await state.update_live_activity({**UNLOCK, "action": "lock", "source": "auto"})
        assert state.apns.sent[0]["push_type"] == "update"
        assert state.apns.sent[0]["payload"]["aps"]["content-state"]["locked"] is True
        await asyncio.sleep(0.2)  # let the linger task run

    asyncio.run(go())

    assert [s["push_type"] for s in state.apns.sent] == ["update", "end"]
    assert state.store.live_activities("front") == []


def test_unlock_within_the_linger_cancels_the_end(cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_LIVE_LINGER", 100)
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_live_activity("d1", "front", "act-token")

    async def go():
        await state.update_live_activity({**UNLOCK, "action": "lock", "source": "auto"})
        assert ("d1", "front") in state._live_end_tasks
        await state.update_live_activity(UNLOCK_EVENT)
        assert ("d1", "front") not in state._live_end_tasks

    asyncio.run(go())


def test_a_pending_start_without_a_token_is_dropped_on_lock(cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_LIVE_LINGER", 0)
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.touch_live_start("d1", "front")  # a push-to-start, no token yet

    asyncio.run(state.update_live_activity({**UNLOCK, "action": "lock", "source": "auto"}))

    assert state.apns.sent == []
    assert state.store.live_activities("front") == []


def test_live_activities_can_be_turned_off(cfg):
    cfg.live_enabled = False
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_live_start_token("d1", "start-token")

    asyncio.run(state.update_live_activity(UNLOCK_EVENT))

    assert state.apns.sent == []


def test_a_dead_live_start_token_is_cleared(cfg):
    state = make_state(cfg)
    state.apns = FakeApns(
        PushResult(ok=False, status=410, reason="Unregistered", invalidate_token=True)
    )
    state.store.add_device("d1", "iPhone")
    state.store.set_live_start_token("d1", "start-token")

    asyncio.run(state.update_live_activity(UNLOCK_EVENT))

    assert state.store.device("d1")["live_start_token"] == ""
