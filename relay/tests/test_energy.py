from __future__ import annotations

from datetime import datetime, timedelta

from app import energy


def _midnight(offset_days: int = 0) -> float:
    point = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return (point + timedelta(days=offset_days)).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def _raw(start: float, values: list[float], slot: int = 900) -> list[dict]:
    return [
        {"start": _iso(start + i * slot), "end": _iso(start + (i + 1) * slot), "value": value}
        for i, value in enumerate(values)
    ]


def _slots(base: float, values: list[float], slot: int = 900) -> list[energy.Slot]:
    return [
        energy.Slot(start=base + i * slot, end=base + (i + 1) * slot, value=value)
        for i, value in enumerate(values)
    ]


def test_parse_prices_reads_raw_today_and_tomorrow_and_skips_junk():
    midnight = _midnight()
    attributes = {
        "raw_today": [*_raw(midnight, [10, 20]), {"start": "nonsense", "value": 5}],
        "raw_tomorrow": _raw(midnight + 86400, [30, 40]),
    }
    slots = energy.parse_prices(attributes)
    assert [slot.value for slot in slots] == [10, 20, 30, 40]
    assert slots[0].start == midnight


def test_the_array_fallback_builds_quarter_hours_from_local_midnight():
    midnight = _midnight()
    plan = energy.plan(
        {"today": [100, 200, 300, 400], "tomorrow": [999], "tomorrow_valid": False},
        now=midnight + 60,
    )
    assert [slot["value"] for slot in plan["days"][0]["slots"]] == [1.0, 2.0, 3.0, 4.0]
    assert len(plan["days"]) == 1  # an invalid tomorrow is never trusted


def test_cheapest_window_is_the_cheapest_contiguous_run():
    base = 1_700_000_000.0
    slots = _slots(base, [10, 30, 5, 5, 40, 10])
    window = energy.cheapest_window(slots, 30)  # two slots
    assert window is not None
    assert window.start == base + 2 * 900
    assert window.end == base + 4 * 900
    assert window.average == 5
    assert window.lowest == 5 and window.highest == 5


def test_a_tie_goes_to_the_earliest_window():
    base = 1_700_000_000.0
    window = energy.cheapest_window(_slots(base, [5, 5, 5]), 15)
    assert window is not None
    assert window.start == base


def test_no_window_when_there_is_not_enough_day_left():
    base = 1_700_000_000.0
    assert energy.cheapest_window(_slots(base, [5]), 120) is None


def test_plan_scales_the_prices_and_finds_both_the_day_and_the_next_window():
    midnight = _midnight()
    attributes = {"raw_today": _raw(midnight, [300, 300, 100, 100, 300, 300, 300, 300]),
                  "currency": "SEK"}
    now = midnight + 3600  # 01:00, so the 100-slots have passed
    plan = energy.plan(attributes, now=now, window_minutes=30)
    assert plan["available"] is True
    assert plan["unit"] == "kr/kWh"
    assert plan["days"][0]["slots"][0]["value"] == 3.0
    assert plan["days"][0]["cheapest"]["average"] == 1.0
    assert plan["ahead"]["average"] == 3.0  # nothing cheap is left today


def test_a_window_that_bridges_midnight_is_ahead():
    midnight = _midnight()
    tomorrow = midnight + 86400
    attributes = {
        "raw_today": _raw(midnight, [500] * 4),
        "raw_tomorrow": _raw(tomorrow, [100, 100, 500, 500]),
    }
    plan = energy.plan(attributes, now=midnight + 23 * 3600, window_minutes=30)
    assert plan["ahead"] is not None
    assert plan["ahead"]["average"] == 1.0


def test_start_payload_carries_the_window_and_a_stale_date():
    state = energy.live_state(start=1000, end=4600, average=1.2, lowest=1.0, currency="SEK")
    payload = energy.start_payload(
        attributes_type="HemnyckelEnergyAttributes",
        attributes={"day": "2026-10-07"},
        state=state,
        timestamp=1000,
    )
    aps = payload["aps"]
    assert aps["event"] == "start"
    assert aps["attributes-type"] == "HemnyckelEnergyAttributes"
    assert aps["attributes"] == {"day": "2026-10-07"}
    assert aps["stale-date"] == 4600
    assert aps["content-state"] == state
    assert aps["relevance-score"] == energy.LIVE_RELEVANCE


def test_end_payload_sets_a_dismissal_date():
    state = energy.live_state(start=1000, end=4600, average=1.2, lowest=1.0, currency="SEK")
    payload = energy.end_payload(state=state, timestamp=5000, dismissal_after=30)
    assert payload["aps"]["event"] == "end"
    assert payload["aps"]["dismissal-date"] == 5030


def test_briefing_text_is_a_swedish_fallback():
    midnight = _midnight()
    window = {
        "start": midnight + 13 * 3600,
        "end": midnight + 15 * 3600,
        "average": 1.234,
    }
    title, body = energy.briefing_text(window, now=midnight + 7 * 3600)
    assert title == "Elpriset"
    assert body == "Billigast idag 13:00\u201315:00 · 1,23 kr/kWh"


def test_a_night_window_reads_i_natt_and_a_later_one_imorgon():
    midnight = _midnight()
    assert energy.day_word(midnight + 2 * 3600, now=midnight) == "i natt"
    assert energy.day_word(midnight + 86400 + 13 * 3600, now=midnight) == "i morgon"
