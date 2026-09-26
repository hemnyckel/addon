# Hemnyckel relay

A small HTTPS service that runs in your home and turns local lock events into
Apple push notifications — and forwards app actions back to Home Assistant.

## What it does

- Subscribes to Home Assistant events (`nimly_journal_entry` + lock
  `state_changed`).
- Maps them to Hemnyckel events (who / when / how / which door).
- Sends APNs pushes (rich, with Lock/Unlock actions) to paired devices.
- Serves the app: pairing, event history, live stream, door state, actions.

Runs with **no vendor cloud**. Apple Push (APNs) is the only external hop.

## Configure

Options (Home Assistant add-on options, or environment variables):

| Option | Meaning |
|---|---|
| `ha_url` | Home Assistant base URL, e.g. `http://homeassistant.local:8123` |
| `ha_token` | A long-lived Home Assistant token |
| `apns_key` | Path to your APNs `.p8` key |
| `apns_key_id` / `apns_team_id` | From the Apple Developer portal |
| `bundle_id` | The app's bundle id (default `se.hemnyckel.app`) |
| `doors` | A list of `{id, name, lock_entity, door_sensor?, entry_id?}` |

Without `apns_key` the relay runs in **dev mode** and logs pushes instead of
sending them — the whole pipeline can be exercised locally.

## Run standalone (development)

```bash
cd relay
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
HEMNYCKEL_DOORS='[{"id":"front","name":"Ytterdörren","lock_entity":"lock.front"}]' \
  .venv/bin/python run.py
```

Then `GET /health`, and `GET /pair`-code is printed at startup.
