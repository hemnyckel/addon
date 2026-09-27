from __future__ import annotations

import asyncio
import time
from datetime import datetime

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


# -- attribution: crediting an app action to the person who pressed ----------

UNATTRIBUTED = {**UNLOCK, "source": "unattributed", "person": None, "method": "Oattribuerad"}


def test_an_app_action_is_credited_to_the_person(cfg):
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "Claes' iPhone", "person": "claes"})
    event = dict(UNATTRIBUTED)

    state._attribute(event)

    assert event["person"] == "claes"
    assert event["source"] == "app"
    assert event["method"] == "App"


def test_attribution_is_consumed_once(cfg):
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "x", "person": "claes"})

    first, second = dict(UNATTRIBUTED), dict(UNATTRIBUTED)
    state._attribute(first)
    state._attribute(second)

    assert first["source"] == "app"
    assert second["source"] == "unattributed"  # only one report belongs to that action


def test_attribution_expires(cfg, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_ATTRIBUTION_TTL", -1)
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "x", "person": "claes"})
    event = dict(UNATTRIBUTED)

    state._attribute(event)

    assert event["source"] == "unattributed"
    assert event["person"] is None


def test_attribution_needs_a_matching_door_and_action(cfg):
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "x", "person": "claes"})

    wrong_action = {**UNATTRIBUTED, "action": "lock"}
    wrong_door = {**UNATTRIBUTED, "door": "back"}
    state._attribute(wrong_action)
    state._attribute(wrong_door)

    assert wrong_action["source"] == "unattributed"
    assert wrong_door["source"] == "unattributed"


def test_a_physical_entry_is_never_overwritten(cfg):
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "x", "person": "claes"})
    physical = {**UNLOCK, "source": "keypad", "person": "Elise", "method": "Kod"}

    state._attribute(physical)

    assert physical["source"] == "keypad"
    assert physical["person"] == "Elise"
    assert physical["method"] == "Kod"


def test_attribution_without_a_configured_person(cfg):
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "iPhone", "person": None})
    event = dict(UNATTRIBUTED)

    state._attribute(event)

    assert event["source"] == "app"
    assert event["method"] == "App"
    assert event["person"] is None


def test_a_zigbee_journal_entry_is_attributed_end_to_end(cfg):
    state = make_state(cfg)
    state.note_app_action("front", "unlock", {"id": "d1", "name": "x", "person": "claes"})
    journal = {
        "event_type": "nimly_journal_entry",
        "data": {"entry": {"action": "unlock", "source": "zigbee", "time": 1700000000}},
    }

    asyncio.run(state.on_ha_event(journal))

    stored = state.store.last_event("front")
    assert stored["source"] == "app"
    assert stored["method"] == "App"
    assert stored["person"] == "claes"


# -- auto-relock: the lock closing itself ------------------------------------

def test_a_lock_shortly_after_an_unlock_is_an_auto_relock(cfg):
    state = make_state(cfg)
    state._track_unlock({**UNLOCK, "ts": 1000.0})
    lock = {**UNATTRIBUTED, "action": "lock", "ts": 1007.0}

    state._classify_auto_relock(lock)

    assert lock["source"] == "auto"
    assert lock["method"] == "Automatiskt"


def test_a_late_lock_is_not_an_auto_relock(cfg):
    state = make_state(cfg)
    state._track_unlock({**UNLOCK, "ts": 1000.0})
    lock = {**UNATTRIBUTED, "action": "lock", "ts": 1000.0 + 3600}

    state._classify_auto_relock(lock)

    assert lock["source"] == "unattributed"


def test_an_app_lock_is_never_labelled_auto(cfg):
    state = make_state(cfg)
    state._track_unlock({**UNLOCK, "ts": 1000.0})
    state.note_app_action("front", "lock", {"id": "d1", "name": "x", "person": "claes"})
    lock = {**UNATTRIBUTED, "action": "lock", "ts": 1002.0}

    state._attribute(lock)
    state._classify_auto_relock(lock)

    assert lock["source"] == "app"


def test_a_physical_lock_is_not_touched_by_the_auto_rule(cfg):
    state = make_state(cfg)
    state._track_unlock({**UNLOCK, "ts": 1000.0})
    lock = {**UNLOCK, "action": "lock", "source": "keypad", "person": "Elise",
            "method": "Kod", "ts": 1005.0}

    state._classify_auto_relock(lock)

    assert lock["source"] == "keypad"
    assert lock["person"] == "Elise"


