# Relay API — Hemnyckel

The relay runs as a Home Assistant add-on inside the home. The iOS app talks to
it over HTTPS (on the LAN, or through a VPN / Home Assistant Cloud when away).
There are **no accounts** — pairing is a one-time code, and every device gets its
own bearer token.

Base URL: `https://<relay-host>/api`

## Pairing

The relay shows a short-lived pairing code (in the add-on log / a Home Assistant
notification). The app scans a QR code or types the code.

```
POST /pair            { "code": "123456", "name": "Claes' iPhone" }
  -> 200 { "device_token": "<opaque>", "relay_id": "<uuid>" }
  -> 401 invalid or expired code
```

Codes are single-use and expire after a few minutes.

## Registration & preferences

```
POST /register        Authorization: Bearer <device_token>
  { "apns_token": "<hex>", "person": "claes", "apns_env": "development", "prefs": {...} }
  -> 200 { "ok": true }
```

`apns_token` is the Apple device token. `person` links the device to a family
member so notifications can be targeted. `apns_env` is `development` (Xcode debug
builds, APNs sandbox) or `production` (TestFlight / App Store); the relay stores
it per device and sends to the matching APNs host. `prefs` is a small object (see
below).

`apns_token` may be **empty** — a device without push (a simulator, or before an
APNs key exists) should still register, so the relay learns its `person` and can
attribute an app-initiated lock/unlock to them.

## Events

```
GET /events?since=<epoch>&door=<id>&person=<id>&limit=<n>
  -> 200 { "events": [Event, ...] }      # newest last
GET /ws                                  # live Event stream (WebSocket)
```

Apps and widgets read history from here. The relay also keeps a bounded local
cache, so the app still shows the last events when Home Assistant is briefly
unavailable.

## State

```
GET /state
  -> 200 {
      "doors": [ { "id":"front","name":"...","locked":true,"open":false,
                   "battery":92,"last_event":Event } ],
      "presence": { "claes":"home", "anna":"away" },
      "role": "owner", "device_id": "...", "expires": null,
      "relay": { "online": true, "apns": true }
    }
```

`presence` is per **person**, from each person's most recent attributed event —
an automatic relock carries no person, so it never makes someone vanish from the
board. It is empty for a guest.

## Actions

```
POST /action          Authorization: Bearer <device_token>
  { "door": "front", "action": "lock" | "unlock" }
  -> 200 { "ok": true, "confirmed": true }
  -> 409 { "ok": false, "detail": "the lock did not confirm" }
```

Actions are forwarded to Home Assistant. If the lock does not confirm, the app
shows a clear, recoverable state — never a silent success.

When the lock reports the operation back, the relay **credits it to the person**
whose device asked for it: the event becomes `source: "app"`, `method: "App"` and
that person, so history reads "Claes · App" instead of "Oattribuerad" (and the
"never notify the person who acted" rule works for app actions too). The credit
is short-lived and only ever applies to a report that carries no attribution of
its own — a keypad or fingerprint entry is never overwritten.

## Live Activities

While a door is unlocked, the relay keeps a Live Activity (Lock Screen +
Dynamic Island) in step with it. All three calls are authenticated with the
device token:

```
POST /live/start-token   { "apns_token": "<hex>" }
  -> 200 { "ok": true }        # the ActivityKit push-to-start token

POST /live/activity      { "door": "front", "apns_token": "<hex>" }
  -> 200 { "ok": true }        # a running activity's per-activity token
  -> 400 unknown door

DELETE /live/activity?door=front
  -> 200 { "ok": true }        # the app ended the activity
```

The relay owns the start/update/end pushes itself (topic
`<bundle>.push-type.liveactivity`), driven by the same journal events that
produce notifications: an unlock starts or updates, a lock (including
auto-relock) ends.

## People (owner only)

The first device to pair owns the install; every later device is a user. The
relay enforces this, so a user never receives what it may not see.

```
GET /devices                     # owner only; 403 otherwise
  -> 200 { "devices": [ { "id": "...", "name": "Claes' iPhone", "person": "Claes",
                          "role": "owner", "created": 1758... } ] }

POST /devices/<id>/role          # owner only
  { "role": "owner" | "user" }
  -> 200 { "ok": true }
  -> 409 the last owner cannot be demoted

POST /pair-code                  # owner only; a fresh code for a new device
  -> 200 { "code": "A1B2C3", "expires_in": 600 }
```

