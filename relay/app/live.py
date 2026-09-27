"""ActivityKit payloads for Live Activities.

A Live Activity is a small, system-rendered view that lives on the Lock Screen
and in the Dynamic Island while a door is unlocked. The relay drives it with
ActivityKit pushes:

* ``start``  — push-to-start, when a door unlocks and the app may not be running;
* ``update`` — while it stays unlocked;
* ``end``    — when it locks again (including auto-relock).

Everything in ``content-state`` is plain JSON. In particular the timestamp is
epoch *seconds* rather than a ``Date``, so the encoding is unambiguous on both
sides (ActivityKit's content-state decoding has no agreed date format).
"""
from __future__ import annotations

import time
from typing import Any

LIVE_TOPIC_SUFFIX = ".push-type.liveactivity"

START = "start"
UPDATE = "update"
END = "end"


def topic(bundle_id: str) -> str:
    """The APNs topic Live Activities are delivered on."""
    return f"{bundle_id}{LIVE_TOPIC_SUFFIX}"


def content_state(*, locked: bool, since: float, person: str | None = None,
                  method: str | None = None, open_: bool | None = None,
                  source: str | None = None) -> dict[str, Any]:
    return {
        "locked": locked,
        "open": open_,
        "person": person,
        "method": method,
        # The code, so the device can translate the method itself.
        "source": source,
        "since": since,
    }


def _relevance(state: dict[str, Any]) -> float:
    # An unlocked door outranks everything else on the Lock Screen.
    return 0.0 if state.get("locked") else 100.0


def start_payload(*, attributes_type: str, attributes: dict[str, Any],
                  state: dict[str, Any], timestamp: int | None = None) -> dict[str, Any]:
    return {
        "aps": {
            "timestamp": int(time.time()) if timestamp is None else timestamp,
            "event": START,
            "attributes-type": attributes_type,
            "attributes": attributes,
            "content-state": state,
            "relevance-score": _relevance(state),
        }
    }


def update_payload(*, state: dict[str, Any], timestamp: int | None = None,
                   stale_after: int = 3600) -> dict[str, Any]:
    now = int(time.time()) if timestamp is None else timestamp
    return {
        "aps": {
            "timestamp": now,
            "event": UPDATE,
            "content-state": state,
            "stale-date": now + stale_after,
            "relevance-score": _relevance(state),
        }
    }


def end_payload(*, state: dict[str, Any], timestamp: int | None = None,
                dismissal_after: int = 60) -> dict[str, Any]:
    now = int(time.time()) if timestamp is None else timestamp
    return {
        "aps": {
            "timestamp": now,
            "event": END,
            "content-state": state,
            "dismissal-date": now + dismissal_after,
        }
    }
