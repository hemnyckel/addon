"""Electricity prices -> the cheapest stretch of the day, as facts.

The relay reads one Home Assistant price sensor (a Nordpool sensor by default,
``sensor.elpris``) and answers the only question a family needs: when, from now
on, is the cheapest stretch long enough to run a machine? Everything here is
pure - the sensor's attributes go in, a plan comes out - so the arithmetic can
be reasoned about and tested without Home Assistant or a network.

All timestamps in the public shape are epoch *seconds*: a Live Activity's
content-state has no agreed date format (see ``live.py``), so the wire carries
numbers and the phone decides how to show them. Prices are scaled from the
sensor's own unit to kr/kWh by ``divisor`` (Nordpool reports öre/kWh, so the
default is 100); the ranking is unaffected either way.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFAULT_ENTITY = "sensor.elpris"
DEFAULT_WINDOW_MINUTES = 120
# Nordpool reports öre/kWh; the family reads kr/kWh.
DEFAULT_DIVISOR = 100.0
DEFAULT_CURRENCY = "SEK"
DISPLAY_UNIT = "kr/kWh"
SLOT_SECONDS = 15 * 60

# A cheap window should sit on the Lock Screen without outranking an unlocked
# door (live.py scores an unlocked door 100).
LIVE_RELEVANCE = 50.0


@dataclass(frozen=True)
class Slot:
    """One price slot: a start, an end and the price across it."""

    start: float
    end: float
    value: float


@dataclass(frozen=True)
class Window:
    """A contiguous stretch of equal length, and what it costs on average."""

    start: float
    end: float
    average: float
    lowest: float
    highest: float


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch(raw: Any) -> float | None:
    """An ISO timestamp from the sensor (offset included) as epoch seconds."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _raw_slots(attributes: dict[str, Any]) -> list[Slot]:
    """The sensor's own rows; they carry real start/end timestamps."""
    slots: list[Slot] = []
    for key in ("raw_today", "raw_tomorrow"):
        rows = attributes.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = _epoch(row.get("start"))
            end = _epoch(row.get("end"))
            value = _as_float(row.get("value"))
            if start is None or end is None or value is None or end <= start:
                continue
            slots.append(Slot(start=start, end=end, value=value))
    return slots


def _array_slots(attributes: dict[str, Any]) -> list[Slot]:
    """The plain ``today``/``tomorrow`` arrays, when the raw rows are absent.

    Each entry is one slot from local midnight; the arrays carry no timestamps,
    so the day boundaries are rebuilt from the local clock. Tomorrow is only
    trusted when the sensor says it is valid - an invalid array still holds the
    previous day's numbers, which would be a lie.
    """
    slots: list[Slot] = []
    midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    tomorrow_valid = bool(attributes.get("tomorrow_valid"))
    for index, values in enumerate((attributes.get("today"), attributes.get("tomorrow"))):
        if not isinstance(values, list):
            continue
        if index == 1 and not tomorrow_valid:
            continue
        day = midnight + timedelta(days=index)
        for i, raw in enumerate(values):
            value = _as_float(raw)
            if value is None:
                continue
            start = (day + timedelta(seconds=i * SLOT_SECONDS)).timestamp()
            slots.append(Slot(start=start, end=start + SLOT_SECONDS, value=value))
    return slots


def parse_prices(attributes: dict[str, Any]) -> list[Slot]:
    """The sensor's attributes as one ordered, de-duplicated slot list."""
    slots = _raw_slots(attributes) or _array_slots(attributes)
    by_start: dict[float, Slot] = {}
    for slot in slots:
        by_start.setdefault(round(slot.start, 3), slot)
    return [by_start[key] for key in sorted(by_start)]


def _slot_count(slots: list[Slot], minutes: int) -> int:
    # One slot alone cannot reveal the spacing, so a quarter-hour is assumed -
    # the same resolution Nordpool publishes on. That keeps a short tail of the
    # day from silently shrinking the window to whatever is left.
    spacing = slots[1].start - slots[0].start if len(slots) >= 2 else SLOT_SECONDS
    if spacing <= 0:
        spacing = SLOT_SECONDS
    return max(1, round(minutes * 60 / spacing))


def cheapest_window(slots: list[Slot], minutes: int) -> Window | None:
    """The cheapest contiguous run of ``minutes`` that fits inside ``slots``.

    The run is summed over its slots rather than averaged per slot, so a short
    trailing slot cannot skew the choice. Ties go to the earliest window - the
    sooner it can run, the better. None when there is not enough to fill it.
    """
    if not slots:
        return None
    count = _slot_count(slots, minutes)
    if count > len(slots):
        return None
    running = sum(slot.value for slot in slots[:count])
    best_sum = running
    best_index = 0
    for index in range(1, len(slots) - count + 1):
        running += slots[index + count - 1].value - slots[index - 1].value
        if running < best_sum:  # strict: a tie keeps the earlier window
            best_sum = running
            best_index = index
    window = slots[best_index:best_index + count]
    return Window(
        start=window[0].start,
        end=window[-1].end,
        average=best_sum / count,
        lowest=min(slot.value for slot in window),
        highest=max(slot.value for slot in window),
    )


def cheapest_ahead(slots: list[Slot], now: float, minutes: int) -> Window | None:
    """The cheapest window that has not started yet - the one to plan on."""
    ahead = [slot for slot in slots if slot.start >= now]
    return cheapest_window(ahead, minutes) if ahead else None