`GET /state` also carries the caller's `role`, `device_id` and — for a guest —
`expires`, so the app can show only what the role may.

### Guests

An owner invites a guest with a **name, the doors they may use and a window**.
The guest redeems the code with `POST /pair` and becomes a `guest` device.

```
POST /invites                    # owner only
  { "name": "Städning", "role": "guest", "doors": ["front"],
    "days": [1, 3], "from_time": "08:00", "to_time": "17:00",
    "expires_at": 1758... }
  -> 200 { "code": "A1B2C3", "role": "guest", "doors": ["front"],
           "days": [1, 3], "expires_at": 1758...,
           "guest_codes": [ { "door": "front", "door_name": "Ytterdörren",
                              "slot": 6, "code": "4821", "until": null } ] }

DELETE /devices/<id>             # owner only
  -> 200 { "ok": true }          # also revokes the lock codes it created
  -> 409 you cannot remove your own device, or the last owner
```

A guest invitation is **one identity**. Besides the redemption code, the relay
writes a matching guest code on **each chosen door** through the integration:
`hemnyckel.create_recurring_guest` when the invitation names weekdays (ISO,
Monday = 1, mapped to the integration's lowercase three-letter day codes with
`start`/`end` times), otherwise `hemnyckel.create_guest_code` with the
invitation's expiry. The guest's name rides on the slot, so a keypad entry
attributes to them; an invitation with no doors chosen covers every door, and a
door whose lock cannot be reached gets no code without failing the invitation.

`guest_codes` carries the created code(s) **once**, each with the door they
belong to. The relay never logs or stores a code — only the slot numbers, so the
codes can be revoked later. Revoking the guest device, or refusing an expired
guest, revokes those lock codes best-effort; a door that is unreachable never
fails the revocation. A guest who *does* install the app redeems `code` (and
scans its QR) exactly as before.

A guest device:

- sees **no history** (`GET /events` → `[]`) and **no presence**;
- sees and acts on **only their doors** (`POST /action` → 403 otherwise);
- is **never notified** and registers no push tokens;
- is refused entirely once `expires` has passed (403).

### Editing a guest

A guest is edited, not just created. The person is the unit — every one of their
device rows moves together, the same way a role does — and the lock codes follow,
so the guest the app shows and the code on the lock stay the same person.

```
POST /people/<person>/guest          # owner only
  { "name": "Städning", "doors": ["front", "back"],
    "days": [1, 3], "from_time": "08:00", "to_time": "17:00",
    "expires_at": 1758... }
  -> 200 { "ok": true, "person": "Städning", "doors": ["back", "front"],
           "days": [1, 3], "from_time": "08:00", "to_time": "17:00",
           "expires_at": 1758...,
           "guest_codes": [ { "door": "back", "door_name": "Källardörren",
                              "slot": 7, "code": "222222", "until": null } ],
           "failed": [], "changed": ["doors", "days"] }
  -> 400 a door is required, or the times/end date are unusable
  -> 404 unknown person
  -> 409 that person is not a guest, or the new name is already taken
```

`days` is optional: no weekdays means a simple guest that lives until
`expires_at`; weekdays mean a recurring guest whose code is written inside each
weekly window. At least one door is required, and a start time and an end time
come as a pair.

The lock codes are reconciled per door. A recurring guest whose end date did not
change is **updated in place**, so the code the guest already knows keeps
working; a door added or removed, a simple guest turned recurring (or back), or a
moved end date cannot be changed in place, so the code is **revoked and
recreated** with the new window. Any new code appears in `guest_codes` **once** —
the relay never stores or logs it, only the slot. `failed` lists the doors whose
lock could not be reached (their old code is not left behind silently), and
`changed` names the fields that really moved (`name`, `doors`, `days`, `window`,
`expires`).

`role` is not part of this call: a guest is a role, and the role endpoints
(`POST /people/<person>/role`, `POST /devices/<id>/role`) already accept only
`owner` and `user`.

## Slots & codes (owner only)

The lock's slots are where attribution comes from: a **named slot** is what turns
a journal entry into "Elise" instead of "slot 6". These endpoints are owner
only; the relay resolves each door's slot table from Home Assistant's states
(the `sensor.*_slots` whose `lock` attribute is the door's name, using its
`entry_id` for the service call) and forwards the matching `hemnyckel.*` service.

