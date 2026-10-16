from __future__ import annotations

from datetime import date, datetime, timedelta

from app import waste

# A real week to reason about: Wednesday the 21st of October 2026 is ISO week 43,
# and odd; the 14th and the 28th are even.
WEDNESDAY_EVEN = date(2026, 10, 14)
WEDNESDAY_ODD = date(2026, 10, 21)
THURSDAY_ODD = date(2026, 10, 22)


def test_a_households_rows_are_read_and_the_broken_ones_dropped():
    parsed = waste.parse_schedules([
        {"name": "Mat & rest", "weekday": 2, "weeks": "even"},
        {"name": "Skräp", "weekday": 9},  # no such weekday
        {"nome": "Trams"},  # no name
        {"name": "Varje", "weekday": 0, "weeks": "ibland"},  # not a parity
        "inte en rad",
    ])
    assert [s.name for s in parsed] == ["Mat & rest", "Varje"]
    assert parsed[0].weeks == "even"
    assert parsed[1].weeks == "every", "an unknown week becomes every week, not nothing"
    assert waste.parse_schedules(None) == []


def test_the_week_parity_is_counted_the_way_the_calendar_does():
    assert waste.iso_week_parity(WEDNESDAY_ODD) == "odd"
    assert waste.iso_week_parity(WEDNESDAY_EVEN) == "even"
    assert waste.iso_week_parity(WEDNESDAY_ODD) != waste.iso_week_parity(
        WEDNESDAY_ODD + timedelta(days=7)
    ), "two weeks in a row can never share a parity"


def test_every_week_comes_every_week_and_a_parity_only_in_its_own():
    every = waste.Schedule("Mat & rest", weekday=2)
    even = waste.Schedule("Mat & rest", weekday=2, weeks="even")
    assert waste.comes_on(every, WEDNESDAY_ODD)
    assert waste.comes_on(even, WEDNESDAY_EVEN)
    assert not waste.comes_on(even, WEDNESDAY_ODD)
    assert not waste.comes_on(every, THURSDAY_ODD), "another weekday is another collection"


def test_two_collections_on_one_day_are_both_named():
    schedules = waste.parse_schedules([
        {"name": "Mat", "weekday": 2},
        {"name": "Papper", "weekday": 2, "weeks": "even"},
    ])
    assert waste.due_on(schedules, WEDNESDAY_EVEN) == ["Mat", "Papper"]
    assert waste.due_on(schedules, WEDNESDAY_ODD) == ["Mat"]


def test_the_next_pickup_counts_today_and_looks_ahead():
    schedules = waste.parse_schedules([{"name": "Återvinning", "weekday": 3, "weeks": "odd"}])
    evening_before = datetime(2026, 10, 20, 18, 0).timestamp()
    assert waste.next_pickup(schedules, evening_before) == (THURSDAY_ODD, ["Återvinning"])
    on_the_day = datetime(2026, 10, 22, 7, 0).timestamp()
    assert waste.next_pickup(schedules, on_the_day) == (THURSDAY_ODD, ["Återvinning"])


def test_the_next_pickup_gives_up_when_nothing_is_within_reach():
    schedules = waste.parse_schedules([{"name": "Återvinning", "weekday": 3, "weeks": "odd"}])
    wednesday = datetime(2026, 10, 21, 12, 0).timestamp()
    assert waste.next_pickup(schedules, wednesday, within=0) is None


def test_the_deadline_falls_back_rather_than_failing():
    assert datetime.fromtimestamp(waste.deadline_at(WEDNESDAY_ODD)).strftime("%H:%M") == "06:45"
    assert datetime.fromtimestamp(waste.deadline_at(WEDNESDAY_ODD, "hej")).strftime("%H:%M") == "06:45"
    assert datetime.fromtimestamp(waste.deadline_at(WEDNESDAY_ODD, "99:99")).strftime("%H:%M") == "23:59"


def test_the_words_name_what_is_coming_and_by_when():
    assert waste.morning_text(["Mat", "Papper"]) == (
        "Sophämtning idag", "Mat, Papper · senast 06:45",
    )
    assert waste.evening_text(["Mat"], THURSDAY_ODD) == (
        "Sophämtning i morgon", "Mat · ut med tunnorna i kväll",
    )


def test_the_snapshot_carries_today_tomorrow_and_what_comes_next():
    schedules = waste.parse_schedules([{"name": "Återvinning", "weekday": 3, "weeks": "odd"}])
    evening_before = datetime(2026, 10, 20, 18, 0).timestamp()
    snapshot = waste.snapshot(schedules, evening_before)
    assert snapshot["enabled"] is True
    assert snapshot["today"] == []
    assert snapshot["tomorrow"] == []
    assert snapshot["deadline"] == "06:45"
    assert snapshot["next"] == {"day": "2026-10-22", "names": ["Återvinning"]}
    assert waste.snapshot([], evening_before)["enabled"] is False


def test_a_reminder_carries_the_facts_the_phone_rewrites_into_words():
    payload = waste.notification_payload(
        title="Sophämtning i morgon",
        body="Mat · ut med tunnorna i kväll",
        names=["Mat"],
        day=THURSDAY_ODD,
        evening=True,
    )
    assert payload["aps"]["thread-id"] == "waste"
    assert payload["aps"]["alert"]["title"] == "Sophämtning i morgon"
    assert payload["waste"] == {"names": ["Mat"], "day": "2026-10-22", "evening": True}


def test_the_live_activity_state_counts_to_the_deadline():
    deadline = waste.deadline_at(THURSDAY_ODD)
    state = waste.live_state(names=["Återvinning"], day=THURSDAY_ODD, deadline=deadline)
    assert state == {
        "names": ["Återvinning"],
        "day": "2026-10-22",
        "deadline": deadline,
    }