def days(slots: list[Slot]) -> list[tuple[str, list[Slot]]]:
    """Group slots into local calendar days, in order."""
    grouped: dict[str, list[Slot]] = {}
    order: list[str] = []
    for slot in slots:
        key = datetime.fromtimestamp(slot.start).date().isoformat()
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(slot)
    return [(key, grouped[key]) for key in order]


def _scaled(value: float, divisor: float) -> float:
    return round(value / divisor, 4) if divisor else value


def window_shape(window: Window | None, divisor: float) -> dict[str, Any] | None:
    """A window as the wire shape: epoch seconds and prices in kr/kWh."""
    if window is None:
        return None
    return {
        "start": window.start,
        "end": window.end,
        "average": _scaled(window.average, divisor),
        "lowest": _scaled(window.lowest, divisor),
        "highest": _scaled(window.highest, divisor),
    }


def plan(
    attributes: dict[str, Any],
    *,
    now: float,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    divisor: float = DEFAULT_DIVISOR,
    currency: str = DEFAULT_CURRENCY,
    entity: str = DEFAULT_ENTITY,
) -> dict[str, Any]:
    """The whole answer: each day's curve, its cheapest window, and the next.

    ``days`` holds today and (once the sensor publishes them) tomorrow, each
    with its slots for the chart and its own cheapest window - the one the
    relay runs a Live Activity for. ``ahead`` is the cheapest window that has
    not started yet, which is what a morning briefing plans around.
    """
    slots = parse_prices(attributes)
    day_shapes: list[dict[str, Any]] = []
    for key, day_slots in days(slots):
        day_shapes.append({
            "date": key,
            "slots": [
                {"start": slot.start, "end": slot.end, "value": _scaled(slot.value, divisor)}
                for slot in day_slots
            ],
            "cheapest": window_shape(cheapest_window(day_slots, window_minutes), divisor),
        })
    current = next((slot for slot in slots if slot.start <= now < slot.end), None)
    return {
        "entity": entity,
        "available": bool(slots),
        "currency": currency,
        "unit": DISPLAY_UNIT,
        "window_minutes": window_minutes,
        "now": {"value": _scaled(current.value, divisor)} if current else None,
        "ahead": window_shape(cheapest_ahead(slots, now, window_minutes), divisor),
        "days": day_shapes,
        "updated_at": now,
    }


def day_word(window_start: float, now: float) -> str:
    """A short Swedish word for the window's day (the fallback alert only).

    The phone rewrites the wording itself; this is what a device without the
    extension still reads.
    """
    start = datetime.fromtimestamp(window_start)
    delta = (start.date() - datetime.fromtimestamp(now).date()).days
    if delta == 0:
        return "i natt" if start.hour < 5 else "idag"
    if delta == 1:
        return "i morgon"
    return start.strftime("%A").lower()


def briefing_text(window: dict[str, Any], now: float) -> tuple[str, str]:
    """The morning briefing's fallback title and body, in Swedish."""
    start = datetime.fromtimestamp(float(window["start"]))
    end = datetime.fromtimestamp(float(window["end"]))
    price = f"{float(window['average']):.2f}".replace(".", ",")
    when = f"{day_word(window['start'], now)} {start:%H:%M}\u2013{end:%H:%M}"
    return "Elpriset", f"Billigast {when} · {price} kr/kWh"


def notification_payload(
    *, title: str, body: str, window: dict[str, Any], currency: str
) -> dict[str, Any]:
    """The morning briefing. Facts ride along; the phone rewrites the words."""
    return {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "mutable-content": 1,
            "thread-id": "energy",
        },
        "energy": {"window": window, "currency": currency},
    }


def live_state(
    *, start: float, end: float, average: float, lowest: float, currency: str
) -> dict[str, Any]:
    """The Live Activity content-state: when the cheap window runs, and how cheap.

    Plain JSON, epoch seconds - the widget counts down on its own, so the
    activity needs no update pushes between start and end.
    """
    return {
        "start": float(start),
        "end": float(end),
        "average": float(average),
        "lowest": float(lowest),
        "currency": currency,
    }


def start_payload(
    *,
    attributes_type: str,
    attributes: dict[str, Any],
    state: dict[str, Any],
    timestamp: int | None = None,
) -> dict[str, Any]:
    """A push-to-start, carrying the window and a stale-date at its end.

    The stale-date is what ends the *content* on time even when the app never
    runs to report the activity's own token: the card greys out when the cheap
    window closes instead of claiming a price that has moved on.
    """
    now = int(time.time()) if timestamp is None else timestamp
    end = int(float(state.get("end", now)))
    return {
        "aps": {
            "timestamp": now,
            "event": "start",
            "attributes-type": attributes_type,
            "attributes": attributes,
            "content-state": state,
            "stale-date": end,
            "relevance-score": LIVE_RELEVANCE,
        }
    }


def end_payload(
    *, state: dict[str, Any], timestamp: int | None = None, dismissal_after: int = 60
) -> dict[str, Any]:
    now = int(time.time()) if timestamp is None else timestamp
    return {
        "aps": {
            "timestamp": now,
            "event": "end",
            "content-state": state,
            "dismissal-date": now + dismissal_after,
        }
    }
