"""Wi-Fi as a second opinion on "at home".

A phone that is on the house's network is at home; that is a stronger claim
than a geofence reading taken through a wall, and it costs nothing. The relay
cannot see the phones itself, so it reads what Home Assistant already knows:
a router's ``device_tracker`` (UniFi and friends report the phone's MAC, with
``source_type: router``) and the companion app's ``sensor.*_ssid`` /
``sensor.*_bssid``.

The rule is one-way, and it is the whole point of this module:

    on the home network  =>  home
    no home network      =>  no conclusion, ever

iOS puts Wi-Fi to sleep when the phone rests, a phone on a walkabout keeps a
table's SSID for a while, and a sensor can read "Not Connected" at the kitchen
table. None of that is evidence of being away, so this module only ever returns
a *home* reading. Presence that is not confirmed here is left to the geofence,
exactly as before.

A reading is attributed to a person the owner's way first (``wifi_people`` in
the add-on options) and otherwise by name: a person "Claes" owns the entities
named after them (``device_tracker.claes_iphone_unifi``). An entity two people
could claim belongs to neither - a phone is not a guess.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Readings that carry no claim at all.
_SILENT = {
    "", "unknown", "unavailable", "none", "null", "not connected",
    "not_home", "off", "false", "0", "--",
}


@dataclass(frozen=True)
class Reading:
    """One phone, on the home network, right now."""

    person: str
    entity_id: str
    evidence: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _tokens(person: str) -> list[str]:
    """The name parts a device could be named after ("Elise Högberg" -> elise)."""
    return [token for token in (_slug(part) for part in person.split()) if token]


class HomeWifi:
    """Maps Home Assistant's Wi-Fi entities to people who are at home.

    ``people`` is the household; ``entity_map`` is the owner's explicit
    person -> entity override. ``home_ssids`` / ``home_bssids`` name the home
    network. With neither, only router trackers count: an SSID sensor reads
    whatever network the phone found, and without a name to compare it says
    nothing about *this* house.
    """

    def __init__(
        self,
        people: Iterable[str],
        *,
        entity_map: Mapping[str, Sequence[str]] | None = None,
        home_ssids: Sequence[str] = (),
        home_bssids: Sequence[str] = (),
    ) -> None:
        self._people = [person for person in people if person.strip()]
        self._ssids = {value.strip().lower() for value in home_ssids if value.strip()}
        self._bssids = {value.strip().lower() for value in home_bssids if value.strip()}
        self._override: dict[str, str] = {}
        for person, entities in (entity_map or {}).items():
            for entity in entities:
                self._override[entity.strip().lower()] = person

    @property
    def configured(self) -> bool:
        """Whether this household has anything to read (router trackers always do)."""
        return bool(self._people)

    def person_for(self, entity_id: str) -> str | None:
        """Who owns this entity, or None when that cannot be said."""
        entity = entity_id.strip().lower()
        if entity in self._override:
            return self._override[entity]
        return self._by_name(entity)

    def _by_name(self, entity: str) -> str | None:
        """The person whose name the entity carries, when exactly one does.

        The domain is not part of the name (``device_tracker.claes_iphone`` is
        "claes"). Two people can share a name part ("Anna" and "Anna-Karin"); an
        entity both could claim is nobody's, because a wrong attribution is
        worse than no attribution.
        """
        object_id = entity.split(".", 1)[-1]
        best: dict[str, int] = {}
        for person in self._people:
            matched = [len(token) for token in _tokens(person)
                       if object_id == token or object_id.startswith(f"{token}_")]
            if matched:
                best[person] = max(matched)
        if not best:
            return None
        longest = max(best.values())
        winners = [person for person, length in best.items() if length == longest]
        # "anna_karin_iphone" is Anna-Karin's, not Anna's: the longer name wins.
        # Two people whose names match equally well means nobody's.
        return winners[0] if len(winners) == 1 else None

    def reading(self, entity_id: str, state: Any,
                attributes: Mapping[str, Any] | None = None) -> Reading | None:
        """A home reading from one state, or None when it proves nothing."""
        entity = (entity_id or "").strip().lower()
        value = str(state if state is not None else "").strip()
        if value.lower() in _SILENT:
            return None

        evidence = self._home_evidence(entity, value, attributes or {})
        if evidence is None:
            return None
        person = self.person_for(entity)
        if person is None:
            return None
        return Reading(person=person, entity_id=entity, evidence=evidence)

    def _home_evidence(self, entity: str, value: str,
                       attributes: Mapping[str, Any]) -> str | None:
        if entity.startswith("device_tracker."):
            # A router's tracker is the house's own view of the phone: it can
            # only say "home" while the phone is actually behind the router.
            if str(attributes.get("source_type") or "").strip().lower() != "router":
                return None
            return "router" if value.lower() in ("home", "on") else None
        if entity.startswith("sensor.") and entity.endswith("_ssid"):
            return "wifi" if value.lower() in self._ssids else None
        if entity.startswith("sensor.") and entity.endswith("_bssid"):
            return "wifi" if value.lower() in self._bssids else None
        return None

    def readings(self, states: Iterable[Mapping[str, Any]]) -> list[Reading]:
        """Every home reading in a full state list (what a fresh start sees)."""
        found: list[Reading] = []
        for item in states:
            reading = self.reading(
                str(item.get("entity_id") or ""),
                item.get("state"),
                item.get("attributes") or {},
            )
            if reading is not None:
                found.append(reading)
        return found
