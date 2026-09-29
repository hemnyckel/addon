"""Hemnyckel relay — the add-on behind the app.

``__version__`` is the one place the code states its version; it must match the
add-on manifest (``relay/config.yaml``), because Home Assistant reads the
manifest while ``/health`` reports this. A test enforces the pair.
"""

__version__ = "0.8.3"
