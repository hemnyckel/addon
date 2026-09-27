from __future__ import annotations


def test_config_uses_the_supervisor_inside_an_add_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    monkeypatch.setenv("HEMNYCKEL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HEMNYCKEL_HA_URL", raising=False)
    monkeypatch.delenv("HEMNYCKEL_HA_TOKEN", raising=False)

    from app.config import load_config

    cfg = load_config()
    assert cfg.ha_url == "http://supervisor/core"
    assert cfg.ha_token == "supervisor-token"

    # An explicit address still wins (standalone development).
    monkeypatch.setenv("HEMNYCKEL_HA_URL", "http://homeassistant.local:8123")
    assert load_config().ha_url == "http://homeassistant.local:8123"
