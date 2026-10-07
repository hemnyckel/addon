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
GET /events?since=<epoch>&before=<epoch>&door=<id>&person=<id>&limit=<n>
  -> 200 { "events": [Event, ...] }      # the newest <limit>, oldest first
GET /ws                                  # live Event stream (WebSocket)
```

Apps and widgets read history from here. The relay also keeps a bounded local
cache, so the app still shows the last events when Home Assistant is briefly
unavailable.

The window is taken from the **newest** end: when the journal holds more events
than `limit`, the newest are returned and the oldest are dropped, so the latest
event is always included however long the journal has grown. The result is
sorted oldest first for display. `since` is inclusive (`ts >= since`); `before`
is the cursor for paging older history (`ts < before`). The default `limit` is
`2000`, the journal's own retention bound, so one call without a limit returns
everything the relay keeps.

## State

```
GET /state
  -> 200 {
      "doors": [ { "id":"front","name":"...","locked":true,"open":false,
                   "battery":92,"last_event":Event } ],
      "presence": {
        "claes": { "state":"home", "source":"geofence", "at":1758...,
                   "last_home":1758..., "stale":false },
        "anna":  { "state":"away", "source":"geofence", "at":1758...,
                   "last_home":1758..., "stale":false }
      },
      "role": "owner", "device_id": "...", "expires": null,
      "last_event_at": 1758...,
      "relay": { "online": true, "apns": true }
    }
```

`presence` is per **person** and comes from that person's phone geofence, with
a shelf life (see [Presence](#presence)). `state` is the effective state now,
`source` is `geofence` (the truth) or `lock` (a brief hint), `at` is when it was
last confirmed, `last_home` is the last confirmed home (so a lapsed one can read
"senast hemma 09:17"), and `stale` marks a `home` whose confirmation has run
out. It is empty for a guest. `last_event_at` is the journal's **ingest** truth
— the newest event's timestamp, `null` on an empty journal — the same pulse
`/health` reports, so the app can tell "no events" from "the read path is
behind".

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
POST /live/start-token   { "apns_token": "<hex>", "kind": "door" | "energy" }
  -> 200 { "ok": true }        # the ActivityKit push-to-start token

POST /live/activity      { "apns_token": "<hex>", "door": "front", "kind": "door" }
  -> 200 { "ok": true }        # a running activity's per-activity token
  -> 400 unknown door

DELETE /live/activity?door=front&kind=door
  -> 200 { "ok": true }        # the app ended the activity
```

The relay owns the start/update/end pushes itself (topic
`<bundle>.push-type.liveactivity`), driven by the same journal events that
produce notifications: an unlock starts or updates, a lock (including
auto-relock) ends.

