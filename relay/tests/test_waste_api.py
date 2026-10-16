from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_the_households_collection_days_are_set_by_an_owner(cfg):
    """The schedule is a house fact: everyone reads it, an owner writes it."""
    app = create_app(cfg)
    with TestClient(app) as client:
        hmk = app.state.hmk
        hmk.store.add_device("owner1", "Claes' iPhone", role="owner")
        hmk.store.add_device("user1", "Elise", role="user")
        owner = {"Authorization": "Bearer owner1"}
        user = {"Authorization": "Bearer user1"}

        # Nothing set up yet: the app is told so, rather than left guessing.
        empty = client.get("/api/waste", headers=owner).json()
        assert empty["enabled"] is False
        assert empty["next"] is None

        saved = client.post("/api/waste/settings", headers=owner, json={
            "schedules": [
                {"name": "Mat & rest", "weekday": 2, "weeks": "even"},
                {"name": "Skräp", "weekday": 9},  # a broken row is dropped, not fatal
            ],
            "deadline": "06:45",
        }).json()
        assert saved["ok"] is True
        assert saved["deadline"] == "06:45"
        assert saved["enabled"] is True

        # Everyone reads the house's schedule; only an owner writes it.
        assert client.get("/api/waste", headers=user).json()["enabled"] is True
        assert client.post("/api/waste/settings", headers=user,
                           json={"schedules": []}).status_code == 403

        # The good row survived, and the broken one never reached the store.
        again = client.get("/api/waste", headers=owner).json()
        assert again["next"] is not None
        assert again["next"]["names"] == ["Mat & rest"]
        stored = hmk.store.setting("waste_schedules")
        assert "Mat & rest" in stored
        assert "Skräp" not in stored

        # Clearing the list turns the feature off rather than leaving it half set.
        cleared = client.post("/api/waste/settings", headers=owner,
                              json={"schedules": []}).json()
        assert cleared["enabled"] is False
