"""Person icons: the shared vocabulary and the pure rules behind an avatar.

An avatar is one of three kinds. ``monogram`` (the default) is derived from the
name by each client and stores nothing; ``symbol`` is a token from a fixed
vocabulary plus an optional colour; ``photo`` is a JPEG the person chose, kept
in the add-on's data directory. This module holds only the pure rules — token
and colour validation, the ETag, the JPEG check — so they can be tested
directly and the HTTP layer stays thin.
"""
from __future__ import annotations

import re
from typing import Any

# The shared vocabulary; clients map each token to their own glyph. Exactly
# these, and nothing else, so every client can draw every icon.
SYMBOLS = frozenset({
    "pawprint", "star", "heart", "bolt", "leaf", "moon", "sun", "house",
    "key", "car", "bike", "music", "book", "game", "flower", "tree",
    "wave", "camera", "plane", "cup",
})

KINDS = ("monogram", "symbol", "photo")

# The photo is bounded by the relay, not trusted to the client: 512 KB.
MAX_PHOTO_BYTES = 512 * 1024

# Where the photo is mirrored so the Home Assistant integration can read it from
# the filesystem. The add-on maps ``share:rw``, so host ``/share`` is mounted at
# ``/share``; the integration registers an authenticated view over this file, and
# the photo bytes never sit on an unauthenticated path. The add-on's own copy
# stays in ``/data/avatars`` (it survives an update and travels in a snapshot).
SHARE_AVATAR_DIR = "/share/hemnyckel/avatars"

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def valid_symbol(token: Any) -> bool:
    """Is this a token from the shared vocabulary?"""
    return isinstance(token, str) and token in SYMBOLS


def valid_color(value: Any) -> bool:
    """None (no colour), or a ``#RRGGBB``; anything else is not a colour."""
    return value is None or (isinstance(value, str) and bool(_COLOR.match(value)))


def normalize_color(value: Any) -> str | None:
    """A canonical ``#RRGGBB`` (upper-case), or None when absent.

    Raises ``ValueError`` for anything that is not a colour, so the caller can
    answer a clean 400 instead of storing junk.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _COLOR.match(value):
        raise ValueError("color must be #RRGGBB")
    return value.upper()


def avatar_etag(version: int) -> str:
    """The quoted entity tag for an avatar version (the version *is* the tag)."""
    return f'"{int(version)}"'


def matches_etag(header: str | None, version: int) -> bool:
    """Whether an ``If-None-Match`` header already has this version.

    Handles a comma-separated list and ``*``, as HTTP allows; the weak prefix
    (``W/``) is not something we mint, so it is not matched.
    """
    if not header:
        return False
    wanted = avatar_etag(version)
    return any(part.strip() in (wanted, "*") for part in header.split(","))


def is_jpeg(data: bytes) -> bool:
    """A JPEG starts with the SOI marker; that is enough to refuse junk."""
    return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"