```
GET /slots?door=front
  -> 200 {
      "door": "front", "name": "Ytterdörren",
      "capacity": { "pin": 50, "rfid": 50, "total": 100 },
      "slots": [
        { "slot": 4, "door": "front", "name": "", "occupied": true,
          "has_pin": false, "has_fingerprint": true, "has_rfid": false,
          "finger_used": false, "finger_state": "claimed",
          "fingers": [ { "label": "left index", "enrolled": "2026-09-28T10:00:00+00:00" } ],
          "credentials": ["fingerprint"] }
      ]
    }
  -> 400 unknown door

POST /slots/6/name        { "door": "front", "name": "Elise" }
  -> 200 { "ok": true, "slot": 6, "name": "Elise" }

POST /slots/6/code        { "door": "front", "name": "Elise", "code": "4821", "until": "..." }
  -> 200 { "ok": true, "slot": 6, "name": "Elise", "until": null, "code": "4821" }
  -> 400 a name is required

POST /slots/6/finger      { "door": "front", "finger": "left index" }
  -> 200 { "ok": true, "slot": 6, "finger": "left index" }
                          # the reader lights; touch it at the door. `finger` is
                          # the owner's label for the finger being enrolled - a
                          # claim, never something the lock reports back.

POST /slots/6/label       { "door": "front", "finger": "left index" }
  -> 200 { "ok": true, "slot": 6, "finger": "left index" }
  -> 400 no fingerprint is recorded in slot 6
                          # names a fingerprint the slot already holds, for the
                          # unlabelled enrolment that predates labels. No reader
                          # is lit and no template is written; the label only.

DELETE /slots/6/finger?door=front
  -> 200 { "ok": true, "slot": 6 }   # clears that slot's fingerprint template

DELETE /slots/6?door=front
  -> 200 { "ok": true, "slot": 6 }
```

`fingers` and `finger_state` are passed through from Home Assistant's slots
sensor unchanged: the integration owns the labels and the policy, and the relay
only carries them. A slot with a fingerprint but no label has an empty `fingers`
list; `finger_state` is `none`, `claimed` or `confirmed`.

`code` and `until` are optional on a code call; when no `code` is given the lock
generates one and it is returned **exactly once** in this response. A code is
**write-only**: the relay never reads, logs or stores it.

A label call stays honest about *why* it failed. An unknown door is refused by
the relay (`400`) before Home Assistant is called; a slot the integration will
not label — one with no fingerprint — comes back as Home Assistant's own `400`,
its reason passed through as `{"detail": "…"}`. Only a real outage (Home
Assistant or the lock unreachable) is the clean `502 {"detail": "the lock is not
reachable right now; try again"}` — never a raw upstream error.

## Presence

Every phone watches a geofence around the house and reports when it comes and
goes. That is what makes a departure real — an unlock is an immediate arrival,
but only leaving the zone says someone is out (and an automatic relock is never
a departure).

```
POST /presence            Authorization: Bearer <device_token>
  { "state": "home" | "away" }
  -> 200 { "ok": true }             # ignored when the device has no person

POST /settings/home       # owner only, set once for the whole family
  { "lat": 59.33, "lon": 18.06, "radius": 150 }
  -> 200 { "ok": true, "radius": 150 }
```

`GET /state` returns `home`, so every phone configures the same zone, and
`presence` merges the newest signal per person (a lock event or a report).

## Health

```
GET /health      -> { "status": "ok", "ha": true, "apns": true, "version": "..." }
GET /api/health  -> the same
```

The root path stays open for probes; `apns` is true only when a real key is
loaded. The same facts are also published, retained, for Home Assistant's MQTT
bridge (see [`mqtt-bridge.md`](mqtt-bridge.md)).

## Event object

See `shared/events.schema.json`. In short:

```
{ "id", "ts", "door", "person", "slot", "action": "lock"|"unlock",
  "source": "keypad"|"finger"|"tag"|"auto"|"unattributed",
  "method": "free text", "door_open": true|false }
```

## Security

- Pairing codes are single-use and time-limited.
- Per-device bearer tokens; revocable from the add-on.
- HTTPS only (LAN, VPN or Home Assistant Cloud). No vendor cloud.
- The relay stores APNs tokens and preferences locally, and nothing else.
