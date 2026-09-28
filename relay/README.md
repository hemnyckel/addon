# Hemnyckel relay

It feeds **the Hemnyckel app** for iPhone — the family's notifications with *who opened
the door, when and how*, history and lock control. The app is heading for TestFlight;
this add-on stores everything meanwhile.

A small HTTPS service that runs in your home and turns local lock events into
Apple push notifications — and forwards app actions back to Home Assistant.

## What it does

- Subscribes to Home Assistant events (`hemnyckel_door_event` + lock
  `state_changed`).
- Maps them to Hemnyckel events (who / when / how / which door).
- Sends APNs pushes (rich, with Lock/Unlock actions) to paired devices.
- Serves the app: pairing, event history, live stream, door state, actions.
- Projects the family's people and roles into Home Assistant over MQTT (optional).

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
| `mqtt_host` / `mqtt_port` / `mqtt_user` / `mqtt_password` | The MQTT broker, only for a Supervisor that hands the add-on no broker of its own — normally Home Assistant supplies it (see *Roles from Home Assistant*) |

Without `apns_key` the relay runs in **dev mode** and logs pushes instead of
sending them — the whole pipeline can be exercised locally.

See `../docs/apns.md` for creating the key and choosing sandbox vs production.

## Security

Everything a phone sends travels over **TLS** to the home (`https://…/hemnyckel/`,
Let's Encrypt), and Apple Push is the only outbound hop. What protects the rest:

- **Per-device tokens.** Pairing trades a one-time code — single use, ten minutes, rate limited
  — for a 128-bit token the phone keeps in the Keychain. Every endpoint but `/health` and
  `/pair` requires it, and the relay enforces roles itself, so the app's UI is never the guard.
- **No Home Assistant credentials.** The relay reaches Home Assistant through the supervisor,
  which injects its token; nothing is stored.
- **The bridge never connects anonymously.** The MQTT broker's host and credentials come
  from Home Assistant itself — the supervisor-injected `MQTT_*` environment, or the
  Supervisor's registered MQTT service. Only when neither exists does the relay fall back
  to the explicit `mqtt_*` options; if none has them it logs once and stays off, rather
  than opening an unauthenticated connection.
- **Codes are write-only.** A code is never read back, logged or stored; a new one is returned
  once, to the owner, at creation.
- **Guests are scoped and they end.** A guest sees only the doors and hours they were given,
  gets no history and no notifications, is refused entirely once expired — and the lock codes
  that belong to them are revoked at that same moment.
- **The APNs key is a file.** Point `apns_key` at the `.p8` in the add-on's `/data` (see
  [`docs/apns.md`](../docs/apns.md)); it is never pasted into the options, so it does not appear
  in the add-on form.
- **The relay's own port (8099) is plain HTTP** on the home network. Always pair from the
  `https://` address, never a bare one — the device token applies either way, but only TLS
  keeps it off the wire.
- **Snapshots include the database.** Home Assistant backups contain the add-on's `/data`,
  which holds the paired device tokens. Turn on **encrypted backups** if they leave the house.

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

## Who writes the notification

The relay sends **facts, not final words**. Each alert carries the door's human
name and the raw event alongside `aps.alert`, with `mutable-content: 1`; the
app's Notification Service Extension composes the visible title/subtitle/body on
the phone, in the phone's own language, from the event's `source` (keypad,
finger, rfid, tag, app, auto, unattributed) rather than the relay's Swedish
`method` label. Fixing a wording is an app update, never a relay redeploy.

`aps.alert` is the fallback for a phone whose extension does not run (older iOS,
extension disabled): it stays Swedish and is only replaced when the push carries
a usable `event`.

## Live Activities

When a door unlocks, the relay also drives a Live Activity on the Lock Screen and
in the Dynamic Island. If the app is running it has registered the activity's
per-activity token and the relay just updates it; if not, the relay uses the
device's push-to-start token. The card's Lock/Unlock button is an App Intent that
talks to the relay directly. A lock updates the card to "Låst", which lingers for
about a minute (an undo window) before the relay ends it; unlocking in that
window cancels the end. See `../docs/api.md` for the three registration calls.

## Keys (slots)

The lock's **slots** are where the journal gets its attribution: a named slot is
what turns an event into "Elise" instead of "slot 6". Owners manage them from the
app, which talks to the relay; the relay resolves each door's slot table from
Home Assistant (the `sensor.*_slots` whose `lock` attribute is the door's name,
using its `entry_id` for the service call) and forwards the
`hemnyckel.set_slot_name` / `create_guest_code` / `enroll_fingerprint` /
`clear_slot` services.

A code is **write-only**: the relay never reads, logs or stores it. A code the
lock generated is returned to the app exactly once, in the response, and is gone.

See `../docs/api.md` for the five endpoints.

## One guest, one identity

A guest invitation is one person, not two. When an owner invites a guest, the
relay also writes a matching **guest code on each chosen door** through the
integration — `hemnyckel.create_recurring_guest` when the invitation has
weekdays (map the ISO weekdays to the integration's day names and the times to a
`start`/`end` window), otherwise `hemnyckel.create_guest_code` with the
invitation's expiry. The guest's name goes on the slot, so a later keypad entry
attributes to them. An invitation with no doors chosen covers every door, and a
door whose lock cannot be reached simply gets no code — it never blocks the
invitation.

The created code(s) are returned **once** in the invite response, each with its
door, and are never logged or stored; the relay keeps only the slot numbers.
Revoking the guest device — or refusing an expired guest — revokes those codes
best-effort, so an unreachable lock never fails the revocation. A guest who
*does* install the app redeems the invitation code exactly as before.

## Roles from Home Assistant

The relay also projects the family into Home Assistant over MQTT: a `select` per
person for their role, plus a `sensor` and a `binary_sensor` for the relay
itself, all built from MQTT discovery. Home Assistant supplies the broker — the
add-on asks for it with `services: mqtt:want` — so nobody types an address or a
password; if Home Assistant provides nothing the bridge stays off.

The select offers only **`owner` and `user`**. A guest is not a bridge role: a
role alone says nothing about doors, hours or an end date, and the relay reads
an empty door list as *all doors*, an empty window as *any time* and no expiry as
*never* — a guest without a life. Guests are made and edited in the app, and a
`guest` command is refused with a logged reason and the truth republished, so the
control snaps back.

A role change over MQTT is applied through the *same* store call as one made in
the app, and the same rule holds: the last owner can never be demoted. After
every attempt the relay republishes the person's state, so the Home Assistant
control always shows the truth. The app stays the place for invitations, guests'
doors and history — the bridge carries roles and health, and nothing else.

A person who became a guest in Home Assistant *before* this rule keeps that role;
their state carries `guest_configured: false` so the unset life is visible.

See [`docs/mqtt-bridge.md`](../docs/mqtt-bridge.md) for the topics, the payloads
and the rules.

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
