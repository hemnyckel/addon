from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from app.apns import PushResult
from app.config import Config, Door
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


class FakeWs:
    """A websocket that answers auth and then ends the session at once."""

    def __init__(self) -> None:
        self._replies = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
        ]
        self.sent: list[str] = []

    async def recv(self) -> str:
        return self._replies.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeHttp:
    """A Home Assistant REST answer, for the origin lookup."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    async def get(self, url, headers=None):
        payload, status = self._payload, self._status

        class Response:
            status_code = status

            def json(self):
                return payload

        return Response()


class FakeConnect:
    """The async context manager ``websockets.connect`` returns."""

    def __init__(self, ws: FakeWs) -> None:
        self._ws = ws

    async def __aenter__(self) -> FakeWs:
        return self._ws

    async def __aexit__(self, *exc) -> bool:
        return False


def test_the_relay_state_is_republished_when_home_assistant_connects(cfg, monkeypatch):
    """The broker comes first, so the first document says ha=false.

    When Home Assistant comes up the relay knows, and must correct the retained
    document instead of leaving it disagreeing with /health.
    """
    import app.ha as ha

    state = State(cfg)
    published: list = []
    state.mqtt._publish = lambda topic, payload, retain: published.append(
        (topic, payload, retain)
    )
    monkeypatch.setattr(ha.websockets, "connect", lambda *a, **k: FakeConnect(FakeWs()))

    asyncio.run(state.ha._session())

    states = [
        json.loads(payload)
        for topic, payload, _ in published
        if topic == "hemnyckel/relay/state"
    ]
    assert states, "Home Assistant coming up must republish the relay's facts"
    assert states[0]["ha"] is True


def test_the_origin_is_learned_and_a_photo_republished_absolute(cfg):
    """Home Assistant rejects a relative entity_picture, so the bridge learns
    Home Assistant's own origin and republishes the photo against it."""
    state = State(cfg)
    state.store.add_invited("phone", "Elise", "owner", [], [], None, None, None,
                            person="Elise")
    state.store.set_avatar("Elise", kind="photo")
    person_id = state.store.people()[0]["id"]
    published: list = []
    state.mqtt._publish = lambda topic, payload, retain: published.append(
        (topic, payload, retain)
    )
    state.ha._http = FakeHttp({"internal_url": None, "external_url": "https://ha.example/"})

    asyncio.run(state.refresh_base_url())

    assert state.mqtt.base_url == "https://ha.example"
    payload = json.loads(
        next(p for t, p, _ in published if t.endswith("/elise/config"))
    )
    assert payload["entity_picture"] == (
        f"https://ha.example/api/hemnyckel/avatar/{person_id}?v=1"
    )


def test_an_internal_origin_is_preferred_and_empty_means_no_picture(cfg):
    """No origin at all is the honest case: the picture is left off."""
    state = State(cfg)

    state.ha._http = FakeHttp({"internal_url": "http://10.0.0.5:8123"})
    asyncio.run(state.refresh_base_url())
    assert state.mqtt.base_url == "http://10.0.0.5:8123"

    state.ha._http = FakeHttp({"internal_url": None, "external_url": None})
    asyncio.run(state.refresh_base_url())
    assert state.mqtt.base_url == ""


def test_payload_answers_who_when_how(cfg):
    payload = make_state(cfg)._payload(UNLOCK, cfg.doors[0])
    aps = payload["aps"]

    assert aps["alert"]["title"] == "Ytterdörren"
    assert aps["alert"]["subtitle"] == "Elise · Kod"
    assert aps["alert"]["body"].startswith("Låstes upp")
    assert aps["category"] == "DOOR_EVENT"
    assert aps["thread-id"] == "door-front"
    assert aps["mutable-content"] == 1
    assert payload["event"]["id"] == "e1"


def test_payload_carries_the_door_name_for_the_phone_to_write_from(cfg):
    """The phone localizes the words; the event is the facts it needs.

    The door's human name travels with the push (so the phone never maps
    "front" to a name), and the fallback alert is left exactly as it was for a
    phone whose Notification Service Extension does not run.
    """
    payload = make_state(cfg)._payload(UNLOCK, cfg.doors[0])
    event = payload["event"]

    assert event["door_name"] == "Ytterdörren"
    # Everything else the phone needs is still there, unchanged.
    assert event["door"] == "front"
    assert event["source"] == "keypad"
    assert event["person"] == "Elise"
    assert event["action"] == "unlock"
    assert event["door_open"] is None


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
        "event_type": "hemnyckel_door_event",
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
    unlock = {"event_type": "hemnyckel_door_event",
              "data": {"entry": {"action": "unlock", "source": "keypad", "name": "Elise",
                                 "time": 1700000000}}}
    lock = {"event_type": "hemnyckel_door_event",
            "data": {"entry": {"action": "lock", "source": "unattributed", "time": 1700000007}}}

    asyncio.run(state.on_ha_event(unlock))
    asyncio.run(state.on_ha_event(lock))

    stored = state.store.last_event("front")
    assert stored["source"] == "auto"
    assert stored["method"] == "Automatiskt"
    assert stored["person"] is None


def test_a_recreated_entry_id_still_reaches_the_right_door(tmp_path, apns_key):
    """A config entry id is re-minted whenever the integration re-creates it.

    It is only a hint, so a driver whose id we no longer know must still land on
    its door through the lock entity - in a house with two doors the event would
    otherwise be dropped silently.
    """
    path, _ = apns_key
    cfg = Config(
        apns_key_path=path,
        apns_key_id="ABC123DEFG",
        apns_team_id="TEAM123456",
        bundle_id="se.hemnyckel.app",
        data_dir=str(tmp_path / "data"),
        doors=[
            Door(id="front", name="Ytterdörren", lock_entity="lock.front", entry_id="gone"),
            Door(id="back", name="Källardörren", lock_entity="lock.back", entry_id="gone"),
        ],
    )
    state = make_state(cfg)
    event = {
        "event_type": "hemnyckel_door_event",
        "data": {
            "action": "unlock",
            "source": "keypad",
            "time": 1700000000,
            "entry_id": "brand-new-id",
            "lock": "lock.back",
        },
    }

    asyncio.run(state.on_ha_event(event))

    assert state.store.last_event("back") is not None
    assert state.store.last_event("front") is None