def test_an_auto_relock_never_notifies(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "claes", {}, "production")
    auto = {**UNLOCK, "action": "lock", "source": "auto", "method": "Automatiskt", "person": None}

    asyncio.run(state.notify(auto))

    assert state.apns.sent == []


def test_a_guest_is_never_notified(cfg):
    state = make_state(cfg)
    # Even with a push token, a guest is not notified.
    state.store.add_guest("g1", "Städning", ["front"], time.time() + 3600)
    state.store.set_apns("g1", DEVICE_TOKEN, None, {}, "production")

    asyncio.run(state.notify(UNLOCK))

    assert state.apns.sent == []


# -- notification preferences -------------------------------------------------

def _with_prefs(state, prefs):
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "claes", prefs, "production")


def test_quiet_hours_silence_a_device(cfg):
    state = make_state(cfg)
    _with_prefs(state, {"quiet": {"from": "22:00", "to": "07:00"}})

    asyncio.run(state.notify({**UNLOCK, "ts": datetime(2026, 9, 27, 23, 0).timestamp()}))

    assert state.apns.sent == []


def test_a_daytime_event_is_not_silenced(cfg):
    state = make_state(cfg)
    _with_prefs(state, {"quiet": {"from": "22:00", "to": "07:00"}})

    asyncio.run(state.notify({**UNLOCK, "ts": datetime(2026, 9, 27, 12, 0).timestamp()}))

    assert len(state.apns.sent) == 1


def test_a_watched_person_is_heard_even_in_quiet_hours(cfg):
    state = make_state(cfg)
    _with_prefs(state, {"quiet": {"from": "22:00", "to": "07:00"}, "watch": ["Elise"]})

    asyncio.run(state.notify({**UNLOCK, "ts": datetime(2026, 9, 27, 23, 0).timestamp()}))

    assert len(state.apns.sent) == 1  # Elise is watched


def test_a_person_filter_narrows_the_notifications(cfg):
    state = make_state(cfg)
    _with_prefs(state, {"people": ["Elise"]})

    asyncio.run(state.notify({**UNLOCK, "person": "Pappa"}))
    assert state.apns.sent == []

    asyncio.run(state.notify(UNLOCK))  # Elise
    assert len(state.apns.sent) == 1


def test_notifications_can_be_switched_off(cfg):
    state = make_state(cfg)
    _with_prefs(state, {"enabled": False})

    asyncio.run(state.notify(UNLOCK))

    assert state.apns.sent == []


def test_a_redundant_lock_never_notifies(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "claes", {}, "production")
    already_locked = {**UNLOCK, "action": "lock", "source": "unattributed", "person": None}

    # Some locks report a redundant "lock" every hour; it is not worth a push.
    asyncio.run(state.notify(already_locked, previous={**UNLOCK, "action": "lock"}))

    assert state.apns.sent == []


def test_a_lock_after_an_unlock_still_notifies(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "claes", {}, "production")
    lock = {**UNLOCK, "action": "lock", "source": "unattributed", "person": None}

    asyncio.run(state.notify(lock, previous={**UNLOCK, "action": "unlock"}))

    assert len(state.apns.sent) == 1


def test_a_redundant_unlock_never_notifies(cfg):
    state = make_state(cfg)
    state.store.add_device("d1", "iPhone")
    state.store.set_apns("d1", DEVICE_TOKEN, "claes", {}, "production")

    asyncio.run(state.notify(UNLOCK, previous={**UNLOCK, "action": "unlock"}))

    assert state.apns.sent == []


def test_auto_relock_is_classified_end_to_end(cfg):
    state = make_state(cfg)
    unlock = {"event_type": "nimly_journal_entry",
              "data": {"entry": {"action": "unlock", "source": "keypad", "name": "Elise",
                                 "time": 1700000000}}}
    lock = {"event_type": "nimly_journal_entry",
            "data": {"entry": {"action": "lock", "source": "unattributed", "time": 1700000007}}}

    asyncio.run(state.on_ha_event(unlock))
    asyncio.run(state.on_ha_event(lock))

    stored = state.store.last_event("front")
    assert stored["source"] == "auto"
    assert stored["method"] == "Automatiskt"
    assert stored["person"] is None
