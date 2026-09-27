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
| `apns_env` | Default environment for devices that report none: `production` or `development` (sandbox). iOS debug builds report `development`. |
| `apns_topic` | Optional APNs topic override; defaults to `bundle_id` |
| `live_enabled` | Keep a Live Activity (Lock Screen / Dynamic Island) in step with an unlocked door (default `true`) |
| `live_attributes_type` | The iOS `ActivityAttributes` type name the start push targets (default `HemnyckelLockAttributes`) |
| `doors` | A list of `{id, name, lock_entity, door_sensor?, entry_id?}` |

Without `apns_key` the relay runs in **dev mode** and logs pushes instead of
sending them — the whole pipeline can be exercised locally.

See `../docs/apns.md` for creating the key and choosing sandbox vs production.

## Push behaviour

- One provider JWT (ES256) is minted per key and refreshed every 45 minutes,
  and again if Apple answers `ExpiredProviderToken`.
- Each device stores the APNs environment it registered from, and pushes go to
  the matching host (`api.sandbox.push.apple.com` or `api.push.apple.com`).
- Alerts set `apns-expiration` (1 h) and a per-door/action `apns-collapse-id`,
  so a busy door collapses instead of flooding.
- When Apple answers `Unregistered` / `BadDeviceToken` / `DeviceTokenNotForTopic`,
  the device's push token is dropped (the pairing survives) until the app
  registers a fresh token.
- Sends are bounded to a small number of concurrent connections per event.

## Live Activities

When a door unlocks, the relay also drives a Live Activity on the Lock Screen and
in the Dynamic Island. If the app is running it has registered the activity's
per-activity token and the relay just updates it; if not, the relay uses the
device's push-to-start token. A lock (including auto-relock) ends the activity.
See `../docs/api.md` for the three registration calls.

## Tests

```bash
cd relay
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```


## Run standalone (development)

```bash
cd relay
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
HEMNYCKEL_DOORS='[{"id":"front","name":"Ytterdörren","lock_entity":"lock.front"}]' \
  .venv/bin/python run.py
```

Then `GET /health`, and `GET /pair`-code is printed at startup.