`kind` selects the activity type and defaults to `door`. Each type has its own
push-to-start token, so a door card and the energy card never collide; the
energy card carries no `door` and lives under `kind: "energy"` (see
[Energy](#energy-opt-in)).

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

### Person icons (avatars)

Every person has an icon, the way Apple does it: an **initials monogram** (the
default, drawn by the client, nothing stored), a chosen **symbol + colour**, or a
**photo** from Photos. Icons hang off a **stable, opaque person id** that never
changes when the name does, so renaming a person never detaches their icon. The
name stays authoritative for display; `id` is only an identity.

An owner may change anyone's icon; any other paired device may change **its own**
person's icon. Any paired device may read the icons, but a guest still sees no
family (their `GET /people` is empty), exactly as `/state` does.

```
GET /people                       # any paired device
  -> 200 { "people": [ { "id": "8f2c…", "name": "Elise", "role": "user",
                         "avatar": { "kind": "symbol", "symbol": "star",
                                     "color": "#FF9500", "version": 3 } } ] }
                          # an owner's rows also carry "devices" and the guest life

PUT /people/<id>/avatar           # <id> may be the opaque id or the name
  { "kind": "monogram" }
  { "kind": "symbol", "symbol": "star", "color": "#FF9500" }   # color may be null
  -> 200 { "ok": true, "id": "8f2c…", "name": "Elise", "avatar": { … } }
  -> 400 unknown symbol, a colour that is not #RRGGBB, or kind "photo" (use POST)
  -> 403 someone else's icon without the owner role

POST /people/<id>/avatar/photo    # raw image/jpeg body, at most 512 KB
  -> 200 { "ok": true, "id": "8f2c…", "name": "Elise",
           "avatar": { "kind": "photo", … } }
  -> 413 the photo is larger than 512 KB
  -> 415 the body is not a JPEG

GET /people/<id>/avatar           # any paired device
  -> 200 image/jpeg, ETag: "<avatar_version>"
  -> 304 when If-None-Match already has that version
  -> 404 when the icon is a monogram or a symbol (the client draws those)

DELETE /people/<id>/avatar        # back to the monogram; the photo is deleted
  -> 200 { "ok": true, "avatar": { "kind": "monogram", … } }
```

`avatar_version` starts at 0 and is bumped on **every** change, and is the avatar's
`ETag`. The symbol vocabulary is shared by every client:
`pawprint star heart bolt leaf moon sun house key car bike music book game flower
tree wave camera plane cup` — anything else is refused. The photo lives at
`/data/avatars/<person_id>.jpg` — so it survives an update, travels in the add-on's
snapshot, and is deleted with the person — and is **mirrored** to
`/share/hemnyckel/avatars/<person_id>.jpg` so the Home Assistant integration can
read it from the filesystem and serve it over its own authenticated view (the
add-on maps `share:rw`).

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
goes. The geofence is the **truth**: a phone's own enter/exit report is the only
thing that sets presence. An unlock (app, keypad, finger or tag) is only a
**hint** — a door can see an arrival, but it is not a location, and an app unlock
can be pressed from anywhere — so it never sets presence by itself.

Presence has a **shelf life**. A geofence `home` is trusted for **4 hours**
measured from the report; after that the person reads as `away` again, with
`stale: true` and `last_home` still carrying the time of the last confirmation
(what the app can show as "senast hemma 09:17"). A phone that goes quiet — a
dead battery, a missed exit event, a geofence blip — therefore cannot pin
someone "home" for a day. A geofence `away` does not expire: away is not a claim
that needs a clock, and it stands until a later report.

Four hours is deliberate: long enough to cover an outing or a school run the
phone never explicitly reported leaving, short enough that one blip has expired
within the same half-day. An unlock can corroborate a home that has *just*
lapsed, for 30 minutes, and only that; such an entry is tagged `source: "lock"`,
never `geofence`, so the board — which shows only geofence-confirmed people —
never guesses from it. A person the relay has never seen a geofence report from
has no presence at all.

```
POST /presence            Authorization: Bearer <device_token>
  { "state": "home" | "away" }
  -> 200 { "ok": true }             # ignored when the device has no person

POST /settings/home       # owner only, set once for the whole family
  { "lat": 59.33, "lon": 18.06, "radius": 150 }
  -> 200 { "ok": true, "radius": 150 }
```

`GET /state` returns `home`, so every phone configures the same zone.

## Health

```
GET /health      -> { "status": "ok", "ha": true, "apns": true, "energy": false,
                      "version": "...",
                      "last_event_at": <epoch|null>, "journal_read_at": <epoch|null>,
                      "journal_ok": true, "events": <count> }
GET /api/health  -> the same
```

The root path stays open for probes; `apns` is true only when a real key is
loaded. `last_event_at` is the newest event's timestamp (the journal's **ingest**
pulse, `null` on an empty journal) and `events` is how many the relay holds, so
"is the journal still receiving?" is answerable from outside.

`journal_read_at` is the newest timestamp the **read path** actually hands back
— measured through the same `Store.events()` call `GET /events` serves from, not
a second query — and `journal_ok` says whether that read side shows the newest
ingested event (within a small slack, so an event arriving between the two reads
is not a divergence; an empty journal is healthy). The two are watched
separately on purpose: the first journal bug was invisible to an ingest-only
pulse, because the events were stored and simply never readable. When the read
path diverges the relay logs one warning naming both timestamps — once per
divergence, not per poll. The same facts are also published, retained, for Home
Assistant's MQTT bridge (see [`mqtt-bridge.md`](mqtt-bridge.md)).

## Energy (opt-in)

The relay can read one Home Assistant price sensor (a Nordpool sensor by
default, `sensor.elpris`) and answer the family's only question: when, from now
on, is the cheapest stretch long enough to run a machine? The module is **off by
default** and sensor-agnostic — the public add-on runs outside Sweden too. Turn
it on with `energy_enabled: true` and point `price_entity` at your sensor.

```
GET /energy            Authorization: Bearer <device_token>
  -> 200 {
      "enabled": true, "available": true,
      "entity": "sensor.elpris", "currency": "SEK", "unit": "kr/kWh",
      "window_minutes": 120,
      "now": { "value": 2.08 },
      "ahead": { "start": 1790…, "end": 1790…, "average": 1.42,
                 "lowest": 1.39, "highest": 1.45 },
      "days": [
        { "date": "2026-10-07",
          "slots": [ { "start": 1790…, "end": 1790…, "value": 2.08 }, … ],
          "cheapest": { "start": 1790…, "end": 1790…, "average": 1.42,
                        "lowest": 1.39, "highest": 1.45 } }
      ],
      "updated_at": 1790… }
  -> 200 { "enabled": false, "available": false }   # the module is off

POST /energy/window    Authorization: Bearer <owner_token>
  { "minutes": 120 }                                # 15–480, else clamped
  -> 200 { "ok": true, "minutes": 120 }
```

`days` holds today and — once the sensor publishes them — tomorrow, each with
its quarter-hour `slots` (for a chart) and its own `cheapest` window: the run of
`window_minutes` with the smallest sum, ties to the earliest. `ahead` is the
cheapest window that has not started yet, which is what the morning briefing
plans around. All timestamps are epoch seconds, matching the Live Activity
content-state. `window_minutes` is a **household** setting (`POST
/energy/window`, owner only), so every phone's chart, the briefing and the Live
Activity all agree; the add-on's `energy_window_minutes` is only the default.

Two pushes carry it. A **morning briefing** (once a day, at
`energy_morning_time`) names the next cheap window; the phone words it. A **Live
Activity** starts, silently, the moment a day's `cheapest` window opens, counts
down on its own, and ends when it closes — so a 02:00 window lights the Lock
Screen without a sound. Both honour the per-device `prefs.energy` object
(`{ "morning": true, "live": true }`); a guest gets neither.

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
