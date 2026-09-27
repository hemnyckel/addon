from __future__ import annotations

import pathlib
import re

from app import __version__


def test_the_code_version_matches_the_add_on_manifest() -> None:
    """Home Assistant reads the manifest; the health endpoint reads the code.

    If they drift, an update looks applied when the container still runs the
    old image - which is exactly how a deploy can silently do nothing.
    """
    manifest = (
        pathlib.Path(__file__).resolve().parents[1] / "config.yaml"
    ).read_text()
    found = re.search(r'(?m)^version:\s*"([^"]+)"', manifest)
    assert found, "the add-on manifest has no version"
    assert found.group(1) == __version__
