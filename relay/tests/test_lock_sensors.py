"""Finding a lock's slots and facts sensors, under either of the two namings.

The integration labels them in Swedish now and did so in English before; an
entity id is minted once and never changes, so both spellings live side by side
in a home that has locks of different vintages. The relay has to read both, or a
lock added today has no keys and no live entry id.
"""

from __future__ import annotations

from app.config import Door
from app.main import _lock_facts, _slot_sensor

GARAGE = Door(id="garage", name="Garagedörren", lock_entity="lock.garagedorren")
FRONT = Door(id="front", name="Ytterdörren", lock_entity="lock.ytterdorren")


def _state(entity_id: str, **attributes) -> dict:
    return {"entity_id": entity_id, "attributes": attributes}


def test_a_lock_named_in_swedish_is_found():
    states = [
        _state("sensor.garagedorren_nycklar", lock="Garagedörren"),
        _state("sensor.garagedorren_lasdata", lock="Garagedörren", entry_id="01ABC"),
    ]
    slots = _slot_sensor(states, GARAGE, "_slots")
    assert slots is not None
    assert slots["entity_id"] == "sensor.garagedorren_nycklar"

    facts = _slot_sensor(states, GARAGE, "_lock_facts")
    assert facts is not None
    assert facts["entity_id"] == "sensor.garagedorren_lasdata"


def test_the_older_english_naming_still_works():
    states = [
        _state("sensor.ytterdorren_slots", lock="Ytterdörren"),
        _state("sensor.ytterdorren_lock_facts", lock="Ytterdörren"),
    ]
    slots = _slot_sensor(states, FRONT, "_slots")
    assert slots is not None
    assert slots["entity_id"] == "sensor.ytterdorren_slots"


def test_a_sensor_without_the_lock_attribute_is_matched_by_its_name():
    for entity_id, name in (
        ("sensor.garagedorren_nycklar", "Garagedörren Nycklar"),
        ("sensor.ytterdorren_slots", "Ytterdörren Slots"),
    ):
        door = GARAGE if "garagedorren" in entity_id else FRONT
        states = [_state(entity_id, friendly_name=name)]
        assert _slot_sensor(states, door, "_slots") is not None, entity_id


def test_another_lock_sensor_is_never_mistaken_for_this_one():
    states = [
        _state("sensor.garagedorren_nycklar", lock="Garagedörren"),
        _state("sensor.ytterdorren_slots", lock="Ytterdörren"),
    ]
    found = _slot_sensor(states, GARAGE, "_slots")
    assert found is not None and found["entity_id"] == "sensor.garagedorren_nycklar"


def test_the_facts_sit_beside_the_slots_in_either_language():
    for slots_id, facts_id in (
        ("sensor.garagedorren_nycklar", "sensor.garagedorren_lasdata"),
        ("sensor.ytterdorren_slots", "sensor.ytterdorren_lock_facts"),
    ):
        door = GARAGE if "garagedorren" in slots_id else FRONT
        slots = _state(slots_id, lock=door.name)
        states = [slots, _state(facts_id, lock=door.name, entry_id="01ABC")]
        assert _lock_facts(states, door, slots, "") == {"lock": door.name,
                                                        "entry_id": "01ABC"}
