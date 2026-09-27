from __future__ import annotations

from app import live


def test_topic_has_the_liveactivity_suffix():
    assert live.topic("se.hemnyckel.app") == "se.hemnyckel.app.push-type.liveactivity"


def test_content_state_uses_plain_json():
    state = live.content_state(locked=False, since=1700000000.5, person="Elise", method="Kod")
    assert state == {
        "locked": False,
        "open": None,
        "person": "Elise",
        "method": "Kod",
        "since": 1700000000.5,
    }


def test_start_payload_carries_attributes_and_state():
    state = live.content_state(locked=False, since=1.0, person="Elise", method="Kod")
    payload = live.start_payload(
        attributes_type="HemnyckelLockAttributes",
        attributes={"doorID": "front", "doorName": "Ytterdörren"},
        state=state,
        timestamp=1700000000,
    )
    aps = payload["aps"]

    assert aps["event"] == "start"
    assert aps["timestamp"] == 1700000000
    assert aps["attributes-type"] == "HemnyckelLockAttributes"
    assert aps["attributes"] == {"doorID": "front", "doorName": "Ytterdörren"}
    assert aps["content-state"] == state
    assert aps["relevance-score"] == 100.0


def test_update_payload_sets_stale_date():
    state = live.content_state(locked=False, since=1.0)
    payload = live.update_payload(state=state, timestamp=1000, stale_after=60)
    assert payload["aps"]["event"] == "update"
    assert payload["aps"]["stale-date"] == 1060
    assert "attributes" not in payload["aps"]


def test_end_payload_sets_dismissal_date():
    state = live.content_state(locked=True, since=2.0)
    payload = live.end_payload(state=state, timestamp=1000, dismissal_after=30)
    assert payload["aps"]["event"] == "end"
    assert payload["aps"]["dismissal-date"] == 1030


def test_a_locked_state_ranks_below_an_unlocked_one():
    locked = live.content_state(locked=True, since=1.0)
    unlocked = live.content_state(locked=False, since=1.0)
    assert live.update_payload(state=locked)["aps"]["relevance-score"] == 0.0
    assert live.update_payload(state=unlocked)["aps"]["relevance-score"] == 100.0
