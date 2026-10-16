"""When the bins go out: a household's own collection days, and what to say.

The schedule is a small list - a name, a weekday and which weeks - because a home
usually has more than one collection, and the food and the recycling rarely come
on the same day or in the same weeks. Everything here is plain logic over a date,
so it can be tested without a clock or a phone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

# The morning the bins must be out by when nothing else is set: before the truck,
# and early enough to matter on the way out of the door.
DEFAULT_DEADLINE = "06:45"

# The hour the evening reminder goes out, and how long before the deadline the
# house starts saying so. Both are the household's, set from the app.
DEFAULT_EVENING = "18:00"
DEFAULT_LEAD_MINUTES = 120

WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
PARITIES = ("every", "odd", "even")


def clamp_lead(raw: Any, default: int = DEFAULT_LEAD_MINUTES) -> int:
    """How long before the deadline the countdown should appear, in minutes.

    Never nothing and never a whole day: a card that has been counting down since
    yesterday says less than one that appeared this morning - and a quarter of an
    hour is short enough to be a reminder rather than a shrug.
    """
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return default
    return min(12 * 60, max(15, minutes))


def clean_clock(raw: Any, default: str) -> str:
    """A "HH:MM" setting, kept as one - or the default when it is not one."""
    text = str(raw or "").strip()
    hour, _, minute = text.partition(":")
    try:
        at_hour, at_minute = int(hour), int(minute)
    except ValueError:
        return default
    if not 0 <= at_hour <= 23 or not 0 <= at_minute <= 59:
        return default
    return f"{at_hour:02d}:{at_minute:02d}"


def clean_evening(raw: Any, default: str = DEFAULT_EVENING) -> str:
    """The evening reminder's hour."""
    return clean_clock(raw, default)


def clean_deadline(raw: Any, default: str = DEFAULT_DEADLINE) -> str:
    """The morning the bins must be out by."""
    return clean_clock(raw, default)


@dataclass(frozen=True)
class Schedule:
    """One collection: what it is called, and when it comes."""

    name: str
    weekday: int  # 0 = Monday, as `date.weekday()` counts
    weeks: str = "every"  # every | odd | even


def parse_schedules(raw: Any) -> list[Schedule]:
    """The schedule list as the settings hold it, ignoring anything malformed.

    A household's own data should never crash a build or a briefing: a bad row is
    dropped, and the rest still works.
    """
    if not isinstance(raw, list):
        return []
    parsed: list[Schedule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        weekday = item.get("weekday")
        weeks = str(item.get("weeks") or "every")
        if not name or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            continue
        if weeks not in PARITIES:
            weeks = "every"
        parsed.append(Schedule(name=name, weekday=weekday, weeks=weeks))
    return parsed


def schedules_to_json(schedules: list[Schedule]) -> list[dict[str, Any]]:
    """The list as the settings store it."""
    return [{"name": s.name, "weekday": s.weekday, "weeks": s.weeks} for s in schedules]


def iso_week_parity(day: date) -> str:
    """'odd' or 'even', counted by ISO week number - the week the calendar uses."""
    return "odd" if day.isocalendar().week % 2 else "even"


def comes_on(schedule: Schedule, day: date) -> bool:
    """True when this collection happens on this day."""
    if day.weekday() != schedule.weekday:
        return False
    if schedule.weeks == "every":
        return True
    return iso_week_parity(day) == schedule.weeks


def due_on(schedules: list[Schedule], day: date) -> list[str]:
    """What is collected on this day, by name, in the order they were set up."""
    return [s.name for s in schedules if comes_on(s, day)]


def next_pickup(schedules: list[Schedule], now: float, within: int = 14) -> tuple[date, list[str]] | None:
    """The next day something is collected - today counts - and what it is."""
    today = datetime.fromtimestamp(now).date()
    for offset in range(within + 1):
        day = today + timedelta(days=offset)
        names = due_on(schedules, day)
        if names:
            return day, names
    return None


def deadline_at(day: date, deadline: str = DEFAULT_DEADLINE) -> float:
    """When the bins must be out on this day, as epoch seconds.

    A malformed setting falls back to the default rather than taking a build
    down with it.
    """
    at_hour, at_minute = (int(part) for part in clean_clock(deadline, DEFAULT_DEADLINE).split(":"))
    moment = datetime.combine(day, datetime.min.time())
    return moment.replace(hour=at_hour, minute=at_minute).timestamp()


def evening_text(names: list[str], day: date) -> tuple[str, str]:
    """The evening before: the fallback words, for a device without the app."""
    return "Sophämtning i morgon", f"{', '.join(names)} · ut med tunnorna i kväll"


def morning_text(names: list[str], deadline: str = DEFAULT_DEADLINE) -> tuple[str, str]:
    """The morning of: the deadline is the message."""
    return "Sophämtning idag", f"{', '.join(names)} · senast {deadline}"


def notification_payload(
    *, title: str, body: str, names: list[str], day: date, evening: bool
) -> dict[str, Any]:
    """A reminder. The facts ride along; the phone rewrites the words."""
    return {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "mutable-content": 1,
            "thread-id": "waste",
        },
        "waste": {"names": names, "day": day.isoformat(), "evening": evening},
    }


def live_state(*, names: list[str], day: date, deadline: float) -> dict[str, Any]:
    """The Live Activity's content-state: what is coming, and the deadline.

    Epoch seconds and a name list - the widget counts down on its own, so the
    activity needs no update pushes between start and deadline.
    """
    return {"names": names, "day": day.isoformat(), "deadline": float(deadline)}


def start_payload(*, attributes_type: str, attributes: dict[str, Any],
                  state: dict[str, Any], timestamp: int | None = None) -> dict[str, Any]:
    """A push-to-start, carrying the deadline as its stale-date.

    The stale-date greys the card out on time even if the relay never gets to end
    it: a countdown that has run out must not claim there is time left.
    """
    now = int(time.time()) if timestamp is None else timestamp
    deadline = int(float(state.get("deadline", now)))
    return {
        "aps": {
            "timestamp": now,
            "event": "start",
            "attributes-type": attributes_type,
            "attributes": attributes,
            "content-state": state,
            "stale-date": deadline,
            "relevance-score": 100.0,
        }
    }


def end_payload(*, state: dict[str, Any], timestamp: int | None = None,
                dismissal_after: int = 60) -> dict[str, Any]:
    now = int(time.time()) if timestamp is None else timestamp
    return {
        "aps": {
            "timestamp": now,
            "event": "end",
            "content-state": state,
            "dismissal-date": now + dismissal_after,
        }
    }


def snapshot(schedules: list[Schedule], now: float, deadline: str = DEFAULT_DEADLINE,
             evening: str = DEFAULT_EVENING,
             lead_minutes: int = DEFAULT_LEAD_MINUTES) -> dict[str, Any]:
    """What the app reads: today, tomorrow, the next collection, and the knobs."""
    today = datetime.fromtimestamp(now).date()
    tomorrow = today + timedelta(days=1)
    upcoming = next_pickup(schedules, now)
    return {
        "enabled": bool(schedules),
        "schedules": schedules_to_json(schedules),
        "today": due_on(schedules, today),
        "tomorrow": due_on(schedules, tomorrow),
        "deadline": clean_deadline(deadline),
        "evening": clean_evening(evening),
        "lead_minutes": clamp_lead(lead_minutes),
        "next": (
            {"day": upcoming[0].isoformat(), "names": upcoming[1]} if upcoming else None
        ),
        "updated_at": now,
    }
