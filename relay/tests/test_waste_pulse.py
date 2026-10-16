"""The bins pulse: the evening reminder, the morning one, and the countdown.

Anchored to real dates rather than to "now": Tuesday the 13th of October 2026 at
six in the evening is the day before an even-week Wednesday, and that Wednesday
is a collection day.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime

from app import waste
from app.apns import PushResult
from app.main import State

from .conftest import DEVICE_TOKEN
from .test_main import FakeApns

EVENING = datetime(2026, 10, 13, 18, 0).timestamp()
MORNING = datetime(2026, 10, 14, 6, 30).timestamp()  # a quarter-hour before 06:45
EVEN_WEDNESDAY = [{"name": "Mat & rest", "weekday": 2, "weeks": "even"}]
THE_DAY = datetime(2026, 10, 14).date()


def _pulse(cfg, rows):
    state = State(replace(cfg, energy_enabled=False))
    fake = FakeApns(PushResult(ok=True, status=200))
    state.apns = fake
    state.store.add_device("dev1", "iPhone")
    state.store.set_apns("dev1", DEVICE_TOKEN, "Claes", {})
    schedules = waste.parse_schedules(rows)
    state.store.set_setting("waste_schedules", json.dumps(waste.schedules_to_json(schedules)))
    return state, fake


def _reminders(fake):
    return [s for s in fake.sent if "alert" in s["payload"]["aps"]]


def _starts(fake):
    return [s for s in fake.sent if s["payload"]["aps"].get("event") == "start"]


def test_the_evening_reminder_goes_once_and_names_the_bins(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    asyncio.run(state._waste_tick(EVENING))
    asyncio.run(state._waste_tick(EVENING))  # once an evening, not twice

    assert len(_reminders(fake)) == 1
    alert = _reminders(fake)[0]["payload"]["aps"]["alert"]
    assert alert["title"] == "Sophämtning i morgon"
    assert "Mat & rest" in alert["body"]
    assert _reminders(fake)[0]["payload"]["waste"]["evening"] is True


def test_the_morning_reminder_carries_the_deadline(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    asyncio.run(state._waste_tick(MORNING))

    assert len(_reminders(fake)) == 1
    alert = _reminders(fake)[0]["payload"]["aps"]["alert"]
    assert alert["title"] == "Sophämtning idag"
    assert "06:45" in alert["body"]


def test_a_reminder_never_arrives_after_the_bins_should_have_been_out(cfg):
    """The whole feature is worth nothing if the morning message is late."""
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    after = datetime(2026, 10, 14, 7, 30).timestamp()

    asyncio.run(state._waste_tick(after))

    assert fake.sent == []
    assert not state.store.setting("waste_morning_sent")


def test_a_day_with_nothing_to_collect_says_nothing(cfg):
    # The same weekday, but the odd weeks: the 14th is an even one.
    state, fake = _pulse(cfg, [{"name": "Mat & rest", "weekday": 2, "weeks": "odd"}])
    asyncio.run(state._waste_tick(EVENING))
    asyncio.run(state._waste_tick(MORNING))

    assert fake.sent == []


def test_the_morning_starts_a_countdown_that_ends_at_the_deadline(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    state.store.set_live_waste_start_token("dev1", "starttok")

    asyncio.run(state._waste_tick(MORNING))

    starts = _starts(fake)
    assert len(starts) == 1
    sent = starts[0]
    assert sent["token"] == "starttok"
    assert sent["payload"]["aps"]["attributes-type"] == "HemnyckelWasteAttributes"
    assert sent["payload"]["aps"]["stale-date"] == int(waste.deadline_at(THE_DAY))
    assert sent["payload"]["aps"]["content-state"]["names"] == ["Mat & rest"]
    assert "alert" not in sent["payload"]["aps"], "a countdown must not make a sound"
    assert [row["device"] for row in state.store.waste_activities()] == ["dev1"]


def test_a_restart_mid_morning_does_not_start_a_second_countdown(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    state.store.set_live_waste_start_token("dev1", "starttok")

    asyncio.run(state._waste_tick(MORNING))
    asyncio.run(state._waste_tick(MORNING))

    assert len(_starts(fake)) == 1


def test_the_countdown_ends_when_the_deadline_has_passed(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    state.store.set_live_waste_start_token("dev1", "starttok")
    asyncio.run(state._waste_tick(MORNING))
    state.store.set_waste_activity("dev1", "acttok")

    after = datetime(2026, 10, 14, 8, 0).timestamp()
    asyncio.run(state._waste_tick(after))

    ends = [s for s in fake.sent if s["payload"]["aps"].get("event") == "end"]
    assert len(ends) == 1
    assert ends[0]["token"] == "acttok"
    assert state.store.waste_activities() == []


def test_the_evening_reminder_follows_the_households_hour(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    state.store.set_setting("waste_evening", "20:00")

    asyncio.run(state._waste_tick(EVENING))  # six in the evening is too early now
    assert _reminders(fake) == []

    asyncio.run(state._waste_tick(datetime(2026, 10, 13, 20, 30).timestamp()))
    assert len(_reminders(fake)) == 1


def test_the_countdown_follows_the_households_lead(cfg):
    state, fake = _pulse(cfg, EVEN_WEDNESDAY)
    state.store.set_live_waste_start_token("dev1", "starttok")
    state.store.set_setting("waste_lead_minutes", "15")

    early = datetime(2026, 10, 14, 6, 20).timestamp()  # 25 minutes before the deadline
    asyncio.run(state._waste_tick(early))
    assert _starts(fake) == []

    asyncio.run(state._waste_tick(MORNING))  # inside the last quarter
    assert len(_starts(fake)) == 1
