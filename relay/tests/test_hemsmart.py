from __future__ import annotations

import asyncio

from app.events import from_ha

from .test_main import UNATTRIBUTED, make_state


# What the Hub puts on Home Assistant's event bus when a phone in the family's
# home app works a lock. It knows who; the lock's own report does not.
HEMSMART = {
    "event_type": "hemsmart_lock",
    "time_fired": "2026-10-10T20:00:00+00:00",
    "data": {
        "action": "unlock",
        "entity_id": "lock.front",
        "person": "Claes",
        "at": 1700000000,
    },
}


def test_the_home_apps_event_names_the_door_and_the_person(cfg):
    mapped = from_ha(cfg, HEMSMART)

    assert mapped is not None
    assert mapped["source"] == "hemsmart"
    assert mapped["door"] == "front"
    assert mapped["action"] == "unlock"
    assert mapped["person"] == "Claes"
    assert mapped["method"] == "Hemsmart"


def test_an_unknown_lock_is_not_one_of_our_doors(cfg):
    stranger = {
        "event_type": "hemsmart_lock",
        "time_fired": "2026-10-10T20:00:00+00:00",
        "data": {"action": "unlock", "entity_id": "lock.someone_else", "person": "Claes"},
    }

    assert from_ha(cfg, stranger) is None


def test_the_intent_credits_the_locks_own_report(cfg):
    """The home app's event is an attribution, not a door event of its own.

    It is recorded before the lock's report lands — exactly like this app's own
    action — so that report reads "Hemsmart" instead of "Oattribuerad".
    """
    state = make_state(cfg)
    report = dict(UNATTRIBUTED)

    asyncio.run(state.on_ha_event(HEMSMART))
    state._attribute(report)

    assert report["person"] == "Claes"
    assert report["source"] == "app"
    assert report["method"] == "Hemsmart"


def test_the_intent_never_becomes_a_door_event_of_its_own(cfg):
    """One action, one notification: nothing is stored for the intent itself."""
    state = make_state(cfg)

    asyncio.run(state.on_ha_event(HEMSMART))

    assert state.store.last_event("front") is None
    assert state.store.last_event_at() is None
